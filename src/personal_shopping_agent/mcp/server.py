"""Official MCP Python SDK v2 adapter for the shopping workflow service."""

import os
from collections.abc import Mapping
from uuid import UUID

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from personal_shopping_agent.application import (
    RenderedShoppingReport,
    RenderedShoppingReportPresentation,
    ShoppingReportAccessService,
    ShoppingReportResult,
    ShoppingReportService,
    ShoppingWorkflowService,
    WorkflowSnapshot,
)
from personal_shopping_agent.llm import (
    ExplanationProviderKind,
    create_report_presentation_service,
    load_llm_explanation_settings,
)
from personal_shopping_agent.mcp.schemas import AgentCapabilities, StartShoppingWorkflowInput
from personal_shopping_agent.rendering import HtmlShoppingReportRenderer
from personal_shopping_agent.storage import (
    SQLiteShoppingReportRepository,
    SQLiteShoppingReportUnitOfWork,
    create_session_factory,
    create_sqlite_engine,
)
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
EXTERNAL_READ = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)


def create_mcp_server(
    workflow_service: ShoppingWorkflowService,
    report_service: ShoppingReportService,
    report_access_service: ShoppingReportAccessService,
    *,
    explanation_provider: ExplanationProviderKind = ExplanationProviderKind.DISABLED,
) -> MCPServer:
    """Register a deterministic, dependency-injected MCP server."""

    server = MCPServer(
        name="personal-shopping-agent",
        title="Personal Shopping Agent",
        version="0.1.0",
        description="Evidence-driven personal shopping workflow service.",
        instructions=(
            "Create and inspect local shopping workflows. Deterministic reports can be rendered "
            "only after scoring has completed. Reading a report never calls an LLM; use the "
            "separate explanation tool to explicitly request an optional model-generated overlay. "
            "Platform collection, scoring, checkout, and payment tools are not exposed."
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
            milestone="M5",
            local_workflows=True,
            local_storage=True,
            platform_collection=False,
            cross_platform_comparison=False,
            ranking=False,
            deterministic_reports=True,
            html_reports=True,
            llm_explanations=explanation_provider is not ExplanationProviderKind.DISABLED,
            llm_explanation_provider=explanation_provider.value,
            automatic_purchase=False,
            message=(
                "M5 exposes deterministic reports for already-scored workflows; platform "
                "collection and scoring MCP tools remain unavailable."
            ),
        )

    @server.tool(
        name="start_shopping_workflow",
        title="Start a local shopping workflow",
        annotations=LOCAL_WRITE,
        structured_output=True,
    )
    def _start_shopping_workflow(request: StartShoppingWorkflowInput) -> WorkflowSnapshot:
        """Validate a structured shopping need and create its local auditable workflow."""

        return workflow_service.start(request.to_domain())

    @server.tool(
        name="get_shopping_workflow",
        title="Get shopping workflow status",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def _get_shopping_workflow(workflow_id: UUID) -> WorkflowSnapshot:
        """Read the validated request, current state, and ordered transition history."""

        return workflow_service.get(workflow_id)

    @server.tool(
        name="render_shopping_report",
        title="Render a deterministic shopping report",
        annotations=LOCAL_WRITE,
        structured_output=True,
    )
    def _render_shopping_report(workflow_id: UUID) -> ShoppingReportResult:
        """Persist a deterministic report and its workflow event for a scored workflow."""

        return report_service.render(workflow_id)

    @server.tool(
        name="get_shopping_report",
        title="Get the stored deterministic shopping report",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def _get_shopping_report(workflow_id: UUID) -> RenderedShoppingReport:
        """Read the stored report without invoking an LLM or changing local state."""

        return report_access_service.get(workflow_id)

    @server.tool(
        name="get_shopping_report_html",
        title="Get a safe standalone HTML shopping report",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def _get_shopping_report_html(workflow_id: UUID) -> RenderedShoppingReport:
        """Derive script-free HTML from the validated deterministic report snapshot."""

        return report_access_service.get_html(workflow_id)

    @server.tool(
        name="explain_shopping_report",
        title="Explicitly request an optional AI report explanation",
        annotations=EXTERNAL_READ,
        structured_output=True,
    )
    def _explain_shopping_report(workflow_id: UUID) -> RenderedShoppingReportPresentation:
        """Return an ephemeral overlay or the byte-identical deterministic fallback."""

        return report_access_service.explain(workflow_id)

    _ = (
        _shopping_agent_status,
        _start_shopping_workflow,
        _get_shopping_workflow,
        _render_shopping_report,
        _get_shopping_report,
        _get_shopping_report_html,
        _explain_shopping_report,
    )
    return server


def create_server_for_database(
    database_url: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> MCPServer:
    """Construct the production adapter graph for an already migrated SQLite database."""

    engine = create_sqlite_engine(database_url)
    session_factory = create_session_factory(engine)
    settings = load_llm_explanation_settings(environment)
    return create_mcp_server(
        ShoppingWorkflowService(SQLiteWorkflowRepository(session_factory)),
        ShoppingReportService(lambda: SQLiteShoppingReportUnitOfWork(session_factory)),
        ShoppingReportAccessService(
            SQLiteShoppingReportRepository(session_factory),
            create_report_presentation_service(settings),
            HtmlShoppingReportRenderer(),
        ),
        explanation_provider=settings.provider,
    )


def main() -> None:  # pragma: no cover - blocking stdio transport entry point
    """Run the MCP server over stdio for a local MCP-compatible host."""

    database_url = os.environ.get(DATABASE_URL_ENV, DEFAULT_DATABASE_URL)
    create_server_for_database(database_url).run(transport="stdio")
