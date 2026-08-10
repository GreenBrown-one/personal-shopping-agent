"""SQLite adapter for validated workflow export and conservative atomic deletion."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from personal_shopping_agent.application.cross_check import EvidenceCheck
from personal_shopping_agent.application.data_lifecycle import (
    WorkflowDataIntegrityError,
    WorkflowDataOwnershipError,
    WorkflowDataSnapshot,
    WorkflowDeletionPlan,
    WorkflowDeletionPlanStaleError,
    deletion_confirmation_token,
)
from personal_shopping_agent.application.normalization import NormalizedSpecification
from personal_shopping_agent.application.observations import (
    DetailObservation,
    PlatformObservationKind,
    SearchObservation,
)
from personal_shopping_agent.application.ranking import CandidateScore
from personal_shopping_agent.application.reporting import RenderedShoppingReport
from personal_shopping_agent.domain import Evidence, EvidenceSubjectType, Offer, Product
from personal_shopping_agent.storage.repository import EntityNotFoundError
from personal_shopping_agent.storage.tables import (
    CandidateScoreRecord,
    EvidenceCheckRecord,
    EvidenceRecord,
    NormalizedSpecificationRecord,
    OfferRecord,
    PlatformObservationRecord,
    ProductRecord,
    ShoppingReportRecord,
    ShoppingRequestRecord,
    WorkflowRecord,
)
from personal_shopping_agent.storage.workflow_repository import load_workflow_snapshot


@dataclass(frozen=True, slots=True)
class _DeletionSelection:
    plan: WorkflowDeletionPlan
    request_id: str
    offer_ids: tuple[str, ...]
    product_ids: tuple[str, ...]


class _HasId(Protocol):
    @property
    def id(self) -> UUID: ...


def _model_ids(models: Sequence[_HasId]) -> tuple[str, ...]:
    return tuple(str(model.id) for model in models)


def _score_offer_ids(score: CandidateScore) -> set[UUID]:
    return {assessment.offer_id for assessment in score.foundation.offer_costs}


def _report_references(report: RenderedShoppingReport) -> tuple[set[UUID], set[UUID]]:
    product_ids = {candidate.product.id for candidate in report.report.candidates}
    offer_ids = {
        candidate.offer.id for candidate in report.report.candidates if candidate.offer is not None
    }
    for candidate in report.report.candidates:
        offer_ids.update(_score_offer_ids(candidate.score))
    return product_ids, offer_ids


class SQLiteWorkflowDataStore:
    """Read typed workflow archives and delete only unshared linked catalog rows."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def load(self, workflow_id: UUID) -> WorkflowDataSnapshot:
        """Load every request/workflow record and revalidate each stored payload."""

        with self._session_factory() as session:
            return self._load_validated(session, workflow_id)

    def prepare_deletion(self, workflow_id: UUID) -> WorkflowDeletionPlan:
        """Calculate a fresh non-destructive manifest and conservative reclamation set."""

        with self._session_factory() as session:
            return self._deletion_selection(session, workflow_id).plan

    def delete(
        self,
        workflow_id: UUID,
        *,
        expected_fingerprint: str,
    ) -> WorkflowDeletionPlan:
        """Lock, recalculate, and atomically delete only the exact confirmed manifest."""

        session = self._session_factory()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            selection = self._deletion_selection(session, workflow_id)
            if selection.plan.fingerprint != expected_fingerprint:
                raise WorkflowDeletionPlanStaleError(
                    "workflow data changed after the deletion preview"
                )

            request_result = cast(
                CursorResult[Any],
                session.execute(
                    delete(ShoppingRequestRecord).where(
                        ShoppingRequestRecord.id == selection.request_id
                    )
                ),
            )
            if request_result.rowcount != 1:  # pragma: no cover - guarded by the write lock
                raise WorkflowDeletionPlanStaleError("workflow request is no longer present")
            if selection.offer_ids:
                offer_result = cast(
                    CursorResult[Any],
                    session.execute(
                        delete(OfferRecord).where(OfferRecord.id.in_(selection.offer_ids))
                    ),
                )
                if offer_result.rowcount != len(  # pragma: no cover - guarded by the write lock
                    selection.offer_ids
                ):
                    raise WorkflowDeletionPlanStaleError("workflow offers changed during deletion")
            if selection.product_ids:
                product_result = cast(
                    CursorResult[Any],
                    session.execute(
                        delete(ProductRecord).where(ProductRecord.id.in_(selection.product_ids))
                    ),
                )
                if product_result.rowcount != len(  # pragma: no cover - guarded by the write lock
                    selection.product_ids
                ):
                    raise WorkflowDeletionPlanStaleError(
                        "workflow products changed during deletion"
                    )
            session.commit()
            return selection.plan
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _load_validated(self, session: Session, workflow_id: UUID) -> WorkflowDataSnapshot:
        workflow_record = session.get(WorkflowRecord, str(workflow_id))
        if workflow_record is None:
            raise EntityNotFoundError("shopping_workflows entity was not found")
        sibling_count = session.scalar(
            select(func.count())
            .select_from(WorkflowRecord)
            .where(WorkflowRecord.request_id == workflow_record.request_id)
        )
        if sibling_count != 1:
            raise WorkflowDataOwnershipError(
                "a shopping request must own exactly one workflow for lifecycle operations"
            )

        request_id = workflow_record.request_id
        try:
            snapshot = load_workflow_snapshot(session, workflow_id)
            observation_records = session.scalars(
                select(PlatformObservationRecord)
                .where(PlatformObservationRecord.request_id == request_id)
                .order_by(
                    PlatformObservationRecord.captured_at,
                    PlatformObservationRecord.id,
                )
            ).all()
            search_observations: list[SearchObservation] = []
            detail_observations: list[DetailObservation] = []
            for record in observation_records:
                if record.kind == PlatformObservationKind.SEARCH.value:
                    search_observations.append(SearchObservation.model_validate(record.payload))
                elif record.kind == PlatformObservationKind.DETAIL.value:
                    detail_observations.append(DetailObservation.model_validate(record.payload))
                else:
                    raise WorkflowDataIntegrityError(
                        "stored platform observation kind is unsupported"
                    )

            evidence_records = session.scalars(
                select(EvidenceRecord)
                .where(EvidenceRecord.request_id == request_id)
                .order_by(EvidenceRecord.captured_at, EvidenceRecord.id)
            ).all()
            evidence = tuple(Evidence.model_validate(record.payload) for record in evidence_records)

            check_records = session.scalars(
                select(EvidenceCheckRecord)
                .where(
                    EvidenceCheckRecord.request_id == request_id,
                    EvidenceCheckRecord.workflow_id == str(workflow_id),
                )
                .order_by(EvidenceCheckRecord.field_path, EvidenceCheckRecord.id)
            ).all()
            evidence_checks = tuple(
                EvidenceCheck.model_validate(record.payload) for record in check_records
            )

            specification_records = session.scalars(
                select(NormalizedSpecificationRecord)
                .where(
                    NormalizedSpecificationRecord.request_id == request_id,
                    NormalizedSpecificationRecord.workflow_id == str(workflow_id),
                )
                .order_by(
                    NormalizedSpecificationRecord.canonical_key,
                    NormalizedSpecificationRecord.id,
                )
            ).all()
            specifications = tuple(
                NormalizedSpecification.model_validate(record.payload)
                for record in specification_records
            )

            score_records = session.scalars(
                select(CandidateScoreRecord)
                .where(
                    CandidateScoreRecord.request_id == request_id,
                    CandidateScoreRecord.workflow_id == str(workflow_id),
                )
                .order_by(CandidateScoreRecord.product_id, CandidateScoreRecord.id)
            ).all()
            scores = tuple(
                CandidateScore.model_validate(record.payload) for record in score_records
            )

            report_records = session.scalars(
                select(ShoppingReportRecord)
                .where(
                    ShoppingReportRecord.request_id == request_id,
                    ShoppingReportRecord.workflow_id == str(workflow_id),
                )
                .order_by(ShoppingReportRecord.id)
            ).all()
            reports = tuple(
                RenderedShoppingReport.model_validate(record.payload) for record in report_records
            )

            product_ids, offer_ids = self._linked_entity_ids(
                evidence,
                evidence_checks,
                specifications,
                scores,
                reports,
            )
            offers = self._load_offers(session, offer_ids)
            product_ids.update(offer.product_id for offer in offers)
            products = self._load_products(session, product_ids)

            data = WorkflowDataSnapshot(
                snapshot=snapshot,
                search_observations=tuple(search_observations),
                detail_observations=tuple(detail_observations),
                products=products,
                offers=offers,
                evidence=evidence,
                evidence_checks=evidence_checks,
                normalized_specifications=specifications,
                candidate_scores=scores,
                reports=reports,
            )
            self._require_scope(data)
            return data
        except ValidationError as error:
            raise WorkflowDataIntegrityError(
                "stored workflow data failed archive validation"
            ) from error

    @staticmethod
    def _linked_entity_ids(
        evidence: tuple[Evidence, ...],
        checks: tuple[EvidenceCheck, ...],
        specifications: tuple[NormalizedSpecification, ...],
        scores: tuple[CandidateScore, ...],
        reports: tuple[RenderedShoppingReport, ...],
    ) -> tuple[set[UUID], set[UUID]]:
        product_ids = {
            item.subject_id for item in evidence if item.subject_type is EvidenceSubjectType.PRODUCT
        }
        offer_ids = {
            item.subject_id for item in evidence if item.subject_type is EvidenceSubjectType.OFFER
        }
        product_ids.update(item.product_id for item in checks)
        product_ids.update(item.product_id for item in specifications)
        for score in scores:
            product_ids.add(score.product_id)
            offer_ids.update(_score_offer_ids(score))
        for report in reports:
            report_products, report_offers = _report_references(report)
            product_ids.update(report_products)
            offer_ids.update(report_offers)
        return product_ids, offer_ids

    @staticmethod
    def _load_offers(session: Session, offer_ids: set[UUID]) -> tuple[Offer, ...]:
        if not offer_ids:
            return ()
        records = session.scalars(
            select(OfferRecord)
            .where(OfferRecord.id.in_(str(item) for item in offer_ids))
            .order_by(OfferRecord.captured_at, OfferRecord.id)
        ).all()
        if len(records) != len(offer_ids):
            raise WorkflowDataIntegrityError("a workflow-linked offer is missing")
        return tuple(Offer.model_validate(record.payload) for record in records)

    @staticmethod
    def _load_products(session: Session, product_ids: set[UUID]) -> tuple[Product, ...]:
        if not product_ids:
            return ()
        records = session.scalars(
            select(ProductRecord)
            .where(ProductRecord.id.in_(str(item) for item in product_ids))
            .order_by(ProductRecord.id)
        ).all()
        if len(records) != len(product_ids):
            raise WorkflowDataIntegrityError("a workflow-linked product is missing")
        return tuple(Product.model_validate(record.payload) for record in records)

    @staticmethod
    def _require_scope(data: WorkflowDataSnapshot) -> None:
        request_id = data.snapshot.request.id
        workflow_id = data.snapshot.workflow.id
        if data.snapshot.workflow.request_id != request_id:
            raise WorkflowDataIntegrityError("workflow request scope is inconsistent")
        if any(
            item.request_id != request_id
            for item in (*data.search_observations, *data.detail_observations)
        ):
            raise WorkflowDataIntegrityError("platform observation request scope is inconsistent")
        if any(item.request_id != request_id for item in data.evidence):
            raise WorkflowDataIntegrityError("evidence request scope is inconsistent")
        if any(
            item.request_id != request_id or item.workflow_id != workflow_id
            for item in (
                *data.evidence_checks,
                *data.normalized_specifications,
                *data.candidate_scores,
            )
        ):
            raise WorkflowDataIntegrityError("derived workflow scope is inconsistent")
        if any(
            item.report.request.id != request_id or item.report.workflow_id != workflow_id
            for item in data.reports
        ):
            raise WorkflowDataIntegrityError("report workflow scope is inconsistent")

    def _deletion_selection(
        self,
        session: Session,
        workflow_id: UUID,
    ) -> _DeletionSelection:
        data = self._load_validated(session, workflow_id)
        request_id = str(data.snapshot.request.id)
        target_product_ids = {str(item.id) for item in data.products}
        target_offer_ids = {str(item.id) for item in data.offers}
        try:
            external_product_ids, external_offer_ids = self._external_references(
                session,
                request_id,
            )
        except ValidationError as error:
            raise WorkflowDataIntegrityError(
                "stored shared catalog references failed validation"
            ) from error

        reclaimable_offer_ids = target_offer_ids - external_offer_ids
        offers_by_product: dict[str, set[str]] = {item: set() for item in target_product_ids}
        if target_product_ids:
            all_product_offers = session.execute(
                select(OfferRecord.product_id, OfferRecord.id).where(
                    OfferRecord.product_id.in_(target_product_ids)
                )
            ).all()
            for product_id, offer_id in all_product_offers:
                offers_by_product[product_id].add(offer_id)
        reclaimable_product_ids = {
            product_id
            for product_id, product_offer_ids in offers_by_product.items()
            if product_id not in external_product_ids
            and product_offer_ids.issubset(reclaimable_offer_ids)
        }

        fingerprint_payload = {
            "request_id": request_id,
            "workflow_id": str(workflow_id),
            "workflow_revision": data.snapshot.workflow.revision,
            "rows": {
                "workflow_events": _model_ids(data.snapshot.events),
                "search_observations": _model_ids(data.search_observations),
                "detail_observations": _model_ids(data.detail_observations),
                "evidence": _model_ids(data.evidence),
                "evidence_checks": _model_ids(data.evidence_checks),
                "normalized_specifications": _model_ids(data.normalized_specifications),
                "candidate_scores": _model_ids(data.candidate_scores),
                "reports": tuple(str(item.report.id) for item in data.reports),
                "linked_products": tuple(sorted(target_product_ids)),
                "linked_offers": tuple(sorted(target_offer_ids)),
                "reclaimable_products": tuple(sorted(reclaimable_product_ids)),
                "reclaimable_offers": tuple(sorted(reclaimable_offer_ids)),
            },
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        plan = WorkflowDeletionPlan(
            workflow_id=workflow_id,
            request_id=data.snapshot.request.id,
            workflow_revision=data.snapshot.workflow.revision,
            fingerprint=fingerprint,
            confirmation_token=deletion_confirmation_token(workflow_id, fingerprint),
            records_to_delete=data.counts(
                products=len(reclaimable_product_ids),
                offers=len(reclaimable_offer_ids),
            ),
            retained_shared_products=len(target_product_ids - reclaimable_product_ids),
            retained_shared_offers=len(target_offer_ids - reclaimable_offer_ids),
        )
        return _DeletionSelection(
            plan=plan,
            request_id=request_id,
            offer_ids=tuple(sorted(reclaimable_offer_ids)),
            product_ids=tuple(sorted(reclaimable_product_ids)),
        )

    @staticmethod
    def _external_references(session: Session, request_id: str) -> tuple[set[str], set[str]]:
        product_ids: set[str] = set()
        offer_ids: set[str] = set()
        evidence_records = session.scalars(
            select(EvidenceRecord).where(
                or_(EvidenceRecord.request_id != request_id, EvidenceRecord.request_id.is_(None))
            )
        ).all()
        for record in evidence_records:
            evidence = Evidence.model_validate(record.payload)
            if evidence.subject_type is EvidenceSubjectType.PRODUCT:
                product_ids.add(str(evidence.subject_id))
            elif evidence.subject_type is EvidenceSubjectType.OFFER:
                offer_ids.add(str(evidence.subject_id))

        product_ids.update(
            session.scalars(
                select(EvidenceCheckRecord.product_id).where(
                    EvidenceCheckRecord.request_id != request_id
                )
            ).all()
        )
        product_ids.update(
            session.scalars(
                select(NormalizedSpecificationRecord.product_id).where(
                    NormalizedSpecificationRecord.request_id != request_id
                )
            ).all()
        )
        score_records = session.scalars(
            select(CandidateScoreRecord).where(CandidateScoreRecord.request_id != request_id)
        ).all()
        for record in score_records:
            score = CandidateScore.model_validate(record.payload)
            product_ids.add(str(score.product_id))
            offer_ids.update(str(item) for item in _score_offer_ids(score))

        report_records = session.scalars(
            select(ShoppingReportRecord).where(ShoppingReportRecord.request_id != request_id)
        ).all()
        for record in report_records:
            report = RenderedShoppingReport.model_validate(record.payload)
            report_products, report_offers = _report_references(report)
            product_ids.update(str(item) for item in report_products)
            offer_ids.update(str(item) for item in report_offers)
        return product_ids, offer_ids
