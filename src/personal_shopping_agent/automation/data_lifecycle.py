"""Validated local export and explicitly confirmed workflow deletion use cases."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field

from personal_shopping_agent.__about__ import __version__
from personal_shopping_agent.application.cross_check import EvidenceCheck
from personal_shopping_agent.application.normalization import NormalizedSpecification
from personal_shopping_agent.application.observations import DetailObservation, SearchObservation
from personal_shopping_agent.application.ranking import CandidateScore
from personal_shopping_agent.application.reporting import RenderedShoppingReport
from personal_shopping_agent.application.workflow import WorkflowSnapshot, workflow_now
from personal_shopping_agent.domain import Evidence, Offer, Product
from personal_shopping_agent.serialization import JsonContractModel

ARCHIVE_FORMAT = "personal-shopping-agent.workflow-data.v1"
EXPORT_PRIVACY_WARNING = (
    "The export can contain the shopping query, region, seller names, source URLs, and report "
    "content. Keep the file private."
)
DELETION_WARNING = (
    "Deletion is irreversible. Export first if the workflow may be needed later. Shared product "
    "or offer facts are retained when another local workflow may still reference them."
)


class DataLifecycleModel(JsonContractModel):
    """Strict immutable base for local data lifecycle contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class WorkflowDataCounts(DataLifecycleModel):
    """Explicit record counts for one workflow archive or deletion plan."""

    shopping_requests: int = Field(ge=0)
    shopping_workflows: int = Field(ge=0)
    workflow_events: int = Field(ge=0)
    platform_observations: int = Field(ge=0)
    evidence_items: int = Field(ge=0)
    evidence_checks: int = Field(ge=0)
    normalized_specifications: int = Field(ge=0)
    candidate_scores: int = Field(ge=0)
    shopping_reports: int = Field(ge=0)
    products: int = Field(ge=0)
    offers: int = Field(ge=0)

    @property
    def total(self) -> int:
        """Return the sum without adding a second serialized source of truth."""

        return sum(self.model_dump().values())


class WorkflowDataSnapshot(DataLifecycleModel):
    """All validated records linked to one local shopping workflow."""

    snapshot: WorkflowSnapshot
    search_observations: tuple[SearchObservation, ...] = ()
    detail_observations: tuple[DetailObservation, ...] = ()
    products: tuple[Product, ...] = ()
    offers: tuple[Offer, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    evidence_checks: tuple[EvidenceCheck, ...] = ()
    normalized_specifications: tuple[NormalizedSpecification, ...] = ()
    candidate_scores: tuple[CandidateScore, ...] = ()
    reports: tuple[RenderedShoppingReport, ...] = ()

    def counts(
        self, *, products: int | None = None, offers: int | None = None
    ) -> WorkflowDataCounts:
        """Count archive records, optionally substituting reclaimable catalog counts."""

        return WorkflowDataCounts(
            shopping_requests=1,
            shopping_workflows=1,
            workflow_events=len(self.snapshot.events),
            platform_observations=(len(self.search_observations) + len(self.detail_observations)),
            evidence_items=len(self.evidence),
            evidence_checks=len(self.evidence_checks),
            normalized_specifications=len(self.normalized_specifications),
            candidate_scores=len(self.candidate_scores),
            shopping_reports=len(self.reports),
            products=len(self.products) if products is None else products,
            offers=len(self.offers) if offers is None else offers,
        )


class WorkflowDataArchive(DataLifecycleModel):
    """Portable JSON envelope with package and export-time metadata."""

    archive_format: Literal["personal-shopping-agent.workflow-data.v1"] = ARCHIVE_FORMAT
    package_version: str = __version__
    exported_at: AwareDatetime
    data: WorkflowDataSnapshot


class ArchiveWriteReceipt(DataLifecycleModel):
    """Non-sensitive integrity result from an exclusive local file write."""

    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes_written: int = Field(gt=0)


class WorkflowDataExportResult(DataLifecycleModel):
    """CLI-safe export result that does not echo private archive contents."""

    workflow_id: UUID
    record_counts: WorkflowDataCounts
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes_written: int = Field(gt=0)
    privacy_warning: str = EXPORT_PRIVACY_WARNING


class WorkflowDeletionPlan(DataLifecycleModel):
    """Fresh preview required before a destructive workflow deletion."""

    workflow_id: UUID
    request_id: UUID
    workflow_revision: int = Field(ge=0)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation_token: str = Field(min_length=1)
    records_to_delete: WorkflowDataCounts
    retained_shared_products: int = Field(ge=0)
    retained_shared_offers: int = Field(ge=0)
    warning: str = DELETION_WARNING


class WorkflowDeletionResult(DataLifecycleModel):
    """Receipt returned only after the exact previewed plan commits."""

    deleted: Literal[True] = True
    plan: WorkflowDeletionPlan


class WorkflowDataIntegrityError(RuntimeError):
    """Raised when stored records cannot form one validated workflow archive."""


class WorkflowDataOwnershipError(RuntimeError):
    """Raised when one request unexpectedly owns multiple workflows."""


class WorkflowDeletionConfirmationError(ValueError):
    """Raised when destructive confirmation does not match the fresh plan."""


class WorkflowDeletionPlanStaleError(RuntimeError):
    """Raised when local data changes after its deletion preview."""


class ArchiveTargetExistsError(FileExistsError):
    """Raised instead of overwriting an existing export file."""


class ArchiveWriteError(OSError):
    """Sanitized failure raised when an archive cannot be written completely."""


class WorkflowDataStore(Protocol):
    """Persistence port for validated snapshots and atomic deletion."""

    def load(self, workflow_id: UUID) -> WorkflowDataSnapshot: ...

    def prepare_deletion(self, workflow_id: UUID) -> WorkflowDeletionPlan: ...

    def delete(
        self,
        workflow_id: UUID,
        *,
        expected_fingerprint: str,
    ) -> WorkflowDeletionPlan: ...


class WorkflowArchiveWriter(Protocol):
    """Exclusive file-output port kept outside the deterministic archive model."""

    def write(self, archive: WorkflowDataArchive, output_path: Path) -> ArchiveWriteReceipt: ...


def deletion_confirmation_token(workflow_id: UUID, fingerprint: str) -> str:
    """Bind confirmation to both the exact workflow and the fresh record manifest."""

    return f"delete-{workflow_id}-{fingerprint[:12]}"


class WorkflowDataLifecycleService:
    """Coordinate privacy-aware export and explicitly confirmed local deletion."""

    def __init__(
        self,
        store: WorkflowDataStore,
        archive_writer: WorkflowArchiveWriter,
        *,
        clock: Callable[[], datetime] = workflow_now,
    ) -> None:
        self._store = store
        self._archive_writer = archive_writer
        self._clock = clock

    def export(self, workflow_id: UUID, output_path: Path) -> WorkflowDataExportResult:
        """Write a new private JSON archive without replacing any existing path."""

        data = self._store.load(workflow_id)
        archive = WorkflowDataArchive(exported_at=self._clock(), data=data)
        receipt = self._archive_writer.write(archive, output_path)
        return WorkflowDataExportResult(
            workflow_id=workflow_id,
            record_counts=data.counts(),
            content_sha256=receipt.content_sha256,
            bytes_written=receipt.bytes_written,
        )

    def prepare_deletion(self, workflow_id: UUID) -> WorkflowDeletionPlan:
        """Return a non-destructive preview and its target-specific confirmation token."""

        return self._store.prepare_deletion(workflow_id)

    def delete(self, workflow_id: UUID, confirmation_token: str) -> WorkflowDeletionResult:
        """Delete only when confirmation matches a freshly calculated manifest."""

        plan = self._store.prepare_deletion(workflow_id)
        if not hmac.compare_digest(confirmation_token, plan.confirmation_token):
            raise WorkflowDeletionConfirmationError(
                "deletion confirmation does not match the current workflow plan"
            )
        committed_plan = self._store.delete(
            workflow_id,
            expected_fingerprint=plan.fingerprint,
        )
        return WorkflowDeletionResult(plan=committed_plan)
