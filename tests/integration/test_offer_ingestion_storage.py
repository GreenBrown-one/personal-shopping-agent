"""Integration tests for atomic Product/Offer/Evidence ingestion."""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import HttpUrl
from sqlalchemy import Engine, event, func, select
from sqlalchemy.exc import IntegrityError

from personal_shopping_agent.application import (
    ConvertedDetailBatch,
    DetailObservation,
    DetailObservationConverter,
    OfferIngestionService,
    PlatformProductDetail,
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
    Offer,
    PriceBreakdown,
    Product,
    ShoppingRequest,
)
from personal_shopping_agent.storage import (
    SQLiteOfferIngestionUnitOfWork,
    SQLitePlatformObservationRepository,
    SQLiteShoppingRepository,
    SQLiteWorkflowRepository,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from personal_shopping_agent.storage.tables import (
    EvidenceRecord,
    OfferRecord,
    ProductRecord,
)

NOW = datetime(2026, 8, 9, 18, 0, tzinfo=UTC)


def build_request() -> ShoppingRequest:
    return ShoppingRequest(
        query="example phone",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        region="云南省曲靖市",
        created_at=NOW,
    )


def build_detail(request_id: UUID, *, valid: bool = True) -> DetailObservation:
    return DetailObservation.model_validate(
        {
            "request_id": request_id,
            "detail": PlatformProductDetail(
                platform="jd",
                external_id="1000001",
                title="Example Phone A1",
                product_url=HttpUrl("https://item.jd.com/1000001.html"),
                captured_at=NOW,
                brand="Example" if valid else None,
                model="A1",
                seller_name="Example 官方旗舰店",
                displayed_price=Decimal("3999"),
                region="云南省 曲靖市 麒麟区",
                stock_status="有货",
                in_stock=True,
            ),
        }
    )


def seed_candidates_discovered(
    engine: Engine,
    *,
    valid_detail: bool = True,
) -> tuple[SQLiteWorkflowRepository, WorkflowSnapshot, DetailObservation]:
    factory = create_session_factory(engine)
    workflow_repository = SQLiteWorkflowRepository(factory)
    workflow_service = ShoppingWorkflowService(workflow_repository, clock=lambda: NOW)
    started = workflow_service.start(build_request())
    discovered = workflow_service.advance(
        started.workflow.id,
        WorkflowState.CANDIDATES_DISCOVERED,
        reason="search_and_detail_observations_persisted",
    )
    observation = build_detail(started.request.id, valid=valid_detail)
    SQLitePlatformObservationRepository(factory).add_detail_observation(observation)
    return workflow_repository, discovered, observation


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


def table_counts(engine: Engine) -> tuple[int, int, int]:
    with engine.connect() as connection:
        return (
            connection.scalar(select(func.count()).select_from(ProductRecord)) or 0,
            connection.scalar(select(func.count()).select_from(OfferRecord)) or 0,
            connection.scalar(select(func.count()).select_from(EvidenceRecord)) or 0,
        )


def test_ingestion_uses_one_connection_and_commits_entities_evidence_and_state() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    workflow_repository, discovered, observation = seed_candidates_discovered(engine)
    factory = create_session_factory(engine)
    lifecycle, remove_listener = connection_events(engine)
    service = OfferIngestionService(
        lambda: SQLiteOfferIngestionUnitOfWork(factory),
        clock=lambda: NOW,
    )

    result = service.ingest(discovered.workflow.id)
    remove_listener()

    assert lifecycle == ["checkout", "checkin"]
    assert result.snapshot.workflow.state is WorkflowState.OFFERS_COLLECTED
    assert workflow_repository.get_snapshot(discovered.workflow.id) == result.snapshot
    assert table_counts(engine) == (1, 1, len(result.batch.evidence))
    repository = SQLiteShoppingRepository(factory)
    product = repository.get_product(result.batch.products[0].id)
    offer = repository.get_offer(result.batch.offers[0].id)
    evidence = repository.list_evidence(EvidenceSubjectType.OFFER, offer.id)
    assert product == result.batch.products[0]
    assert offer == result.batch.offers[0]
    assert evidence
    assert all(item.origin_observation_id == observation.id for item in result.batch.evidence)

    engine.dispose()


def test_conversion_failure_rolls_back_and_keeps_candidates_state() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    workflow_repository, discovered, _observation = seed_candidates_discovered(
        engine,
        valid_detail=False,
    )
    factory = create_session_factory(engine)
    lifecycle, remove_listener = connection_events(engine)
    service = OfferIngestionService(lambda: SQLiteOfferIngestionUnitOfWork(factory))

    with pytest.raises(ValueError, match="brand"):
        service.ingest(discovered.workflow.id)
    remove_listener()

    assert lifecycle == ["checkout", "checkin"]
    assert table_counts(engine) == (0, 0, 0)
    assert workflow_repository.get_snapshot(discovered.workflow.id).workflow.state is (
        WorkflowState.CANDIDATES_DISCOVERED
    )
    engine.dispose()


class OrphanOfferConverter(DetailObservationConverter):
    def convert(
        self,
        request: ShoppingRequest,
        observations: tuple[DetailObservation, ...],
    ) -> ConvertedDetailBatch:
        del request
        observation = observations[0]
        product = Product(
            brand="Example",
            model="A1",
            category="smartphone",
            canonical_name="Example Phone A1",
        )
        offer = Offer(
            product_id=uuid4(),
            platform="jd",
            seller="Example",
            url=observation.detail.product_url,
            sku=observation.detail.external_id,
            region=observation.detail.region,
            captured_at=NOW,
            price=PriceBreakdown(displayed_price=Money(amount=Decimal("3999"))),
        )
        evidence = Evidence(
            subject_type=EvidenceSubjectType.OFFER,
            subject_id=offer.id,
            field_path="price.displayed_price",
            source_type=EvidenceSourceType.PLATFORM_LISTING,
            source_url=observation.detail.product_url,
            source_title=observation.detail.title,
            captured_at=NOW,
            origin_observation_id=observation.id,
            observed_value=Decimal("3999"),
        )
        return ConvertedDetailBatch(products=(product,), offers=(offer,), evidence=(evidence,))


def test_persistence_failure_rolls_back_products_evidence_and_state() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    workflow_repository, discovered, _observation = seed_candidates_discovered(engine)
    factory = create_session_factory(engine)
    service = OfferIngestionService(
        lambda: SQLiteOfferIngestionUnitOfWork(factory),
        converter=OrphanOfferConverter(),
    )

    with pytest.raises(IntegrityError):
        service.ingest(discovered.workflow.id)

    assert table_counts(engine) == (0, 0, 0)
    assert workflow_repository.get_snapshot(discovered.workflow.id).workflow.state is (
        WorkflowState.CANDIDATES_DISCOVERED
    )
    engine.dispose()


def test_ingestion_unit_of_work_requires_context_and_rolls_back_without_commit() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    unit_of_work = SQLiteOfferIngestionUnitOfWork(create_session_factory(engine))

    with pytest.raises(RuntimeError, match="not active"):
        unit_of_work.commit()
    with unit_of_work:
        pass

    engine.dispose()
