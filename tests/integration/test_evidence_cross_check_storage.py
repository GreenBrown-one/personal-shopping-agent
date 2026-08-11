"""Integration tests for atomic official evidence cross-check persistence."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import HttpUrl
from sqlalchemy import Engine, event, func, select
from sqlalchemy.exc import IntegrityError

from personal_shopping_agent.application import (
    EvidenceCheck,
    EvidenceCrossCheckBatch,
    EvidenceCrossCheckService,
    OfficialEvidenceIdentityMismatchError,
    OfficialProductObservation,
    OfficialSpecificationObservation,
    ProductEvidenceChecker,
    ShoppingWorkflowService,
    WorkflowSnapshot,
    WorkflowState,
)
from personal_shopping_agent.domain import (
    Budget,
    Evidence,
    EvidenceSourceType,
    EvidenceSubjectType,
    Money,
    Product,
    ShoppingRequest,
)
from personal_shopping_agent.storage import (
    SQLiteEvidenceCrossCheckUnitOfWork,
    SQLiteShoppingRepository,
    SQLiteWorkflowRepository,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from personal_shopping_agent.storage.tables import EvidenceCheckRecord, EvidenceRecord

NOW = datetime(2026, 8, 9, 19, 0, tzinfo=UTC)


class FakeOfficialProvider:
    def __init__(self, *, model: str = "A1") -> None:
        self.model = model

    async def collect(self, product: Product) -> tuple[OfficialProductObservation, ...]:
        return (
            OfficialProductObservation(
                brand=product.brand,
                model=self.model,
                source_url=HttpUrl("https://manufacturer.example.com/a1/specifications"),
                source_title="Example A1 official specifications",
                captured_at=NOW,
                specifications=(
                    OfficialSpecificationObservation(key="battery", raw_value="6000 mAh"),
                    OfficialSpecificationObservation(key="weight", raw_value="180 g"),
                ),
            ),
        )


def seed_offers_collected(
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
    started = workflow_service.start(request)
    discovered = workflow_service.advance(
        started.workflow.id,
        WorkflowState.CANDIDATES_DISCOVERED,
        reason="observations_persisted",
    )
    collected = workflow_service.advance(
        discovered.workflow.id,
        WorkflowState.OFFERS_COLLECTED,
        reason="offers_persisted",
    )
    product = Product(
        brand="Example",
        model="A1",
        category="smartphone",
        canonical_name="Example A1",
    )
    repository = SQLiteShoppingRepository(factory)
    repository.add_product(product)
    for field_path, value in (
        ("brand", "Example"),
        ("model", "A1"),
        ("specifications.battery", "6000 mAh"),
    ):
        repository.add_evidence(
            Evidence(
                request_id=request.id,
                subject_type=EvidenceSubjectType.PRODUCT,
                subject_id=product.id,
                field_path=field_path,
                source_type=EvidenceSourceType.PLATFORM_LISTING,
                source_url=HttpUrl("https://item.example.com/1"),
                source_title="Platform product page",
                captured_at=NOW,
                observed_value=value,
            )
        )
    return workflows, product, request, collected


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


def counts(engine: Engine) -> tuple[int, int]:
    with engine.connect() as connection:
        return (
            connection.scalar(select(func.count()).select_from(EvidenceRecord)) or 0,
            connection.scalar(select(func.count()).select_from(EvidenceCheckRecord)) or 0,
        )


def test_cross_check_uses_one_connection_and_commits_evidence_checks_and_state() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    workflows, product, request, collected = seed_offers_collected(engine)
    factory = create_session_factory(engine)
    before = counts(engine)
    lifecycle, remove_listener = connection_events(engine)
    service = EvidenceCrossCheckService(
        lambda: SQLiteEvidenceCrossCheckUnitOfWork(factory),
        FakeOfficialProvider(),
        clock=lambda: NOW,
    )

    result = asyncio.run(service.cross_check(collected.workflow.id))
    remove_listener()

    assert lifecycle == ["checkout", "checkin"]
    assert result.snapshot.workflow.state is WorkflowState.EVIDENCE_CROSS_CHECKED
    assert workflows.get_snapshot(collected.workflow.id) == result.snapshot
    assert counts(engine) == (
        before[0] + len(result.batch.official_evidence),
        len(result.batch.checks),
    )
    repository = SQLiteShoppingRepository(factory)
    stored = repository.list_evidence(EvidenceSubjectType.PRODUCT, product.id)
    official = tuple(
        item for item in stored if item.source_type is EvidenceSourceType.MANUFACTURER_OFFICIAL
    )
    assert {item.id for item in official} == {item.id for item in result.batch.official_evidence}
    assert all(item.request_id == request.id for item in official)
    with factory() as session:
        records = session.scalars(
            select(EvidenceCheckRecord).order_by(EvidenceCheckRecord.field_path)
        ).all()
        checks = tuple(EvidenceCheck.model_validate(record.payload) for record in records)
    assert checks == tuple(sorted(result.batch.checks, key=lambda item: item.field_path))
    engine.dispose()


def test_identity_failure_rolls_back_and_keeps_offers_collected() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    workflows, _product, _request, collected = seed_offers_collected(engine)
    factory = create_session_factory(engine)
    before = counts(engine)
    service = EvidenceCrossCheckService(
        lambda: SQLiteEvidenceCrossCheckUnitOfWork(factory),
        FakeOfficialProvider(model="A2"),
    )

    with pytest.raises(OfficialEvidenceIdentityMismatchError):
        asyncio.run(service.cross_check(collected.workflow.id))

    assert counts(engine) == before
    assert workflows.get_snapshot(collected.workflow.id).workflow.state is (
        WorkflowState.OFFERS_COLLECTED
    )
    engine.dispose()


class OrphanCheckBuilder(ProductEvidenceChecker):
    def build(
        self,
        *,
        request_id: UUID,
        workflow_id: UUID,
        product: Product,
        platform_evidence: tuple[Evidence, ...],
        official_observations: tuple[OfficialProductObservation, ...],
        checked_at: datetime,
    ) -> EvidenceCrossCheckBatch:
        valid = super().build(
            request_id=request_id,
            workflow_id=workflow_id,
            product=product,
            platform_evidence=platform_evidence,
            official_observations=official_observations,
            checked_at=checked_at,
        )
        orphan = valid.checks[0].model_copy(update={"product_id": uuid4()})
        return EvidenceCrossCheckBatch(
            official_evidence=valid.official_evidence,
            checks=(orphan, *valid.checks[1:]),
        )


def test_persistence_failure_rolls_back_official_evidence_checks_and_state() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    workflows, _product, _request, collected = seed_offers_collected(engine)
    factory = create_session_factory(engine)
    before = counts(engine)
    service = EvidenceCrossCheckService(
        lambda: SQLiteEvidenceCrossCheckUnitOfWork(factory),
        FakeOfficialProvider(),
        checker=OrphanCheckBuilder(),
    )

    with pytest.raises(IntegrityError):
        asyncio.run(service.cross_check(collected.workflow.id))

    assert counts(engine) == before
    assert workflows.get_snapshot(collected.workflow.id).workflow.state is (
        WorkflowState.OFFERS_COLLECTED
    )
    engine.dispose()


def test_cross_check_unit_of_work_requires_context_and_rolls_back_without_commit() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    unit = SQLiteEvidenceCrossCheckUnitOfWork(create_session_factory(engine))

    with pytest.raises(RuntimeError, match="not active"):
        unit.commit()
    with unit:
        pass

    engine.dispose()
