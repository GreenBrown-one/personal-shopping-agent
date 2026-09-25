"""Official MCP Python SDK v2 adapter for the shopping workflow service."""

from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from sqlalchemy.orm import Session, sessionmaker

from personal_shopping_agent.__about__ import __version__
from personal_shopping_agent.automation import (
    ShoppingDecisionPipelineService,
    ShoppingWorkflowService,
)
from personal_shopping_agent.domain import (
    WorkflowSnapshot,
)
from personal_shopping_agent.infrastructure.benchmark_store import (
    BenchmarkStoreError,
    LocalJsonBenchmarkStore,
)
from personal_shopping_agent.infrastructure.settings import (
    LiveJDConfigurationError,
    benchmark_file_from_environment,
    database_url_from_environment,
    inbox_directory_from_environment,
    load_live_jd_settings,
)
from personal_shopping_agent.infrastructure.storage import (
    SQLiteShoppingReportRepository,
    SQLiteShoppingReportUnitOfWork,
    create_session_factory,
    create_sqlite_engine,
    require_current_database,
)
from personal_shopping_agent.infrastructure.storage.workflow_repository import (
    SQLiteWorkflowRepository,
)
from personal_shopping_agent.intake import (
    SHOPPING_REQUEST_GUIDE,
    RequirementReview,
    RequirementReviewer,
)
from personal_shopping_agent.interfaces.composition import (
    NoOfficialEvidenceProvider,
    create_jd_collection_policy,
    create_jd_pipeline_service,
)
from personal_shopping_agent.interfaces.mcp.schemas import (
    AgentCapabilities,
    PipelineRunView,
    ReportExplanationView,
    ReportRenderView,
    RunShoppingPipelineInput,
    ShoppingReportView,
    StartShoppingWorkflowInput,
)
from personal_shopping_agent.presentation import (
    ShoppingReportAccessService,
    ShoppingReportService,
)
from personal_shopping_agent.presentation.llm import (
    ExplanationProviderKind,
    LLMExplanationSettings,
    create_report_presentation_service,
    load_llm_explanation_settings,
)
from personal_shopping_agent.presentation.rendering import HtmlShoppingReportRenderer
from personal_shopping_agent.sourcing import (
    ChipBenchmarkEvidenceProvider,
    IndependentEvidenceProvider,
    OfficialEvidenceProvider,
    PageCollector,
    SavedPagePlan,
    SavedPagePlanner,
)
from personal_shopping_agent.sourcing.browser import (
    BrowserManager,
    BrowserManagerSettings,
    ControlledPageCollector,
    SavedPageCollector,
    StatusPageCollector,
)
from personal_shopping_agent.sourcing.platforms import JDSearchAdapter
from personal_shopping_agent.sourcing.platforms.jd import jd_page_key

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


class CollectionMode(StrEnum):
    """Where platform pages come from; reported honestly in shopping_agent_status."""

    NONE = "none"
    ASSISTED = "assisted"
    BROWSER = "browser"


_STATUS_MESSAGES = {
    CollectionMode.NONE: "Local workflows and reports are ready; no platform collection is set up.",
    CollectionMode.ASSISTED: (
        "Assisted collection: the user saves JD pages from their own browser into data/inbox. "
        "Call list_pages_to_save, ask the user to save any unsaved URL, then "
        "run_shopping_pipeline. No network access."
    ),
    CollectionMode.BROWSER: (
        "Experimental automated browser collection is configured; JD may block it and real page "
        "compatibility still requires manual acceptance."
    ),
}


class ManagedStatusPageCollector(StatusPageCollector, Protocol):
    """Status-aware collector with a server-owned asynchronous lifecycle."""

    async def start(self) -> None: ...

    async def close(self) -> None: ...


ServerLifespan = Callable[[MCPServer[None]], AbstractAsyncContextManager[None]]


def create_mcp_server(
    workflow_service: ShoppingWorkflowService,
    report_service: ShoppingReportService,
    report_access_service: ShoppingReportAccessService,
    *,
    explanation_provider: ExplanationProviderKind = ExplanationProviderKind.DISABLED,
    pipeline_service: ShoppingDecisionPipelineService | None = None,
    requirement_reviewer: RequirementReviewer | None = None,
    chip_benchmark: bool = False,
    collection_mode: CollectionMode = CollectionMode.NONE,
    page_planner: SavedPagePlanner | None = None,
    lifespan: ServerLifespan | None = None,
) -> MCPServer[None]:
    """Register a deterministic, dependency-injected MCP server."""

    reviewer = requirement_reviewer or RequirementReviewer()

    server = MCPServer(
        name="personal-shopping-agent",
        title="Personal Shopping Agent",
        version=__version__,
        description="Evidence-driven personal shopping workflow service.",
        instructions=(
            "Review a structured shopping request, then create and inspect local shopping "
            "workflows; requests with blocking issues are rejected. Deterministic reports can be "
            "rendered only after scoring has completed. Report and pipeline tools return compact "
            "views whose content field is the complete user-facing report: show that text rather "
            "than re-deriving facts. Reading a report never calls an LLM; use the separate "
            "explanation tool to explicitly request an optional model-generated overlay. An "
            "end-to-end JD pipeline tool is present only in an explicitly configured server. "
            "Checkout and payment are never available."
        ),
        lifespan=lifespan,
    )

    @server.prompt(
        name="prepare_shopping_request",
        title="Prepare a structured shopping request",
        description="Clarify natural-language needs before starting a typed local workflow.",
    )
    def _prepare_shopping_request() -> str:
        """Return static guidance without tools, storage, models, or network access."""

        return SHOPPING_REQUEST_GUIDE

    @server.tool(
        name="shopping_agent_status",
        title="Shopping agent capability status",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def _shopping_agent_status() -> AgentCapabilities:
        """Return an honest summary of implemented and unavailable capabilities."""

        return AgentCapabilities(
            milestone="M9",
            requirement_review=True,
            local_workflows=True,
            local_storage=True,
            end_to_end_pipeline=pipeline_service is not None,
            platform_collection=pipeline_service is not None,
            cross_platform_comparison=False,
            ranking=pipeline_service is not None,
            chip_benchmark=chip_benchmark,
            collection_mode=collection_mode.value,
            deterministic_reports=True,
            html_reports=True,
            llm_explanations=explanation_provider is not ExplanationProviderKind.DISABLED,
            llm_explanation_provider=explanation_provider.value,
            automatic_purchase=False,
            message=_STATUS_MESSAGES[collection_mode],
        )

    @server.tool(
        name="review_shopping_request",
        title="Review a structured shopping request before starting",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def _review_shopping_request(request: StartShoppingWorkflowInput) -> RequirementReview:
        """List blocking gaps and warnings without storing, guessing, or accessing the network."""

        return reviewer.review(request.to_domain())

    @server.tool(
        name="start_shopping_workflow",
        title="Start a local shopping workflow",
        annotations=LOCAL_WRITE,
        structured_output=True,
    )
    def _start_shopping_workflow(request: StartShoppingWorkflowInput) -> WorkflowSnapshot:
        """Validate a structured shopping need and create its local auditable workflow."""

        shopping_request = request.to_domain()
        reviewer.require_ready(shopping_request)
        return workflow_service.start(shopping_request)

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
    def _render_shopping_report(workflow_id: UUID) -> ReportRenderView:
        """Persist a deterministic report and its workflow event for a scored workflow."""

        result = report_service.render(workflow_id)
        return ReportRenderView(
            workflow_state=result.snapshot.workflow.state,
            report=ShoppingReportView.from_rendered(result.rendered),
        )

    @server.tool(
        name="get_shopping_report",
        title="Get the stored deterministic shopping report",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def _get_shopping_report(workflow_id: UUID) -> ShoppingReportView:
        """Read the stored Markdown report without invoking an LLM or changing local state."""

        return ShoppingReportView.from_rendered(report_access_service.get(workflow_id))

    @server.tool(
        name="get_shopping_report_html",
        title="Get a safe standalone HTML shopping report",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def _get_shopping_report_html(workflow_id: UUID) -> ShoppingReportView:
        """Derive script-free HTML from the validated deterministic report snapshot."""

        return ShoppingReportView.from_rendered(report_access_service.get_html(workflow_id))

    @server.tool(
        name="explain_shopping_report",
        title="Explicitly request an optional AI report explanation",
        annotations=EXTERNAL_READ,
        structured_output=True,
    )
    def _explain_shopping_report(workflow_id: UUID) -> ReportExplanationView:
        """Return an ephemeral overlay or the byte-identical deterministic fallback."""

        return ReportExplanationView.from_presentation(report_access_service.explain(workflow_id))

    if pipeline_service is not None:
        configured_pipeline = pipeline_service

        @server.tool(
            name="run_shopping_pipeline",
            title="Run or resume the configured shopping decision pipeline",
            annotations=(
                PIPELINE_WRITE if collection_mode is CollectionMode.BROWSER else LOCAL_WRITE
            ),
            structured_output=True,
        )
        async def _run_shopping_pipeline(
            request: RunShoppingPipelineInput,
        ) -> PipelineRunView:
            """Execute only real remaining stages and return the final report text."""

            result = await configured_pipeline.run(
                request.workflow_id,
                options=request.to_options(),
            )
            return PipelineRunView.from_result(result)

        _ = _run_shopping_pipeline

    if page_planner is not None:
        planner = page_planner

        @server.tool(
            name="list_pages_to_save",
            title="List the JD pages the user should save for the assisted pipeline",
            annotations=READ_ONLY,
            structured_output=True,
        )
        async def _list_pages_to_save(request: RunShoppingPipelineInput) -> SavedPagePlan:
            """Show the search and product pages the pipeline will read and which are saved."""

            snapshot = workflow_service.get(request.workflow_id)
            return await planner.plan(
                snapshot.request.query,
                maximum_candidates=request.maximum_candidates,
                maximum_details=request.maximum_details,
            )

        _ = _list_pages_to_save

    _ = (
        _prepare_shopping_request,
        _shopping_agent_status,
        _review_shopping_request,
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
    chip_benchmark: bool = False,
    collection_mode: CollectionMode = CollectionMode.NONE,
    page_planner: SavedPagePlanner | None = None,
    lifespan: ServerLifespan | None = None,
) -> MCPServer[None]:
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
        chip_benchmark=chip_benchmark,
        collection_mode=collection_mode,
        page_planner=page_planner,
        lifespan=lifespan,
    )


def create_server_for_database(
    database_url: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> MCPServer[None]:
    """Construct the default graph: local workflows plus assisted collection from saved pages."""

    engine = create_sqlite_engine(database_url)
    session_factory = create_session_factory(engine)
    settings = load_llm_explanation_settings(environment)
    inbox = Path(inbox_directory_from_environment(environment))
    collector = SavedPageCollector(inbox, jd_page_key)
    independent_providers = load_chip_benchmark_providers(environment)
    return _create_server_for_session_factory(
        session_factory,
        settings,
        pipeline_service=create_jd_pipeline_service(
            session_factory,
            collector,
            NoOfficialEvidenceProvider(),
            independent_providers=independent_providers,
        ),
        chip_benchmark=bool(independent_providers),
        collection_mode=CollectionMode.ASSISTED,
        page_planner=SavedPagePlanner(collector, JDSearchAdapter()),
    )


def create_jd_pipeline_server_for_database(
    database_url: str,
    page_collector: PageCollector,
    official_evidence_provider: OfficialEvidenceProvider,
    *,
    environment: Mapping[str, str] | None = None,
    lifespan: ServerLifespan | None = None,
    independent_providers: tuple[IndependentEvidenceProvider, ...] = (),
) -> MCPServer[None]:
    """Compose an explicitly enabled JD pipeline around caller-owned safe external ports."""

    engine = create_sqlite_engine(database_url)
    session_factory = create_session_factory(engine)
    settings = load_llm_explanation_settings(environment)
    pipeline_service = create_jd_pipeline_service(
        session_factory,
        page_collector,
        official_evidence_provider,
        independent_providers=independent_providers,
    )
    return _create_server_for_session_factory(
        session_factory,
        settings,
        pipeline_service=pipeline_service,
        chip_benchmark=bool(independent_providers),
        collection_mode=CollectionMode.BROWSER,
        lifespan=lifespan,
    )


def load_chip_benchmark_providers(
    environment: Mapping[str, str] | None = None,
) -> tuple[IndependentEvidenceProvider, ...]:
    """Enable chip evidence only from a present, private, valid local reference."""

    store = LocalJsonBenchmarkStore(Path(benchmark_file_from_environment(environment)))
    try:
        reference = store.load()
    except BenchmarkStoreError:
        return ()
    return () if reference is None else (ChipBenchmarkEvidenceProvider(reference),)


def _collector_lifespan(collector: ManagedStatusPageCollector) -> ServerLifespan:
    @asynccontextmanager
    async def lifespan(_: MCPServer[None]) -> AsyncGenerator[None, None]:
        await collector.start()
        try:
            yield None
        finally:
            await collector.close()

    return lifespan


def create_live_jd_server_for_database(
    database_url: str,
    *,
    environment: Mapping[str, str] | None = None,
    managed_collector: ManagedStatusPageCollector | None = None,
) -> MCPServer[None]:
    """Construct the explicitly enabled JD server with one managed browser lifecycle."""

    settings = load_live_jd_settings(environment)
    if not settings.enabled:
        raise LiveJDConfigurationError(
            "live_jd_disabled",
            "Live JD access must be explicitly enabled.",
        )
    collector = managed_collector
    if collector is None:
        collector = BrowserManager(
            create_jd_collection_policy(),
            settings=BrowserManagerSettings(headless=settings.headless),
        )
    bounded_collector = ControlledPageCollector(collector)
    return create_jd_pipeline_server_for_database(
        database_url,
        bounded_collector,
        NoOfficialEvidenceProvider(),
        environment=environment,
        lifespan=_collector_lifespan(collector),
        independent_providers=load_chip_benchmark_providers(environment),
    )


def create_default_server(
    environment: Mapping[str, str] | None = None,
) -> MCPServer[None]:
    """Construct the default server only after its packaged schema is current."""

    database_url = database_url_from_environment(environment)
    require_current_database(database_url)
    return create_server_for_database(database_url, environment=environment)


def create_live_jd_server(
    environment: Mapping[str, str] | None = None,
    *,
    managed_collector: ManagedStatusPageCollector | None = None,
) -> MCPServer[None]:
    """Construct an explicitly enabled live-JD server after the same schema gate."""

    database_url = database_url_from_environment(environment)
    require_current_database(database_url)
    return create_live_jd_server_for_database(
        database_url,
        environment=environment,
        managed_collector=managed_collector,
    )


def main() -> None:  # pragma: no cover - blocking stdio transport entry point
    """Run the MCP server over stdio for a local MCP-compatible host."""

    create_default_server().run(transport="stdio")


def main_jd() -> None:  # pragma: no cover - blocking stdio transport entry point
    """Run the explicitly enabled, lifecycle-managed JD MCP server over stdio."""

    create_live_jd_server().run(transport="stdio")
