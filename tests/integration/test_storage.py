"""Integration tests for SQLite persistence and domain revalidation."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import HttpUrl, ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

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
from personal_shopping_agent.infrastructure.storage import (
    DuplicateEntityError,
    EntityNotFoundError,
    InvalidReferenceError,
    SQLiteShoppingRepository,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from personal_shopping_agent.infrastructure.storage.tables import ProductRecord

CAPTURED_AT = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)


def build_product(*, product_id: UUID | None = None) -> Product:
    return Product(
        id=product_id or uuid4(),
        brand="Example",
        model="X1",
        category="smartphone",
        canonical_name="Example X1",
    )


def build_offer(product_id: UUID, *, minutes: int = 0, sku: str = "sku-1") -> Offer:
    return Offer(
        product_id=product_id,
        platform="JD",
        seller="Example 官方旗舰店",
        store_type=StoreType.BRAND_FLAGSHIP,
        url=HttpUrl(f"https://example.com/item/{sku}"),
        sku=sku,
        variant="12GB+256GB",
        region="云南省曲靖市",
        captured_at=CAPTURED_AT + timedelta(minutes=minutes),
        price=PriceBreakdown(estimated_total_cost=Money(amount=Decimal("4199"))),
        in_stock=True,
    )


def build_evidence(product_id: UUID, value: str, *, minutes: int = 0) -> Evidence:
    return Evidence(
        subject_type=EvidenceSubjectType.PRODUCT,
        subject_id=product_id,
        field_path="specifications.battery_capacity",
        source_type=EvidenceSourceType.MANUFACTURER_OFFICIAL,
        source_url=HttpUrl(f"https://example.com/evidence/{minutes}"),
        source_title="Example 官方参数",
        captured_at=CAPTURED_AT + timedelta(minutes=minutes),
        observed_value=value,
        reliability=Decimal("0.95"),
        freshness=Decimal("0.90"),
    )


def build_repository() -> tuple[Engine, sessionmaker[Session], SQLiteShoppingRepository]:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    factory = create_session_factory(engine)
    return engine, factory, SQLiteShoppingRepository(factory)


def test_repository_round_trips_all_domain_objects_and_preserves_conflicts() -> None:
    engine, _factory, repository = build_repository()
    product = build_product()
    request = ShoppingRequest(
        query="续航优先的手机",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        criteria=(ShoppingCriterion(key="battery_capacity", minimum=Decimal("5000")),),
        region="云南省曲靖市",
        created_at=CAPTURED_AT,
    )
    later_offer = build_offer(product.id, minutes=10, sku="sku-2")
    earlier_offer = build_offer(product.id)
    official_value = build_evidence(product.id, "6000mAh")
    conflicting_value = build_evidence(product.id, "5900mAh", minutes=1)

    repository.add_request(request)
    repository.add_product(product)
    repository.add_offer(later_offer)
    repository.add_offer(earlier_offer)
    repository.add_evidence(conflicting_value)
    repository.add_evidence(official_value)

    assert repository.get_request(request.id) == request
    assert repository.get_product(product.id) == product
    assert repository.get_offer(earlier_offer.id) == earlier_offer
    assert repository.get_evidence(official_value.id) == official_value
    assert repository.list_offers_for_product(product.id) == (earlier_offer, later_offer)
    assert repository.list_evidence(EvidenceSubjectType.PRODUCT, product.id) == (
        official_value,
        conflicting_value,
    )
    assert repository.list_evidence(
        EvidenceSubjectType.PRODUCT,
        product.id,
        field_path="specifications.battery_capacity",
    ) == (official_value, conflicting_value)
    assert (
        repository.list_evidence(
            EvidenceSubjectType.PRODUCT,
            product.id,
            field_path="specifications.weight",
        )
        == ()
    )

    engine.dispose()


def test_repository_rejects_duplicate_or_orphaned_entities_and_reports_missing() -> None:
    engine, _factory, repository = build_repository()
    product = build_product()
    repository.add_product(product)

    with pytest.raises(DuplicateEntityError):
        repository.add_product(product)
    with pytest.raises(InvalidReferenceError):
        repository.add_offer(build_offer(uuid4()))
    with pytest.raises(EntityNotFoundError, match="products entity was not found"):
        repository.get_product(uuid4())

    engine.dispose()


def test_repository_revalidates_database_payloads() -> None:
    engine, factory, repository = build_repository()
    product = build_product()
    repository.add_product(product)

    with session_scope(factory) as session:
        record = session.get(ProductRecord, str(product.id))
        assert record is not None
        record.payload = {"id": str(product.id), "brand": "missing required fields"}

    with pytest.raises(ValidationError):
        repository.get_product(product.id)

    engine.dispose()


def test_database_factory_supports_files_enables_foreign_keys_and_rolls_back(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "shopping.db"
    engine = create_sqlite_engine(f"sqlite:///{database_path}", echo=False)
    create_schema(engine)
    factory = create_session_factory(engine)

    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1

    product = build_product()
    with pytest.raises(RuntimeError, match="rollback"), session_scope(factory) as session:
        session.add(
            ProductRecord(
                id=str(product.id),
                brand=product.brand,
                model=product.model,
                category=product.category,
                canonical_name=product.canonical_name,
                payload=product.model_dump(mode="json"),
            )
        )
        raise RuntimeError("rollback")

    with factory() as session:
        assert session.query(ProductRecord).count() == 0

    engine.dispose()


def test_database_factory_rejects_non_sqlite_urls() -> None:
    with pytest.raises(ValueError, match="only SQLite"):
        create_sqlite_engine("postgresql://localhost/shopping")
