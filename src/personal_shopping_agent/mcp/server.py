"""Official MCP Python SDK v2 adapter for the shopping workflow service."""

from collections.abc import Mapping
from uuid import UUID

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from sqlalchemy.orm import Session, sessionmaker

from personal_shopping_agent.__about__ import __version__
from personal_shopping_agent.application import (
    OfficialEvidenceProvider,
    PageCollector,
    RenderedShoppingReport,
    RenderedShoppingReportPresentation,
    ShoppingDecisionPipelineService,
    ShoppingPipelineResult,
    ShoppingReportAccessService,
    ShoppingReportResult,
    ShoppingReportService,
    ShoppingWorkflowService,
    WorkflowSnapshot,
)
from personal_shopping_agent.llm import (
    ExplanationProviderKind,
    LLMExplanationSettings,
    create_report_presentation_service,
    load_llm_explanation_settings,
)
from personal_shopping_agent.mcp.schemas import (
    AgentCapabilities,
    RunShoppingPipelineInput,
    StartShoppingWorkflowInput,
)
from personal_shopping_agent.rendering import HtmlShoppingReportRenderer
from personal_shopping_agent.runtime import create_jd_pipeline_service
from personal_shopping_agent.runtime_settings import database_url_from_environment
from personal_shopping_agent.storage import (
    SQLiteShoppingReportRepository,
    SQLiteShoppingReportUnitOfWork,
    create_session_factory,
    create_sqlite_engine,
    require_current_database,
)
from personal_shopping_agent.storage.workflow_repository import SQLiteWorkflowRepository

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
PIPELINE_WRITE = ToolAnnotations(
    read_only_hint=False,
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
    pipeline_service: ShoppingDecisionPipelineService | None = None,
) -> MCPServer:
    """Register a deterministic, dependency-injected MCP server."""

    server = MCPServer(
        name="personal-shopping-agent",
        title="Personal Shopping Agent",
        version=__version__,
        description="Evidence-driven personal shopping workflow service.",
        instructions=(
            "Create and inspect local shopping workflows. Deterministic reports can be rendered "
            "only after scoring has completed. Reading a report never calls an LLM; use the "
            "separate explanation tool to explicitly request an optional model-generated overlay. "
            "An end-to-end JD pipeline tool is present only in an explicitly configured server. "
            "Checkout and payment are never available."
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
            milestone="M6" if pipeline_service is not None else "M5",
            local_workflows=True,
            local_storage=True,
            end_to_end_pipeline=pipeline_service is not None,
            platform_collection=pipeline_service is not None,
            cross_platform_comparison=False,
            ranking=pipeline_service is not None,
            deterministic_reports=True,
            html_reports=True,
            llm_explanations=explanation_provider is not ExplanationProviderKind.DISABLED,
            llm_explanation_provider=explanation_provider.value,
            automatic_purchase=False,
            message=(
                "M6 JD pipeline is explicitly configured and resumable."
                if pipeline_service is not None
                else "M5 report tools are available; the end-to-end pipeline is not configured."
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

    if pipeline_service is not None:
        configured_pipeline = pipeline_service

        @server.tool(
            name="run_shopping_pipeline",
            title="Run or resume the configured shopping decision pipeline",
            annotations=PIPELINE_WRITE,
            structured_output=True,
        )
        async def _run_shopping_pipeline(
            request: RunShoppingPipelineInput,
        ) -> ShoppingPipelineResult:
            """Execute only real remaining stages and stop at the last committed stage on error."""

            return await configured_pipeline.run(
                request.workflow_id,
                options=request.to_options(),
            )

        _ = _run_shopping_pipeline

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


def _create_server_for_session_factory(
    session_factory: sessionmaker[Session],
    settings: LLMExplanationSettings,
    *,
    pipeline_service: ShoppingDecisionPipelineService | None = None,
) -> MCPServer:
    return create_mcp_server(
        ShoppingWorkflowService(SQLiteWorkflowRepository(session_factory)),
        ShoppingReportService(lambda: SQLiteShoppingReportUnitOfWork(session_factory)),
        ShoppingReportAccessService(
            SQLiteShoppingReportRepository(session_factory),
            create_report_presentation_service(settings),
            HtmlShoppingReportRenderer(),
        ),
        explanation_provider=settings.provider,
        pipeline_service=pipeline_service,
    )


def create_server_for_database(
    database_url: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> MCPServer:
    """Construct the production adapter graph for an already migrated SQLite database."""

    engine = create_sqlite_engine(database_url)
    session_factory = create_session_factory(engine)
    settings = load_llm_explanation_settings(environment)
    return _create_server_for_session_factory(session_factory, settings)


def create_jd_pipeline_server_for_database(
    database_url: str,
    page_collector: PageCollector,
    official_evidence_provider: OfficialEvidenceProvider,
    *,
    environment: Mapping[str, str] | None = None,
) -> MCPServer:
    """Compose an explicitly enabled JD pipeline around caller-owned safe external ports."""

    engine = create_sqlite_engine(database_url)
    session_factory = create_session_factory(engine)
    settings = load_llm_explanation_settings(environment)
    pipeline_service = create_jd_pipeline_service(
        session_factory,
        page_collector,
        official_evidence_provider,
    )
    return _create_server_for_session_factory(
        session_factory,
        settings,
        pipeline_service=pipeline_service,
    )


def create_default_server(
    environment: Mapping[str, str] | None = None,
) -> MCPServer:
    """Construct the default server only after its packaged schema is current."""

    database_url = database_url_from_environment(environment)
    require_current_database(database_url)
    return create_server_for_database(database_url, environment=environment)


def main() -> None:  # pragma: no cover - blocking stdio transport entry point
    """Run the MCP server over stdio for a local MCP-compatible host."""

    create_default_server().run(transport="stdio")
