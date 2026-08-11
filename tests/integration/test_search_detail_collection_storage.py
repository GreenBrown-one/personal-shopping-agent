"""Integration tests for one-connection atomic search/detail collection."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import HttpUrl
from sqlalchemy import Engine, event, func, select

from personal_shopping_agent.application import (
    CandidateDiscoveryService,
    ProductDetailService,
    SearchDetailCollectionService,
    ShoppingWorkflowService,
    WorkflowState,
)
from personal_shopping_agent.domain import Budget, Money, ShoppingRequest
from personal_shopping_agent.platforms import JDDetailAdapter, JDSearchAdapter
from personal_shopping_agent.storage import (
    SQLitePlatformObservationRepository,
    SQLiteSearchDetailUnitOfWork,
    SQLiteWorkflowRepository,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from personal_shopping_agent.storage.tables import (
    PlatformObservationRecord,
    ShoppingRequestRecord,
)

NOW = datetime(2026, 8, 9, 16, 0, tzinfo=UTC)
FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "jd"


class FixturePage:
    def __init__(self, final_url: str, html: str) -> None:
        self.final_url = HttpUrl(final_url)
        self.html = html
        self.captured_at = NOW


class FixtureCollector:
    def __init__(self, *, fail_detail: bool = False) -> None:
        self.fail_detail = fail_detail
        self.calls: list[str] = []

    async def open(self, url: str, *, screenshot: bool = False) -> FixturePage:
        self.calls.append(url)
        if url.startswith("https://search.jd.com/"):
            return FixturePage(
                "https://search.jd.com/Search?keyword=example+phone&enc=utf-8",
                (FIXTURE_ROOT / "search_results.html").read_text(encoding="utf-8"),
            )
        if self.fail_detail:
            raise RuntimeError("sanitized detail collection failure")
        return FixturePage(
            "https://item.jd.com/1000001.html",
            (FIXTURE_ROOT / "product_detail.html").read_text(encoding="utf-8"),
        )


def build_request() -> ShoppingRequest:
    return ShoppingRequest(
        query="example phone",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        region="云南省曲靖市",
        created_at=NOW,
    )


def connection_events(engine: Engine) -> tuple[list[str], Callable[[], None]]:
    lifecycle: list[str] = []

    def checked_out(_connection: object, _record: object, _proxy: object) -> None:
        lifecycle.append("checkout")

    def checked_in(_connection: object, _record: object) -> None:
        lifecycle.append("checkin")

    event.listen(engine, "checkout", checked_out)
    event.listen(engine, "checkin", checked_in)

    def remove() -> None:
        event.remove(engine, "checkout", checked_out)
        event.remove(engine, "checkin", checked_in)

    return lifecycle, remove


def build_collection_service(
    collector: FixtureCollector,
    engine: Engine,
) -> tuple[SearchDetailCollectionService, SQLiteWorkflowRepository]:
    factory = create_session_factory(engine)
    workflow_repository = SQLiteWorkflowRepository(factory)
    service = SearchDetailCollectionService(
        CandidateDiscoveryService(collector, JDSearchAdapter()),
        ProductDetailService(collector, JDDetailAdapter()),
        lambda: SQLiteSearchDetailUnitOfWork(factory),
        clock=lambda: NOW,
    )
    return service, workflow_repository


def test_collection_uses_one_connection_and_commits_linked_observations_and_state() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    collector = FixtureCollector()
    service, workflow_repository = build_collection_service(collector, engine)
    started = ShoppingWorkflowService(workflow_repository, clock=lambda: NOW).start(build_request())
    lifecycle, remove_listener = connection_events(engine)

    result = asyncio.run(
        service.collect(
            started.workflow.id,
            maximum_candidates=1,
            maximum_details=1,
        )
    )
    remove_listener()

    assert lifecycle == ["checkout", "checkin"]
    assert result.snapshot.workflow.state is WorkflowState.CANDIDATES_DISCOVERED
    assert result.search_observation.request_id == started.request.id
    assert result.detail_observations[0].request_id == started.request.id
    assert collector.calls == [
        "https://search.jd.com/Search?keyword=example+phone&enc=utf-8",
        "https://item.jd.com/1000001.html",
    ]

    factory = create_session_factory(engine)
    observation_repository = SQLitePlatformObservationRepository(factory)
    assert observation_repository.list_search_observations(started.request.id) == (
        result.search_observation,
    )
    assert observation_repository.list_detail_observations(started.request.id) == (
        result.detail_observations[0],
    )
    assert workflow_repository.get_snapshot(started.workflow.id) == result.snapshot
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ShoppingRequestRecord)) == 1
        assert session.scalar(select(func.count()).select_from(PlatformObservationRecord)) == 2

    engine.dispose()


def test_collection_failure_rolls_back_observations_and_state_and_closes_connection() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    collector = FixtureCollector(fail_detail=True)
    service, workflow_repository = build_collection_service(collector, engine)
    started = ShoppingWorkflowService(workflow_repository, clock=lambda: NOW).start(build_request())
    lifecycle, remove_listener = connection_events(engine)

    with pytest.raises(RuntimeError, match="sanitized detail collection failure"):
        asyncio.run(
            service.collect(
                started.workflow.id,
                maximum_candidates=1,
                maximum_details=1,
            )
        )
    remove_listener()

    assert lifecycle == ["checkout", "checkin"]
    factory = create_session_factory(engine)
    observation_repository = SQLitePlatformObservationRepository(factory)
    assert observation_repository.list_search_observations(started.request.id) == ()
    assert observation_repository.list_detail_observations(started.request.id) == ()
    assert workflow_repository.get_snapshot(started.workflow.id).workflow.state is (
        WorkflowState.REQUEST_VALIDATED
    )

    engine.dispose()


def test_unit_of_work_requires_context_and_rolls_back_when_not_committed() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    unit_of_work = SQLiteSearchDetailUnitOfWork(create_session_factory(engine))

    with pytest.raises(RuntimeError, match="not active"):
        unit_of_work.commit()
    with unit_of_work:
        pass

    engine.dispose()
