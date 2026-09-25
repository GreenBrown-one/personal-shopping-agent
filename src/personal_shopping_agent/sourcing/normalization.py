"""Deterministic specification normalization over the shared domain measurement catalog."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from personal_shopping_agent.domain import Evidence, EvidenceSubjectType, Product
from personal_shopping_agent.domain.measurements import (
    MeasurementDefinition,
    measurement_definition,
    normalize_measurement,
    specification_key_token,
)
from personal_shopping_agent.domain.workflow import (
    InvalidWorkflowTransitionError,
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStateMachine,
    workflow_now,
)


class NormalizationModel(BaseModel):
    """Strict immutable base for durable normalized specification results."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SpecificationNormalizationStatus(StrEnum):
    """Outcome of normalizing all observations for one canonical field."""

    NORMALIZED = "normalized"
    CONFLICT = "conflict"
    UNSUPPORTED_KEY = "unsupported_key"
    UNPARSEABLE_VALUE = "unparseable_value"


class NormalizedSpecification(NormalizationModel):
    """Auditable normalized values that retain every raw input and evidence link."""

    id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    workflow_id: UUID
    product_id: UUID
    canonical_key: str = Field(min_length=1, max_length=160)
    source_field_paths: tuple[str, ...] = Field(min_length=1)
    raw_values: tuple[str, ...] = Field(min_length=1)
    normalized_values: tuple[Decimal, ...] = ()
    canonical_unit: str | None = Field(default=None, max_length=40)
    evidence_ids: tuple[UUID, ...] = Field(min_length=1)
    status: SpecificationNormalizationStatus
    normalized_at: AwareDatetime

    @model_validator(mode="after")
    def values_match_status(self) -> Self:
        """Keep normalized, conflicting, unsupported, and failed shapes unambiguous."""

        if len(self.source_field_paths) != len(set(self.source_field_paths)):
            raise ValueError("source field paths must be unique")
        if len(self.raw_values) != len(set(self.raw_values)):
            raise ValueError("raw values must be unique")
        if len(self.normalized_values) != len(set(self.normalized_values)):
            raise ValueError("normalized values must be unique")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("evidence identifiers must be unique")
        if any(value < 0 for value in self.normalized_values):
            raise ValueError("normalized measurement values cannot be negative")

        if self.status is SpecificationNormalizationStatus.UNSUPPORTED_KEY:
            if self.canonical_unit is not None or self.normalized_values:
                raise ValueError("unsupported keys cannot contain normalized measurements")
        elif self.canonical_unit is None:
            raise ValueError("recognized specification keys require a canonical unit")
        elif self.status is SpecificationNormalizationStatus.NORMALIZED:
            if len(self.normalized_values) != 1:
                raise ValueError("normalized status requires exactly one value")
        elif (
            self.status is SpecificationNormalizationStatus.CONFLICT
            and len(self.normalized_values) < 2
        ):
            raise ValueError("conflict status requires at least two values")
        return self


class SpecificationNormalizationBatch(NormalizationModel):
    """All normalized product specification facts for one workflow transaction."""

    specifications: tuple[NormalizedSpecification, ...] = Field(min_length=1)


class SpecificationNormalizationResult(NormalizationModel):
    """Committed normalized specifications and their advanced workflow snapshot."""

    snapshot: WorkflowSnapshot
    batch: SpecificationNormalizationBatch


class NoSpecificationsForNormalizationError(RuntimeError):
    """Raised when cross-checked products contain no raw specification evidence."""


@dataclass(slots=True)
class _SpecificationGroup:
    canonical_key: str
    definition: MeasurementDefinition | None
    source_field_paths: list[str]
    raw_values: list[str]
    normalized_values: list[Decimal]
    evidence_ids: list[UUID]
    parse_failed: bool = False


def _append_unique[T](items: list[T], value: T) -> None:
    if value not in items:
        items.append(value)


class ProductSpecificationNormalizer:
    """Pure rule engine that groups aliases and preserves unresolved observations."""

    def normalize(
        self,
        *,
        request_id: UUID,
        workflow_id: UUID,
        product: Product,
        evidence: tuple[Evidence, ...],
        normalized_at: datetime,
    ) -> tuple[NormalizedSpecification, ...]:
        """Normalize recognized measurements and retain unknown or invalid raw facts."""

        groups: dict[str, _SpecificationGroup] = {}
        for item in evidence:
            if (
                item.request_id != request_id
                or item.subject_type is not EvidenceSubjectType.PRODUCT
                or item.subject_id != product.id
                or not item.field_path.startswith("specifications.")
                or item.observed_value is None
            ):
                continue
            raw_value = str(item.observed_value)
            definition = measurement_definition(item.field_path)
            raw_key = item.field_path.removeprefix("specifications.")
            canonical_key = (
                definition.canonical_key
                if definition is not None
                else specification_key_token(raw_key)
            )
            group = groups.setdefault(
                canonical_key,
                _SpecificationGroup(
                    canonical_key=canonical_key,
                    definition=definition,
                    source_field_paths=[],
                    raw_values=[],
                    normalized_values=[],
                    evidence_ids=[],
                ),
            )
            _append_unique(group.source_field_paths, item.field_path)
            _append_unique(group.raw_values, raw_value)
            _append_unique(group.evidence_ids, item.id)
            if definition is not None:
                normalized = normalize_measurement(raw_value, definition)
                if normalized is None:
                    group.parse_failed = True
                else:
                    _append_unique(group.normalized_values, normalized)

        return tuple(
            self._to_specification(
                request_id=request_id,
                workflow_id=workflow_id,
                product_id=product.id,
                group=groups[key],
                normalized_at=normalized_at,
            )
            for key in sorted(groups)
        )

    @staticmethod
    def _to_specification(
        *,
        request_id: UUID,
        workflow_id: UUID,
        product_id: UUID,
        group: _SpecificationGroup,
        normalized_at: datetime,
    ) -> NormalizedSpecification:
        definition = group.definition
        if definition is None:
            status = SpecificationNormalizationStatus.UNSUPPORTED_KEY
        elif group.parse_failed:
            status = SpecificationNormalizationStatus.UNPARSEABLE_VALUE
        elif len(group.normalized_values) == 1:
            status = SpecificationNormalizationStatus.NORMALIZED
        else:
            status = SpecificationNormalizationStatus.CONFLICT
        return NormalizedSpecification(
            request_id=request_id,
            workflow_id=workflow_id,
            product_id=product_id,
            canonical_key=group.canonical_key,
            source_field_paths=tuple(group.source_field_paths),
            raw_values=tuple(group.raw_values),
            normalized_values=tuple(group.normalized_values),
            canonical_unit=definition.canonical_unit if definition is not None else None,
            evidence_ids=tuple(group.evidence_ids),
            status=status,
            normalized_at=normalized_at,
        )


class SpecificationNormalizationUnitOfWork(Protocol):
    """Transaction port for evidence reads, normalized facts, and workflow state."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def get_snapshot(self, workflow_id: UUID) -> WorkflowSnapshot: ...

    def list_products_for_request(self, request_id: UUID) -> tuple[Product, ...]: ...

    def list_product_evidence(self, request_id: UUID, product_id: UUID) -> tuple[Evidence, ...]: ...

    def add_specification(self, specification: NormalizedSpecification) -> None: ...

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None: ...

    def commit(self) -> None: ...


class SpecificationNormalizationUnitOfWorkFactory(Protocol):
    """Create one isolated specification-normalization transaction."""

    def __call__(self) -> SpecificationNormalizationUnitOfWork: ...


class SpecificationNormalizationService:
    """Normalize cross-checked evidence and atomically advance to data_normalized."""

    def __init__(
        self,
        unit_of_work_factory: SpecificationNormalizationUnitOfWorkFactory,
        *,
        normalizer: ProductSpecificationNormalizer | None = None,
        state_machine: WorkflowStateMachine | None = None,
        clock: Callable[[], datetime] = workflow_now,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._normalizer = normalizer or ProductSpecificationNormalizer()
        self._state_machine = state_machine or WorkflowStateMachine()
        self._clock = clock

    def normalize(self, workflow_id: UUID) -> SpecificationNormalizationResult:
        """Persist normalized facts and the next workflow state in one transaction."""

        with self._unit_of_work_factory() as unit_of_work:
            snapshot = unit_of_work.get_snapshot(workflow_id)
            if (
                self._state_machine.next_state(snapshot.workflow.state)
                is not WorkflowState.DATA_NORMALIZED
            ):
                raise InvalidWorkflowTransitionError(
                    "specification normalization requires an evidence_cross_checked workflow"
                )
            normalized_at = self._clock()
            facts = tuple(
                fact
                for product in unit_of_work.list_products_for_request(snapshot.request.id)
                for fact in self._normalizer.normalize(
                    request_id=snapshot.request.id,
                    workflow_id=snapshot.workflow.id,
                    product=product,
                    evidence=unit_of_work.list_product_evidence(snapshot.request.id, product.id),
                    normalized_at=normalized_at,
                )
            )
            if not facts:
                raise NoSpecificationsForNormalizationError(
                    "No request-linked specification evidence is available for normalization."
                )
            batch = SpecificationNormalizationBatch(specifications=facts)
            for fact in batch.specifications:
                unit_of_work.add_specification(fact)

            updated, event = self._state_machine.advance(
                snapshot.workflow,
                WorkflowState.DATA_NORMALIZED,
                reason="product_specifications_normalized",
                occurred_at=normalized_at,
            )
            unit_of_work.save_transition(
                updated,
                event,
                expected_revision=snapshot.workflow.revision,
            )
            result = SpecificationNormalizationResult(
                snapshot=WorkflowSnapshot(
                    request=snapshot.request,
                    workflow=updated,
                    events=(*snapshot.events, event),
                ),
                batch=batch,
            )
            unit_of_work.commit()
            return result
