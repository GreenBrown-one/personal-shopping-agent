"""Deterministic conversion from platform detail observations to domain facts."""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from personal_shopping_agent.application.observations import DetailObservation
from personal_shopping_agent.domain import (
    Evidence,
    EvidenceSourceType,
    EvidenceSubjectType,
    Money,
    Offer,
    PriceBreakdown,
    Product,
    ShoppingRequest,
    StoreType,
)


class ConvertedDetailBatch(BaseModel):
    """Validated domain entities produced from one observation history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    products: tuple[Product, ...] = Field(min_length=1)
    offers: tuple[Offer, ...] = Field(min_length=1)
    evidence: tuple[Evidence, ...] = Field(min_length=1)


class NoDetailObservationsError(RuntimeError):
    """Raised when no durable detail observations exist for conversion."""


class DetailConversionError(ValueError):
    """Sanitized rejection of an incomplete or internally inconsistent observation."""

    def __init__(self, code: str, observation_id: UUID, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.observation_id = observation_id


_STORE_SOURCE_TYPES = {
    StoreType.PLATFORM_SELF_OPERATED: EvidenceSourceType.PLATFORM_SELF_OPERATED,
    StoreType.BRAND_FLAGSHIP: EvidenceSourceType.BRAND_FLAGSHIP,
    StoreType.AUTHORIZED_RETAILER: EvidenceSourceType.AUTHORIZED_RETAILER,
    StoreType.THIRD_PARTY: EvidenceSourceType.THIRD_PARTY_SELLER,
    StoreType.UNKNOWN: EvidenceSourceType.PLATFORM_LISTING,
}


def evidence_source_for_store(store_type: StoreType) -> EvidenceSourceType:
    """Map an explicit store classification without upgrading unknown sellers."""

    return _STORE_SOURCE_TYPES[store_type]


def _identity_part(value: str) -> str:
    return " ".join(value.casefold().split())


def _money(amount: Decimal | None, currency: str) -> Money | None:
    return Money(amount=amount, currency=currency) if amount is not None else None


def _evidence(
    observation: DetailObservation,
    *,
    subject_type: EvidenceSubjectType,
    subject_id: UUID,
    field_path: str,
    observed_value: Decimal | bool | str | None,
) -> Evidence:
    detail = observation.detail
    return Evidence(
        subject_type=subject_type,
        subject_id=subject_id,
        field_path=field_path,
        source_type=evidence_source_for_store(detail.store_type),
        source_url=detail.product_url,
        source_title=detail.title,
        captured_at=detail.captured_at,
        origin_observation_id=observation.id,
        observed_value=observed_value,
        notes="Unverified platform detail observation; confidence is not yet assessed.",
    )


class DetailObservationConverter:
    """Create conservative Product, Offer, and Evidence records from detail history."""

    def convert(
        self,
        request: ShoppingRequest,
        observations: tuple[DetailObservation, ...],
    ) -> ConvertedDetailBatch:
        """Convert complete observations while preserving price layers and conflicts."""

        if not observations:
            raise NoDetailObservationsError("No detail observations are available for conversion.")

        products_by_identity: dict[tuple[str, str], Product] = {}
        products: list[Product] = []
        offers: list[Offer] = []
        evidence: list[Evidence] = []

        for observation in observations:
            self._validate_observation(request.id, observation)
            detail = observation.detail
            assert detail.brand is not None
            assert detail.model is not None
            assert detail.seller_name is not None
            assert detail.region is not None

            identity = (_identity_part(detail.brand), _identity_part(detail.model))
            product = products_by_identity.get(identity)
            if product is None:
                product = Product(
                    brand=detail.brand,
                    model=detail.model,
                    category=request.category,
                    canonical_name=detail.title,
                    identifiers={"manufacturer_model": detail.model},
                )
                products_by_identity[identity] = product
                products.append(product)

            price = PriceBreakdown(
                list_price=_money(detail.list_price, detail.currency),
                displayed_price=_money(detail.displayed_price, detail.currency),
                conditional_price=_money(detail.conditional_price, detail.currency),
            )
            offer = Offer(
                product_id=product.id,
                platform=detail.platform,
                seller=detail.seller_name,
                store_type=detail.store_type,
                url=detail.product_url,
                sku=detail.external_id,
                variant=detail.selected_variant,
                region=detail.region,
                captured_at=detail.captured_at,
                price=price,
                promotion_conditions=detail.promotion_conditions,
                in_stock=detail.in_stock,
            )
            offers.append(offer)
            evidence.extend(self._product_evidence(observation, product))
            evidence.extend(self._offer_evidence(observation, offer))

        return ConvertedDetailBatch(
            products=tuple(products),
            offers=tuple(offers),
            evidence=tuple(evidence),
        )

    @staticmethod
    def _validate_observation(request_id: UUID, observation: DetailObservation) -> None:
        detail = observation.detail
        if observation.request_id != request_id:
            raise DetailConversionError(
                "request_id_mismatch",
                observation.id,
                "Detail observation belongs to a different shopping request.",
            )
        required = {
            "brand": detail.brand,
            "model": detail.model,
            "seller_name": detail.seller_name,
            "region": detail.region,
            "stock_status": detail.stock_status if not detail.is_off_shelf else "off_shelf",
        }
        missing = tuple(name for name, value in required.items() if value is None)
        if missing:
            raise DetailConversionError(
                "detail_context_incomplete",
                observation.id,
                f"Detail observation is missing required context: {', '.join(missing)}.",
            )
        if len(detail.title) > 400:
            raise DetailConversionError(
                "canonical_name_too_long",
                observation.id,
                "Detail title is too long for canonical product identity.",
            )
        if all(
            amount is None
            for amount in (detail.list_price, detail.displayed_price, detail.conditional_price)
        ):
            raise DetailConversionError(
                "price_missing",
                observation.id,
                "Detail observation has no usable price layer.",
            )
        if detail.conditional_price is not None and not detail.promotion_conditions:
            raise DetailConversionError(
                "conditional_price_terms_missing",
                observation.id,
                "Conditional price requires visible promotion terms.",
            )
        selected = tuple(option for option in detail.variant_options if option.selected)
        if bool(detail.selected_variant) != bool(selected) or any(
            option.sku != detail.external_id for option in selected
        ):
            raise DetailConversionError(
                "selected_variant_mismatch",
                observation.id,
                "Selected variant context does not match the detail SKU.",
            )
        if detail.is_off_shelf and detail.in_stock is not False:
            raise DetailConversionError(
                "off_shelf_stock_conflict",
                observation.id,
                "An off-shelf detail cannot be marked available or unknown.",
            )

    @staticmethod
    def _product_evidence(
        observation: DetailObservation,
        product: Product,
    ) -> tuple[Evidence, ...]:
        detail = observation.detail
        items = [
            _evidence(
                observation,
                subject_type=EvidenceSubjectType.PRODUCT,
                subject_id=product.id,
                field_path="brand",
                observed_value=detail.brand,
            ),
            _evidence(
                observation,
                subject_type=EvidenceSubjectType.PRODUCT,
                subject_id=product.id,
                field_path="model",
                observed_value=detail.model,
            ),
            _evidence(
                observation,
                subject_type=EvidenceSubjectType.PRODUCT,
                subject_id=product.id,
                field_path="canonical_name",
                observed_value=detail.title,
            ),
        ]
        items.extend(
            _evidence(
                observation,
                subject_type=EvidenceSubjectType.PRODUCT,
                subject_id=product.id,
                field_path=f"specifications.{specification.key}",
                observed_value=specification.raw_value,
            )
            for specification in detail.specifications
        )
        return tuple(items)

    @staticmethod
    def _offer_evidence(
        observation: DetailObservation,
        offer: Offer,
    ) -> tuple[Evidence, ...]:
        detail = observation.detail
        values: list[tuple[str, Decimal | bool | str | None]] = [
            ("seller", detail.seller_name),
            ("store_type", detail.store_type.value),
            ("store_type_basis", detail.store_type_basis),
            ("sku", detail.external_id),
            ("variant", detail.selected_variant),
            ("region", detail.region),
            ("stock_status", detail.stock_status),
            ("in_stock", detail.in_stock),
            ("is_off_shelf", detail.is_off_shelf),
            ("price.list_price", detail.list_price),
            ("price.displayed_price", detail.displayed_price),
            ("price.conditional_price", detail.conditional_price),
        ]
        values.extend(
            (f"promotion_conditions.{index}", condition)
            for index, condition in enumerate(detail.promotion_conditions)
        )
        return tuple(
            _evidence(
                observation,
                subject_type=EvidenceSubjectType.OFFER,
                subject_id=offer.id,
                field_path=field_path,
                observed_value=value,
            )
            for field_path, value in values
            if value is not None
        )
