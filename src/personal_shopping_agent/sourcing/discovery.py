"""Provider-neutral candidate discovery contracts and deterministic use case."""

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl


class DiscoveryModel(BaseModel):
    """Strict immutable base for candidate-discovery data."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class PlatformCandidate(DiscoveryModel):
    """Unverified platform listing kept separate from canonical Product and Offer."""

    platform: str = Field(min_length=1, max_length=40)
    external_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=500)
    product_url: HttpUrl
    displayed_price: Decimal | None = Field(default=None, ge=0)
    seller_name: str | None = Field(default=None, max_length=240)
    badges: tuple[str, ...] = ()
    review_summary: str | None = Field(default=None, max_length=120)


class PlatformSearchResult(DiscoveryModel):
    """One bounded, timestamped platform search observation."""

    platform: str = Field(min_length=1, max_length=40)
    query: str = Field(min_length=1, max_length=200)
    source_url: HttpUrl
    captured_at: AwareDatetime
    candidates: tuple[PlatformCandidate, ...]


class PlatformAccessRestrictedError(RuntimeError):
    """Sanitized signal that collection must stop for human intervention."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CollectedPage(Protocol):
    """Read-only page fields needed by parsers, independent of Playwright."""

    @property
    def final_url(self) -> HttpUrl: ...

    @property
    def html(self) -> str: ...

    @property
    def captured_at(self) -> datetime: ...


class PageCollector(Protocol):
    """Application-owned port implemented by the controlled browser adapter."""

    async def open(self, url: str, *, screenshot: bool = False) -> CollectedPage: ...


class PlatformSearchAdapter(Protocol):
    """Replaceable platform adapter that builds and parses one search request."""

    @property
    def platform(self) -> str: ...

    def build_search_url(self, query: str) -> str: ...

    def parse_search(
        self,
        query: str,
        page: CollectedPage,
        *,
        maximum_candidates: int,
    ) -> PlatformSearchResult: ...


class CandidateDiscoveryService:
    """Fetch exactly one result page and parse it through a selected adapter."""

    def __init__(self, collector: PageCollector, adapter: PlatformSearchAdapter) -> None:
        self._collector = collector
        self._adapter = adapter

    async def discover(
        self,
        query: str,
        *,
        maximum_candidates: int = 20,
        screenshot: bool = False,
    ) -> PlatformSearchResult:
        """Perform a bounded read-only discovery request without ranking candidates."""

        url = self._adapter.build_search_url(query)
        page = await self._collector.open(url, screenshot=screenshot)
        return self._adapter.parse_search(
            query,
            page,
            maximum_candidates=maximum_candidates,
        )
