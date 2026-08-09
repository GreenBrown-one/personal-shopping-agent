"""Transactional search/detail collection orchestration contracts."""

from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from personal_shopping_agent.application.details import PlatformProductDetail
from personal_shopping_agent.application.discovery import (
    PlatformCandidate,
    PlatformSearchResult,
)
from personal_shopping_agent.application.observations import (
    DetailObservation,
    SearchObservation,
)
from personal_shopping_agent.application.workflow import (
    InvalidWorkflowTransitionError,
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStateMachine,
    workflow_now,
)


class SearchDetailCollectionResult(BaseModel):
    """Validated result of one atomically persisted collection batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: WorkflowSnapshot
    search_observation: SearchObservation
    detail_observations: tuple[DetailObservation, ...] = Field(min_length=1)


class NoCandidatesDiscoveredError(RuntimeError):
    """Sanitized signal that a search returned no candidates to inspect."""


class CandidateDiscovery(Protocol):
    """Bounded search use case required by transactional collection."""

    async def discover(
        self,
        query: str,
        *,
        maximum_candidates: int = 20,
        screenshot: bool = False,
    ) -> PlatformSearchResult: ...


class ProductDetailCollector(Protocol):
    """Detail collection use case required by transactional collection."""

    async def collect(
        self,
        candidate: PlatformCandidate,
        *,
        screenshot: bool = False,
    ) -> PlatformProductDetail: ...


class SearchDetailUnitOfWork(Protocol):
    """Request-scoped transaction port shared across search and detail collection."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def get_snapshot(self, workflow_id: UUID) -> WorkflowSnapshot: ...

    def add_search_observation(self, observation: SearchObservation) -> None: ...

    def add_detail_observation(self, observation: DetailObservation) -> None: ...

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None: ...

    def commit(self) -> None: ...


class SearchDetailUnitOfWorkFactory(Protocol):
    """Create one isolated database work unit for a collection request."""

    def __call__(self) -> SearchDetailUnitOfWork: ...


class SearchDetailCollectionService:
    """Collect search and detail observations in one durable transaction."""

    def __init__(
        self,
        discovery: CandidateDiscovery,
        details: ProductDetailCollector,
        unit_of_work_factory: SearchDetailUnitOfWorkFactory,
        *,
        state_machine: WorkflowStateMachine | None = None,
        clock: Callable[[], datetime] = workflow_now,
    ) -> None:
        self._discovery = discovery
        self._details = details
        self._unit_of_work_factory = unit_of_work_factory
        self._state_machine = state_machine or WorkflowStateMachine()
        self._clock = clock

    async def collect(
        self,
        workflow_id: UUID,
        *,
        maximum_candidates: int = 20,
        maximum_details: int = 5,
        screenshot: bool = False,
    ) -> SearchDetailCollectionResult:
        """Persist one bounded search/detail batch and its conservative state advance."""

        self._validate_limits(maximum_candidates, maximum_details)
        with self._unit_of_work_factory() as unit_of_work:
            snapshot = unit_of_work.get_snapshot(workflow_id)
            if (
                self._state_machine.next_state(snapshot.workflow.state)
                is not WorkflowState.CANDIDATES_DISCOVERED
            ):
                raise InvalidWorkflowTransitionError(
                    "search/detail collection requires a request_validated workflow"
                )

            search_result = await self._discovery.discover(
                snapshot.request.query,
                maximum_candidates=maximum_candidates,
                screenshot=screenshot,
            )
            if not search_result.candidates:
                raise NoCandidatesDiscoveredError(
                    "The platform search returned no candidates; collection was not committed."
                )

            search_observation = SearchObservation(
                request_id=snapshot.request.id,
                result=search_result,
            )
            unit_of_work.add_search_observation(search_observation)

            detail_observations: list[DetailObservation] = []
            for candidate in search_result.candidates[:maximum_details]:
                detail = await self._details.collect(candidate, screenshot=screenshot)
                observation = DetailObservation(
                    request_id=snapshot.request.id,
                    detail=detail,
                )
                unit_of_work.add_detail_observation(observation)
                detail_observations.append(observation)

            updated, event = self._state_machine.advance(
                snapshot.workflow,
                WorkflowState.CANDIDATES_DISCOVERED,
                reason="search_and_detail_observations_persisted",
                occurred_at=self._clock(),
            )
            unit_of_work.save_transition(
                updated,
                event,
                expected_revision=snapshot.workflow.revision,
            )
            updated_snapshot = WorkflowSnapshot(
                request=snapshot.request,
                workflow=updated,
                events=(*snapshot.events, event),
            )
            result = SearchDetailCollectionResult(
                snapshot=updated_snapshot,
                search_observation=search_observation,
                detail_observations=tuple(detail_observations),
            )
            unit_of_work.commit()
            return result

    @staticmethod
    def _validate_limits(maximum_candidates: int, maximum_details: int) -> None:
        if maximum_candidates < 1:
            raise ValueError("maximum_candidates must be at least 1")
        if maximum_details < 1:
            raise ValueError("maximum_details must be at least 1")
        if maximum_details > maximum_candidates:
            raise ValueError("maximum_details cannot exceed maximum_candidates")
