"""The default server runs the whole pipeline from pages the user saved, asking for each one."""

import asyncio
from pathlib import Path

from mcp import Client
from mcp.types import TextContent

from personal_shopping_agent.domain import WorkflowSnapshot, WorkflowState
from personal_shopping_agent.infrastructure.settings import BENCHMARK_FILE_ENV, INBOX_DIR_ENV
from personal_shopping_agent.infrastructure.storage import create_schema, create_sqlite_engine
from personal_shopping_agent.interfaces.mcp import create_server_for_database
from personal_shopping_agent.interfaces.mcp.schemas import PipelineRunView
from personal_shopping_agent.sourcing import SavedPagePlan
from personal_shopping_agent.sourcing.platforms import JDSearchAdapter

FIXTURES = Path(__file__).parents[1] / "fixtures" / "jd"
QUERY = "Example Aurora Phone"
ITEM_URL = "https://item.jd.com/1000001.html"


def save_page(inbox: Path, name: str, url: str, fixture: str) -> None:
    """Imitate Chrome's 'Webpage, Complete', which records the source URL in a comment."""

    html = (FIXTURES / fixture).read_text(encoding="utf-8")
    (inbox / name).write_text(f"<!-- saved from url=({len(url):04d}){url} -->\n{html}", "utf-8")


def test_default_server_guides_the_user_page_by_page_to_a_report(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'assisted.db'}"
    engine = create_sqlite_engine(database_url)
    create_schema(engine)
    engine.dispose()
    inbox = tmp_path / "inbox"
    environment = {
        INBOX_DIR_ENV: str(inbox),
        BENCHMARK_FILE_ENV: str(tmp_path / "no-benchmark.json"),
    }
    server = create_server_for_database(database_url, environment=environment)
    search_url = JDSearchAdapter().build_search_url(QUERY)

    async def scenario() -> PipelineRunView:
        async with Client(server, raise_exceptions=True) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            pipeline_annotations = tools["run_shopping_pipeline"].annotations
            assert pipeline_annotations is not None
            assert pipeline_annotations.open_world_hint is False
            planner_annotations = tools["list_pages_to_save"].annotations
            assert planner_annotations is not None and planner_annotations.read_only_hint

            status = await client.call_tool("shopping_agent_status", {})
            assert status.structured_content is not None
            assert status.structured_content["collection_mode"] == "assisted"
            assert status.structured_content["end_to_end_pipeline"] is True

            started_call = await client.call_tool(
                "start_shopping_workflow",
                {
                    "request": {
                        "query": QUERY,
                        "category": "smartphone",
                        "budget_maximum": "5000",
                        "region": "北京市",
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
            workflow_id = str(
                WorkflowSnapshot.model_validate(started_call.structured_content).workflow.id
            )
            limits = {"workflow_id": workflow_id, "maximum_candidates": 1, "maximum_details": 1}

            first = await client.call_tool("list_pages_to_save", {"request": limits})
            assert first.structured_content is not None
            plan = SavedPagePlan.model_validate(first.structured_content)
            assert [(page.kind, page.url, page.saved) for page in plan.pages] == [
                ("search", search_url, False)
            ]
            assert plan.ready is False
            assert inbox.is_dir()

            save_page(inbox, "search.html", search_url, "search_results.html")
            second = await client.call_tool("list_pages_to_save", {"request": limits})
            assert second.structured_content is not None
            plan = SavedPagePlan.model_validate(second.structured_content)
            assert [(page.kind, page.url, page.saved) for page in plan.pages] == [
                ("search", search_url, True),
                ("detail", ITEM_URL, False),
            ]

            blocked = await client.call_tool("run_shopping_pipeline", {"request": limits})
            assert blocked.is_error is True
            assert isinstance(blocked.content[0], TextContent)
            assert ITEM_URL in blocked.content[0].text

            save_page(inbox, "item.html", ITEM_URL, "product_detail.html")
            third = await client.call_tool("list_pages_to_save", {"request": limits})
            assert third.structured_content is not None
            assert SavedPagePlan.model_validate(third.structured_content).ready is True

            completed = await client.call_tool("run_shopping_pipeline", {"request": limits})
            assert completed.structured_content is not None
            return PipelineRunView.model_validate(completed.structured_content)

    view = asyncio.run(scenario())
    assert view.started_from is WorkflowState.REQUEST_VALIDATED
    assert view.workflow_state is WorkflowState.COMPLETED
    assert view.report.candidates == 1
    assert view.report.content.startswith("# 购物比较报告")
