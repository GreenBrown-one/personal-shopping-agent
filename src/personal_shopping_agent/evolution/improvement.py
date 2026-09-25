"""Local, sanitized-by-construction improvement case previews for the maintenance loop.

A case holds only software version, workflow state enums, stable codes, counts, and catalog keys.
It never holds query text, category, region, amounts, product or seller names, URLs, report
content, or page content, so the user can review it before deciding to share it anywhere.
"""

import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from personal_shopping_agent.__about__ import __version__
from personal_shopping_agent.domain import (
    MEASUREMENT_DEFINITIONS,
    JsonContractModel,
    ShoppingRequest,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStateMachine,
    specification_key_token,
)

CASE_FORMAT = "personal-shopping-agent.improvement-case.v1"
UNRECOGNIZED_ERROR_CODE = "unrecognized_error_code"
CODE_PATTERN = r"^[a-z0-9][a-z0-9_.:-]{0,119}$"
PRIVACY_NOTICE = (
    "Local preview only: nothing was uploaded or saved. It contains no query, category, region, "
    "amount, product, seller, URL, or page content. Share it only if you choose to."
)
DEFAULT_ACCEPTANCE_CHECKS = (
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run pyright",
    "uv run pytest --cov",
    "a sanitized fixture or unit test reproduces the reported stage outcome",
)

StableCode = Annotated[str, Field(pattern=CODE_PATTERN)]
_CODE_REGEX = re.compile(CODE_PATTERN)
_CANONICAL_KEYS = {
    specification_key_token(item.canonical_key): item.canonical_key
    for item in MEASUREMENT_DEFINITIONS
}


class ImprovementOutcome(StrEnum):
    """How the workflow ended from the user's point of view."""

    FAILED = "failed"
    STALLED = "stalled"
    COMPLETED = "completed"


class ImprovementModel(JsonContractModel):
    """Strict immutable base for shareable improvement contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RequestShape(ImprovementModel):
    """Structure of the request without any of the user's own words or amounts."""

    criteria_count: int = Field(ge=0)
    hard_criteria_count: int = Field(ge=0)
    supported_criterion_keys: tuple[str, ...]
    unsupported_criteria_count: int = Field(ge=0)
    region_specified: bool
    stretch_budget_specified: bool
    budget_currency: str = Field(pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def only_catalog_keys_and_consistent_counts(self) -> Self:
        if any(key not in _CANONICAL_KEYS.values() for key in self.supported_criterion_keys):
            raise ValueError("supported criterion keys must come from the measurement catalog")
        supported = len(self.supported_criterion_keys)
        if supported + self.unsupported_criteria_count != self.criteria_count:
            raise ValueError("criterion counts must add up")
        if self.hard_criteria_count > self.criteria_count:
            raise ValueError("hard criteria cannot exceed all criteria")
        return self


class ImprovementCase(ImprovementModel):
    """Minimal reproduction case from docs/AI_EVOLUTION.md, safe to preview locally."""

    format: Literal["personal-shopping-agent.improvement-case.v1"] = CASE_FORMAT
    software_version: str = Field(pattern=r"^[0-9A-Za-z.+-]{1,40}$")
    outcome: ImprovementOutcome
    workflow_state: WorkflowState
    stage: WorkflowState | None
    stable_error_code: StableCode | None
    stage_trail: tuple[WorkflowState, ...] = Field(min_length=1)
    request_shape: RequestShape
    expected_behavior: str
    actual_behavior: str
    fixture_ids: tuple[StableCode, ...] = ()
    acceptance_checks: tuple[str, ...] = DEFAULT_ACCEPTANCE_CHECKS
    privacy_notice: str = PRIVACY_NOTICE

    @model_validator(mode="after")
    def text_is_derived_from_codes_only(self) -> Self:
        if self.stage_trail[-1] is not self.workflow_state:
            raise ValueError("stage trail must end at the current workflow state")
        if (self.outcome is ImprovementOutcome.COMPLETED) is not (self.stage is None):
            raise ValueError("only completed workflows have no unfinished stage")
        expected, actual = _behavior_text(self.outcome, self.stage, self.stable_error_code)
        if (self.expected_behavior, self.actual_behavior) != (expected, actual):
            raise ValueError("behavior text must be derived from the case codes")
        if self.acceptance_checks != DEFAULT_ACCEPTANCE_CHECKS:
            raise ValueError("acceptance checks are fixed project gates")
        if self.privacy_notice != PRIVACY_NOTICE:
            raise ValueError("the privacy notice cannot be changed")
        return self


class InvalidImprovementCodeError(ValueError):
    """Raised when a user-supplied error code is not a stable project code."""


def _behavior_text(
    outcome: ImprovementOutcome,
    stage: WorkflowState | None,
    error_code: str | None,
) -> tuple[str, str]:
    code = f" with error code {error_code}" if error_code else ""
    if outcome is ImprovementOutcome.COMPLETED:
        return (
            "The completed report helps the user decide within the stated constraints.",
            f"The workflow completed but the user reports an unhelpful result{code}.",
        )
    assert stage is not None
    if outcome is ImprovementOutcome.FAILED:
        return (
            f"The {stage.value} stage succeeds or stops with an actionable stable error.",
            f"The workflow failed while attempting {stage.value}{code}.",
        )
    return (
        f"The {stage.value} stage succeeds or reports why it cannot continue.",
        f"The workflow stopped before {stage.value}{code}.",
    )


def _request_shape(request: ShoppingRequest) -> RequestShape:
    supported = tuple(
        _CANONICAL_KEYS[token]
        for token in (specification_key_token(item.key) for item in request.criteria)
        if token in _CANONICAL_KEYS
    )
    return RequestShape(
        criteria_count=len(request.criteria),
        hard_criteria_count=sum(1 for item in request.criteria if item.hard_requirement),
        supported_criterion_keys=supported,
        unsupported_criteria_count=len(request.criteria) - len(supported),
        region_specified=request.region is not None,
        stretch_budget_specified=request.budget.stretch_maximum is not None,
        budget_currency=request.budget.maximum.currency,
    )


def is_stable_code(value: str) -> bool:
    """Return whether a value has the shape of a project-defined stable code."""

    return _CODE_REGEX.fullmatch(value) is not None


class ImprovementCaseBuilder:
    """Project one persisted workflow onto a minimal sanitized reproduction case."""

    def __init__(self, *, software_version: str = __version__) -> None:
        self._software_version = software_version

    def build(
        self,
        snapshot: WorkflowSnapshot,
        *,
        reported_error_code: str | None = None,
    ) -> ImprovementCase:
        """Build a case; a failed workflow's own code takes precedence over a reported one."""

        if reported_error_code is not None and not is_stable_code(reported_error_code):
            raise InvalidImprovementCodeError(
                "Error codes may contain only lowercase letters, digits, and _.:-"
            )

        workflow = snapshot.workflow
        error_code = reported_error_code
        if workflow.state is WorkflowState.FAILED:
            outcome = ImprovementOutcome.FAILED
            failed_from = snapshot.events[-1].from_state
            assert failed_from is not None, "a failed event always leaves a prior state"
            stage = WorkflowStateMachine.next_state(failed_from)
            stored_code = workflow.error_code or ""
            error_code = stored_code if is_stable_code(stored_code) else UNRECOGNIZED_ERROR_CODE
        elif workflow.state is WorkflowState.COMPLETED:
            outcome = ImprovementOutcome.COMPLETED
            stage = None
        else:
            outcome = ImprovementOutcome.STALLED
            stage = WorkflowStateMachine.next_state(workflow.state)

        expected, actual = _behavior_text(outcome, stage, error_code)
        return ImprovementCase(
            software_version=self._software_version,
            outcome=outcome,
            workflow_state=workflow.state,
            stage=stage,
            stable_error_code=error_code,
            stage_trail=tuple(event.to_state for event in snapshot.events),
            request_shape=_request_shape(snapshot.request),
            expected_behavior=expected,
            actual_behavior=actual,
        )
