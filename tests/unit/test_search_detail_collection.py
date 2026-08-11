"""Unit tests for transactional search/detail collection orchestration."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from uuid import UUID

import pytest
from pydantic import HttpUrl

from personal_shopping_agent.application import (
    DetailObservation,
    InvalidWorkflowTransitionError,
    NoCandidatesDiscoveredError,
    PlatformCandidate,
    PlatformProductDetail,
    PlatformSearchResult,
    SearchDetailCollectionService,
    SearchObservation,
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStateMachine,
)
from personal_shopping_agent.domain import Budget, Money, ShoppingRequest

NOW = datetime(2026, 8, 9, 16, 0, tzinfo=UTC)


def build_snapshot(*, discovered: bool = False) -> WorkflowSnapshot:
    request = ShoppingRequest(
        query="example phone",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        created_at=NOW,
    )
    machine = WorkflowStateMachine()
    workflow, events = machine.initialize(request.id, occurred_at=NOW)
    if discovered:
        workflow, event = machine.advance(
            workflow,
            WorkflowState.CANDIDATES_DISCOVERED,
            reason="already_discovered",
            occurred_at=NOW,
        )
        events = (*events, event)
    return WorkflowSnapshot(request=request, workflow=workflow, events=events)


def candidate(external_id: str) -> PlatformCandidate:
    return PlatformCandidate(
        platform="jd",
        external_id=external_id,
        title=f"Example Phone {external_id}",
        product_url=HttpUrl(f"https://item.jd.com/{external_id}.html"),
        displayed_price=Decimal("3999"),
    )


class FakeDiscovery:
    def __init__(self, candidates: tuple[PlatformCandidate, ...]) -> None:
        self.candidates = candidates
        self.calls: list[tuple[str, int, bool]] = []

    async def discover(
        self,
        query: str,
        *,
        maximum_candidates: int = 20,
        screenshot: bool = False,
    ) -> PlatformSearchResult:
        self.calls.append((query, maximum_candidates, screenshot))
        return PlatformSearchResult(
            platform="jd",
            query=query,
            source_url=HttpUrl("https://search.jd.com/Search?keyword=example+phone"),
            captured_at=NOW,
            candidates=self.candidates[:maximum_candidates],
        )


class FakeDetails:
    def __init__(self, *, failure: RuntimeError | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, bool]] = []

    async def collect(
        self,
        candidate: PlatformCandidate,
        *,
        screenshot: bool = False,
    ) -> PlatformProductDetail:
        self.calls.append((candidate.external_id, screenshot))
        if self.failure is not None:
            raise self.failure
        return PlatformProductDetail(
            platform=candidate.platform,
            external_id=candidate.external_id,
            title=candidate.title,
            product_url=candidate.product_url,
            captured_at=NOW,
            brand="Example",
            model="A1",
            seller_name="Example 官方旗舰店",
            displayed_price=Decimal("3999"),
        )


class FakeUnitOfWork:
    def __init__(self, snapshot: WorkflowSnapshot) -> None:
        self.snapshot = snapshot
        self.entered = False
        self.exited = False
        self.committed = False
        self.rolled_back = False
        self.searches: list[SearchObservation] = []
        self.details: list[DetailObservation] = []
        self.transition: tuple[ShoppingWorkflow, WorkflowEvent, int] | None = None

    def __enter__(self) -> "FakeUnitOfWork":
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        self.exited = True
        self.rolled_back = exc_type is not None or not self.committed

    def get_snapshot(self, workflow_id: UUID) -> WorkflowSnapshot:
        assert workflow_id == self.snapshot.workflow.id
        return self.snapshot

    def add_search_observation(self, observation: SearchObservation) -> None:
        self.searches.append(observation)

    def add_detail_observation(self, observation: DetailObservation) -> None:
        self.details.append(observation)

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None:
        self.transition = (workflow, event, expected_revision)

    def commit(self) -> None:
        self.committed = True


def test_service_commits_bounded_observations_and_conservative_transition() -> None:
    snapshot = build_snapshot()
    discovery = FakeDiscovery((candidate("1000001"), candidate("1000002"), candidate("1000003")))
    details = FakeDetails()
    unit_of_work = FakeUnitOfWork(snapshot)
    service = SearchDetailCollectionService(
        discovery,
        details,
        lambda: unit_of_work,
        clock=lambda: NOW,
    )

    result = asyncio.run(
        service.collect(
            snapshot.workflow.id,
            maximum_candidates=3,
            maximum_details=2,
            screenshot=True,
        )
    )

    assert discovery.calls == [(snapshot.request.query, 3, True)]
    assert details.calls == [("1000001", True), ("1000002", True)]
    assert unit_of_work.entered and unit_of_work.exited and unit_of_work.committed
    assert not unit_of_work.rolled_back
    assert unit_of_work.searches == [result.search_observation]
    assert unit_of_work.details == list(result.detail_observations)
    assert all(item.request_id == snapshot.request.id for item in unit_of_work.details)
    assert result.snapshot.workflow.state is WorkflowState.CANDIDATES_DISCOVERED
    assert result.snapshot.workflow.updated_at == NOW
    assert result.snapshot.events[-1].reason == "search_and_detail_observations_persisted"
    assert unit_of_work.transition is not None
    assert unit_of_work.transition[2] == snapshot.workflow.revision


@pytest.mark.parametrize(
    ("maximum_candidates", "maximum_details", "message"),
    ((0, 1, "maximum_candidates"), (1, 0, "maximum_details"), (1, 2, "cannot exceed")),
)
def test_service_rejects_invalid_limits_before_opening_work_unit(
    maximum_candidates: int,
    maximum_details: int,
    message: str,
) -> None:
    snapshot = build_snapshot()
    unit_of_work = FakeUnitOfWork(snapshot)
    service = SearchDetailCollectionService(FakeDiscovery(()), FakeDetails(), lambda: unit_of_work)

    with pytest.raises(ValueError, match=message):
        asyncio.run(
            service.collect(
                snapshot.workflow.id,
                maximum_candidates=maximum_candidates,
                maximum_details=maximum_details,
            )
        )

    assert not unit_of_work.entered


def test_service_rolls_back_empty_search_detail_failure_and_wrong_state() -> None:
    snapshot = build_snapshot()
    empty_unit = FakeUnitOfWork(snapshot)
    empty_service = SearchDetailCollectionService(
        FakeDiscovery(()), FakeDetails(), lambda: empty_unit
    )
    with pytest.raises(NoCandidatesDiscoveredError, match="no candidates"):
        asyncio.run(empty_service.collect(snapshot.workflow.id))
    assert empty_unit.exited and empty_unit.rolled_back and not empty_unit.committed

    failure_unit = FakeUnitOfWork(snapshot)
    failure_service = SearchDetailCollectionService(
        FakeDiscovery((candidate("1000001"),)),
        FakeDetails(failure=RuntimeError("sanitized detail failure")),
        lambda: failure_unit,
    )
    with pytest.raises(RuntimeError, match="sanitized detail failure"):
        asyncio.run(failure_service.collect(snapshot.workflow.id))
    assert failure_unit.exited and failure_unit.rolled_back and not failure_unit.committed

    discovered_snapshot = build_snapshot(discovered=True)
    wrong_state_unit = FakeUnitOfWork(discovered_snapshot)
    discovery = FakeDiscovery((candidate("1000001"),))
    wrong_state_service = SearchDetailCollectionService(
        discovery,
        FakeDetails(),
        lambda: wrong_state_unit,
    )
    with pytest.raises(InvalidWorkflowTransitionError, match="request_validated"):
        asyncio.run(wrong_state_service.collect(discovered_snapshot.workflow.id))
    assert discovery.calls == []
    assert wrong_state_unit.exited and wrong_state_unit.rolled_back
