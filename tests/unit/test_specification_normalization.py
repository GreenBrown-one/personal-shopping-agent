"""Unit tests for deterministic specification aliases, units, and orchestration."""

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
    measurement_definition,
    normalize_measurement,
)
from personal_shopping_agent.sourcing import (
    NormalizedSpecification,
    NoSpecificationsForNormalizationError,
    ProductSpecificationNormalizer,
    SpecificationNormalizationService,
    SpecificationNormalizationStatus,
)

NOW = datetime(2026, 8, 9, 21, 0, tzinfo=UTC)


def build_product(*, model: str = "A1") -> Product:
    return Product(
        brand="Example",
        model=model,
        category="smartphone",
        canonical_name=f"Example {model}",
    )


def build_snapshot(*, cross_checked: bool = True) -> WorkflowSnapshot:
    request = ShoppingRequest(
        query="example phone",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        created_at=NOW,
    )
    machine = WorkflowStateMachine()
    workflow, events = machine.initialize(request.id, occurred_at=NOW)
    if cross_checked:
        for state in (
            WorkflowState.CANDIDATES_DISCOVERED,
            WorkflowState.OFFERS_COLLECTED,
            WorkflowState.EVIDENCE_CROSS_CHECKED,
        ):
            workflow, event = machine.advance(
                workflow,
                state,
                reason=f"advanced_to_{state.value}",
                occurred_at=NOW,
            )
            events = (*events, event)
    return WorkflowSnapshot(request=request, workflow=workflow, events=events)


def evidence(
    request_id: UUID,
    product_id: UUID,
    field_path: str,
    raw_value: str | None,
    *,
    subject_type: EvidenceSubjectType = EvidenceSubjectType.PRODUCT,
) -> Evidence:
    return Evidence(
        request_id=request_id,
        subject_type=subject_type,
        subject_id=product_id,
        field_path=field_path,
        source_type=EvidenceSourceType.PLATFORM_LISTING,
        source_url=HttpUrl("https://item.example.com/1"),
        source_title="Example product page",
        captured_at=NOW,
        observed_value=raw_value,
    )


@pytest.mark.parametrize(
    ("field_path", "raw_value", "expected"),
    (
        ("specifications.电池容量", "6 Ah", Decimal("6000")),
        ("specifications.weight", "1.5 kg", Decimal("1500")),
        ("specifications.屏幕尺寸", '6.7"', Decimal("6.7")),
        ("specifications.storage", "1 TB", Decimal("1024")),
        ("specifications.ram", "8,192 MB", Decimal("8")),
    ),
)
def test_declared_aliases_convert_only_complete_supported_measurements(
    field_path: str,
    raw_value: str,
    expected: Decimal,
) -> None:
    definition = measurement_definition(field_path)
    assert definition is not None
    assert normalize_measurement(raw_value, definition) == expected


def test_measurement_resolution_rejects_unknown_fields_values_and_units() -> None:
    assert measurement_definition("brand") is None
    assert measurement_definition("specifications.color") is None
    definition = measurement_definition("specifications.battery_capacity")
    assert definition is not None
    for raw_value in ("6000", "about 6000 mAh", "-1 mAh", "1,2,3 mAh", "6 kWh"):
        assert normalize_measurement(raw_value, definition) is None


def test_normalizer_groups_aliases_preserves_raw_facts_and_marks_conflicts() -> None:
    request_id = uuid4()
    workflow_id = uuid4()
    product = build_product()
    items = (
        evidence(request_id, product.id, "specifications.电池容量", "6000 mAh"),
        evidence(request_id, product.id, "specifications.battery", "6 Ah"),
        evidence(request_id, product.id, "specifications.重量", "1.5 kg"),
        evidence(request_id, product.id, "specifications.weight", "1400 g"),
        evidence(request_id, product.id, "specifications.存储容量", "1 TB"),
        evidence(request_id, product.id, "specifications.storage", "1024 GB"),
        evidence(request_id, product.id, "specifications.color", "Black"),
    )

    facts = ProductSpecificationNormalizer().normalize(
        request_id=request_id,
        workflow_id=workflow_id,
        product=product,
        evidence=items,
        normalized_at=NOW,
    )
    by_key = {fact.canonical_key: fact for fact in facts}

    battery = by_key["battery_capacity"]
    assert battery.status is SpecificationNormalizationStatus.NORMALIZED
    assert battery.normalized_values == (Decimal("6000"),)
    assert battery.canonical_unit == "mAh"
    assert battery.source_field_paths == (
        "specifications.电池容量",
        "specifications.battery",
    )
    assert battery.raw_values == ("6000 mAh", "6 Ah")
    assert battery.evidence_ids == (items[0].id, items[1].id)

    weight = by_key["weight"]
    assert weight.status is SpecificationNormalizationStatus.CONFLICT
    assert weight.normalized_values == (Decimal("1.5E+3"), Decimal("1.4E+3"))

    storage = by_key["storage_capacity"]
    assert storage.status is SpecificationNormalizationStatus.NORMALIZED
    assert storage.normalized_values == (Decimal("1024"),)

    color = by_key["color"]
    assert color.status is SpecificationNormalizationStatus.UNSUPPORTED_KEY
    assert color.canonical_unit is None
    assert color.normalized_values == ()


def test_normalizer_marks_partial_parse_failure_and_ignores_unrelated_evidence() -> None:
    request_id = uuid4()
    product = build_product()
    other_product = build_product(model="A2")
    included = evidence(
        request_id,
        product.id,
        "specifications.battery_capacity",
        "6000 mAh",
    )
    invalid = evidence(
        request_id,
        product.id,
        "specifications.电池容量",
        "approximately six amp hours",
    )
    ignored = (
        evidence(uuid4(), product.id, "specifications.weight", "180 g"),
        evidence(request_id, other_product.id, "specifications.weight", "180 g"),
        evidence(
            request_id,
            product.id,
            "specifications.weight",
            "180 g",
            subject_type=EvidenceSubjectType.OFFER,
        ),
        evidence(request_id, product.id, "brand", "Example"),
        evidence(request_id, product.id, "specifications.weight", None),
    )

    facts = ProductSpecificationNormalizer().normalize(
        request_id=request_id,
        workflow_id=uuid4(),
        product=product,
        evidence=(included, invalid, *ignored),
        normalized_at=NOW,
    )

    assert len(facts) == 1
    assert facts[0].status is SpecificationNormalizationStatus.UNPARSEABLE_VALUE
    assert facts[0].normalized_values == (Decimal("6000"),)
    assert facts[0].raw_values == ("6000 mAh", "approximately six amp hours")
    assert (
        ProductSpecificationNormalizer().normalize(
            request_id=request_id,
            workflow_id=uuid4(),
            product=product,
            evidence=(),
            normalized_at=NOW,
        )
        == ()
    )


def valid_normalized_payload() -> dict[str, object]:
    return {
        "request_id": uuid4(),
        "workflow_id": uuid4(),
        "product_id": uuid4(),
        "canonical_key": "weight",
        "source_field_paths": ("specifications.weight",),
        "raw_values": ("180 g",),
        "normalized_values": (Decimal("180"),),
        "canonical_unit": "g",
        "evidence_ids": (uuid4(),),
        "status": SpecificationNormalizationStatus.NORMALIZED,
        "normalized_at": NOW,
    }


def test_normalized_specification_rejects_inconsistent_status_shapes() -> None:
    base = valid_normalized_payload()
    duplicate_evidence_id = uuid4()
    invalid: tuple[tuple[dict[str, object], str], ...] = (
        ({"source_field_paths": ("x", "x")}, "field paths"),
        ({"raw_values": ("x", "x")}, "raw values"),
        ({"normalized_values": (Decimal("1"), Decimal("1"))}, "normalized values"),
        (
            {"evidence_ids": (duplicate_evidence_id, duplicate_evidence_id)},
            "evidence identifiers",
        ),
        ({"normalized_values": (Decimal("-1"),)}, "cannot be negative"),
        (
            {
                "status": SpecificationNormalizationStatus.UNSUPPORTED_KEY,
                "normalized_values": (),
            },
            "unsupported keys",
        ),
        (
            {
                "status": SpecificationNormalizationStatus.UNSUPPORTED_KEY,
                "canonical_unit": None,
            },
            "unsupported keys",
        ),
        ({"canonical_unit": None}, "require a canonical unit"),
        ({"normalized_values": ()}, "exactly one value"),
        (
            {
                "status": SpecificationNormalizationStatus.CONFLICT,
                "normalized_values": (Decimal("1"),),
            },
            "at least two values",
        ),
    )
    for updates, message in invalid:
        with pytest.raises(ValidationError, match=message):
            NormalizedSpecification.model_validate(base | updates)

    unparseable = NormalizedSpecification.model_validate(
        base
        | {
            "status": SpecificationNormalizationStatus.UNPARSEABLE_VALUE,
            "normalized_values": (),
        }
    )
    assert unparseable.status is SpecificationNormalizationStatus.UNPARSEABLE_VALUE


class FakeNormalizationUnitOfWork:
    def __init__(
        self,
        snapshot: WorkflowSnapshot,
        products: tuple[Product, ...],
        evidence_items: tuple[Evidence, ...] = (),
    ) -> None:
        self.snapshot = snapshot
        self.products = products
        self.evidence_items = evidence_items
        self.specifications: list[NormalizedSpecification] = []
        self.transition: tuple[ShoppingWorkflow, WorkflowEvent, int] | None = None
        self.entered = False
        self.exited = False
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> "FakeNormalizationUnitOfWork":
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

    def list_product_evidence(
        self,
        request_id: UUID,
        product_id: UUID,
    ) -> tuple[Evidence, ...]:
        assert request_id == self.snapshot.request.id
        return tuple(item for item in self.evidence_items if item.subject_id == product_id)

    def add_specification(self, specification: NormalizedSpecification) -> None:
        self.specifications.append(specification)

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


def test_service_persists_normalized_facts_and_advances_atomically() -> None:
    snapshot = build_snapshot()
    product = build_product()
    item = evidence(
        snapshot.request.id,
        product.id,
        "specifications.battery_capacity",
        "6000 mAh",
    )
    unit = FakeNormalizationUnitOfWork(snapshot, (product,), (item,))
    service = SpecificationNormalizationService(lambda: unit, clock=lambda: NOW)

    result = service.normalize(snapshot.workflow.id)

    assert unit.entered and unit.exited and unit.committed and not unit.rolled_back
    assert unit.specifications == list(result.batch.specifications)
    assert result.snapshot.workflow.state is WorkflowState.DATA_NORMALIZED
    assert result.snapshot.events[-1].reason == "product_specifications_normalized"
    assert unit.transition is not None
    assert unit.transition[2] == snapshot.workflow.revision


def test_service_rolls_back_wrong_state_and_missing_specifications() -> None:
    product = build_product()
    wrong_snapshot = build_snapshot(cross_checked=False)
    wrong_unit = FakeNormalizationUnitOfWork(wrong_snapshot, (product,))
    with pytest.raises(InvalidWorkflowTransitionError, match="evidence_cross_checked"):
        SpecificationNormalizationService(lambda: wrong_unit).normalize(wrong_snapshot.workflow.id)
    assert wrong_unit.exited and wrong_unit.rolled_back

    snapshot = build_snapshot()
    empty_unit = FakeNormalizationUnitOfWork(snapshot, (product,))
    with pytest.raises(NoSpecificationsForNormalizationError, match="No request-linked"):
        SpecificationNormalizationService(lambda: empty_unit).normalize(snapshot.workflow.id)
    assert empty_unit.exited and empty_unit.rolled_back and not empty_unit.committed
