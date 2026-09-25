"""SQLite work unit for atomic detail conversion ingestion."""

from types import TracebackType
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from personal_shopping_agent.domain import (
    Evidence,
    Offer,
    Product,
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
)
from personal_shopping_agent.infrastructure.storage.repository import (
    evidence_record,
    offer_record,
    product_record,
)
from personal_shopping_agent.infrastructure.storage.tables import PlatformObservationRecord
from personal_shopping_agent.infrastructure.storage.workflow_repository import (
    apply_workflow_transition,
    load_workflow_snapshot,
)
from personal_shopping_agent.sourcing import (
    DetailObservation,
    PlatformObservationKind,
)


class SQLiteOfferIngestionUnitOfWork:
    """Hold one SQLite transaction across details, facts, evidence, and workflow state."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._active_session: Session | None = None
        self._committed = False

    def __enter__(self) -> "SQLiteOfferIngestionUnitOfWork":
        session = self._session_factory()
        session.connection()
        self._active_session = session
        self._committed = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        session = self._session()
        try:
            if exc_type is not None or not self._committed:
                session.rollback()
        finally:
            session.close()
            self._active_session = None
            self._committed = False

    def get_snapshot(self, workflow_id: UUID) -> WorkflowSnapshot:
        """Load workflow state and request through the active transaction."""

        return load_workflow_snapshot(self._session(), workflow_id)

    def list_detail_observations(self, request_id: UUID) -> tuple[DetailObservation, ...]:
        """Load and revalidate detail observations in deterministic capture order."""

        statement = (
            select(PlatformObservationRecord)
            .where(
                PlatformObservationRecord.request_id == str(request_id),
                PlatformObservationRecord.kind == PlatformObservationKind.DETAIL.value,
            )
            .order_by(PlatformObservationRecord.captured_at, PlatformObservationRecord.id)
        )
        records = self._session().scalars(statement).all()
        return tuple(DetailObservation.model_validate(record.payload) for record in records)

    def add_product(self, product: Product) -> None:
        """Stage and flush a product parent before dependent offers."""

        session = self._session()
        session.add(product_record(product))
        session.flush()

    def add_offer(self, offer: Offer) -> None:
        """Stage a seller-, SKU-, region-, and time-specific offer."""

        self._session().add(offer_record(offer))

    def add_evidence(self, evidence: Evidence) -> None:
        """Stage one provenance-bearing observation without replacing conflicts."""

        self._session().add(evidence_record(evidence))

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None:
        """Stage offers_collected in the same transaction as its facts."""

        apply_workflow_transition(
            self._session(),
            workflow,
            event,
            expected_revision=expected_revision,
        )

    def commit(self) -> None:
        """Commit converted entities, evidence, and workflow event atomically."""

        self._session().commit()
        self._committed = True

    def _session(self) -> Session:
        if self._active_session is None:
            raise RuntimeError("offer ingestion unit of work is not active")
        return self._active_session
