"""Atomic conversion, persistence, and offers-collected workflow transition."""

from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from personal_shopping_agent.domain import Evidence, Offer, Product
from personal_shopping_agent.domain.workflow import (
    InvalidWorkflowTransitionError,
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStateMachine,
    workflow_now,
)
from personal_shopping_agent.sourcing.conversion import (
    ConvertedDetailBatch,
    DetailObservationConverter,
)
from personal_shopping_agent.sourcing.observations import DetailObservation


class OfferIngestionResult(BaseModel):
    """Validated result returned only after the ingestion transaction commits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: WorkflowSnapshot
    batch: ConvertedDetailBatch


class OfferIngestionUnitOfWork(Protocol):
    """Transaction port for details, domain entities, evidence, and workflow state."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def get_snapshot(self, workflow_id: UUID) -> WorkflowSnapshot: ...

    def list_detail_observations(self, request_id: UUID) -> tuple[DetailObservation, ...]: ...

    def add_product(self, product: Product) -> None: ...

    def add_offer(self, offer: Offer) -> None: ...

    def add_evidence(self, evidence: Evidence) -> None: ...

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None: ...

    def commit(self) -> None: ...


class OfferIngestionUnitOfWorkFactory(Protocol):
    """Create one isolated ingestion transaction."""

    def __call__(self) -> OfferIngestionUnitOfWork: ...


class OfferIngestionService:
    """Convert durable details and atomically advance to offers_collected."""

    def __init__(
        self,
        unit_of_work_factory: OfferIngestionUnitOfWorkFactory,
        *,
        converter: DetailObservationConverter | None = None,
        state_machine: WorkflowStateMachine | None = None,
        clock: Callable[[], datetime] = workflow_now,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._converter = converter or DetailObservationConverter()
        self._state_machine = state_machine or WorkflowStateMachine()
        self._clock = clock

    def ingest(self, workflow_id: UUID) -> OfferIngestionResult:
        """Persist converted domain entities and state in one transaction."""

        with self._unit_of_work_factory() as unit_of_work:
            snapshot = unit_of_work.get_snapshot(workflow_id)
            if (
                self._state_machine.next_state(snapshot.workflow.state)
                is not WorkflowState.OFFERS_COLLECTED
            ):
                raise InvalidWorkflowTransitionError(
                    "offer ingestion requires a candidates_discovered workflow"
                )

            observations = unit_of_work.list_detail_observations(snapshot.request.id)
            batch = self._converter.convert(snapshot.request, observations)
            for product in batch.products:
                unit_of_work.add_product(product)
            for offer in batch.offers:
                unit_of_work.add_offer(offer)
            for evidence in batch.evidence:
                unit_of_work.add_evidence(evidence)

            updated, event = self._state_machine.advance(
                snapshot.workflow,
                WorkflowState.OFFERS_COLLECTED,
                reason="products_offers_and_evidence_persisted",
                occurred_at=self._clock(),
            )
            unit_of_work.save_transition(
                updated,
                event,
                expected_revision=snapshot.workflow.revision,
            )
            result = OfferIngestionResult(
                snapshot=WorkflowSnapshot(
                    request=snapshot.request,
                    workflow=updated,
                    events=(*snapshot.events, event),
                ),
                batch=batch,
            )
            unit_of_work.commit()
            return result
