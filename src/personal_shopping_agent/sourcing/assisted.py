"""Plan which pages the user should save so the assisted pipeline can run without fetching."""

from typing import Literal, Protocol

from pydantic import ConfigDict

from personal_shopping_agent.domain import JsonContractModel
from personal_shopping_agent.sourcing.discovery import CollectedPage, PlatformSearchAdapter


class SavedPageIndex(Protocol):
    """Read-only view of the user's saved pages."""

    def is_saved(self, url: str) -> bool: ...

    async def open(self, url: str, *, screenshot: bool = False) -> CollectedPage: ...


class PageToSave(JsonContractModel):
    """One page the pipeline will read, and whether the user has saved it yet."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["search", "detail"]
    url: str
    saved: bool


class SavedPagePlan(JsonContractModel):
    """Pages to save, in the order the pipeline reads them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pages: tuple[PageToSave, ...]
    ready: bool
    instructions: str = (
        "Open each unsaved URL in your own browser and save it as 'Webpage, Complete' (.html) or "
        "'Webpage, Single File' (.mhtml) into data/inbox. Save the search page first; its "
        "results decide which product pages are needed."
    )


class SavedPagePlanner:
    """Derive the exact search and detail URLs the collection stage will request."""

    def __init__(self, index: SavedPageIndex, search_adapter: PlatformSearchAdapter) -> None:
        self._index = index
        self._search_adapter = search_adapter

    async def plan(
        self,
        query: str,
        *,
        maximum_candidates: int,
        maximum_details: int,
    ) -> SavedPagePlan:
        """List the search page, then (once it is saved) the product pages it leads to."""

        search_url = self._search_adapter.build_search_url(query)
        if not self._index.is_saved(search_url):
            return SavedPagePlan(
                pages=(PageToSave(kind="search", url=search_url, saved=False),), ready=False
            )
        page = await self._index.open(search_url)
        result = self._search_adapter.parse_search(
            query, page, maximum_candidates=maximum_candidates
        )
        details = tuple(
            PageToSave(
                kind="detail",
                url=str(candidate.product_url),
                saved=self._index.is_saved(str(candidate.product_url)),
            )
            for candidate in result.candidates[:maximum_details]
        )
        return SavedPagePlan(
            pages=(PageToSave(kind="search", url=search_url, saved=True), *details),
            ready=bool(details) and all(item.saved for item in details),
        )
