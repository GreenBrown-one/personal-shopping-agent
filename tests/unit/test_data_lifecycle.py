"""Unit tests for private archive and explicit deletion application boundaries."""

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from personal_shopping_agent import __version__
from personal_shopping_agent.application import (
    ARCHIVE_FORMAT,
    ArchiveTargetExistsError,
    ArchiveWriteError,
    ArchiveWriteReceipt,
    WorkflowDataArchive,
    WorkflowDataCounts,
    WorkflowDataLifecycleService,
    WorkflowDataSnapshot,
    WorkflowDeletionConfirmationError,
    WorkflowDeletionPlan,
    WorkflowStateMachine,
    deletion_confirmation_token,
)
from personal_shopping_agent.application.workflow import WorkflowSnapshot
from personal_shopping_agent.domain import Budget, Money, ShoppingRequest
from personal_shopping_agent.storage import LocalJsonWorkflowArchiveWriter

NOW = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)


def build_data() -> WorkflowDataSnapshot:
    request = ShoppingRequest(
        query="private test query",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        region="private test region",
        created_at=NOW,
    )
    workflow, events = WorkflowStateMachine().initialize(request.id, occurred_at=NOW)
    return WorkflowDataSnapshot(
        snapshot=WorkflowSnapshot(request=request, workflow=workflow, events=events)
    )


def build_plan(data: WorkflowDataSnapshot) -> WorkflowDeletionPlan:
    fingerprint = "a" * 64
    workflow = data.snapshot.workflow
    return WorkflowDeletionPlan(
        workflow_id=workflow.id,
        request_id=data.snapshot.request.id,
        workflow_revision=workflow.revision,
        fingerprint=fingerprint,
        confirmation_token=deletion_confirmation_token(workflow.id, fingerprint),
        records_to_delete=data.counts(products=0, offers=0),
        retained_shared_products=0,
        retained_shared_offers=0,
    )


class FakeDataStore:
    def __init__(self, data: WorkflowDataSnapshot, plan: WorkflowDeletionPlan) -> None:
        self.data = data
        self.plan = plan
        self.deleted: tuple[UUID, str] | None = None

    def load(self, workflow_id: UUID) -> WorkflowDataSnapshot:
        assert workflow_id == self.data.snapshot.workflow.id
        return self.data

    def prepare_deletion(self, workflow_id: UUID) -> WorkflowDeletionPlan:
        assert workflow_id == self.plan.workflow_id
        return self.plan

    def delete(
        self,
        workflow_id: UUID,
        *,
        expected_fingerprint: str,
    ) -> WorkflowDeletionPlan:
        self.deleted = (workflow_id, expected_fingerprint)
        return self.plan


class CapturingArchiveWriter:
    def __init__(self) -> None:
        self.archive: WorkflowDataArchive | None = None
        self.output_path: Path | None = None

    def write(self, archive: WorkflowDataArchive, output_path: Path) -> ArchiveWriteReceipt:
        self.archive = archive
        self.output_path = output_path
        return ArchiveWriteReceipt(content_sha256="b" * 64, bytes_written=123)


def test_counts_and_lifecycle_service_keep_preview_and_execution_separate(tmp_path: Path) -> None:
    data = build_data()
    plan = build_plan(data)
    store = FakeDataStore(data, plan)
    writer = CapturingArchiveWriter()
    service = WorkflowDataLifecycleService(store, writer, clock=lambda: NOW)
    output_path = tmp_path / "archive.json"

    counts = data.counts()
    assert counts == WorkflowDataCounts(
        shopping_requests=1,
        shopping_workflows=1,
        workflow_events=2,
        platform_observations=0,
        evidence_items=0,
        evidence_checks=0,
        normalized_specifications=0,
        candidate_scores=0,
        shopping_reports=0,
        products=0,
        offers=0,
    )
    assert counts.total == 4
    assert data.counts(products=3, offers=4).total == 11

    exported = service.export(data.snapshot.workflow.id, output_path)
    assert exported.workflow_id == data.snapshot.workflow.id
    assert exported.record_counts == counts
    assert exported.content_sha256 == "b" * 64
    assert exported.bytes_written == 123
    assert writer.output_path == output_path
    assert writer.archive == WorkflowDataArchive(exported_at=NOW, data=data)
    captured_archive = writer.archive
    assert captured_archive is not None
    assert captured_archive.archive_format == ARCHIVE_FORMAT
    assert captured_archive.package_version == __version__

    assert service.prepare_deletion(plan.workflow_id) == plan
    with pytest.raises(WorkflowDeletionConfirmationError, match="does not match"):
        service.delete(plan.workflow_id, "wrong-token")
    assert store.deleted is None

    deleted = service.delete(plan.workflow_id, plan.confirmation_token)
    assert deleted.deleted is True
    assert deleted.plan == plan
    assert store.deleted == (plan.workflow_id, plan.fingerprint)


def test_local_archive_writer_is_private_integrity_checked_and_never_overwrites(
    tmp_path: Path,
) -> None:
    archive = WorkflowDataArchive(exported_at=NOW, data=build_data())
    output_path = tmp_path / "workflow.json"
    writer = LocalJsonWorkflowArchiveWriter()

    receipt = writer.write(archive, output_path)
    content = output_path.read_bytes()

    assert receipt.bytes_written == len(content)
    assert receipt.content_sha256 == hashlib.sha256(content).hexdigest()
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    payload = json.loads(content)
    assert payload["archive_format"] == ARCHIVE_FORMAT
    assert payload["data"]["snapshot"]["request"]["query"] == "private test query"

    with pytest.raises(ArchiveTargetExistsError, match="already exists"):
        writer.write(archive, output_path)
    assert output_path.read_bytes() == content


def test_local_archive_writer_sanitizes_failures_and_removes_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = WorkflowDataArchive(exported_at=NOW, data=build_data())
    writer = LocalJsonWorkflowArchiveWriter()
    missing_parent = tmp_path / "missing" / "workflow.json"

    with pytest.raises(ArchiveWriteError, match="could not be written"):
        writer.write(archive, missing_parent)
    assert not missing_parent.exists()

    output_path = tmp_path / "partial.json"

    def fail_fsync(_: int) -> None:
        raise OSError("sensitive disk error")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(ArchiveWriteError, match="could not be written"):
        writer.write(archive, output_path)
    assert not output_path.exists()
