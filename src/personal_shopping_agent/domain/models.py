"""Validated domain contracts for shopping requests, products, offers, and evidence."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Self
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from personal_shopping_agent.domain.enums import (
    EvidenceSourceType,
    EvidenceSubjectType,
    PriceKind,
    StoreType,
)

ScalarValue = Annotated[Decimal | bool | str, Field(union_mode="left_to_right")]


def utc_now() -> datetime:
    """Return an aware UTC timestamp for deterministic domain defaults."""

    return datetime.now(UTC)


class DomainModel(BaseModel):
    """Base contract that rejects unknown input and prevents reassignment."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Money(DomainModel):
    """A non-negative monetary amount with an ISO-style currency code."""

    amount: Decimal = Field(ge=0)
    currency: str = Field(default="CNY", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        """Accept lowercase user input while storing one canonical representation."""

        return value.upper() if isinstance(value, str) else value


class Budget(DomainModel):
    """Normal and optional stretch ceilings for one shopping request."""

    maximum: Money
    stretch_maximum: Money | None = None

    @model_validator(mode="after")
    def validate_stretch_maximum(self) -> Self:
        """Ensure the stretch ceiling is comparable and not lower than the normal ceiling."""

        if self.stretch_maximum is None:
            return self
        if self.stretch_maximum.currency != self.maximum.currency:
            raise ValueError("budget currencies must match")
        if self.stretch_maximum.amount < self.maximum.amount:
            raise ValueError("stretch maximum must be greater than or equal to maximum")
        return self


class ShoppingCriterion(DomainModel):
    """One user-visible requirement used by later filtering and scoring stages."""

    key: str = Field(min_length=1, max_length=120)
    weight: Decimal = Field(default=Decimal("1"), gt=0)
    hard_requirement: bool = False
    minimum: ScalarValue | None = None
    preferred: ScalarValue | None = None
    maximum: ScalarValue | None = None
    unit: str | None = Field(default=None, max_length=40)


class ShoppingRequest(DomainModel):
    """Structured and validated interpretation of a user's shopping intent."""

    id: UUID = Field(default_factory=uuid4)
    query: str = Field(min_length=1, max_length=2_000)
    category: str = Field(min_length=1, max_length=160)
    budget: Budget
    region: str | None = Field(default=None, max_length=160)
    criteria: tuple[ShoppingCriterion, ...] = ()
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def criteria_keys_are_unique(self) -> Self:
        """Reject ambiguous duplicate criteria before any later scoring step."""

        keys = [criterion.key for criterion in self.criteria]
        if len(keys) != len(set(keys)):
            raise ValueError("criterion keys must be unique")
        return self


class Specification(DomainModel):
    """A raw and optionally normalized product fact with evidence references."""

    key: str = Field(min_length=1, max_length=160)
    raw_value: str = Field(min_length=1, max_length=2_000)
    normalized_value: ScalarValue | None = None
    unit: str | None = Field(default=None, max_length=40)
    evidence_ids: tuple[UUID, ...] = ()


class Product(DomainModel):
    """Stable product identity, intentionally free of seller-specific prices."""

    id: UUID = Field(default_factory=uuid4)
    brand: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=240)
    category: str = Field(min_length=1, max_length=160)
    canonical_name: str = Field(min_length=1, max_length=400)
    identifiers: dict[str, str] = Field(default_factory=dict)
    specifications: tuple[Specification, ...] = ()


class PriceBreakdown(DomainModel):
    """Distinct price layers captured without silently treating them as equivalent."""

    list_price: Money | None = None
    displayed_price: Money | None = None
    unconditional_price: Money | None = None
    conditional_price: Money | None = None
    estimated_total_cost: Money | None = None

    @model_validator(mode="after")
    def prices_are_comparable(self) -> Self:
        """Require at least one price and a single currency across populated layers."""

        populated = [
            price
            for price in (
                self.list_price,
                self.displayed_price,
                self.unconditional_price,
                self.conditional_price,
                self.estimated_total_cost,
            )
            if price is not None
        ]
        if not populated:
            raise ValueError("at least one price layer is required")
        if len({price.currency for price in populated}) != 1:
            raise ValueError("all price layers must use the same currency")
        return self

    @property
    def comparison_price(self) -> tuple[PriceKind, Money]:
        """Return the best available layer using the documented fallback order."""

        candidates = (
            (PriceKind.ESTIMATED_TOTAL, self.estimated_total_cost),
            (PriceKind.UNCONDITIONAL, self.unconditional_price),
            (PriceKind.DISPLAYED, self.displayed_price),
            (PriceKind.CONDITIONAL, self.conditional_price),
            (PriceKind.LIST, self.list_price),
        )
        return next((kind, price) for kind, price in candidates if price is not None)


class Offer(DomainModel):
    """Seller-, variant-, region-, and time-specific commercial observation."""

    id: UUID = Field(default_factory=uuid4)
    product_id: UUID
    platform: str = Field(min_length=1, max_length=120)
    seller: str = Field(min_length=1, max_length=240)
    store_type: StoreType = StoreType.UNKNOWN
    url: HttpUrl
    sku: str | None = Field(default=None, max_length=240)
    variant: str | None = Field(default=None, max_length=400)
    region: str | None = Field(default=None, max_length=160)
    captured_at: AwareDatetime = Field(default_factory=utc_now)
    price: PriceBreakdown
    promotion_conditions: tuple[str, ...] = ()
    in_stock: bool | None = None


class Evidence(DomainModel):
    """One provenance-bearing observation; conflicting observations remain separate rows."""

    id: UUID = Field(default_factory=uuid4)
    subject_type: EvidenceSubjectType
    subject_id: UUID
    field_path: str = Field(min_length=1, max_length=255)
    source_type: EvidenceSourceType
    source_url: HttpUrl
    source_title: str = Field(min_length=1, max_length=500)
    captured_at: AwareDatetime = Field(default_factory=utc_now)
    observed_value: ScalarValue | None = None
    reliability: Decimal = Field(ge=0, le=1)
    freshness: Decimal = Field(ge=0, le=1)
    notes: str | None = Field(default=None, max_length=2_000)
