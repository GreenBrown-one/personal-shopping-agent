"""SQLite work unit for atomic normalized specification persistence."""

from types import TracebackType
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from personal_shopping_agent.domain import Evidence, EvidenceSubjectType, Product
from personal_shopping_agent.domain.workflow import (
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
)
from personal_shopping_agent.infrastructure.storage.repository import domain_payload
from personal_shopping_agent.infrastructure.storage.tables import (
    EvidenceRecord,
    NormalizedSpecificationRecord,
    ProductRecord,
)
from personal_shopping_agent.infrastructure.storage.workflow_repository import (
    apply_workflow_transition,
    load_workflow_snapshot,
)
from personal_shopping_agent.sourcing.normalization import NormalizedSpecification


def normalized_specification_record(
    specification: NormalizedSpecification,
) -> NormalizedSpecificationRecord:
    """Map one validated normalization result to its indexed persistence record."""

    return NormalizedSpecificationRecord(
        id=str(specification.id),
        request_id=str(specification.request_id),
        workflow_id=str(specification.workflow_id),
        product_id=str(specification.product_id),
        canonical_key=specification.canonical_key,
        status=specification.status.value,
        canonical_unit=specification.canonical_unit,
        normalized_at=specification.normalized_at,
        payload=domain_payload(specification),
    )


class SQLiteSpecificationNormalizationUnitOfWork:
    """Hold one transaction across normalized facts and workflow state."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._active_session: Session | None = None
        self._committed = False

    def __enter__(self) -> "SQLiteSpecificationNormalizationUnitOfWork":
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
        """Load products linked to the request by product Evidence."""

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
        """Load only request-linked raw specification Evidence for one product."""

        statement = (
            select(EvidenceRecord)
            .where(
                EvidenceRecord.request_id == str(request_id),
                EvidenceRecord.subject_type == EvidenceSubjectType.PRODUCT.value,
                EvidenceRecord.subject_id == str(product_id),
                EvidenceRecord.field_path.like("specifications.%"),
            )
            .order_by(EvidenceRecord.captured_at, EvidenceRecord.id)
        )
        records = self._session().scalars(statement).all()
        return tuple(Evidence.model_validate(record.payload) for record in records)

    def add_specification(self, specification: NormalizedSpecification) -> None:
        """Stage one append-only normalized specification result."""

        self._session().add(normalized_specification_record(specification))

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None:
        """Stage data_normalized in the same transaction as normalized facts."""

        apply_workflow_transition(
            self._session(),
            workflow,
            event,
            expected_revision=expected_revision,
        )

    def commit(self) -> None:
        """Commit normalized facts and workflow event atomically."""

        self._session().commit()
        self._committed = True

    def _session(self) -> Session:
        if self._active_session is None:
            raise RuntimeError("specification normalization unit of work is not active")
        return self._active_session
