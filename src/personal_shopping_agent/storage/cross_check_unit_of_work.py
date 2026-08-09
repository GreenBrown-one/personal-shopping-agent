"""SQLite work unit for atomic official evidence cross-checking."""

from types import TracebackType
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from personal_shopping_agent.application.cross_check import EvidenceCheck
from personal_shopping_agent.application.workflow import (
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
)
from personal_shopping_agent.domain import Evidence, EvidenceSubjectType, Product
from personal_shopping_agent.storage.repository import domain_payload, evidence_record
from personal_shopping_agent.storage.tables import (
    EvidenceCheckRecord,
    EvidenceRecord,
    ProductRecord,
)
from personal_shopping_agent.storage.workflow_repository import (
    apply_workflow_transition,
    load_workflow_snapshot,
)


def evidence_check_record(check: EvidenceCheck) -> EvidenceCheckRecord:
    """Map one validated field comparison to an indexed persistence record."""

    return EvidenceCheckRecord(
        id=str(check.id),
        request_id=str(check.request_id),
        workflow_id=str(check.workflow_id),
        product_id=str(check.product_id),
        field_path=check.field_path,
        status=check.status.value,
        checked_at=check.checked_at,
        payload=domain_payload(check),
    )


class SQLiteEvidenceCrossCheckUnitOfWork:
    """Hold one transaction across official evidence, checks, and workflow state."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._active_session: Session | None = None
        self._committed = False

    def __enter__(self) -> "SQLiteEvidenceCrossCheckUnitOfWork":
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

    def list_products_for_request(self, request_id: UUID) -> tuple[Product, ...]:
        """Load Products linked by request-scoped platform evidence."""

        product_ids = (
            select(EvidenceRecord.subject_id)
            .where(
                EvidenceRecord.request_id == str(request_id),
                EvidenceRecord.subject_type == EvidenceSubjectType.PRODUCT.value,
            )
            .distinct()
        )
        statement = (
            select(ProductRecord)
            .where(ProductRecord.id.in_(product_ids))
            .order_by(ProductRecord.id)
        )
        records = self._session().scalars(statement).all()
        return tuple(Product.model_validate(record.payload) for record in records)

    def list_product_evidence(
        self,
        request_id: UUID,
        product_id: UUID,
    ) -> tuple[Evidence, ...]:
        """Load request-linked product evidence in deterministic observation order."""

        statement = (
            select(EvidenceRecord)
            .where(
                EvidenceRecord.request_id == str(request_id),
                EvidenceRecord.subject_type == EvidenceSubjectType.PRODUCT.value,
                EvidenceRecord.subject_id == str(product_id),
            )
            .order_by(EvidenceRecord.captured_at, EvidenceRecord.id)
        )
        records = self._session().scalars(statement).all()
        return tuple(Evidence.model_validate(record.payload) for record in records)

    def add_evidence(self, evidence: Evidence) -> None:
        """Stage one manufacturer-controlled observation."""

        self._session().add(evidence_record(evidence))

    def add_check(self, check: EvidenceCheck) -> None:
        """Stage one append-only field comparison result."""

        self._session().add(evidence_check_record(check))

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None:
        """Stage evidence_cross_checked in the same transaction as its evidence."""

        apply_workflow_transition(
            self._session(),
            workflow,
            event,
            expected_revision=expected_revision,
        )

    def commit(self) -> None:
        """Commit official evidence, checks, and workflow event atomically."""

        self._session().commit()
        self._committed = True

    def _session(self) -> Session:
        if self._active_session is None:
            raise RuntimeError("evidence cross-check unit of work is not active")
        return self._active_session
