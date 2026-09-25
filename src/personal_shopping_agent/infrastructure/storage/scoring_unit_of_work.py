"""SQLite work unit for atomic candidate-score persistence."""

from types import TracebackType
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from personal_shopping_agent.application.cross_check import EvidenceCheck
from personal_shopping_agent.application.normalization import NormalizedSpecification
from personal_shopping_agent.application.ranking import CandidateScore
from personal_shopping_agent.application.workflow import (
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
)
from personal_shopping_agent.domain import Evidence, EvidenceSubjectType, Offer, Product
from personal_shopping_agent.storage.repository import domain_payload
from personal_shopping_agent.storage.tables import (
    CandidateScoreRecord,
    EvidenceCheckRecord,
    EvidenceRecord,
    NormalizedSpecificationRecord,
    OfferRecord,
    ProductRecord,
)
from personal_shopping_agent.storage.workflow_repository import (
    apply_workflow_transition,
    load_workflow_snapshot,
)


def candidate_score_record(score: CandidateScore) -> CandidateScoreRecord:
    """Map one validated candidate result to its indexed persistence record."""

    return CandidateScoreRecord(
        id=str(score.id),
        request_id=str(score.request_id),
        workflow_id=str(score.workflow_id),
        product_id=str(score.product_id),
        eligible=score.eligible,
        rank=score.rank,
        pareto_front=score.pareto_front,
        final_score=str(score.final_score) if score.final_score is not None else None,
        scored_at=score.scored_at,
        payload=domain_payload(score),
    )


class SQLiteCandidateScoringUnitOfWork:
    """Hold one transaction across scoring inputs, results, and workflow state."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._active_session: Session | None = None
        self._committed = False

    def __enter__(self) -> "SQLiteCandidateScoringUnitOfWork":
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
        """Load Products linked by request-scoped Product evidence."""

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

    def list_offers_for_request_product(
        self,
        request_id: UUID,
        product_id: UUID,
    ) -> tuple[Offer, ...]:
        """Load only offers proven to belong to this request through Offer evidence."""

        offer_ids = (
            select(EvidenceRecord.subject_id)
            .where(
                EvidenceRecord.request_id == str(request_id),
                EvidenceRecord.subject_type == EvidenceSubjectType.OFFER.value,
            )
            .distinct()
        )
        statement = (
            select(OfferRecord)
            .where(
                OfferRecord.product_id == str(product_id),
                OfferRecord.id.in_(offer_ids),
            )
            .order_by(OfferRecord.captured_at, OfferRecord.id)
        )
        records = self._session().scalars(statement).all()
        return tuple(Offer.model_validate(record.payload) for record in records)

    def list_specifications(
        self,
        request_id: UUID,
        workflow_id: UUID,
        product_id: UUID,
    ) -> tuple[NormalizedSpecification, ...]:
        """Load exact-workflow normalized facts in stable field order."""

        statement = (
            select(NormalizedSpecificationRecord)
            .where(
                NormalizedSpecificationRecord.request_id == str(request_id),
                NormalizedSpecificationRecord.workflow_id == str(workflow_id),
                NormalizedSpecificationRecord.product_id == str(product_id),
            )
            .order_by(
                NormalizedSpecificationRecord.canonical_key,
                NormalizedSpecificationRecord.id,
            )
        )
        records = self._session().scalars(statement).all()
        return tuple(NormalizedSpecification.model_validate(record.payload) for record in records)

    def list_product_evidence(
        self,
        request_id: UUID,
        product_id: UUID,
    ) -> tuple[Evidence, ...]:
        """Load request-linked Product evidence in observation order."""

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

    def list_evidence_checks(
        self,
        request_id: UUID,
        workflow_id: UUID,
        product_id: UUID,
    ) -> tuple[EvidenceCheck, ...]:
        """Load exact-workflow field checks in stable field order."""

        statement = (
            select(EvidenceCheckRecord)
            .where(
                EvidenceCheckRecord.request_id == str(request_id),
                EvidenceCheckRecord.workflow_id == str(workflow_id),
                EvidenceCheckRecord.product_id == str(product_id),
            )
            .order_by(EvidenceCheckRecord.field_path, EvidenceCheckRecord.id)
        )
        records = self._session().scalars(statement).all()
        return tuple(EvidenceCheck.model_validate(record.payload) for record in records)

    def add_score(self, score: CandidateScore) -> None:
        """Stage one candidate score without changing any source fact."""

        self._session().add(candidate_score_record(score))

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None:
        """Stage candidates_scored in the same transaction as candidate results."""

        apply_workflow_transition(
            self._session(),
            workflow,
            event,
            expected_revision=expected_revision,
        )

    def commit(self) -> None:
        """Commit candidate scores and workflow event atomically."""

        self._session().commit()
        self._committed = True

    def _session(self) -> Session:
        if self._active_session is None:
            raise RuntimeError("candidate scoring unit of work is not active")
        return self._active_session
