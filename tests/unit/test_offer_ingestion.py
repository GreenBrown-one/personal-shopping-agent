"""Unit tests for atomic offer-ingestion orchestration."""

from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from uuid import UUID

import pytest
from pydantic import HttpUrl

from personal_shopping_agent.application import (
    DetailObservation,
    InvalidWorkflowTransitionError,
    OfferIngestionService,
    PlatformProductDetail,
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStateMachine,
)
from personal_shopping_agent.domain import (
    Budget,
    Evidence,
    Money,
    Offer,
    Product,
    ShoppingRequest,
)

NOW = datetime(2026, 8, 9, 18, 0, tzinfo=UTC)


def build_snapshot(*, candidates_discovered: bool = True) -> WorkflowSnapshot:
    request = ShoppingRequest(
        query="example phone",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        created_at=NOW,
    )
    machine = WorkflowStateMachine()
    workflow, events = machine.initialize(request.id, occurred_at=NOW)
    if candidates_discovered:
        workflow, event = machine.advance(
            workflow,
            WorkflowState.CANDIDATES_DISCOVERED,
            reason="observations_persisted",
            occurred_at=NOW,
        )
        events = (*events, event)
    return WorkflowSnapshot(request=request, workflow=workflow, events=events)


def build_detail(request_id: UUID) -> DetailObservation:
    return DetailObservation(
        request_id=request_id,
        detail=PlatformProductDetail(
            platform="jd",
            external_id="1000001",
            title="Example Phone A1",
            product_url=HttpUrl("https://item.jd.com/1000001.html"),
            captured_at=NOW,
            brand="Example",
            model="A1",
            seller_name="Example 官方旗舰店",
            displayed_price=Decimal("3999"),
            region="云南省 曲靖市 麒麟区",
            stock_status="有货",
            in_stock=True,
        ),
    )


class FakeIngestionUnitOfWork:
    def __init__(self, snapshot: WorkflowSnapshot, details: tuple[DetailObservation, ...]) -> None:
        self.snapshot = snapshot
        self.details = details
        self.entered = False
        self.exited = False
        self.committed = False
        self.rolled_back = False
        self.products: list[Product] = []
        self.offers: list[Offer] = []
        self.evidence: list[Evidence] = []
        self.transition: tuple[ShoppingWorkflow, WorkflowEvent, int] | None = None

    def __enter__(self) -> "FakeIngestionUnitOfWork":
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        self.exited = True
        self.rolled_back = exc_type is not None or not self.committed

    def get_snapshot(self, workflow_id: UUID) -> WorkflowSnapshot:
        assert workflow_id == self.snapshot.workflow.id
        return self.snapshot

    def list_detail_observations(self, request_id: UUID) -> tuple[DetailObservation, ...]:
        assert request_id == self.snapshot.request.id
        return self.details

    def add_product(self, product: Product) -> None:
        self.products.append(product)

    def add_offer(self, offer: Offer) -> None:
        self.offers.append(offer)

    def add_evidence(self, evidence: Evidence) -> None:
        self.evidence.append(evidence)

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None:
        self.transition = (workflow, event, expected_revision)

    def commit(self) -> None:
        self.committed = True


def test_service_stages_domain_entities_evidence_and_offers_transition() -> None:
    snapshot = build_snapshot()
    unit_of_work = FakeIngestionUnitOfWork(snapshot, (build_detail(snapshot.request.id),))
    service = OfferIngestionService(lambda: unit_of_work, clock=lambda: NOW)

    result = service.ingest(snapshot.workflow.id)

    assert unit_of_work.entered and unit_of_work.exited and unit_of_work.committed
    assert not unit_of_work.rolled_back
    assert unit_of_work.products == list(result.batch.products)
    assert unit_of_work.offers == list(result.batch.offers)
    assert unit_of_work.evidence == list(result.batch.evidence)
    assert result.snapshot.workflow.state is WorkflowState.OFFERS_COLLECTED
    assert result.snapshot.workflow.updated_at == NOW
    assert result.snapshot.events[-1].reason == "products_offers_and_evidence_persisted"
    assert unit_of_work.transition is not None
    assert unit_of_work.transition[2] == snapshot.workflow.revision


def test_service_rejects_wrong_state_and_rolls_back_conversion_failure() -> None:
    wrong_snapshot = build_snapshot(candidates_discovered=False)
    wrong_unit = FakeIngestionUnitOfWork(
        wrong_snapshot,
        (build_detail(wrong_snapshot.request.id),),
    )
    with pytest.raises(InvalidWorkflowTransitionError, match="candidates_discovered"):
        OfferIngestionService(lambda: wrong_unit).ingest(wrong_snapshot.workflow.id)
    assert wrong_unit.exited and wrong_unit.rolled_back and not wrong_unit.products

    snapshot = build_snapshot()
    empty_unit = FakeIngestionUnitOfWork(snapshot, ())
    with pytest.raises(RuntimeError, match="No detail observations"):
        OfferIngestionService(lambda: empty_unit).ingest(snapshot.workflow.id)
    assert empty_unit.exited and empty_unit.rolled_back and not empty_unit.committed
