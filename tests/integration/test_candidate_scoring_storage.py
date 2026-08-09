"""Integration tests for atomic candidate-score persistence."""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Self
from uuid import uuid4

import pytest
from pydantic import HttpUrl
from sqlalchemy import Engine, event, func, select
from sqlalchemy.exc import IntegrityError

from personal_shopping_agent.application import (
    CandidateScore,
    CandidateScoringService,
    EvidenceCheck,
    EvidenceCheckStatus,
    NormalizedSpecification,
    ShoppingWorkflowService,
    SpecificationNormalizationStatus,
    WorkflowSnapshot,
    WorkflowState,
)
from personal_shopping_agent.domain import (
    Budget,
    Evidence,
    EvidenceSourceType,
    EvidenceSubjectType,
    Money,
    Offer,
    PriceBreakdown,
    Product,
    ShoppingCriterion,
    ShoppingRequest,
    StoreType,
)
from personal_shopping_agent.storage import (
    SQLiteCandidateScoringUnitOfWork,
    SQLiteShoppingRepository,
    SQLiteWorkflowRepository,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from personal_shopping_agent.storage.cross_check_unit_of_work import evidence_check_record
from personal_shopping_agent.storage.normalization_unit_of_work import (
    normalized_specification_record,
)
from personal_shopping_agent.storage.scoring_unit_of_work import candidate_score_record
from personal_shopping_agent.storage.tables import CandidateScoreRecord

NOW = datetime(2026, 8, 10, 1, 0, tzinfo=UTC)


def seed_normalized(
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
    product_evidence = Evidence(
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
    offer_evidence = Evidence(
        request_id=request.id,
        subject_type=EvidenceSubjectType.OFFER,
        subject_id=offer.id,
        field_path="price.estimated_total_cost",
        source_type=EvidenceSourceType.PLATFORM_SELF_OPERATED,
        source_url=offer.url,
        source_title="JD offer",
        captured_at=NOW,
        observed_value="CNY 80",
    )
    repository = SQLiteShoppingRepository(factory)
    repository.add_product(product)
    repository.add_offer(offer)
    repository.add_evidence(product_evidence)
    repository.add_evidence(offer_evidence)

    specification = NormalizedSpecification(
        request_id=request.id,
        workflow_id=snapshot.workflow.id,
        product_id=product.id,
        canonical_key="battery_capacity",
        source_field_paths=("specifications.battery_capacity",),
        raw_values=("5000 mAh",),
        normalized_values=(Decimal("5000"),),
        canonical_unit="mAh",
        evidence_ids=(product_evidence.id,),
        status=SpecificationNormalizationStatus.NORMALIZED,
        normalized_at=NOW,
    )
    check = EvidenceCheck(
        request_id=request.id,
        workflow_id=snapshot.workflow.id,
        product_id=product.id,
        field_path="specifications.battery_capacity",
        status=EvidenceCheckStatus.MATCH,
        platform_values=("5000 mAh",),
        official_values=("5000 mAh",),
        checked_at=NOW,
    )
    with session_scope(factory) as session:
        session.add(normalized_specification_record(specification))
        session.add(evidence_check_record(check))
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


def score_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(CandidateScoreRecord)) or 0


def test_scoring_uses_one_connection_and_commits_scores_and_state() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    workflows, product, request, normalized = seed_normalized(engine)
    factory = create_session_factory(engine)
    lifecycle, remove_listener = connection_events(engine)
    service = CandidateScoringService(
        lambda: SQLiteCandidateScoringUnitOfWork(factory),
        clock=lambda: NOW,
    )

    result = service.score(normalized.workflow.id)
    remove_listener()

    assert lifecycle == ["checkout", "checkin"]
    assert result.snapshot.workflow.state is WorkflowState.CANDIDATES_SCORED
    assert workflows.get_snapshot(normalized.workflow.id) == result.snapshot
    assert score_count(engine) == len(result.batch.scores) == 1
    score = result.batch.scores[0]
    assert score.product_id == product.id
    assert score.request_id == request.id
    assert score.eligible is True
    assert score.rank == score.pareto_front == 1
    with factory() as session:
        record = session.scalar(select(CandidateScoreRecord))
        assert record is not None
        stored = CandidateScore.model_validate(record.payload)
    assert stored == score
    engine.dispose()


class OrphanScoreUnitOfWork(SQLiteCandidateScoringUnitOfWork):
    def __enter__(self) -> Self:
        super().__enter__()
        return self

    def add_score(self, score: CandidateScore) -> None:
        record = candidate_score_record(score)
        record.product_id = str(uuid4())
        self._session().add(record)


def test_persistence_failure_rolls_back_scores_and_state() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    workflows, _product, _request, normalized = seed_normalized(engine)
    factory = create_session_factory(engine)
    service = CandidateScoringService(
        lambda: OrphanScoreUnitOfWork(factory),
        clock=lambda: NOW,
    )

    with pytest.raises(IntegrityError):
        service.score(normalized.workflow.id)

    assert score_count(engine) == 0
    assert workflows.get_snapshot(normalized.workflow.id).workflow.state is (
        WorkflowState.DATA_NORMALIZED
    )
    engine.dispose()


def test_scoring_unit_of_work_requires_context_and_rolls_back_without_commit() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    unit = SQLiteCandidateScoringUnitOfWork(create_session_factory(engine))

    with pytest.raises(RuntimeError, match="not active"):
        unit.commit()
    with unit:
        pass

    engine.dispose()
