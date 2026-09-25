"""Unit tests for conservative official evidence cross-checking."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from uuid import UUID, uuid4

import pytest
from pydantic import HttpUrl, ValidationError

from personal_shopping_agent.domain import (
    Budget,
    Evidence,
    EvidenceSourceType,
    EvidenceSubjectType,
    InvalidWorkflowTransitionError,
    Money,
    Product,
    ShoppingRequest,
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStateMachine,
)
from personal_shopping_agent.sourcing import (
    EvidenceCheck,
    EvidenceCheckStatus,
    EvidenceCrossCheckService,
    IndependentEvidenceScopeError,
    NoProductsForEvidenceCheckError,
    OfficialEvidenceIdentityMismatchError,
    OfficialProductObservation,
    OfficialSpecificationObservation,
    ProductEvidenceChecker,
)

NOW = datetime(2026, 8, 9, 19, 0, tzinfo=UTC)


def build_product() -> Product:
    return Product(
        brand="Example",
        model="A1",
        category="smartphone",
        canonical_name="Example A1",
    )


def build_snapshot(*, offers_collected: bool = True) -> WorkflowSnapshot:
    request = ShoppingRequest(
        query="example phone",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        created_at=NOW,
    )
    machine = WorkflowStateMachine()
    workflow, events = machine.initialize(request.id, occurred_at=NOW)
    if offers_collected:
        for state in (WorkflowState.CANDIDATES_DISCOVERED, WorkflowState.OFFERS_COLLECTED):
            workflow, event = machine.advance(
                workflow,
                state,
                reason=f"advanced_to_{state.value}",
                occurred_at=NOW,
            )
            events = (*events, event)
    return WorkflowSnapshot(request=request, workflow=workflow, events=events)


def platform_evidence(
    request_id: UUID,
    product_id: UUID,
    field_path: str,
    value: str | None,
    *,
    source_type: EvidenceSourceType = EvidenceSourceType.PLATFORM_LISTING,
    subject_type: EvidenceSubjectType = EvidenceSubjectType.PRODUCT,
) -> Evidence:
    return Evidence(
        request_id=request_id,
        subject_type=subject_type,
        subject_id=product_id,
        field_path=field_path,
        source_type=source_type,
        source_url=HttpUrl("https://item.example.com/1"),
        source_title="Platform page",
        captured_at=NOW,
        observed_value=value,
    )


def official_observation(
    *,
    battery: str = "6000 mAh",
    extra: tuple[OfficialSpecificationObservation, ...] = (),
    brand: str = " example ",
    model: str = "a1",
    source: str = "one",
) -> OfficialProductObservation:
    return OfficialProductObservation(
        brand=brand,
        model=model,
        source_url=HttpUrl(f"https://manufacturer.example.com/{source}"),
        source_title="Official specifications",
        captured_at=NOW,
        specifications=(
            OfficialSpecificationObservation(key="battery", raw_value=battery),
            *extra,
        ),
    )


def status_map(checks: tuple[EvidenceCheck, ...]) -> dict[str, EvidenceCheckStatus]:
    return {check.field_path: check.status for check in checks}


def test_checker_preserves_raw_values_and_classifies_each_relationship() -> None:
    product = build_product()
    request_id = uuid4()
    platform = (
        platform_evidence(request_id, product.id, "brand", "EXAMPLE"),
        platform_evidence(request_id, product.id, "model", "A1"),
        platform_evidence(request_id, product.id, "specifications.battery", "6000   MAH"),
        platform_evidence(request_id, product.id, "specifications.color", "black"),
        platform_evidence(request_id, product.id, "ignored", None),
        platform_evidence(
            request_id,
            product.id,
            "ignored_offer",
            "x",
            subject_type=EvidenceSubjectType.OFFER,
        ),
        platform_evidence(
            request_id,
            product.id,
            "ignored_official",
            "x",
            source_type=EvidenceSourceType.MANUFACTURER_OFFICIAL,
        ),
    )
    official = official_observation(
        extra=(OfficialSpecificationObservation(key="weight", raw_value="180 g"),)
    )

    batch = ProductEvidenceChecker().build(
        request_id=request_id,
        workflow_id=uuid4(),
        product=product,
        platform_evidence=platform,
        official_observations=(official,),
        checked_at=NOW,
    )

    statuses = status_map(batch.checks)
    assert statuses == {
        "brand": EvidenceCheckStatus.MATCH,
        "model": EvidenceCheckStatus.MATCH,
        "specifications.battery": EvidenceCheckStatus.MATCH,
        "specifications.color": EvidenceCheckStatus.PLATFORM_ONLY,
        "specifications.weight": EvidenceCheckStatus.OFFICIAL_ONLY,
    }
    battery = next(check for check in batch.checks if check.field_path.endswith("battery"))
    assert battery.platform_values == ("6000   MAH",)
    assert battery.official_values == ("6000 mAh",)
    assert battery.official_source_urls == (official.source_url,)
    assert len(batch.official_evidence) == 4
    assert all(
        item.request_id == request_id
        and item.subject_id == product.id
        and item.source_type is EvidenceSourceType.MANUFACTURER_OFFICIAL
        and item.origin_observation_id == official.id
        and item.reliability is None
        for item in batch.official_evidence
    )


def test_checker_marks_any_multi_source_value_disagreement_as_conflict() -> None:
    product = build_product()
    request_id = uuid4()
    platform = (
        platform_evidence(request_id, product.id, "specifications.battery", "6000 mAh"),
        platform_evidence(request_id, product.id, "specifications.battery", "5900 mAh"),
    )
    observations = (
        official_observation(),
        official_observation(battery="5900 mAh", source="two"),
    )

    batch = ProductEvidenceChecker().build(
        request_id=request_id,
        workflow_id=uuid4(),
        product=product,
        platform_evidence=platform,
        official_observations=observations,
        checked_at=NOW,
    )

    check = next(item for item in batch.checks if item.field_path.endswith("battery"))
    assert check.status is EvidenceCheckStatus.CONFLICT
    assert check.platform_values == ("6000 mAh", "5900 mAh")
    assert check.official_values == ("6000 mAh", "5900 mAh")

    changed = ProductEvidenceChecker().build(
        request_id=request_id,
        workflow_id=uuid4(),
        product=product,
        platform_evidence=platform[:1],
        official_observations=observations,
        checked_at=NOW,
    )
    assert (
        next(item for item in changed.checks if item.field_path.endswith("battery")).status
        is EvidenceCheckStatus.CONFLICT
    )


def test_checker_records_missing_official_source_with_and_without_platform_fields() -> None:
    product = build_product()
    request_id = uuid4()
    checker = ProductEvidenceChecker()
    with_platform = checker.build(
        request_id=request_id,
        workflow_id=uuid4(),
        product=product,
        platform_evidence=(platform_evidence(request_id, product.id, "brand", "Example"),),
        official_observations=(),
        checked_at=NOW,
    )
    empty = checker.build(
        request_id=request_id,
        workflow_id=uuid4(),
        product=product,
        platform_evidence=(),
        official_observations=(),
        checked_at=NOW,
    )

    assert [(item.field_path, item.status) for item in with_platform.checks] == [
        ("brand", EvidenceCheckStatus.MISSING_OFFICIAL_SOURCE)
    ]
    assert [(item.field_path, item.status) for item in empty.checks] == [
        ("official_source", EvidenceCheckStatus.MISSING_OFFICIAL_SOURCE)
    ]


def test_checker_rejects_identity_mismatch_and_duplicate_official_keys() -> None:
    product = build_product()
    observation = official_observation(model="A2")

    with pytest.raises(OfficialEvidenceIdentityMismatchError) as error:
        ProductEvidenceChecker().build(
            request_id=uuid4(),
            workflow_id=uuid4(),
            product=product,
            platform_evidence=(),
            official_observations=(observation,),
            checked_at=NOW,
        )
    assert error.value.product_id == product.id
    assert error.value.observation_id == observation.id

    with pytest.raises(ValidationError, match="keys must be unique"):
        OfficialProductObservation(
            brand="Example",
            model="A1",
            source_url=HttpUrl("https://manufacturer.example.com/a1"),
            source_title="Official specifications",
            captured_at=NOW,
            specifications=(
                OfficialSpecificationObservation(key="Battery", raw_value="6000 mAh"),
                OfficialSpecificationObservation(key=" battery ", raw_value="6000 mAh"),
            ),
        )


class FakeProvider:
    def __init__(
        self,
        observations: tuple[OfficialProductObservation, ...],
        *,
        failure: RuntimeError | None = None,
    ) -> None:
        self.observations = observations
        self.failure = failure
        self.products: list[Product] = []

    async def collect(self, product: Product) -> tuple[OfficialProductObservation, ...]:
        self.products.append(product)
        if self.failure is not None:
            raise self.failure
        return self.observations


class FakeCrossCheckUnitOfWork:
    def __init__(
        self,
        snapshot: WorkflowSnapshot,
        products: tuple[Product, ...],
        evidence: tuple[Evidence, ...] = (),
    ) -> None:
        self.snapshot = snapshot
        self.products = products
        self.evidence = evidence
        self.staged_evidence: list[Evidence] = []
        self.checks: list[EvidenceCheck] = []
        self.transition: tuple[ShoppingWorkflow, WorkflowEvent, int] | None = None
        self.entered = False
        self.exited = False
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> "FakeCrossCheckUnitOfWork":
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        self.exited = True
        self.rolled_back = exc_type is not None or not self.committed

    def get_snapshot(self, workflow_id: UUID) -> WorkflowSnapshot:
        assert workflow_id == self.snapshot.workflow.id
        return self.snapshot

    def list_products_for_request(self, request_id: UUID) -> tuple[Product, ...]:
        assert request_id == self.snapshot.request.id
        return self.products

    def list_product_evidence(self, request_id: UUID, product_id: UUID) -> tuple[Evidence, ...]:
        assert request_id == self.snapshot.request.id
        assert product_id in {product.id for product in self.products}
        return tuple(item for item in self.evidence if item.subject_id == product_id)

    def add_evidence(self, evidence: Evidence) -> None:
        self.staged_evidence.append(evidence)

    def add_check(self, check: EvidenceCheck) -> None:
        self.checks.append(check)

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None:
        self.transition = (workflow, event, expected_revision)

    def commit(self) -> None:
        self.committed = True


def test_service_collects_persists_and_advances_atomically() -> None:
    snapshot = build_snapshot()
    product = build_product()
    evidence = (
        platform_evidence(snapshot.request.id, product.id, "brand", "Example"),
        platform_evidence(snapshot.request.id, product.id, "model", "A1"),
    )
    unit = FakeCrossCheckUnitOfWork(snapshot, (product,), evidence)
    provider = FakeProvider((official_observation(),))
    service = EvidenceCrossCheckService(lambda: unit, provider, clock=lambda: NOW)

    result = asyncio.run(service.cross_check(snapshot.workflow.id))

    assert provider.products == [product]
    assert unit.entered and unit.exited and unit.committed and not unit.rolled_back
    assert unit.staged_evidence == list(result.batch.official_evidence)
    assert unit.checks == list(result.batch.checks)
    assert result.snapshot.workflow.state is WorkflowState.EVIDENCE_CROSS_CHECKED
    assert result.snapshot.events[-1].reason == "official_evidence_cross_checked"
    assert unit.transition is not None
    assert unit.transition[2] == snapshot.workflow.revision


def test_service_rolls_back_wrong_state_missing_products_and_provider_failure() -> None:
    product = build_product()
    wrong_snapshot = build_snapshot(offers_collected=False)
    wrong_unit = FakeCrossCheckUnitOfWork(wrong_snapshot, (product,))
    wrong_provider = FakeProvider((official_observation(),))
    with pytest.raises(InvalidWorkflowTransitionError, match="offers_collected"):
        asyncio.run(
            EvidenceCrossCheckService(lambda: wrong_unit, wrong_provider).cross_check(
                wrong_snapshot.workflow.id
            )
        )
    assert wrong_provider.products == []
    assert wrong_unit.exited and wrong_unit.rolled_back

    snapshot = build_snapshot()
    empty_unit = FakeCrossCheckUnitOfWork(snapshot, ())
    with pytest.raises(NoProductsForEvidenceCheckError, match="No request-linked products"):
        asyncio.run(
            EvidenceCrossCheckService(
                lambda: empty_unit,
                FakeProvider((official_observation(),)),
            ).cross_check(snapshot.workflow.id)
        )
    assert empty_unit.exited and empty_unit.rolled_back

    failure_unit = FakeCrossCheckUnitOfWork(snapshot, (product,))
    failure_provider = FakeProvider((), failure=RuntimeError("sanitized provider failure"))
    with pytest.raises(RuntimeError, match="sanitized provider failure"):
        asyncio.run(
            EvidenceCrossCheckService(lambda: failure_unit, failure_provider).cross_check(
                snapshot.workflow.id
            )
        )
    assert failure_unit.exited and failure_unit.rolled_back
    assert not failure_unit.staged_evidence and not failure_unit.checks


class FakeIndependentProvider:
    def __init__(self, *, source_type: EvidenceSourceType, captured_at: datetime) -> None:
        self.source_type = source_type
        self.captured_at = captured_at
        self.seen: list[tuple[UUID, UUID, int]] = []

    def evidence_for(
        self,
        *,
        request_id: UUID,
        product: Product,
        platform_evidence: tuple[Evidence, ...],
    ) -> tuple[Evidence, ...]:
        self.seen.append((request_id, product.id, len(platform_evidence)))
        return (
            platform_evidence[0].model_copy(
                update={
                    "id": uuid4(),
                    "field_path": "specifications.chip_performance",
                    "source_type": self.source_type,
                    "observed_value": "280 SOCPK",
                    "captured_at": self.captured_at,
                }
            ),
        )


def test_independent_evidence_is_staged_in_the_same_transaction() -> None:
    snapshot = build_snapshot()
    product = build_product()
    evidence = (platform_evidence(snapshot.request.id, product.id, "specifications.CPU型号", "x"),)
    unit = FakeCrossCheckUnitOfWork(snapshot, (product,), evidence)
    independent = FakeIndependentProvider(
        source_type=EvidenceSourceType.INDEPENDENT_REVIEW, captured_at=NOW
    )
    service = EvidenceCrossCheckService(
        lambda: unit,
        FakeProvider(()),
        independent_providers=(independent,),
        clock=lambda: NOW,
    )

    result = asyncio.run(service.cross_check(snapshot.workflow.id))

    assert independent.seen == [(snapshot.request.id, product.id, 1)]
    assert len(result.batch.independent_evidence) == 1
    assert unit.staged_evidence == [
        *result.batch.official_evidence,
        *result.batch.independent_evidence,
    ]
    assert unit.committed


@pytest.mark.parametrize(
    ("source_type", "captured_at"),
    [
        (EvidenceSourceType.MANUFACTURER_OFFICIAL, NOW),
        (EvidenceSourceType.INDEPENDENT_REVIEW, datetime(2026, 8, 10, tzinfo=UTC)),
    ],
)
def test_out_of_scope_independent_evidence_rolls_back_everything(
    source_type: EvidenceSourceType, captured_at: datetime
) -> None:
    snapshot = build_snapshot()
    product = build_product()
    evidence = (platform_evidence(snapshot.request.id, product.id, "specifications.CPU型号", "x"),)
    unit = FakeCrossCheckUnitOfWork(snapshot, (product,), evidence)
    service = EvidenceCrossCheckService(
        lambda: unit,
        FakeProvider(()),
        independent_providers=(
            FakeIndependentProvider(source_type=source_type, captured_at=captured_at),
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(IndependentEvidenceScopeError, match="independent_review"):
        asyncio.run(service.cross_check(snapshot.workflow.id))
    assert unit.rolled_back and not unit.staged_evidence
