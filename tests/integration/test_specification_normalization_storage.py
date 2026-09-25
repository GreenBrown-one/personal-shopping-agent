"""Integration tests for atomic normalized specification persistence."""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

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
    Product,
    ShoppingRequest,
    WorkflowSnapshot,
    WorkflowState,
)
from personal_shopping_agent.infrastructure.storage import (
    SQLiteShoppingRepository,
    SQLiteSpecificationNormalizationUnitOfWork,
    SQLiteWorkflowRepository,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from personal_shopping_agent.infrastructure.storage.tables import NormalizedSpecificationRecord
from personal_shopping_agent.sourcing import (
    NormalizedSpecification,
    ProductSpecificationNormalizer,
    SpecificationNormalizationService,
)

NOW = datetime(2026, 8, 9, 21, 0, tzinfo=UTC)


def seed_cross_checked(
    engine: Engine,
) -> tuple[SQLiteWorkflowRepository, Product, ShoppingRequest, WorkflowSnapshot]:
    factory = create_session_factory(engine)
    workflows = SQLiteWorkflowRepository(factory)
    workflow_service = ShoppingWorkflowService(workflows, clock=lambda: NOW)
    request = ShoppingRequest(
        query="example phone",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        created_at=NOW,
    )
    snapshot = workflow_service.start(request)
    for state in (
        WorkflowState.CANDIDATES_DISCOVERED,
        WorkflowState.OFFERS_COLLECTED,
        WorkflowState.EVIDENCE_CROSS_CHECKED,
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
    repository = SQLiteShoppingRepository(factory)
    repository.add_product(product)
    for field_path, value, source_type in (
        ("specifications.电池容量", "6000 mAh", EvidenceSourceType.PLATFORM_LISTING),
        (
            "specifications.battery_capacity",
            "6 Ah",
            EvidenceSourceType.MANUFACTURER_OFFICIAL,
        ),
        ("specifications.weight", "180 g", EvidenceSourceType.PLATFORM_LISTING),
    ):
        repository.add_evidence(
            Evidence(
                request_id=request.id,
                subject_type=EvidenceSubjectType.PRODUCT,
                subject_id=product.id,
                field_path=field_path,
                source_type=source_type,
                source_url=HttpUrl("https://example.com/specifications"),
                source_title="Example specifications",
                captured_at=NOW,
                observed_value=value,
            )
        )
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


def normalized_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return (
            connection.scalar(select(func.count()).select_from(NormalizedSpecificationRecord)) or 0
        )


def test_normalization_uses_one_connection_and_commits_facts_and_state() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    workflows, product, request, cross_checked = seed_cross_checked(engine)
    factory = create_session_factory(engine)
    lifecycle, remove_listener = connection_events(engine)
    service = SpecificationNormalizationService(
        lambda: SQLiteSpecificationNormalizationUnitOfWork(factory),
        clock=lambda: NOW,
    )

    result = service.normalize(cross_checked.workflow.id)
    remove_listener()

    assert lifecycle == ["checkout", "checkin"]
    assert result.snapshot.workflow.state is WorkflowState.DATA_NORMALIZED
    assert workflows.get_snapshot(cross_checked.workflow.id) == result.snapshot
    assert normalized_count(engine) == len(result.batch.specifications) == 2
    assert all(
        fact.product_id == product.id and fact.request_id == request.id
        for fact in result.batch.specifications
    )
    with factory() as session:
        records = session.scalars(
            select(NormalizedSpecificationRecord).order_by(
                NormalizedSpecificationRecord.canonical_key
            )
        ).all()
        stored = tuple(NormalizedSpecification.model_validate(record.payload) for record in records)
    assert stored == result.batch.specifications
    battery = next(item for item in stored if item.canonical_key == "battery_capacity")
    assert battery.normalized_values == (Decimal("6000"),)
    assert len(battery.evidence_ids) == 2
    engine.dispose()


class OrphanNormalizationBuilder(ProductSpecificationNormalizer):
    def normalize(
        self,
        *,
        request_id: UUID,
        workflow_id: UUID,
        product: Product,
        evidence: tuple[Evidence, ...],
        normalized_at: datetime,
    ) -> tuple[NormalizedSpecification, ...]:
        valid = super().normalize(
            request_id=request_id,
            workflow_id=workflow_id,
            product=product,
            evidence=evidence,
            normalized_at=normalized_at,
        )
        return (valid[0].model_copy(update={"product_id": uuid4()}), *valid[1:])


def test_persistence_failure_rolls_back_normalized_facts_and_state() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    workflows, _product, _request, cross_checked = seed_cross_checked(engine)
    factory = create_session_factory(engine)
    service = SpecificationNormalizationService(
        lambda: SQLiteSpecificationNormalizationUnitOfWork(factory),
        normalizer=OrphanNormalizationBuilder(),
    )

    with pytest.raises(IntegrityError):
        service.normalize(cross_checked.workflow.id)

    assert normalized_count(engine) == 0
    assert workflows.get_snapshot(cross_checked.workflow.id).workflow.state is (
        WorkflowState.EVIDENCE_CROSS_CHECKED
    )
    engine.dispose()


def test_normalization_unit_of_work_requires_context_and_rolls_back_without_commit() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    unit = SQLiteSpecificationNormalizationUnitOfWork(create_session_factory(engine))

    with pytest.raises(RuntimeError, match="not active"):
        unit.commit()
    with unit:
        pass

    engine.dispose()
