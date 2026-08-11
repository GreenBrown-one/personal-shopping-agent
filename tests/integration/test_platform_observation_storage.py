"""Integration tests for append-only structured platform observations."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import HttpUrl, ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from personal_shopping_agent.application import (
    DetailObservation,
    PlatformCandidate,
    PlatformProductDetail,
    PlatformSearchResult,
    SearchObservation,
)
from personal_shopping_agent.domain import Budget, Money, ShoppingRequest
from personal_shopping_agent.storage import (
    DuplicateEntityError,
    EntityNotFoundError,
    InvalidReferenceError,
    ObservationKindMismatchError,
    SQLitePlatformObservationRepository,
    SQLiteShoppingRepository,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from personal_shopping_agent.storage.tables import PlatformObservationRecord

CAPTURED_AT = datetime(2026, 8, 9, 15, 0, tzinfo=UTC)


def build_request() -> ShoppingRequest:
    return ShoppingRequest(
        query="预算五千元、续航优先的手机",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        region="云南省曲靖市",
        created_at=CAPTURED_AT,
    )


def build_search(
    request_id: UUID,
    *,
    minutes: int = 0,
    price: Decimal = Decimal("3999"),
) -> SearchObservation:
    result = PlatformSearchResult(
        platform="jd",
        query="example phone",
        source_url=HttpUrl("https://search.jd.com/Search?keyword=example+phone"),
        captured_at=CAPTURED_AT + timedelta(minutes=minutes),
        candidates=(
            PlatformCandidate(
                platform="jd",
                external_id="1000001",
                title="Example Phone",
                product_url=HttpUrl("https://item.jd.com/1000001.html"),
                displayed_price=price,
            ),
        ),
    )
    return SearchObservation(request_id=request_id, result=result)


def build_detail(
    request_id: UUID,
    *,
    minutes: int = 0,
    external_id: str = "1000001",
    price: Decimal = Decimal("3999"),
) -> DetailObservation:
    detail = PlatformProductDetail(
        platform="jd",
        external_id=external_id,
        title="Example Phone",
        product_url=HttpUrl(f"https://item.jd.com/{external_id}.html"),
        captured_at=CAPTURED_AT + timedelta(minutes=minutes),
        brand="Example",
        model="A1",
        seller_name="Example 官方旗舰店",
        displayed_price=price,
        region="云南省 曲靖市 麒麟区",
        stock_status="有货",
        in_stock=True,
    )
    return DetailObservation(request_id=request_id, detail=detail)


def build_repository() -> tuple[
    SQLiteShoppingRepository,
    SQLitePlatformObservationRepository,
    sessionmaker[Session],
    Engine,
]:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    factory = create_session_factory(engine)
    return (
        SQLiteShoppingRepository(factory),
        SQLitePlatformObservationRepository(factory),
        factory,
        engine,
    )


def test_repository_round_trips_observations_and_preserves_conflicting_history() -> None:
    shopping_repository, repository, factory, engine = build_repository()
    request = build_request()
    shopping_repository.add_request(request)
    later_search = build_search(request.id, minutes=5, price=Decimal("3899"))
    earlier_search = build_search(request.id)
    later_detail = build_detail(request.id, minutes=10, price=Decimal("3799"))
    earlier_detail = build_detail(request.id, price=Decimal("3999"))
    other_detail = build_detail(request.id, minutes=20, external_id="1000002")

    repository.add_search_observation(later_search)
    repository.add_search_observation(earlier_search)
    repository.add_detail_observation(later_detail)
    repository.add_detail_observation(earlier_detail)
    repository.add_detail_observation(other_detail)

    assert repository.get_search_observation(earlier_search.id) == earlier_search
    assert repository.get_detail_observation(earlier_detail.id) == earlier_detail
    assert repository.list_search_observations(request.id) == (earlier_search, later_search)
    assert repository.list_detail_observations(request.id) == (
        earlier_detail,
        later_detail,
        other_detail,
    )
    assert repository.list_detail_observations(request.id, platform="jd") == (
        earlier_detail,
        later_detail,
        other_detail,
    )
    assert repository.list_detail_observations(request.id, external_id="1000001") == (
        earlier_detail,
        later_detail,
    )
    assert repository.list_detail_observations(
        request.id,
        platform="jd",
        external_id="1000002",
    ) == (other_detail,)
    assert repository.list_detail_observations(request.id, platform="other") == ()
    assert repository.list_search_observations(uuid4()) == ()

    with factory() as session:
        records = session.scalars(select(PlatformObservationRecord)).all()
        payload_text = repr([record.payload for record in records]).lower()
        assert "<html" not in payload_text
        assert "screenshot" not in payload_text
        assert "cookie" not in payload_text

    engine.dispose()


def test_repository_rejects_unknown_requests_duplicates_missing_ids_and_wrong_kinds() -> None:
    shopping_repository, repository, _factory, engine = build_repository()
    request = build_request()
    shopping_repository.add_request(request)
    search = build_search(request.id)
    detail = build_detail(request.id)
    repository.add_search_observation(search)
    repository.add_detail_observation(detail)

    with pytest.raises(DuplicateEntityError):
        repository.add_search_observation(search)
    with pytest.raises(InvalidReferenceError):
        repository.add_detail_observation(build_detail(uuid4()))
    with pytest.raises(EntityNotFoundError, match="platform_observations"):
        repository.get_search_observation(uuid4())
    with pytest.raises(ObservationKindMismatchError, match="not 'detail'"):
        repository.get_detail_observation(search.id)
    with pytest.raises(ObservationKindMismatchError, match="not 'search'"):
        repository.get_search_observation(detail.id)

    engine.dispose()


def test_repository_revalidates_observation_payloads() -> None:
    shopping_repository, repository, factory, engine = build_repository()
    request = build_request()
    shopping_repository.add_request(request)
    observation = build_detail(request.id)
    repository.add_detail_observation(observation)

    with session_scope(factory) as session:
        record = session.get(PlatformObservationRecord, str(observation.id))
        assert record is not None
        record.payload = {"id": str(observation.id), "request_id": str(request.id)}

    with pytest.raises(ValidationError):
        repository.get_detail_observation(observation.id)

    engine.dispose()
