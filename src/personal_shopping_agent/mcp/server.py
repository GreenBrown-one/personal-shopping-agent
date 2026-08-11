"""Official MCP Python SDK v2 adapter for the shopping workflow service."""

import os
from uuid import UUID

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from personal_shopping_agent.application import ShoppingWorkflowService, WorkflowSnapshot
from personal_shopping_agent.mcp.schemas import AgentCapabilities, StartShoppingWorkflowInput
from personal_shopping_agent.storage import create_session_factory, create_sqlite_engine
from personal_shopping_agent.storage.workflow_repository import SQLiteWorkflowRepository

DEFAULT_DATABASE_URL = "sqlite:///data/personal-shopping-agent.db"
DATABASE_URL_ENV = "PERSONAL_SHOPPING_DATABASE_URL"

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
LOCAL_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)


def create_mcp_server(service: ShoppingWorkflowService) -> MCPServer:
    """Register a deterministic, dependency-injected MCP server."""

    server = MCPServer(
        name="personal-shopping-agent",
        title="Personal Shopping Agent",
        version="0.1.0",
        description="Evidence-driven personal shopping workflow service.",
        instructions=(
            "Use these tools to structure and track shopping decisions. "
            "Platform collection, ranking, checkout, and payment are not available yet."
        ),
    )

    @server.tool(
        name="shopping_agent_status",
        title="Shopping agent capability status",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def _shopping_agent_status() -> AgentCapabilities:
        """Return an honest summary of implemented and unavailable capabilities."""

        return AgentCapabilities(
            milestone="M2",
            local_workflows=True,
            local_storage=True,
            platform_collection=False,
            cross_platform_comparison=False,
            ranking=False,
            automatic_purchase=False,
            message="M2 saves workflows; platform comparison begins in M3/M4.",
        )

    @server.tool(
        name="start_shopping_workflow",
        title="Start a local shopping workflow",
        annotations=LOCAL_WRITE,
        structured_output=True,
    )
    def _start_shopping_workflow(request: StartShoppingWorkflowInput) -> WorkflowSnapshot:
        """Validate a structured shopping need and create its local auditable workflow."""

        return service.start(request.to_domain())

    @server.tool(
        name="get_shopping_workflow",
        title="Get shopping workflow status",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def _get_shopping_workflow(workflow_id: UUID) -> WorkflowSnapshot:
        """Read the validated request, current state, and ordered transition history."""

        return service.get(workflow_id)

    _ = (_shopping_agent_status, _start_shopping_workflow, _get_shopping_workflow)
    return server


def create_server_for_database(database_url: str) -> MCPServer:
    """Construct the production adapter graph for an already migrated SQLite database."""

    engine = create_sqlite_engine(database_url)
    repository = SQLiteWorkflowRepository(create_session_factory(engine))
    return create_mcp_server(ShoppingWorkflowService(repository))


def main() -> None:  # pragma: no cover - blocking stdio transport entry point
    """Run the MCP server over stdio for a local MCP-compatible host."""

    database_url = os.environ.get(DATABASE_URL_ENV, DEFAULT_DATABASE_URL)
    create_server_for_database(database_url).run(transport="stdio")
