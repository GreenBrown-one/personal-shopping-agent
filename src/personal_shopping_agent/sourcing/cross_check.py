"""Official product evidence collection and conservative field-level cross-checking."""

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator

from personal_shopping_agent.domain import (
    Evidence,
    EvidenceSourceType,
    EvidenceSubjectType,
    Product,
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


class CrossCheckModel(BaseModel):
    """Strict immutable base for official observations and comparison results."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class OfficialSpecificationObservation(CrossCheckModel):
    """One raw specification visible on a manufacturer-controlled page."""

    key: str = Field(min_length=1, max_length=160)
    raw_value: str = Field(min_length=1, max_length=2_000)


class OfficialProductObservation(CrossCheckModel):
    """Structured official page observation returned by a provider adapter."""

    id: UUID = Field(default_factory=uuid4)
    brand: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=240)
    source_url: HttpUrl
    source_title: str = Field(min_length=1, max_length=500)
    captured_at: AwareDatetime
    specifications: tuple[OfficialSpecificationObservation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def specification_keys_are_unique(self) -> Self:
        """Reject ambiguous duplicate keys within one official page observation."""

        normalized = [_normalized(item.key) for item in self.specifications]
        if len(normalized) != len(set(normalized)):
            raise ValueError("official specification keys must be unique")
        return self


class EvidenceCheckStatus(StrEnum):
    """Auditable relationship between platform and official field observations."""

    MATCH = "match"
    CONFLICT = "conflict"
    PLATFORM_ONLY = "platform_only"
    OFFICIAL_ONLY = "official_only"
    MISSING_OFFICIAL_SOURCE = "missing_official_source"


class EvidenceCheck(CrossCheckModel):
    """Durable result for one product field without collapsing conflicting values."""

    id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    workflow_id: UUID
    product_id: UUID
    field_path: str = Field(min_length=1, max_length=255)
    status: EvidenceCheckStatus
    platform_values: tuple[str, ...] = ()
    official_values: tuple[str, ...] = ()
    official_source_urls: tuple[HttpUrl, ...] = ()
    checked_at: AwareDatetime


class EvidenceCrossCheckBatch(CrossCheckModel):
    """Official evidence and check rows staged by one workflow transaction."""

    official_evidence: tuple[Evidence, ...] = ()
    checks: tuple[EvidenceCheck, ...] = Field(min_length=1)


class EvidenceCrossCheckResult(CrossCheckModel):
    """Committed workflow snapshot and the exact cross-check batch."""

    snapshot: WorkflowSnapshot
    batch: EvidenceCrossCheckBatch


class NoProductsForEvidenceCheckError(RuntimeError):
    """Raised when an offers-collected request has no linked product facts."""


class OfficialEvidenceIdentityMismatchError(ValueError):
    """Reject an official page that does not exactly identify the target product."""

    def __init__(self, product_id: UUID, observation_id: UUID) -> None:
        super().__init__("Official evidence brand/model does not match the target product.")
        self.product_id = product_id
        self.observation_id = observation_id


class OfficialEvidenceProvider(Protocol):
    """Provider-neutral asynchronous port for manufacturer-controlled sources."""

    async def collect(self, product: Product) -> tuple[OfficialProductObservation, ...]: ...


class EvidenceCrossCheckUnitOfWork(Protocol):
    """Transaction port for product evidence, check rows, and workflow state."""

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

    def add_evidence(self, evidence: Evidence) -> None: ...

    def add_check(self, check: EvidenceCheck) -> None: ...

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None: ...

    def commit(self) -> None: ...


class EvidenceCrossCheckUnitOfWorkFactory(Protocol):
    """Create one isolated cross-check transaction."""

    def __call__(self) -> EvidenceCrossCheckUnitOfWork: ...


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _distinct(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _official_fields(
    observations: tuple[OfficialProductObservation, ...],
) -> dict[str, tuple[str, ...]]:
    fields: dict[str, list[str]] = {}
    for observation in observations:
        fields.setdefault("brand", []).append(observation.brand)
        fields.setdefault("model", []).append(observation.model)
        for specification in observation.specifications:
            fields.setdefault(f"specifications.{specification.key}", []).append(
                specification.raw_value
            )
    return {field: _distinct(values) for field, values in fields.items()}


def _platform_fields(evidence: tuple[Evidence, ...]) -> dict[str, tuple[str, ...]]:
    fields: dict[str, list[str]] = {}
    for item in evidence:
        if (
            item.subject_type is not EvidenceSubjectType.PRODUCT
            or item.source_type is EvidenceSourceType.MANUFACTURER_OFFICIAL
            or item.observed_value is None
        ):
            continue
        fields.setdefault(item.field_path, []).append(str(item.observed_value))
    return {field: _distinct(values) for field, values in fields.items()}


def _comparison_status(
    platform_values: tuple[str, ...],
    official_values: tuple[str, ...],
) -> EvidenceCheckStatus:
    if not platform_values:
        return EvidenceCheckStatus.OFFICIAL_ONLY
    if not official_values:
        return EvidenceCheckStatus.PLATFORM_ONLY
    platform_normalized = {_normalized(value) for value in platform_values}
    official_normalized = {_normalized(value) for value in official_values}
    if len(platform_normalized) == 1 and platform_normalized == official_normalized:
        return EvidenceCheckStatus.MATCH
    return EvidenceCheckStatus.CONFLICT


class ProductEvidenceChecker:
    """Pure builder for official Evidence and field-level comparison records."""

    def build(
        self,
        *,
        request_id: UUID,
        workflow_id: UUID,
        product: Product,
        platform_evidence: tuple[Evidence, ...],
        official_observations: tuple[OfficialProductObservation, ...],
        checked_at: datetime,
    ) -> EvidenceCrossCheckBatch:
        """Build conservative checks with no fuzzy matching or unit conversion."""

        for observation in official_observations:
            if _normalized(observation.brand) != _normalized(product.brand) or _normalized(
                observation.model
            ) != _normalized(product.model):
                raise OfficialEvidenceIdentityMismatchError(product.id, observation.id)

        official_evidence = tuple(
            item
            for observation in official_observations
            for item in self._evidence_for_observation(request_id, product.id, observation)
        )
        platform_fields = _platform_fields(platform_evidence)
        official_fields = _official_fields(official_observations)
        source_urls = tuple(dict.fromkeys(item.source_url for item in official_observations))

        if not official_observations:
            field_paths = tuple(sorted(platform_fields)) or ("official_source",)
            checks = tuple(
                EvidenceCheck(
                    request_id=request_id,
                    workflow_id=workflow_id,
                    product_id=product.id,
                    field_path=field_path,
                    status=EvidenceCheckStatus.MISSING_OFFICIAL_SOURCE,
                    platform_values=platform_fields.get(field_path, ()),
                    checked_at=checked_at,
                )
                for field_path in field_paths
            )
        else:
            checks = tuple(
                EvidenceCheck(
                    request_id=request_id,
                    workflow_id=workflow_id,
                    product_id=product.id,
                    field_path=field_path,
                    status=_comparison_status(
                        platform_fields.get(field_path, ()),
                        official_fields.get(field_path, ()),
                    ),
                    platform_values=platform_fields.get(field_path, ()),
                    official_values=official_fields.get(field_path, ()),
                    official_source_urls=source_urls,
                    checked_at=checked_at,
                )
                for field_path in sorted(platform_fields.keys() | official_fields.keys())
            )

        return EvidenceCrossCheckBatch(official_evidence=official_evidence, checks=checks)

    @staticmethod
    def _evidence_for_observation(
        request_id: UUID,
        product_id: UUID,
        observation: OfficialProductObservation,
    ) -> tuple[Evidence, ...]:
        values = [
            ("brand", observation.brand),
            ("model", observation.model),
            *(
                (f"specifications.{item.key}", item.raw_value)
                for item in observation.specifications
            ),
        ]
        return tuple(
            Evidence(
                request_id=request_id,
                subject_type=EvidenceSubjectType.PRODUCT,
                subject_id=product_id,
                field_path=field_path,
                source_type=EvidenceSourceType.MANUFACTURER_OFFICIAL,
                source_url=observation.source_url,
                source_title=observation.source_title,
                captured_at=observation.captured_at,
                origin_observation_id=observation.id,
                observed_value=value,
                notes="Official manufacturer observation; reliability is not yet scored.",
            )
            for field_path, value in values
        )


class EvidenceCrossCheckService:
    """Collect official facts and atomically advance to evidence_cross_checked."""

    def __init__(
        self,
        unit_of_work_factory: EvidenceCrossCheckUnitOfWorkFactory,
        provider: OfficialEvidenceProvider,
        *,
        checker: ProductEvidenceChecker | None = None,
        state_machine: WorkflowStateMachine | None = None,
        clock: Callable[[], datetime] = workflow_now,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._provider = provider
        self._checker = checker or ProductEvidenceChecker()
        self._state_machine = state_machine or WorkflowStateMachine()
        self._clock = clock

    async def cross_check(self, workflow_id: UUID) -> EvidenceCrossCheckResult:
        """Persist official evidence, checks, and state in one transaction."""

        with self._unit_of_work_factory() as unit_of_work:
            snapshot = unit_of_work.get_snapshot(workflow_id)
            if (
                self._state_machine.next_state(snapshot.workflow.state)
                is not WorkflowState.EVIDENCE_CROSS_CHECKED
            ):
                raise InvalidWorkflowTransitionError(
                    "evidence cross-check requires an offers_collected workflow"
                )
            products = unit_of_work.list_products_for_request(snapshot.request.id)
            if not products:
                raise NoProductsForEvidenceCheckError(
                    "No request-linked products are available for evidence cross-check."
                )

            evidence: list[Evidence] = []
            checks: list[EvidenceCheck] = []
            checked_at = self._clock()
            for product in products:
                platform_evidence = unit_of_work.list_product_evidence(
                    snapshot.request.id, product.id
                )
                observations = await self._provider.collect(product)
                batch = self._checker.build(
                    request_id=snapshot.request.id,
                    workflow_id=snapshot.workflow.id,
                    product=product,
                    platform_evidence=platform_evidence,
                    official_observations=observations,
                    checked_at=checked_at,
                )
                evidence.extend(batch.official_evidence)
                checks.extend(batch.checks)

            complete_batch = EvidenceCrossCheckBatch(
                official_evidence=tuple(evidence),
                checks=tuple(checks),
            )
            for item in complete_batch.official_evidence:
                unit_of_work.add_evidence(item)
            for check in complete_batch.checks:
                unit_of_work.add_check(check)

            updated, event = self._state_machine.advance(
                snapshot.workflow,
                WorkflowState.EVIDENCE_CROSS_CHECKED,
                reason="official_evidence_cross_checked",
                occurred_at=checked_at,
            )
            unit_of_work.save_transition(
                updated,
                event,
                expected_revision=snapshot.workflow.revision,
            )
            result = EvidenceCrossCheckResult(
                snapshot=WorkflowSnapshot(
                    request=snapshot.request,
                    workflow=updated,
                    events=(*snapshot.events, event),
                ),
                batch=complete_batch,
            )
            unit_of_work.commit()
            return result
