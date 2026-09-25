"""Validated structured platform observations and recording use case."""

from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from personal_shopping_agent.sourcing.details import PlatformProductDetail
from personal_shopping_agent.sourcing.discovery import PlatformSearchResult


class PlatformObservationKind(StrEnum):
    """Structured observation kinds stored without raw page material."""

    SEARCH = "search"
    DETAIL = "detail"


class ObservationModel(BaseModel):
    """Strict immutable base for structured observation envelopes."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SearchObservation(ObservationModel):
    """One search result linked to the shopping request that caused it."""

    id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    result: PlatformSearchResult


class DetailObservation(ObservationModel):
    """One unverified detail snapshot linked to its shopping request."""

    id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    detail: PlatformProductDetail


class PlatformObservationStore(Protocol):
    """Persistence port implemented by local storage adapters."""

    def add_search_observation(self, observation: SearchObservation) -> None: ...

    def add_detail_observation(self, observation: DetailObservation) -> None: ...


class PlatformObservationService:
    """Record validated parser output without promoting it to Product or Offer."""

    def __init__(self, store: PlatformObservationStore) -> None:
        self._store = store

    def record_search(
        self,
        request_id: UUID,
        result: PlatformSearchResult,
    ) -> SearchObservation:
        """Persist one structured search observation and return its envelope."""

        observation = SearchObservation(request_id=request_id, result=result)
        self._store.add_search_observation(observation)
        return observation

    def record_detail(
        self,
        request_id: UUID,
        detail: PlatformProductDetail,
    ) -> DetailObservation:
        """Persist one structured detail observation and return its envelope."""

        observation = DetailObservation(request_id=request_id, detail=detail)
        self._store.add_detail_observation(observation)
        return observation
