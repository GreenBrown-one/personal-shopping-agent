"""Deterministic review of a structured shopping request before any storage or network access."""

from enum import StrEnum
from typing import Self

from pydantic import ConfigDict, Field, model_validator

from personal_shopping_agent.domain import (
    MEASUREMENT_DEFINITIONS,
    InvalidCriterionDefinitionError,
    JsonContractModel,
    MeasurementDefinition,
    ShoppingCriterion,
    ShoppingRequest,
    is_declared_unit,
    measurement_for_criterion_key,
    numeric_criterion_bounds,
    specification_key_token,
)


class RequirementIssueSeverity(StrEnum):
    """Whether an issue prevents starting a workflow or only weakens the result."""

    BLOCKING = "blocking"
    WARNING = "warning"


class RequirementIssueCode(StrEnum):
    """Stable codes the host can map to one concise follow-up question."""

    NON_NUMERIC_BOUND = "non_numeric_bound"
    NEGATIVE_BOUND = "negative_bound"
    BOUNDS_REVERSED = "bounds_reversed"
    PREFERRED_OUTSIDE_BOUNDS = "preferred_outside_bounds"
    PREFERRED_DIRECTION_UNDEFINED = "preferred_direction_undefined"
    BOUNDS_MISSING = "bounds_missing"
    CRITERION_KEY_DUPLICATE = "criterion_key_duplicate"
    CRITERION_KEY_ALIAS = "criterion_key_alias"
    CRITERION_KEY_UNSUPPORTED = "criterion_key_unsupported"
    CRITERION_UNIT_MISSING = "criterion_unit_missing"
    CRITERION_UNIT_MISMATCH = "criterion_unit_mismatch"
    NO_CRITERIA = "no_criteria"
    REGION_MISSING = "region_missing"


class ReviewModel(JsonContractModel):
    """Strict immutable base for requirement review contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RequirementIssue(ReviewModel):
    """One gap between the stated need and what deterministic scoring can evaluate."""

    code: RequirementIssueCode
    severity: RequirementIssueSeverity
    criterion_key: str | None = None
    message: str = Field(min_length=1)
    suggestion: str = Field(min_length=1)


class SupportedMeasurement(ReviewModel):
    """One criterion key and unit the scoring engine can evaluate today."""

    canonical_key: str
    canonical_unit: str
    aliases: tuple[str, ...]


class RequirementReview(ReviewModel):
    """Complete deterministic review; it never rewrites or completes the request."""

    ready_to_start: bool
    ready_for_ranking: bool
    issues: tuple[RequirementIssue, ...]
    supported_measurements: tuple[SupportedMeasurement, ...]

    @model_validator(mode="after")
    def readiness_matches_issues(self) -> Self:
        blocking = any(item.severity is RequirementIssueSeverity.BLOCKING for item in self.issues)
        if self.ready_to_start is blocking:
            raise ValueError("ready_to_start must be true exactly when no issue is blocking")
        if self.ready_for_ranking and not self.ready_to_start:
            raise ValueError("a request that cannot start cannot be ready for ranking")
        return self

    @property
    def blocking_issues(self) -> tuple[RequirementIssue, ...]:
        """Return the issues that must be clarified before a workflow is created."""

        return tuple(
            item for item in self.issues if item.severity is RequirementIssueSeverity.BLOCKING
        )


class RequirementClarificationRequiredError(ValueError):
    """Raised when a request would fail or exclude every candidate during scoring."""

    def __init__(self, review: RequirementReview) -> None:
        details = "; ".join(
            f"{item.criterion_key or 'request'}: {item.code.value}"
            for item in review.blocking_issues
        )
        super().__init__(
            "The shopping request needs clarification before it can start "
            f"({details}). Call review_shopping_request for suggestions."
        )
        self.review = review


_SUPPORTED_MEASUREMENTS = tuple(
    SupportedMeasurement(
        canonical_key=item.canonical_key,
        canonical_unit=item.canonical_unit,
        aliases=tuple(sorted(item.aliases)),
    )
    for item in MEASUREMENT_DEFINITIONS
)
_SUPPORTED_KEYS = ", ".join(
    f"{item.canonical_key} ({item.canonical_unit})" for item in MEASUREMENT_DEFINITIONS
)
_CANONICAL_BY_TOKEN = {
    specification_key_token(item.canonical_key): item for item in MEASUREMENT_DEFINITIONS
}
_BOUND_SUGGESTIONS = {
    RequirementIssueCode.NON_NUMERIC_BOUND: "Ask for a numeric minimum or maximum.",
    RequirementIssueCode.NEGATIVE_BOUND: "Ask for a non-negative bound.",
    RequirementIssueCode.BOUNDS_REVERSED: "Confirm which value is the minimum and the maximum.",
    RequirementIssueCode.PREFERRED_OUTSIDE_BOUNDS: (
        "Ask for a preferred value between the minimum and maximum."
    ),
    RequirementIssueCode.PREFERRED_DIRECTION_UNDEFINED: (
        "Ask whether the preferred value is a minimum (more is better) or a maximum."
    ),
    RequirementIssueCode.BOUNDS_MISSING: (
        "Ask for a minimum (more is better) or a maximum (less is better)."
    ),
}


class RequirementReviewer:
    """Apply the same criterion rules as scoring, without guessing any missing value."""

    def review(self, request: ShoppingRequest) -> RequirementReview:
        """Return every blocking gap and warning for one structured request."""

        issues: list[RequirementIssue] = []
        evaluable = 0
        seen_tokens: set[str] = set()
        for criterion in request.criteria:
            token = specification_key_token(criterion.key)
            if token in seen_tokens:
                issues.append(
                    RequirementIssue(
                        code=RequirementIssueCode.CRITERION_KEY_DUPLICATE,
                        severity=RequirementIssueSeverity.BLOCKING,
                        criterion_key=criterion.key,
                        message="Two criteria refer to the same key after case/spacing folding.",
                        suggestion="Merge the duplicated criteria into one.",
                    )
                )
                continue
            seen_tokens.add(token)
            criterion_issues = self._review_criterion(criterion)
            issues.extend(criterion_issues)
            if not criterion_issues:
                evaluable += 1

        if not request.criteria:
            issues.append(
                RequirementIssue(
                    code=RequirementIssueCode.NO_CRITERIA,
                    severity=RequirementIssueSeverity.WARNING,
                    message="Without criteria every candidate is excluded from ranking.",
                    suggestion=f"Ask which measurable needs matter: {_SUPPORTED_KEYS}.",
                )
            )
        if request.region is None:
            issues.append(
                RequirementIssue(
                    code=RequirementIssueCode.REGION_MISSING,
                    severity=RequirementIssueSeverity.WARNING,
                    message="The report will show the delivery region as unspecified.",
                    suggestion="Ask for the delivery city or province if it matters.",
                )
            )

        ready_to_start = not any(
            item.severity is RequirementIssueSeverity.BLOCKING for item in issues
        )
        return RequirementReview(
            ready_to_start=ready_to_start,
            ready_for_ranking=ready_to_start and evaluable > 0,
            issues=tuple(issues),
            supported_measurements=_SUPPORTED_MEASUREMENTS,
        )

    def require_ready(self, request: ShoppingRequest) -> RequirementReview:
        """Return the review, or raise before any workflow or platform access is attempted."""

        review = self.review(request)
        if not review.ready_to_start:
            raise RequirementClarificationRequiredError(review)
        return review

    @staticmethod
    def _review_criterion(criterion: ShoppingCriterion) -> list[RequirementIssue]:
        try:
            numeric_criterion_bounds(criterion)
        except InvalidCriterionDefinitionError as error:
            code = RequirementIssueCode(error.code)
            return [
                RequirementIssue(
                    code=code,
                    severity=RequirementIssueSeverity.BLOCKING,
                    criterion_key=criterion.key,
                    message=str(error),
                    suggestion=_BOUND_SUGGESTIONS[code],
                )
            ]

        severity = (
            RequirementIssueSeverity.BLOCKING
            if criterion.hard_requirement
            else RequirementIssueSeverity.WARNING
        )
        consequence = (
            "As a hard requirement it would exclude every candidate."
            if criterion.hard_requirement
            else "It would always score 0."
        )
        definition = _CANONICAL_BY_TOKEN.get(specification_key_token(criterion.key))
        if definition is None:
            alias_of = measurement_for_criterion_key(criterion.key)
            if alias_of is not None:
                return [
                    RequirementIssue(
                        code=RequirementIssueCode.CRITERION_KEY_ALIAS,
                        severity=severity,
                        criterion_key=criterion.key,
                        message=f"Scoring matches canonical keys only. {consequence}",
                        suggestion=(
                            f"Resubmit as '{alias_of.canonical_key}' "
                            f"with unit '{alias_of.canonical_unit}'."
                        ),
                    )
                ]
            return [
                RequirementIssue(
                    code=RequirementIssueCode.CRITERION_KEY_UNSUPPORTED,
                    severity=severity,
                    criterion_key=criterion.key,
                    message=f"No normalized specification exists for this key. {consequence}",
                    suggestion=f"Use one of: {_SUPPORTED_KEYS}, or drop this criterion.",
                )
            ]
        return _review_unit(criterion, definition, severity, consequence)


def _review_unit(
    criterion: ShoppingCriterion,
    definition: MeasurementDefinition,
    severity: RequirementIssueSeverity,
    consequence: str,
) -> list[RequirementIssue]:
    if criterion.unit == definition.canonical_unit:
        return []
    if criterion.unit is None:
        return [
            RequirementIssue(
                code=RequirementIssueCode.CRITERION_UNIT_MISSING,
                severity=severity,
                criterion_key=criterion.key,
                message=f"The unit is missing, so bounds cannot be compared. {consequence}",
                suggestion=f"Confirm the bounds in '{definition.canonical_unit}'.",
            )
        ]
    suggestion = (
        f"Convert the bounds from '{criterion.unit}' to '{definition.canonical_unit}' "
        "and confirm the result with the user."
        if is_declared_unit(definition, criterion.unit)
        else f"Confirm the bounds in '{definition.canonical_unit}'."
    )
    return [
        RequirementIssue(
            code=RequirementIssueCode.CRITERION_UNIT_MISMATCH,
            severity=severity,
            criterion_key=criterion.key,
            message=(
                f"Scoring compares only the canonical unit '{definition.canonical_unit}'. "
                f"{consequence}"
            ),
            suggestion=suggestion,
        )
    ]
