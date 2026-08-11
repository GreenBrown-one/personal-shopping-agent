"""In-memory protocol tests for the official MCP Python SDK v2 adapter."""

import asyncio
from pathlib import Path

from mcp import Client

from personal_shopping_agent.application import ShoppingWorkflowService, WorkflowSnapshot
from personal_shopping_agent.mcp.server import create_mcp_server, create_server_for_database
from personal_shopping_agent.storage import (
    SQLiteWorkflowRepository,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)


def test_mcp_tools_are_discoverable_and_round_trip_structured_workflows() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    service = ShoppingWorkflowService(SQLiteWorkflowRepository(create_session_factory(engine)))
    server = create_mcp_server(service)

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == [
                "shopping_agent_status",
                "start_shopping_workflow",
                "get_shopping_workflow",
            ]
            assert listed.tools[0].annotations is not None
            assert listed.tools[0].annotations.read_only_hint is True
            assert listed.tools[1].annotations is not None
            assert listed.tools[1].annotations.read_only_hint is False
            assert listed.tools[1].annotations.destructive_hint is False

            status = await client.call_tool("shopping_agent_status", {})
            assert status.structured_content is not None
            assert status.structured_content["milestone"] == "M2"
            assert status.structured_content["cross_platform_comparison"] is False
            assert status.structured_content["automatic_purchase"] is False

            started_result = await client.call_tool(
                "start_shopping_workflow",
                {
                    "request": {
                        "query": "预算五千元、续航优先的手机",
                        "category": "smartphone",
                        "budget_maximum": "5000",
                        "stretch_budget_maximum": "5500",
                        "region": "云南省曲靖市",
                        "criteria": [
                            {
                                "key": "battery_capacity",
                                "weight": "2",
                                "hard_requirement": True,
                                "minimum": "5000",
                                "unit": "mAh",
                            }
                        ],
                    }
                },
            )
            assert started_result.structured_content is not None
            started = WorkflowSnapshot.model_validate(started_result.structured_content)
            assert started.workflow.state.value == "request_validated"
            assert started.request.budget.stretch_maximum is not None
            assert started.request.budget.stretch_maximum.amount == 5500

            fetched_result = await client.call_tool(
                "get_shopping_workflow",
                {"workflow_id": str(started.workflow.id)},
            )
            assert fetched_result.structured_content is not None
            assert WorkflowSnapshot.model_validate(fetched_result.structured_content) == started

    asyncio.run(scenario())
    engine.dispose()


def test_database_server_factory_uses_existing_migrated_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "server.db"
    database_url = f"sqlite:///{database_path}"
    setup_engine = create_sqlite_engine(database_url)
    create_schema(setup_engine)
    setup_engine.dispose()
    server = create_server_for_database(database_url)

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            result = await client.call_tool("shopping_agent_status", {})
            assert result.structured_content is not None
            assert result.structured_content["local_storage"] is True

    asyncio.run(scenario())
