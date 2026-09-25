"""Provider-neutral product-detail observations and collection use case."""

from decimal import Decimal
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl

from personal_shopping_agent.domain import StoreType
from personal_shopping_agent.sourcing.discovery import (
    CollectedPage,
    PageCollector,
    PlatformCandidate,
)


class DetailModel(BaseModel):
    """Strict immutable base for unverified detail-page observations."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class PlatformSpecificationObservation(DetailModel):
    """Raw key/value text observed in a platform specification section."""

    key: str = Field(min_length=1, max_length=160)
    raw_value: str = Field(min_length=1, max_length=2_000)


class PlatformVariantOption(DetailModel):
    """One variant choice and the SKU referenced by the platform DOM."""

    attribute: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=240)
    sku: str = Field(min_length=1, max_length=120)
    selected: bool = False


class PlatformProductDetail(DetailModel):
    """Timestamped detail observation that is not yet a canonical Product or Offer."""

    platform: str = Field(min_length=1, max_length=40)
    external_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=500)
    product_url: HttpUrl
    captured_at: AwareDatetime
    brand: str | None = Field(default=None, max_length=160)
    model: str | None = Field(default=None, max_length=240)
    seller_name: str | None = Field(default=None, max_length=240)
    store_type: StoreType = StoreType.UNKNOWN
    store_type_basis: str | None = Field(default=None, max_length=240)
    currency: str = Field(default="CNY", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    list_price: Decimal | None = Field(default=None, ge=0)
    displayed_price: Decimal | None = Field(default=None, ge=0)
    conditional_price: Decimal | None = Field(default=None, ge=0)
    promotion_conditions: tuple[str, ...] = ()
    selected_variant: str | None = Field(default=None, max_length=400)
    variant_options: tuple[PlatformVariantOption, ...] = ()
    region: str | None = Field(default=None, max_length=160)
    stock_status: str | None = Field(default=None, max_length=160)
    in_stock: bool | None = None
    is_off_shelf: bool = False
    specifications: tuple[PlatformSpecificationObservation, ...] = ()


class PlatformDetailParseError(ValueError):
    """Sanitized detail parser failure caused by missing or inconsistent identity."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PlatformDetailAdapter(Protocol):
    """Replaceable parser for one platform's product detail page."""

    @property
    def platform(self) -> str: ...

    def parse_detail(
        self,
        candidate: PlatformCandidate,
        page: CollectedPage,
    ) -> PlatformProductDetail: ...


class ProductDetailService:
    """Collect one candidate detail page without promoting it to domain identity."""

    def __init__(self, collector: PageCollector, adapter: PlatformDetailAdapter) -> None:
        self._collector = collector
        self._adapter = adapter

    async def collect(
        self,
        candidate: PlatformCandidate,
        *,
        screenshot: bool = False,
    ) -> PlatformProductDetail:
        """Collect and parse one read-only detail page through the selected adapter."""

        if candidate.platform != self._adapter.platform:
            raise ValueError("candidate platform does not match the detail adapter")
        page = await self._collector.open(str(candidate.product_url), screenshot=screenshot)
        return self._adapter.parse_detail(candidate, page)
