"""Integration proof for private workflow export and explicitly confirmed deletion."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import HttpUrl
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

import personal_shopping_agent.interfaces.cli as cli_module
from personal_shopping_agent.automation import (
    ArchiveWriteError,
    ShoppingPipelineOptions,
    ShoppingWorkflowService,
    WorkflowDataIntegrityError,
    WorkflowDataLifecycleService,
    WorkflowDataOwnershipError,
    WorkflowDeletionPlanStaleError,
)
from personal_shopping_agent.automation.data_lifecycle import WorkflowDataArchive
from personal_shopping_agent.domain import (
    Budget,
    Evidence,
    EvidenceSourceType,
    EvidenceSubjectType,
    Money,
    Product,
    ShoppingCriterion,
    ShoppingRequest,
    WorkflowState,
    WorkflowStateMachine,
)
from personal_shopping_agent.infrastructure.storage import (
    DatabaseMigrationError,
    EntityNotFoundError,
    LocalJsonWorkflowArchiveWriter,
    SQLiteOfferIngestionUnitOfWork,
    SQLitePlatformObservationRepository,
    SQLiteShoppingRepository,
    SQLiteWorkflowDataStore,
    SQLiteWorkflowRepository,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
    upgrade_database,
)
from personal_shopping_agent.infrastructure.storage.tables import (
    EvidenceRecord,
    OfferRecord,
    PlatformObservationRecord,
    ProductRecord,
    ShoppingRequestRecord,
    WorkflowEventRecord,
    WorkflowRecord,
)
from personal_shopping_agent.infrastructure.storage.workflow_repository import workflow_record
from personal_shopping_agent.interfaces.cli import main
from personal_shopping_agent.interfaces.composition import (
    NoOfficialEvidenceProvider,
    create_jd_pipeline_service,
)
from personal_shopping_agent.sourcing import (
    DetailObservation,
    OfferIngestionService,
    PlatformCandidate,
    PlatformProductDetail,
    PlatformSearchResult,
    PlatformSpecificationObservation,
    SearchObservation,
)
from personal_shopping_agent.sourcing.browser import BrowserSnapshot

NOW = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SeededWorkflow:
    database_url: str
    workflow_id: UUID
    request_id: UUID
    product_id: UUID
    offer_id: UUID


def _output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def _request(query: str = "private lifecycle phone") -> ShoppingRequest:
    return ShoppingRequest(
        query=query,
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        region="云南省 曲靖市 麒麟区",
        criteria=(
            ShoppingCriterion(
                key="battery_capacity",
                minimum=Decimal("5000"),
                preferred=Decimal("6000"),
                unit="mAh",
            ),
        ),
        created_at=NOW,
    )


def _search(request_id: UUID) -> SearchObservation:
    return SearchObservation(
        request_id=request_id,
        result=PlatformSearchResult(
            platform="jd",
            query="Example Phone",
            source_url=HttpUrl("https://search.jd.com/Search?keyword=Example+Phone"),
            captured_at=NOW,
            candidates=(
                PlatformCandidate(
                    platform="jd",
                    external_id="1000001",
                    title="Example Phone A1",
                    product_url=HttpUrl("https://item.jd.com/1000001.html"),
                    displayed_price=Decimal("3999"),
                ),
            ),
        ),
    )


def _detail(request_id: UUID) -> DetailObservation:
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
            specifications=(
                PlatformSpecificationObservation(key="电池容量", raw_value="6000 mAh"),
            ),
        ),
    )


class UnusedPageCollector:
    async def open(self, url: str, *, screenshot: bool = False) -> BrowserSnapshot:
        del url, screenshot
        raise AssertionError("resumed lifecycle fixture must not access a page")


def _seed_workflow(
    factory: sessionmaker[Session],
    database_url: str,
    *,
    query: str = "private lifecycle phone",
) -> SeededWorkflow:
    workflow_service = ShoppingWorkflowService(
        SQLiteWorkflowRepository(factory),
        clock=lambda: NOW,
    )
    started = workflow_service.start(_request(query))
    observations = SQLitePlatformObservationRepository(factory)
    observations.add_search_observation(_search(started.request.id))
    observations.add_detail_observation(_detail(started.request.id))
    discovered = workflow_service.advance(
        started.workflow.id,
        WorkflowState.CANDIDATES_DISCOVERED,
        reason="fixture_observations_persisted",
    )
    ingested = OfferIngestionService(
        lambda: SQLiteOfferIngestionUnitOfWork(factory),
        clock=lambda: NOW,
    ).ingest(discovered.workflow.id)
    completed = asyncio.run(
        create_jd_pipeline_service(
            factory,
            UnusedPageCollector(),
            NoOfficialEvidenceProvider(),
        ).run(
            ingested.snapshot.workflow.id,
            options=ShoppingPipelineOptions(maximum_candidates=1, maximum_details=1),
        )
    )
    assert completed.snapshot.workflow.state is WorkflowState.COMPLETED
    return SeededWorkflow(
        database_url=database_url,
        workflow_id=completed.snapshot.workflow.id,
        request_id=completed.snapshot.request.id,
        product_id=ingested.batch.products[0].id,
        offer_id=ingested.batch.offers[0].id,
    )


def _seed_database(database_path: Path) -> SeededWorkflow:
    database_url = f"sqlite:///{database_path}"
    assert upgrade_database(database_url).ready is True
    engine = create_sqlite_engine(database_url)
    result = _seed_workflow(create_session_factory(engine), database_url)
    engine.dispose()
    return result


def _service(
    database_url: str,
) -> tuple[Engine, sessionmaker[Session], WorkflowDataLifecycleService]:
    engine = create_sqlite_engine(database_url)
    factory = create_session_factory(engine)
    return (
        engine,
        factory,
        WorkflowDataLifecycleService(
            SQLiteWorkflowDataStore(factory),
            LocalJsonWorkflowArchiveWriter(),
            clock=lambda: NOW,
        ),
    )


def test_cli_exports_previews_and_requires_exact_confirmation_before_deleting(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seeded = _seed_database(tmp_path / "lifecycle.db")
    archive_path = tmp_path / "workflow.json"

    assert (
        main(
            [
                "data",
                "export",
                str(seeded.workflow_id),
                "--output",
                str(archive_path),
                "--database-url",
                seeded.database_url,
            ],
            environment={},
        )
        == 0
    )
    exported = _output(capsys)
    assert exported["command"] == "data_export"
    assert exported["ok"] is True
    assert str(archive_path) not in json.dumps(exported)
    archive_bytes = archive_path.read_bytes()
    archive = WorkflowDataArchive.model_validate_json(archive_bytes)
    assert archive.data.snapshot.workflow.id == seeded.workflow_id
    assert len(archive.data.search_observations) == 1
    assert len(archive.data.detail_observations) == 1
    assert len(archive.data.products) == 1
    assert len(archive.data.offers) == 1
    result = exported["result"]
    assert isinstance(result, dict)
    assert result["content_sha256"] == hashlib.sha256(archive_bytes).hexdigest()

    assert (
        main(
            [
                "data",
                "export",
                str(seeded.workflow_id),
                "--output",
                str(archive_path),
                "--database-url",
                seeded.database_url,
            ],
            environment={},
        )
        == 1
    )
    assert _output(capsys)["error_code"] == "export_target_exists"

    preview_arguments = [
        "data",
        "delete",
        str(seeded.workflow_id),
        "--database-url",
        seeded.database_url,
    ]
    assert main(preview_arguments, environment={}) == 0
    preview = _output(capsys)
    assert preview["executed"] is False
    plan = cast(dict[str, object], preview["plan"])
    records_to_delete = cast(dict[str, object], plan["records_to_delete"])
    assert records_to_delete["shopping_requests"] == 1
    assert records_to_delete["shopping_workflows"] == 1
    assert records_to_delete["platform_observations"] == 2
    assert records_to_delete["products"] == 1
    assert records_to_delete["offers"] == 1
    assert plan["retained_shared_products"] == 0
    assert plan["retained_shared_offers"] == 0
    confirmation = plan["confirmation_token"]
    assert isinstance(confirmation, str)

    assert main([*preview_arguments, "--confirm", "wrong-token"], environment={}) == 2
    assert _output(capsys)["error_code"] == "confirmation_mismatch"
    engine, factory, _ = _service(seeded.database_url)
    assert SQLiteWorkflowRepository(factory).get_snapshot(seeded.workflow_id).workflow.id == (
        seeded.workflow_id
    )
    engine.dispose()

    assert main([*preview_arguments, "--confirm", confirmation], environment={}) == 0
    deleted = _output(capsys)
    assert deleted["executed"] is True
    assert deleted["plan"] == plan

    engine = create_sqlite_engine(seeded.database_url)
    with engine.connect() as connection:
        for table in (
            ShoppingRequestRecord,
            WorkflowRecord,
            WorkflowEventRecord,
            PlatformObservationRecord,
            EvidenceRecord,
            ProductRecord,
            OfferRecord,
        ):
            assert connection.scalar(select(func.count()).select_from(table)) == 0
    engine.dispose()

    assert main(preview_arguments, environment={}) == 1
    assert _output(capsys)["error_code"] == "workflow_not_found"


def test_shared_product_and_offer_are_retained_for_another_request(tmp_path: Path) -> None:
    seeded = _seed_database(tmp_path / "shared.db")
    engine, factory, service = _service(seeded.database_url)
    second = _seed_workflow(factory, seeded.database_url, query="second workflow")
    repository = SQLiteShoppingRepository(factory)
    for subject_type, subject_id, field_path in (
        (EvidenceSubjectType.PRODUCT, seeded.product_id, "model"),
        (EvidenceSubjectType.OFFER, seeded.offer_id, "price.displayed_price"),
        (EvidenceSubjectType.SPECIFICATION, uuid4(), "normalized_value"),
    ):
        repository.add_evidence(
            Evidence(
                request_id=second.request_id,
                subject_type=subject_type,
                subject_id=subject_id,
                field_path=field_path,
                source_type=EvidenceSourceType.PLATFORM_LISTING,
                source_url=HttpUrl("https://item.jd.com/1000001.html"),
                source_title="Shared fixture",
                captured_at=NOW,
                observed_value="shared",
            )
        )

    plan = service.prepare_deletion(seeded.workflow_id)
    assert plan.records_to_delete.products == 0
    assert plan.records_to_delete.offers == 0
    assert plan.retained_shared_products == 1
    assert plan.retained_shared_offers == 1

    service.delete(seeded.workflow_id, plan.confirmation_token)
    assert repository.get_product(seeded.product_id).id == seeded.product_id
    assert repository.get_offer(seeded.offer_id).id == seeded.offer_id
    assert SQLiteWorkflowRepository(factory).get_snapshot(second.workflow_id).workflow.id == (
        second.workflow_id
    )
    with pytest.raises(EntityNotFoundError):
        SQLiteWorkflowRepository(factory).get_snapshot(seeded.workflow_id)
    engine.dispose()


def test_invalid_shared_reference_fails_closed(tmp_path: Path) -> None:
    seeded = _seed_database(tmp_path / "invalid-shared.db")
    engine, factory, service = _service(seeded.database_url)
    second = _seed_workflow(factory, seeded.database_url, query="invalid shared reference")
    with session_scope(factory) as session:
        record = session.scalar(
            select(EvidenceRecord).where(EvidenceRecord.request_id == str(second.request_id))
        )
        assert record is not None
        record.payload = {}

    with pytest.raises(WorkflowDataIntegrityError, match="shared catalog references"):
        service.prepare_deletion(seeded.workflow_id)
    assert SQLiteWorkflowRepository(factory).get_snapshot(seeded.workflow_id).workflow.id == (
        seeded.workflow_id
    )
    engine.dispose()


def test_stale_manifest_and_ambiguous_request_never_delete(tmp_path: Path) -> None:
    seeded = _seed_database(tmp_path / "guards.db")
    engine, factory, service = _service(seeded.database_url)
    store = SQLiteWorkflowDataStore(factory)
    stale_plan = store.prepare_deletion(seeded.workflow_id)
    SQLiteShoppingRepository(factory).add_evidence(
        Evidence(
            request_id=seeded.request_id,
            subject_type=EvidenceSubjectType.PRODUCT,
            subject_id=seeded.product_id,
            field_path="new_field",
            source_type=EvidenceSourceType.PLATFORM_LISTING,
            source_url=HttpUrl("https://item.jd.com/1000001.html"),
            source_title="New fixture fact",
            captured_at=NOW,
            observed_value="changed",
        )
    )
    with pytest.raises(WorkflowDeletionPlanStaleError, match="changed"):
        store.delete(seeded.workflow_id, expected_fingerprint=stale_plan.fingerprint)
    assert service.prepare_deletion(seeded.workflow_id).fingerprint != stale_plan.fingerprint

    extra_workflow, _ = WorkflowStateMachine().initialize(seeded.request_id, occurred_at=NOW)
    with session_scope(factory) as session:
        session.add(workflow_record(extra_workflow))
    with pytest.raises(WorkflowDataOwnershipError, match="exactly one"):
        store.load(seeded.workflow_id)
    assert SQLiteWorkflowRepository(factory).get_snapshot(seeded.workflow_id).workflow.id == (
        seeded.workflow_id
    )
    engine.dispose()


def test_integrity_and_cli_input_failures_are_sanitized(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seeded = _seed_database(tmp_path / "integrity.db")
    engine, factory, _ = _service(seeded.database_url)
    with session_scope(factory) as session:
        record = session.scalar(select(PlatformObservationRecord).limit(1))
        assert record is not None
        record.kind = "unsupported"
    with pytest.raises(WorkflowDataIntegrityError, match="unsupported"):
        SQLiteWorkflowDataStore(factory).load(seeded.workflow_id)
    with session_scope(factory) as session:
        record = session.scalar(select(PlatformObservationRecord).limit(1))
        assert record is not None
        record.kind = "search"
        record.payload = {}
    with pytest.raises(WorkflowDataIntegrityError, match="failed archive validation"):
        SQLiteWorkflowDataStore(factory).load(seeded.workflow_id)
    engine.dispose()

    assert main(["data", "delete", "not-a-uuid"], environment={}) == 2
    assert _output(capsys)["error_code"] == "invalid_workflow_id"

    assert (
        main(
            [
                "data",
                "delete",
                str(seeded.workflow_id),
                "--database-url",
                "postgresql://localhost/shopping",
            ],
            environment={},
        )
        == 2
    )
    assert _output(capsys)["error_code"] == "invalid_database_url"

    missing_database = tmp_path / "missing" / "shopping.db"
    assert (
        main(
            [
                "data",
                "delete",
                str(seeded.workflow_id),
                "--database-url",
                f"sqlite:///{missing_database}",
            ],
            environment={},
        )
        == 1
    )
    assert _output(capsys)["error_code"] == "database_not_ready"
    assert not missing_database.exists()

    archive_target = tmp_path / "absent" / "workflow.json"
    assert (
        main(
            [
                "data",
                "export",
                str(seeded.workflow_id),
                "--output",
                str(archive_target),
                "--database-url",
                seeded.database_url,
            ],
            environment={},
        )
        == 2
    )
    assert _output(capsys)["error_code"] == "workflow_data_integrity_failed"


def test_cli_sanitizes_archive_writer_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_database(tmp_path / "writer.db")

    def fail_write(
        self: LocalJsonWorkflowArchiveWriter,
        archive: WorkflowDataArchive,
        output_path: Path,
    ) -> None:
        del self, archive, output_path
        raise ArchiveWriteError("sensitive disk failure")

    monkeypatch.setattr(LocalJsonWorkflowArchiveWriter, "write", fail_write)
    assert (
        main(
            [
                "data",
                "export",
                str(seeded.workflow_id),
                "--output",
                str(tmp_path / "failure.json"),
                "--database-url",
                seeded.database_url,
            ],
            environment={},
        )
        == 2
    )
    assert _output(capsys)["error_code"] == "archive_write_failed"


def test_empty_workflow_deletes_without_catalog_entities(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'empty.db'}"
    assert upgrade_database(database_url).ready is True
    engine = create_sqlite_engine(database_url)
    factory = create_session_factory(engine)
    started = ShoppingWorkflowService(
        SQLiteWorkflowRepository(factory),
        clock=lambda: NOW,
    ).start(_request("empty workflow"))
    store = SQLiteWorkflowDataStore(factory)

    data = store.load(started.workflow.id)
    assert data.products == ()
    assert data.offers == ()
    plan = store.prepare_deletion(started.workflow.id)
    assert plan.records_to_delete.products == 0
    assert plan.records_to_delete.offers == 0
    assert store.delete(started.workflow.id, expected_fingerprint=plan.fingerprint) == plan
    with pytest.raises(EntityNotFoundError):
        store.load(started.workflow.id)
    engine.dispose()


def test_missing_linked_catalog_rows_fail_archive_integrity(tmp_path: Path) -> None:
    missing_offer = _seed_database(tmp_path / "missing-offer.db")
    engine, factory, _ = _service(missing_offer.database_url)
    with session_scope(factory) as session:
        record = session.get(OfferRecord, str(missing_offer.offer_id))
        assert record is not None
        session.delete(record)
    with pytest.raises(WorkflowDataIntegrityError, match="offer is missing"):
        SQLiteWorkflowDataStore(factory).load(missing_offer.workflow_id)
    engine.dispose()

    database_url = f"sqlite:///{tmp_path / 'missing-product.db'}"
    assert upgrade_database(database_url).ready is True
    engine = create_sqlite_engine(database_url)
    factory = create_session_factory(engine)
    started = ShoppingWorkflowService(
        SQLiteWorkflowRepository(factory),
        clock=lambda: NOW,
    ).start(_request("missing product"))
    product = Product(
        brand="Example",
        model="Detached",
        category="smartphone",
        canonical_name="Detached product",
    )
    repository = SQLiteShoppingRepository(factory)
    repository.add_product(product)
    repository.add_evidence(
        Evidence(
            request_id=started.request.id,
            subject_type=EvidenceSubjectType.PRODUCT,
            subject_id=product.id,
            field_path="model",
            source_type=EvidenceSourceType.PLATFORM_LISTING,
            source_url=HttpUrl("https://item.jd.com/1000001.html"),
            source_title="Missing product fixture",
            captured_at=NOW,
            observed_value="Detached",
        )
    )
    with session_scope(factory) as session:
        record = session.get(ProductRecord, str(product.id))
        assert record is not None
        session.delete(record)
    with pytest.raises(WorkflowDataIntegrityError, match="product is missing"):
        SQLiteWorkflowDataStore(factory).load(started.workflow.id)
    engine.dispose()


def test_every_archive_scope_guard_rejects_cross_workflow_data(tmp_path: Path) -> None:
    seeded = _seed_database(tmp_path / "scope.db")
    engine, factory, _ = _service(seeded.database_url)
    data = SQLiteWorkflowDataStore(factory).load(seeded.workflow_id)
    other_id = uuid4()
    assert data.search_observations
    assert data.evidence
    assert data.evidence_checks
    assert data.reports

    mismatched_workflow = data.model_copy(
        update={
            "snapshot": data.snapshot.model_copy(
                update={
                    "workflow": data.snapshot.workflow.model_copy(update={"request_id": other_id})
                }
            )
        }
    )
    mismatched_observation = data.model_copy(
        update={
            "search_observations": (
                data.search_observations[0].model_copy(update={"request_id": other_id}),
            )
        }
    )
    mismatched_evidence = data.model_copy(
        update={
            "evidence": (
                data.evidence[0].model_copy(update={"request_id": other_id}),
                *data.evidence[1:],
            )
        }
    )
    mismatched_derived = data.model_copy(
        update={
            "evidence_checks": (
                data.evidence_checks[0].model_copy(update={"workflow_id": other_id}),
                *data.evidence_checks[1:],
            )
        }
    )
    mismatched_report = data.model_copy(
        update={
            "reports": (
                data.reports[0].model_copy(
                    update={
                        "report": data.reports[0].report.model_copy(
                            update={"workflow_id": other_id}
                        )
                    }
                ),
            )
        }
    )
    for mismatched, message in (
        (mismatched_workflow, "workflow request"),
        (mismatched_observation, "platform observation"),
        (mismatched_evidence, "evidence request"),
        (mismatched_derived, "derived workflow"),
        (mismatched_report, "report workflow"),
    ):
        with pytest.raises(WorkflowDataIntegrityError, match=message):
            SQLiteWorkflowDataStore._require_scope(  # pyright: ignore[reportPrivateUsage]
                mismatched
            )
    engine.dispose()


def test_cli_sanitizes_stale_plan_and_database_operation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_database(tmp_path / "cli-errors.db")
    preview_arguments = [
        "data",
        "delete",
        str(seeded.workflow_id),
        "--database-url",
        seeded.database_url,
    ]
    assert main(preview_arguments, environment={}) == 0
    preview = _output(capsys)
    plan = cast(dict[str, object], preview["plan"])
    confirmation = plan["confirmation_token"]
    assert isinstance(confirmation, str)

    def stale_delete(
        self: SQLiteWorkflowDataStore,
        workflow_id: UUID,
        *,
        expected_fingerprint: str,
    ) -> None:
        del self, workflow_id, expected_fingerprint
        raise WorkflowDeletionPlanStaleError("sensitive race detail")

    monkeypatch.setattr(SQLiteWorkflowDataStore, "delete", stale_delete)
    assert main([*preview_arguments, "--confirm", confirmation], environment={}) == 1
    assert _output(capsys)["error_code"] == "deletion_plan_stale"

    def migration_failure(_: str) -> None:
        raise DatabaseMigrationError("sensitive database detail")

    monkeypatch.setattr(cli_module, "require_current_database", migration_failure)
    assert main(preview_arguments, environment={}) == 2
    assert _output(capsys)["error_code"] == "database_operation_failed"

    def migration_succeeds(_: str) -> None:
        return None

    monkeypatch.setattr(cli_module, "require_current_database", migration_succeeds)

    def database_failure(
        self: SQLiteWorkflowDataStore,
        workflow_id: UUID,
    ) -> None:
        del self, workflow_id
        raise SQLAlchemyError("sensitive query detail")

    monkeypatch.setattr(SQLiteWorkflowDataStore, "prepare_deletion", database_failure)
    assert main(preview_arguments, environment={}) == 2
    assert _output(capsys)["error_code"] == "database_operation_failed"
