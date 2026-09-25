"""In-memory protocol tests for the official MCP Python SDK v2 adapter."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from mcp import Client
from mcp.server import MCPServer
from mcp.types import TextContent
from pydantic import HttpUrl
from sqlalchemy import Engine

from personal_shopping_agent import __version__
from personal_shopping_agent.automation import (
    ShoppingWorkflowService,
)
from personal_shopping_agent.domain import (
    Budget,
    Evidence,
    EvidenceSourceType,
    EvidenceSubjectType,
    Money,
    Offer,
    PriceBreakdown,
    PriceKind,
    Product,
    ShoppingCriterion,
    ShoppingRequest,
    StoreType,
    WorkflowSnapshot,
    WorkflowState,
)
from personal_shopping_agent.infrastructure.storage import (
    SQLiteShoppingReportRepository,
    SQLiteShoppingReportUnitOfWork,
    SQLiteShoppingRepository,
    SQLiteWorkflowRepository,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from personal_shopping_agent.infrastructure.storage.scoring_unit_of_work import (
    candidate_score_record,
)
from personal_shopping_agent.intake import RequirementIssueCode, RequirementReview
from personal_shopping_agent.interfaces.mcp.schemas import (
    ReportExplanationView,
    ReportRenderView,
    ShoppingReportView,
)
from personal_shopping_agent.interfaces.mcp.server import (
    create_mcp_server,
    create_server_for_database,
)
from personal_shopping_agent.presentation import (
    CandidateDecisionInput,
    CandidateEvidenceConfidence,
    CandidateRankingEngine,
    CandidateScoringFoundation,
    CriterionEvaluation,
    CriterionEvaluationStatus,
    CriterionEvidenceAssessment,
    ExplanationStatus,
    OfferCostAssessment,
    ReportFormat,
    ShoppingReportAccessService,
    ShoppingReportService,
)
from personal_shopping_agent.presentation.llm import (
    ExplanationProviderKind,
    LLMExplanationSettings,
    create_report_presentation_service,
)
from personal_shopping_agent.presentation.rendering import HtmlShoppingReportRenderer

NOW = datetime(2026, 8, 9, 20, 0, tzinfo=UTC)
REPORT_AT = NOW + timedelta(seconds=1)


def create_test_server(
    engine: Engine,
    *,
    explanation_provider: ExplanationProviderKind = ExplanationProviderKind.DISABLED,
) -> MCPServer:
    """Compose the same report boundaries as production around one in-memory database."""

    session_factory = create_session_factory(engine)
    return create_mcp_server(
        ShoppingWorkflowService(SQLiteWorkflowRepository(session_factory)),
        ShoppingReportService(
            lambda: SQLiteShoppingReportUnitOfWork(session_factory),
            clock=lambda: REPORT_AT,
        ),
        ShoppingReportAccessService(
            SQLiteShoppingReportRepository(session_factory),
            create_report_presentation_service(LLMExplanationSettings()),
            HtmlShoppingReportRenderer(),
        ),
        explanation_provider=explanation_provider,
    )


def seed_scored_workflow(engine: Engine) -> UUID:
    """Persist one complete scored candidate so MCP report tools can run end to end."""

    session_factory = create_session_factory(engine)
    workflow_service = ShoppingWorkflowService(
        SQLiteWorkflowRepository(session_factory),
        clock=lambda: NOW,
    )
    request = ShoppingRequest(
        query="续航优先的测试手机",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        criteria=(
            ShoppingCriterion(
                key="battery_capacity",
                weight=Decimal("1"),
                minimum=Decimal("4000"),
                preferred=Decimal("6000"),
                unit="mAh",
            ),
        ),
        created_at=NOW,
    )
    snapshot = workflow_service.start(request)
    for state in (
        WorkflowState.CANDIDATES_DISCOVERED,
        WorkflowState.OFFERS_COLLECTED,
        WorkflowState.EVIDENCE_CROSS_CHECKED,
        WorkflowState.DATA_NORMALIZED,
        WorkflowState.CANDIDATES_SCORED,
    ):
        snapshot = workflow_service.advance(
            snapshot.workflow.id,
            state,
            reason=f"seed_{state.value}",
        )

    product = Product(
        brand="Example",
        model="M5",
        category="smartphone",
        canonical_name="Example M5",
    )
    offer = Offer(
        product_id=product.id,
        platform="JD",
        seller="JD self operated",
        store_type=StoreType.PLATFORM_SELF_OPERATED,
        url=HttpUrl("https://item.example.com/m5"),
        region="云南省曲靖市",
        captured_at=NOW,
        price=PriceBreakdown(estimated_total_cost=Money(amount=Decimal("4000"))),
        in_stock=True,
    )
    evidence = Evidence(
        request_id=request.id,
        subject_type=EvidenceSubjectType.PRODUCT,
        subject_id=product.id,
        field_path="specifications.battery_capacity",
        source_type=EvidenceSourceType.MANUFACTURER_OFFICIAL,
        source_url=HttpUrl("https://manufacturer.example.com/m5/specifications"),
        source_title="Example M5 official specifications",
        captured_at=NOW,
        observed_value="5000 mAh",
    )
    repository = SQLiteShoppingRepository(session_factory)
    repository.add_product(product)
    repository.add_offer(offer)
    repository.add_evidence(evidence)

    evaluation = CriterionEvaluation(
        key="battery_capacity",
        weight=Decimal("1"),
        hard_requirement=False,
        observed_values=(Decimal("5000"),),
        status=CriterionEvaluationStatus.SATISFIED,
        score=Decimal("0.8"),
        reason_code="test",
    )
    price = Money(amount=Decimal("4000"))
    foundation = CandidateScoringFoundation(
        request_id=request.id,
        product_id=product.id,
        criterion_evaluations=(evaluation,),
        utility=Decimal("0.8"),
        hard_requirements_met=True,
        offer_costs=(
            OfferCostAssessment(
                offer_id=offer.id,
                product_id=product.id,
                selected_price_kind=PriceKind.ESTIMATED_TOTAL,
                selected_price=price,
                risk_coefficient=Decimal("0"),
                effective_cost=price,
                comparable=True,
                reason_codes=("test",),
            ),
        ),
        best_offer_id=offer.id,
    )
    confidence = CandidateEvidenceConfidence(
        request_id=request.id,
        product_id=product.id,
        criteria=(
            CriterionEvidenceAssessment(
                key="battery_capacity",
                weight=Decimal("1"),
                complete=True,
                source_reliability=Decimal("0.9"),
                freshness=Decimal("1"),
                consistency=Decimal("1"),
                confidence=Decimal("0.9"),
                evidence_ids=(evidence.id,),
                reason_codes=("test",),
            ),
        ),
        completeness=Decimal("1"),
        source_reliability=Decimal("0.9"),
        freshness=Decimal("1"),
        consistency=Decimal("1"),
        evidence_confidence=Decimal("0.9"),
        base_utility=Decimal("0.8"),
        adjusted_utility=Decimal("0.72"),
        assessed_at=NOW,
    )
    batch = CandidateRankingEngine().rank(
        request=request,
        workflow_id=snapshot.workflow.id,
        candidates=(CandidateDecisionInput(foundation=foundation, confidence=confidence),),
        scored_at=NOW,
    )
    with session_scope(session_factory) as session:
        session.add(candidate_score_record(batch.scores[0]))
    return snapshot.workflow.id


def test_mcp_tools_are_discoverable_and_round_trip_structured_workflows() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    server = create_test_server(engine)
    assert server.version == __version__

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            prompts = await client.list_prompts()
            assert [prompt.name for prompt in prompts.prompts] == ["prepare_shopping_request"]
            prepared = await client.get_prompt("prepare_shopping_request")
            assert len(prepared.messages) == 1
            prompt_content = prepared.messages[0].content
            assert isinstance(prompt_content, TextContent)
            assert "shopping_agent_status" in prompt_content.text
            assert "start_shopping_workflow" in prompt_content.text
            assert "review_shopping_request" in prompt_content.text
            assert "Never claim" in prompt_content.text

            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == [
                "shopping_agent_status",
                "review_shopping_request",
                "start_shopping_workflow",
                "get_shopping_workflow",
                "render_shopping_report",
                "get_shopping_report",
                "get_shopping_report_html",
                "explain_shopping_report",
            ]
            assert listed.tools[0].annotations is not None
            assert listed.tools[0].annotations.read_only_hint is True
            assert listed.tools[1].annotations is not None
            assert listed.tools[1].annotations.read_only_hint is True
            assert listed.tools[1].annotations.idempotent_hint is True
            assert listed.tools[1].annotations.open_world_hint is False
            assert listed.tools[2].annotations is not None
            assert listed.tools[2].annotations.read_only_hint is False
            assert listed.tools[2].annotations.destructive_hint is False
            assert listed.tools[4].annotations is not None
            assert listed.tools[4].annotations.read_only_hint is False
            assert listed.tools[5].annotations is not None
            assert listed.tools[5].annotations.read_only_hint is True
            assert listed.tools[5].annotations.idempotent_hint is True
            assert listed.tools[5].annotations.open_world_hint is False
            assert listed.tools[6].annotations is not None
            assert listed.tools[6].annotations.read_only_hint is True
            assert listed.tools[6].annotations.idempotent_hint is True
            assert listed.tools[6].annotations.open_world_hint is False
            assert listed.tools[7].annotations is not None
            assert listed.tools[7].annotations.read_only_hint is True
            assert listed.tools[7].annotations.idempotent_hint is False
            assert listed.tools[7].annotations.open_world_hint is True

            status = await client.call_tool("shopping_agent_status", {})
            assert status.structured_content is not None
            assert status.structured_content["milestone"] == "M8"
            assert status.structured_content["requirement_review"] is True
            assert status.structured_content["end_to_end_pipeline"] is False
            assert status.structured_content["platform_collection"] is False
            assert status.structured_content["ranking"] is False
            assert status.structured_content["cross_platform_comparison"] is False
            assert status.structured_content["deterministic_reports"] is True
            assert status.structured_content["html_reports"] is True
            assert status.structured_content["llm_explanations"] is False
            assert status.structured_content["llm_explanation_provider"] == "disabled"
            assert status.structured_content["automatic_purchase"] is False
            assert status.structured_content["message"] == (
                "M8 local MCP is ready; live platform collection is not configured."
            )

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


def test_mcp_report_tools_render_read_and_explicitly_fallback_without_llm() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    workflow_id = seed_scored_workflow(engine)
    server = create_test_server(engine)

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            rendered_result = await client.call_tool(
                "render_shopping_report",
                {"workflow_id": str(workflow_id)},
            )
            assert rendered_result.structured_content is not None
            committed = ReportRenderView.model_validate(rendered_result.structured_content)
            assert committed.workflow_state is WorkflowState.REPORT_RENDERED
            assert committed.report.workflow_id == workflow_id
            assert committed.report.candidates == 1
            assert committed.report.eligible_candidates == 1
            assert committed.report.format is ReportFormat.MARKDOWN

            stored_result = await client.call_tool(
                "get_shopping_report",
                {"workflow_id": str(workflow_id)},
            )
            assert stored_result.structured_content is not None
            stored = ShoppingReportView.model_validate(stored_result.structured_content)
            assert stored == committed.report
            assert (
                hashlib.sha256(stored.content.encode("utf-8")).hexdigest() == stored.content_sha256
            )

            html_result = await client.call_tool(
                "get_shopping_report_html",
                {"workflow_id": str(workflow_id)},
            )
            assert html_result.structured_content is not None
            html = ShoppingReportView.model_validate(html_result.structured_content)
            assert html.report_id == stored.report_id
            assert html.format is ReportFormat.HTML
            assert html.content.startswith("<!doctype html>")
            assert "Content-Security-Policy" in html.content

            explained_result = await client.call_tool(
                "explain_shopping_report",
                {"workflow_id": str(workflow_id)},
            )
            assert explained_result.structured_content is not None
            presentation = ReportExplanationView.model_validate(explained_result.structured_content)
            assert presentation.status is ExplanationStatus.DETERMINISTIC_FALLBACK
            assert presentation.provider_name is None
            assert presentation.content == stored.content
            assert presentation.content_sha256 == stored.content_sha256

    asyncio.run(scenario())
    engine.dispose()


def test_mcp_status_reports_an_explicitly_enabled_explanation_provider() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    server = create_test_server(engine, explanation_provider=ExplanationProviderKind.OPENAI)

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            result = await client.call_tool("shopping_agent_status", {})
            assert result.structured_content is not None
            assert result.structured_content["llm_explanations"] is True
            assert result.structured_content["llm_explanation_provider"] == "openai"

    asyncio.run(scenario())
    engine.dispose()


def test_database_server_factory_uses_existing_migrated_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "server.db"
    database_url = f"sqlite:///{database_path}"
    setup_engine = create_sqlite_engine(database_url)
    create_schema(setup_engine)
    setup_engine.dispose()
    server = create_server_for_database(database_url, environment={})

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            result = await client.call_tool("shopping_agent_status", {})
            assert result.structured_content is not None
            assert result.structured_content["local_storage"] is True

    asyncio.run(scenario())


def test_mcp_reviews_requests_and_rejects_blocking_gaps_before_storage() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    server = create_test_server(engine)
    draft = {
        "query": "8GB 内存以上的手机",
        "category": "smartphone",
        "budget_maximum": "3000",
        "criteria": [
            {"key": "内存", "hard_requirement": True, "minimum": "8", "unit": "GB"},
            {"key": "battery_capacity", "preferred": "5000", "unit": "mAh"},
        ],
    }

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            reviewed = await client.call_tool("review_shopping_request", {"request": draft})
            assert reviewed.structured_content is not None
            review = RequirementReview.model_validate(reviewed.structured_content)
            assert review.ready_to_start is False
            assert [(item.criterion_key, item.code) for item in review.issues] == [
                ("内存", RequirementIssueCode.CRITERION_KEY_ALIAS),
                ("battery_capacity", RequirementIssueCode.PREFERRED_DIRECTION_UNDEFINED),
                (None, RequirementIssueCode.REGION_MISSING),
            ]
            assert "memory_capacity" in review.issues[0].suggestion

            rejected = await client.call_tool("start_shopping_workflow", {"request": draft})
            assert rejected.is_error is True
            assert isinstance(rejected.content[0], TextContent)
            assert "criterion_key_alias" in rejected.content[0].text
            assert "preferred_direction_undefined" in rejected.content[0].text

    asyncio.run(scenario())
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM shopping_requests").scalar() == 0
    engine.dispose()
