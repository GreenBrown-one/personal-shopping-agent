"""Offline SDK-level proof that the configured JD pipeline runs and resumes end to end."""

# ruff: noqa: RUF001 -- JD specification fixtures intentionally use full-width punctuation.

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from mcp import Client
from mcp.types import TextContent
from pydantic import HttpUrl, ValidationError

from personal_shopping_agent.automation import (
    PIPELINE_STATE_SEQUENCE,
    ShoppingPipelineOptions,
    ShoppingPipelineResult,
    ShoppingWorkflowService,
)
from personal_shopping_agent.domain import (
    Budget,
    Money,
    ShoppingCriterion,
    ShoppingRequest,
    WorkflowSnapshot,
    WorkflowState,
)
from personal_shopping_agent.infrastructure.storage import (
    SQLiteWorkflowRepository,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from personal_shopping_agent.interfaces.composition import NoOfficialEvidenceProvider
from personal_shopping_agent.interfaces.mcp import create_jd_pipeline_server_for_database
from personal_shopping_agent.presentation import (
    RenderedShoppingReport,
)
from personal_shopping_agent.sourcing import (
    ChipBenchmarkEntry,
    ChipBenchmarkEvidenceProvider,
    ChipBenchmarkReference,
)
from personal_shopping_agent.sourcing.browser import BrowserSnapshot, NavigationPolicyError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "jd"


def test_pipeline_detail_limit_cannot_exceed_candidate_limit() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        ShoppingPipelineOptions(maximum_candidates=1, maximum_details=2)


class FixtureJDPageCollector:
    """Serve sanitized inert fixtures through the same page port as the controlled browser."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def open(self, url: str, *, screenshot: bool = False) -> BrowserSnapshot:
        assert screenshot is False
        self.calls.append(url)
        hostname = (urlsplit(url).hostname or "").lower()
        if hostname == "search.jd.com":
            fixture_name = "search_results.html"
            final_url = url
            title = "Sanitized JD search fixture"
        elif url == "https://item.jd.com/1000001.html":
            fixture_name = "product_detail.html"
            final_url = url
            title = "Sanitized JD detail fixture"
        else:
            raise AssertionError(f"unexpected fixture URL: {url}")
        return BrowserSnapshot.model_validate(
            {
                "requested_url": url,
                "final_url": final_url,
                "status_code": 200,
                "title": title,
                "html": (FIXTURES / fixture_name).read_text(encoding="utf-8"),
                "captured_at": datetime.now(UTC),
            }
        )


def test_explicit_jd_pipeline_runs_resumes_and_keeps_mcp_boundaries(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'pipeline.db'}"
    setup_engine = create_sqlite_engine(database_url)
    create_schema(setup_engine)
    setup_engine.dispose()
    collector = FixtureJDPageCollector()
    server = create_jd_pipeline_server_for_database(
        database_url,
        collector,
        NoOfficialEvidenceProvider(),
        environment={},
    )

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            listed = await client.list_tools()
            assert listed.tools[-1].name == "run_shopping_pipeline"
            assert listed.tools[-1].annotations is not None
            assert listed.tools[-1].annotations.read_only_hint is False
            assert listed.tools[-1].annotations.destructive_hint is False
            assert listed.tools[-1].annotations.idempotent_hint is False
            assert listed.tools[-1].annotations.open_world_hint is True

            status = await client.call_tool("shopping_agent_status", {})
            assert status.structured_content is not None
            assert status.structured_content["milestone"] == "M8"
            assert status.structured_content["end_to_end_pipeline"] is True
            assert status.structured_content["platform_collection"] is True
            assert status.structured_content["ranking"] is True
            assert status.structured_content["cross_platform_comparison"] is False

            started_call = await client.call_tool(
                "start_shopping_workflow",
                {
                    "request": {
                        "query": "Example Aurora Phone",
                        "category": "smartphone",
                        "budget_maximum": "5000",
                        "region": "云南省 曲靖市 麒麟区",
                        "criteria": [
                            {
                                "key": "battery_capacity",
                                "minimum": "5000",
                                "preferred": "6000",
                                "unit": "mAh",
                            }
                        ],
                    }
                },
            )
            assert started_call.structured_content is not None
            started = WorkflowSnapshot.model_validate(started_call.structured_content)

            run_call = await client.call_tool(
                "run_shopping_pipeline",
                {
                    "request": {
                        "workflow_id": str(started.workflow.id),
                        "maximum_candidates": 1,
                        "maximum_details": 1,
                    }
                },
            )
            assert run_call.structured_content is not None
            result = ShoppingPipelineResult.model_validate(run_call.structured_content)
            assert result.started_from is WorkflowState.REQUEST_VALIDATED
            assert result.executed_stages == PIPELINE_STATE_SEQUENCE[1:]
            assert result.snapshot.workflow.state is WorkflowState.COMPLETED
            assert result.rendered.report.recommended_product_id is not None
            assert len(result.rendered.report.candidates) == 1
            assert len(collector.calls) == 2

            stored_call = await client.call_tool(
                "get_shopping_report",
                {"workflow_id": str(started.workflow.id)},
            )
            assert stored_call.structured_content is not None
            assert RenderedShoppingReport.model_validate(stored_call.structured_content) == (
                result.rendered
            )

            resumed_call = await client.call_tool(
                "run_shopping_pipeline",
                {
                    "request": {
                        "workflow_id": str(started.workflow.id),
                        "maximum_candidates": 1,
                        "maximum_details": 1,
                    }
                },
            )
            assert resumed_call.structured_content is not None
            resumed = ShoppingPipelineResult.model_validate(resumed_call.structured_content)
            assert resumed.started_from is WorkflowState.COMPLETED
            assert resumed.executed_stages == ()
            assert resumed.rendered == result.rendered
            assert len(collector.calls) == 2

            invalid_snapshot = result.snapshot.model_copy(
                update={
                    "workflow": result.snapshot.workflow.model_copy(
                        update={"state": WorkflowState.REPORT_RENDERED}
                    )
                }
            )
            with pytest.raises(ValidationError, match="completed workflow"):
                ShoppingPipelineResult.model_validate(
                    result.model_dump() | {"snapshot": invalid_snapshot}
                )
            mismatched_snapshot = result.snapshot.model_copy(
                update={"workflow": result.snapshot.workflow.model_copy(update={"id": uuid4()})}
            )
            with pytest.raises(ValidationError, match="report must match"):
                ShoppingPipelineResult.model_validate(
                    result.model_dump() | {"snapshot": mismatched_snapshot}
                )
            with pytest.raises(ValidationError, match="start state is not resumable"):
                ShoppingPipelineResult.model_validate(
                    result.model_dump() | {"started_from": WorkflowState.FAILED}
                )
            with pytest.raises(ValidationError, match="exact remaining suffix"):
                ShoppingPipelineResult.model_validate(result.model_dump() | {"executed_stages": ()})

            failed_call = await client.call_tool(
                "start_shopping_workflow",
                {
                    "request": {
                        "query": "failed fixture",
                        "category": "smartphone",
                        "budget_maximum": "5000",
                    }
                },
            )
            assert failed_call.structured_content is not None
            failed = WorkflowSnapshot.model_validate(failed_call.structured_content)
            failure_engine = create_sqlite_engine(database_url)
            failure_service = ShoppingWorkflowService(
                SQLiteWorkflowRepository(create_session_factory(failure_engine))
            )
            failure_service.fail(
                failed.workflow.id,
                error_code="fixture_failure",
                error_message="Sanitized fixture failure.",
            )
            failure_engine.dispose()
            failed_run = await client.call_tool(
                "run_shopping_pipeline",
                {
                    "request": {
                        "workflow_id": str(failed.workflow.id),
                        "maximum_candidates": 1,
                        "maximum_details": 1,
                    }
                },
            )
            assert failed_run.is_error is True
            assert len(failed_run.content) == 1
            assert isinstance(failed_run.content[0], TextContent)
            assert "cannot enter" in failed_run.content[0].text

    asyncio.run(scenario())


def test_pipeline_stops_before_platform_access_when_stored_request_is_unclear(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'unclear.db'}"
    engine = create_sqlite_engine(database_url)
    create_schema(engine)
    workflow_service = ShoppingWorkflowService(
        SQLiteWorkflowRepository(create_session_factory(engine))
    )
    unclear = workflow_service.start(
        ShoppingRequest(
            query="stored before intake review existed",
            category="smartphone",
            budget=Budget(maximum=Money(amount=Decimal("5000"))),
            criteria=(ShoppingCriterion(key="battery_capacity", unit="mAh"),),
        )
    )
    collector = FixtureJDPageCollector()
    server = create_jd_pipeline_server_for_database(
        database_url,
        collector,
        NoOfficialEvidenceProvider(),
        environment={},
    )

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            result = await client.call_tool(
                "run_shopping_pipeline",
                {"request": {"workflow_id": str(unclear.workflow.id)}},
            )
            assert result.is_error is True
            assert isinstance(result.content[0], TextContent)
            assert "battery_capacity: bounds_missing" in result.content[0].text

    asyncio.run(scenario())
    assert collector.calls == []
    assert workflow_service.get(unclear.workflow.id).workflow.state is (
        WorkflowState.REQUEST_VALIDATED
    )
    engine.dispose()


class SignInWallCollector(FixtureJDPageCollector):
    """Simulate JD redirecting an unauthenticated search to its sign-in host."""

    async def open(self, url: str, *, screenshot: bool = False) -> BrowserSnapshot:
        self.calls.append(url)
        raise NavigationPolicyError(
            "sign_in_required",
            "The platform requires a manual sign-in. Run `personal-shopping-agent login` "
            "for this platform on this computer, then retry.",
        )


def test_sign_in_wall_stops_with_login_instructions_and_stays_resumable(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'sign-in.db'}"
    engine = create_sqlite_engine(database_url)
    create_schema(engine)
    workflow_service = ShoppingWorkflowService(
        SQLiteWorkflowRepository(create_session_factory(engine))
    )
    started = workflow_service.start(
        ShoppingRequest(
            query="大电池手机",
            category="smartphone",
            region="云南省曲靖市",
            budget=Budget(maximum=Money(amount=Decimal("3000"))),
            criteria=(
                ShoppingCriterion(key="battery_capacity", minimum=Decimal("5000"), unit="mAh"),
            ),
        )
    )
    collector = SignInWallCollector()
    server = create_jd_pipeline_server_for_database(
        database_url, collector, NoOfficialEvidenceProvider(), environment={}
    )

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            result = await client.call_tool(
                "run_shopping_pipeline",
                {"request": {"workflow_id": str(started.workflow.id)}},
            )
            assert result.is_error is True
            assert isinstance(result.content[0], TextContent)
            assert "personal-shopping-agent login" in result.content[0].text

    asyncio.run(scenario())
    assert len(collector.calls) == 1
    assert workflow_service.get(started.workflow.id).workflow.state is (
        WorkflowState.REQUEST_VALIDATED
    )
    engine.dispose()


class ChipSpecFixtureCollector(FixtureJDPageCollector):
    """Serve the sanitized detail fixture with one declared chip specification line added."""

    async def open(self, url: str, *, screenshot: bool = False) -> BrowserSnapshot:
        page = await super().open(url, screenshot=screenshot)
        if "item.jd.com" not in url:
            return page
        html = page.html.replace(
            "<li>电池容量：6000mAh</li>",
            "<li>电池容量：6000mAh</li><li>CPU型号：第三代骁龙8移动平台</li>",
        )
        assert html != page.html
        return page.model_copy(update={"html": html})


def test_chip_benchmark_evidence_flows_into_normalization_ranking_and_report(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'chip.db'}"
    setup_engine = create_sqlite_engine(database_url)
    create_schema(setup_engine)
    setup_engine.dispose()
    reference = ChipBenchmarkReference(
        source_url=HttpUrl("https://www.socpk.com/allperf/?brand=phone"),
        source_title="极客湾 SOCPK 手机/平板芯片综合性能排行",
        method="synthetic test reference",
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        entries=(
            ChipBenchmarkEntry(name="骁龙 8 Gen3", score=Decimal("280")),
            ChipBenchmarkEntry(name="骁龙 8 Elite Gen5", score=Decimal("420")),
        ),
    )
    server = create_jd_pipeline_server_for_database(
        database_url,
        ChipSpecFixtureCollector(),
        NoOfficialEvidenceProvider(),
        environment={},
        independent_providers=(ChipBenchmarkEvidenceProvider(reference),),
    )

    async def scenario() -> ShoppingPipelineResult:
        async with Client(server, raise_exceptions=True) as client:
            status = await client.call_tool("shopping_agent_status", {})
            assert status.structured_content is not None
            assert status.structured_content["chip_benchmark"] is True
            started_call = await client.call_tool(
                "start_shopping_workflow",
                {
                    "request": {
                        "query": "Example Aurora Phone",
                        "category": "smartphone",
                        "budget_maximum": "5000",
                        "region": "北京市",
                        "criteria": [
                            {
                                "key": "battery_capacity",
                                "minimum": "5000",
                                "preferred": "6000",
                                "unit": "mAh",
                            },
                            {
                                "key": "chip_performance",
                                "minimum": "100",
                                "preferred": "420",
                                "unit": "SOCPK",
                            },
                        ],
                    }
                },
            )
            assert started_call.structured_content is not None
            started = WorkflowSnapshot.model_validate(started_call.structured_content)
            run_call = await client.call_tool(
                "run_shopping_pipeline",
                {
                    "request": {
                        "workflow_id": str(started.workflow.id),
                        "maximum_candidates": 1,
                        "maximum_details": 1,
                    }
                },
            )
            assert run_call.structured_content is not None
            return ShoppingPipelineResult.model_validate(run_call.structured_content)

    result = asyncio.run(scenario())
    candidate = result.rendered.report.candidates[0]
    chip = next(
        item
        for item in candidate.score.foundation.criterion_evaluations
        if item.key == "chip_performance"
    )
    assert chip.status.value == "satisfied"
    assert chip.observed_values == (Decimal("280"),)
    # 0.5 + 0.5 * (280 - 100) / (420 - 100)
    assert chip.score == Decimal("0.78125")
    assert any(
        item.field_path == "specifications.chip_performance"
        and item.source_type.value == "independent_review"
        for item in candidate.evidence
    )
    assert "| chip\\_performance |" in result.rendered.content
