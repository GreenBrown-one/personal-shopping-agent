"""Offline SDK-level proof that the configured JD pipeline runs and resumes end to end."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from mcp import Client
from mcp.types import TextContent
from pydantic import ValidationError

from personal_shopping_agent.application import (
    PIPELINE_STATE_SEQUENCE,
    RenderedShoppingReport,
    ShoppingPipelineOptions,
    ShoppingPipelineResult,
    ShoppingWorkflowService,
    WorkflowSnapshot,
    WorkflowState,
)
from personal_shopping_agent.browser import BrowserSnapshot
from personal_shopping_agent.mcp import create_jd_pipeline_server_for_database
from personal_shopping_agent.runtime import NoOfficialEvidenceProvider
from personal_shopping_agent.storage import (
    SQLiteWorkflowRepository,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

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
            assert status.structured_content["milestone"] == "M7"
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
