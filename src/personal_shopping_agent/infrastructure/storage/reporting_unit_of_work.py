"""SQLite work unit for atomic deterministic report persistence."""

from types import TracebackType
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from personal_shopping_agent.domain import Evidence, Offer, Product
from personal_shopping_agent.domain.workflow import (
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
)
from personal_shopping_agent.infrastructure.storage.repository import domain_payload
from personal_shopping_agent.infrastructure.storage.tables import (
    CandidateScoreRecord,
    EvidenceRecord,
    OfferRecord,
    ProductRecord,
    ShoppingReportRecord,
)
from personal_shopping_agent.infrastructure.storage.workflow_repository import (
    apply_workflow_transition,
    load_workflow_snapshot,
)
from personal_shopping_agent.presentation.ranking import CandidateScore
from personal_shopping_agent.presentation.reporting import RenderedShoppingReport


def shopping_report_record(rendered: RenderedShoppingReport) -> ShoppingReportRecord:
    """Map one validated rendered report to indexed content and its full snapshot."""

    return ShoppingReportRecord(
        id=str(rendered.report.id),
        request_id=str(rendered.report.request.id),
        workflow_id=str(rendered.report.workflow_id),
        format=rendered.format.value,
        rendered_at=rendered.rendered_at,
        content_sha256=rendered.content_sha256,
        content=rendered.content,
        payload=domain_payload(rendered),
    )


class SQLiteShoppingReportUnitOfWork:
    """Hold one transaction across report inputs, output, and workflow state."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._active_session: Session | None = None
        self._committed = False

    def __enter__(self) -> "SQLiteShoppingReportUnitOfWork":
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

    def list_scores(self, workflow_id: UUID) -> tuple[CandidateScore, ...]:
        """Load and revalidate all candidate score snapshots for one workflow."""

        records = (
            self._session()
            .scalars(
                select(CandidateScoreRecord)
                .where(CandidateScoreRecord.workflow_id == str(workflow_id))
                .order_by(CandidateScoreRecord.product_id)
            )
            .all()
        )
        return tuple(CandidateScore.model_validate(record.payload) for record in records)

    def list_products(self, product_ids: tuple[UUID, ...]) -> tuple[Product, ...]:
        """Load exactly the Product identities cited by candidate scores."""

        records = (
            self._session()
            .scalars(
                select(ProductRecord)
                .where(ProductRecord.id.in_(str(item) for item in product_ids))
                .order_by(ProductRecord.id)
            )
            .all()
        )
        return tuple(Product.model_validate(record.payload) for record in records)

    def list_offers(self, offer_ids: tuple[UUID, ...]) -> tuple[Offer, ...]:
        """Load exactly the best Offer snapshots cited by candidate scores."""

        records = (
            self._session()
            .scalars(
                select(OfferRecord)
                .where(OfferRecord.id.in_(str(item) for item in offer_ids))
                .order_by(OfferRecord.id)
            )
            .all()
        )
        return tuple(Offer.model_validate(record.payload) for record in records)

    def list_evidence(self, evidence_ids: tuple[UUID, ...]) -> tuple[Evidence, ...]:
        """Load exactly the Product Evidence snapshots used in confidence scoring."""

        records = (
            self._session()
            .scalars(
                select(EvidenceRecord)
                .where(EvidenceRecord.id.in_(str(item) for item in evidence_ids))
                .order_by(EvidenceRecord.captured_at, EvidenceRecord.id)
            )
            .all()
        )
        return tuple(Evidence.model_validate(record.payload) for record in records)

    def add_report(self, rendered: RenderedShoppingReport) -> None:
        """Stage one immutable report without changing scored facts."""

        self._session().add(shopping_report_record(rendered))

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None:
        """Stage report_rendered in the same transaction as the report."""

        apply_workflow_transition(
            self._session(),
            workflow,
            event,
            expected_revision=expected_revision,
        )

    def commit(self) -> None:
        """Commit rendered report and workflow event atomically."""

        self._session().commit()
        self._committed = True

    def _session(self) -> Session:
        if self._active_session is None:
            raise RuntimeError("shopping report unit of work is not active")
        return self._active_session
