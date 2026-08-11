"""Unit tests for conservative detail-to-domain conversion."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import HttpUrl

from personal_shopping_agent.application import (
    DetailConversionError,
    DetailObservation,
    DetailObservationConverter,
    NoDetailObservationsError,
    PlatformProductDetail,
    PlatformSpecificationObservation,
    PlatformVariantOption,
    evidence_source_for_store,
)
from personal_shopping_agent.domain import (
    Budget,
    EvidenceSourceType,
    EvidenceSubjectType,
    Money,
    ShoppingRequest,
    StoreType,
)

NOW = datetime(2026, 8, 9, 17, 0, tzinfo=UTC)


def build_request() -> ShoppingRequest:
    return ShoppingRequest(
        query="example phone",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        created_at=NOW,
    )


def build_observation(
    *,
    minutes: int = 0,
    external_id: str = "1000001",
    model: str = "A1",
    displayed_price: Decimal = Decimal("3999"),
    minimal: bool = False,
) -> DetailObservation:
    detail = PlatformProductDetail(
        platform="jd",
        external_id=external_id,
        title=f"Example Aurora {model}",
        product_url=HttpUrl(f"https://item.jd.com/{external_id}.html"),
        captured_at=NOW + timedelta(minutes=minutes),
        brand="Example",
        model=model,
        seller_name="Example 官方旗舰店",
        store_type=StoreType.BRAND_FLAGSHIP,
        store_type_basis=None if minimal else "官方旗舰店 Example 官方旗舰店",
        list_price=None if minimal else Decimal("4299"),
        displayed_price=displayed_price,
        conditional_price=None if minimal else Decimal("3899"),
        promotion_conditions=() if minimal else ("需领券",),
        selected_variant=None if minimal else "颜色:黑色; 版本:12GB+256GB",
        variant_options=()
        if minimal
        else (
            PlatformVariantOption(attribute="颜色", value="黑色", sku=external_id, selected=True),
            PlatformVariantOption(
                attribute="版本", value="12GB+256GB", sku=external_id, selected=True
            ),
        ),
        region="云南省 曲靖市 麒麟区",
        stock_status="有货",
        in_stock=None if minimal else True,
        specifications=()
        if minimal
        else (PlatformSpecificationObservation(key="电池容量", raw_value="6000mAh"),),
    )
    return DetailObservation(request_id=build_request().id, detail=detail)


def test_converter_groups_stable_products_and_preserves_offer_and_evidence_history() -> None:
    request = build_request()
    earlier = build_observation()
    later = build_observation(minutes=10, displayed_price=Decimal("3799"))
    other_model = build_observation(minutes=20, external_id="1000002", model="A2")
    observations = tuple(
        observation.model_copy(update={"request_id": request.id})
        for observation in (earlier, later, other_model)
    )

    batch = DetailObservationConverter().convert(request, observations)

    assert len(batch.products) == 2
    assert len(batch.offers) == 3
    assert batch.offers[0].product_id == batch.offers[1].product_id
    assert batch.offers[2].product_id != batch.offers[0].product_id
    assert batch.products[0].identifiers == {"manufacturer_model": "A1"}
    assert batch.offers[0].price.displayed_price == Money(amount=Decimal("3999"))
    assert batch.offers[0].price.unconditional_price is None
    assert batch.offers[0].price.estimated_total_cost is None

    displayed = tuple(item for item in batch.evidence if item.field_path == "price.displayed_price")
    assert [item.observed_value for item in displayed] == [
        Decimal("3999"),
        Decimal("3799"),
        Decimal("3999"),
    ]
    assert {item.origin_observation_id for item in displayed} == {
        observation.id for observation in observations
    }
    assert all(item.reliability is None and item.freshness is None for item in batch.evidence)
    specification_values = {
        item.observed_value
        for item in batch.evidence
        if item.field_path == "specifications.电池容量"
    }
    assert specification_values == {"6000mAh"}


def test_converter_keeps_optional_unknown_offer_fields_missing() -> None:
    request = build_request()
    observation = build_observation(minimal=True).model_copy(update={"request_id": request.id})

    batch = DetailObservationConverter().convert(request, (observation,))

    offer = batch.offers[0]
    assert offer.variant is None
    assert offer.in_stock is None
    assert offer.price.list_price is None
    assert offer.price.conditional_price is None
    assert not any(item.field_path == "variant" for item in batch.evidence)
    assert not any(item.field_path == "in_stock" for item in batch.evidence)
    assert not any(item.field_path.startswith("promotion_conditions") for item in batch.evidence)


@pytest.mark.parametrize(
    ("store_type", "source_type"),
    (
        (StoreType.PLATFORM_SELF_OPERATED, EvidenceSourceType.PLATFORM_SELF_OPERATED),
        (StoreType.BRAND_FLAGSHIP, EvidenceSourceType.BRAND_FLAGSHIP),
        (StoreType.AUTHORIZED_RETAILER, EvidenceSourceType.AUTHORIZED_RETAILER),
        (StoreType.THIRD_PARTY, EvidenceSourceType.THIRD_PARTY_SELLER),
        (StoreType.UNKNOWN, EvidenceSourceType.PLATFORM_LISTING),
    ),
)
def test_store_classification_maps_without_upgrading_unknown_sellers(
    store_type: StoreType,
    source_type: EvidenceSourceType,
) -> None:
    assert evidence_source_for_store(store_type) is source_type


@pytest.mark.parametrize("field", ("brand", "model", "seller_name", "region", "stock_status"))
def test_converter_rejects_missing_required_context(field: str) -> None:
    request = build_request()
    observation = build_observation().model_copy(update={"request_id": request.id})
    invalid = observation.model_copy(
        update={"detail": observation.detail.model_copy(update={field: None})}
    )

    with pytest.raises(DetailConversionError, match=field) as error:
        DetailObservationConverter().convert(request, (invalid,))

    assert error.value.code == "detail_context_incomplete"
    assert error.value.observation_id == observation.id


def test_converter_rejects_invalid_title_prices_terms_variant_and_stock() -> None:
    request = build_request()
    observation = build_observation().model_copy(update={"request_id": request.id})
    cases = (
        (
            {"title": "x" * 401},
            "canonical_name_too_long",
        ),
        (
            {"list_price": None, "displayed_price": None, "conditional_price": None},
            "price_missing",
        ),
        (
            {"conditional_price": Decimal("3899"), "promotion_conditions": ()},
            "conditional_price_terms_missing",
        ),
        (
            {"selected_variant": "颜色:黑色", "variant_options": ()},
            "selected_variant_mismatch",
        ),
        (
            {
                "selected_variant": "颜色:黑色",
                "variant_options": (
                    PlatformVariantOption(
                        attribute="颜色", value="黑色", sku="different", selected=True
                    ),
                ),
            },
            "selected_variant_mismatch",
        ),
        (
            {"is_off_shelf": True, "in_stock": None, "stock_status": None},
            "off_shelf_stock_conflict",
        ),
    )

    for updates, code in cases:
        invalid = observation.model_copy(
            update={"detail": observation.detail.model_copy(update=updates)}
        )
        with pytest.raises(DetailConversionError) as error:
            DetailObservationConverter().convert(request, (invalid,))
        assert error.value.code == code


def test_converter_rejects_observation_from_another_request() -> None:
    request = build_request()
    observation = build_observation()

    with pytest.raises(DetailConversionError) as error:
        DetailObservationConverter().convert(request, (observation,))

    assert error.value.code == "request_id_mismatch"


def test_converter_accepts_consistent_off_shelf_context_and_rejects_empty_input() -> None:
    request = build_request()
    observation = build_observation(minimal=True).model_copy(update={"request_id": request.id})
    off_shelf = observation.model_copy(
        update={
            "detail": observation.detail.model_copy(
                update={"is_off_shelf": True, "in_stock": False, "stock_status": None}
            )
        }
    )

    batch = DetailObservationConverter().convert(request, (off_shelf,))
    assert batch.offers[0].in_stock is False
    assert any(
        item.subject_type is EvidenceSubjectType.OFFER
        and item.field_path == "is_off_shelf"
        and item.observed_value is True
        for item in batch.evidence
    )
    with pytest.raises(NoDetailObservationsError):
        DetailObservationConverter().convert(request, ())
