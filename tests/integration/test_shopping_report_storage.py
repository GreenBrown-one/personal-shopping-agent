"""Integration tests for atomic deterministic shopping report persistence."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Self
from uuid import uuid4

import pytest
from pydantic import HttpUrl
from sqlalchemy import Engine, event, func, select
from sqlalchemy.exc import IntegrityError

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
from personal_shopping_agent.infrastructure.storage.reporting_unit_of_work import (
    shopping_report_record,
)
from personal_shopping_agent.infrastructure.storage.scoring_unit_of_work import (
    candidate_score_record,
)
from personal_shopping_agent.infrastructure.storage.tables import (
    CandidateScoreRecord,
    ShoppingReportRecord,
)
from personal_shopping_agent.presentation import (
    CandidateDecisionInput,
    CandidateEvidenceConfidence,
    CandidateRankingEngine,
    CandidateScoringFoundation,
    CriterionEvaluation,
    CriterionEvaluationStatus,
    CriterionEvidenceAssessment,
    OfferCostAssessment,
    RenderedShoppingReport,
    ShoppingReportNotFoundError,
    ShoppingReportService,
    StoredShoppingReportIntegrityError,
)

NOW = datetime(2026, 8, 10, 3, 0, tzinfo=UTC)
RENDERED_AT = NOW + timedelta(seconds=1)


def seed_scored(
    engine: Engine,
) -> tuple[SQLiteWorkflowRepository, Product, ShoppingRequest, WorkflowSnapshot]:
    factory = create_session_factory(engine)
    workflows = SQLiteWorkflowRepository(factory)
    workflow_service = ShoppingWorkflowService(workflows, clock=lambda: NOW)
    request = ShoppingRequest(
        query="example phone",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("100"))),
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
        model="A1",
        category="smartphone",
        canonical_name="Example A1",
    )
    offer = Offer(
        product_id=product.id,
        platform="JD",
        seller="JD self operated",
        store_type=StoreType.PLATFORM_SELF_OPERATED,
        url=HttpUrl("https://item.example.com/1"),
        region="云南省曲靖市",
        captured_at=NOW,
        price=PriceBreakdown(estimated_total_cost=Money(amount=Decimal("80"))),
        in_stock=True,
    )
    evidence = Evidence(
        request_id=request.id,
        subject_type=EvidenceSubjectType.PRODUCT,
        subject_id=product.id,
        field_path="specifications.battery_capacity",
        source_type=EvidenceSourceType.MANUFACTURER_OFFICIAL,
        source_url=HttpUrl("https://manufacturer.example.com/a1/specifications"),
        source_title="Example A1 official specifications",
        captured_at=NOW,
        observed_value="5000 mAh",
    )
    repository = SQLiteShoppingRepository(factory)
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
    price = Money(amount=Decimal("80"))
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
    with session_scope(factory) as session:
        session.add(candidate_score_record(batch.scores[0]))
    return workflows, product, request, snapshot


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


def report_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(ShoppingReportRecord)) or 0


def test_report_uses_one_connection_and_commits_content_and_state() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    workflows, product, request, scored = seed_scored(engine)
    factory = create_session_factory(engine)
    lifecycle, remove_listener = connection_events(engine)
    service = ShoppingReportService(
        lambda: SQLiteShoppingReportUnitOfWork(factory),
        clock=lambda: RENDERED_AT,
    )

    result = service.render(scored.workflow.id)
    remove_listener()

    assert lifecycle == ["checkout", "checkin"]
    assert result.snapshot.workflow.state is WorkflowState.REPORT_RENDERED
    assert workflows.get_snapshot(scored.workflow.id) == result.snapshot
    assert report_count(engine) == 1
    assert result.rendered.report.request.id == request.id
    assert result.rendered.report.recommended_product_id == product.id
    with factory() as session:
        record = session.scalar(select(ShoppingReportRecord))
        assert record is not None
        stored = RenderedShoppingReport.model_validate(record.payload)
    assert stored == result.rendered
    assert record.content == stored.content
    assert record.content_sha256 == stored.content_sha256

    reader = SQLiteShoppingReportRepository(factory)
    assert reader.get(scored.workflow.id) == result.rendered
    with session_scope(factory) as session:
        persisted = session.scalar(select(ShoppingReportRecord))
        assert persisted is not None
        persisted.content = f"{persisted.content}\ntampered"
    with pytest.raises(StoredShoppingReportIntegrityError, match="indexes must match"):
        reader.get(scored.workflow.id)
    engine.dispose()


class OrphanReportUnitOfWork(SQLiteShoppingReportUnitOfWork):
    def __enter__(self) -> Self:
        super().__enter__()
        return self

    def add_report(self, rendered: RenderedShoppingReport) -> None:
        record = shopping_report_record(rendered)
        record.request_id = str(uuid4())
        self._session().add(record)


def test_persistence_failure_rolls_back_report_and_state() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    workflows, _product, _request, scored = seed_scored(engine)
    factory = create_session_factory(engine)
    service = ShoppingReportService(
        lambda: OrphanReportUnitOfWork(factory),
        clock=lambda: RENDERED_AT,
    )

    with pytest.raises(IntegrityError):
        service.render(scored.workflow.id)

    assert report_count(engine) == 0
    assert workflows.get_snapshot(scored.workflow.id).workflow.state is (
        WorkflowState.CANDIDATES_SCORED
    )
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(CandidateScoreRecord)) == 1
    engine.dispose()


def test_report_unit_of_work_requires_context_and_rolls_back_without_commit() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    unit = SQLiteShoppingReportUnitOfWork(create_session_factory(engine))

    with pytest.raises(RuntimeError, match="not active"):
        unit.commit()
    with unit:
        pass

    with pytest.raises(ShoppingReportNotFoundError, match="was not found"):
        SQLiteShoppingReportRepository(create_session_factory(engine)).get(uuid4())

    engine.dispose()
