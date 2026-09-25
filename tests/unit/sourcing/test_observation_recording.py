"""Unit tests for recording validated structured platform observations."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import HttpUrl

from personal_shopping_agent.sourcing import (
    DetailObservation,
    PlatformCandidate,
    PlatformObservationKind,
    PlatformObservationService,
    PlatformProductDetail,
    PlatformSearchResult,
    SearchObservation,
)

CAPTURED_AT = datetime(2026, 8, 9, 15, 0, tzinfo=UTC)


class RecordingStore:
    def __init__(self) -> None:
        self.searches: list[SearchObservation] = []
        self.details: list[DetailObservation] = []

    def add_search_observation(self, observation: SearchObservation) -> None:
        self.searches.append(observation)

    def add_detail_observation(self, observation: DetailObservation) -> None:
        self.details.append(observation)


def search_result() -> PlatformSearchResult:
    return PlatformSearchResult(
        platform="jd",
        query="example phone",
        source_url=HttpUrl("https://search.jd.com/Search?keyword=example+phone"),
        captured_at=CAPTURED_AT,
        candidates=(
            PlatformCandidate(
                platform="jd",
                external_id="1000001",
                title="Example Phone",
                product_url=HttpUrl("https://item.jd.com/1000001.html"),
                displayed_price=Decimal("3999"),
            ),
        ),
    )


def product_detail() -> PlatformProductDetail:
    return PlatformProductDetail(
        platform="jd",
        external_id="1000001",
        title="Example Phone",
        product_url=HttpUrl("https://item.jd.com/1000001.html"),
        captured_at=CAPTURED_AT,
        brand="Example",
        model="A1",
        seller_name="Example 官方旗舰店",
        displayed_price=Decimal("3999"),
        region="云南省 曲靖市 麒麟区",
    )


def test_service_records_search_and_detail_without_promoting_domain_entities() -> None:
    store = RecordingStore()
    service = PlatformObservationService(store)
    request_id = uuid4()

    search = service.record_search(request_id, search_result())
    detail = service.record_detail(request_id, product_detail())

    assert isinstance(search.id, UUID)
    assert isinstance(detail.id, UUID)
    assert store.searches == [search]
    assert store.details == [detail]
    assert search.request_id == request_id
    assert detail.request_id == request_id
    assert PlatformObservationKind.SEARCH.value == "search"
    assert PlatformObservationKind.DETAIL.value == "detail"
