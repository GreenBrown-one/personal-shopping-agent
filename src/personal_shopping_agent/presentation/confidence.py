"""Deterministic evidence confidence for pre-ranking candidate utility."""

from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from personal_shopping_agent.domain import (
    Evidence,
    EvidenceSourceType,
    EvidenceSubjectType,
    ShoppingRequest,
)
from personal_shopping_agent.presentation.decision import (
    CandidateScoringFoundation,
    CriterionEvaluation,
    CriterionEvaluationStatus,
)
from personal_shopping_agent.sourcing.cross_check import EvidenceCheck, EvidenceCheckStatus
from personal_shopping_agent.sourcing.normalization import NormalizedSpecification

_SCORE_QUANTUM = Decimal("0.000001")
_ZERO = Decimal("0")
_ONE = Decimal("1")

_SOURCE_RELIABILITY = {
    EvidenceSourceType.MANUFACTURER_OFFICIAL: Decimal("0.95"),
    EvidenceSourceType.PLATFORM_SELF_OPERATED: Decimal("0.90"),
    EvidenceSourceType.BRAND_FLAGSHIP: Decimal("0.85"),
    EvidenceSourceType.AUTHORIZED_RETAILER: Decimal("0.80"),
    EvidenceSourceType.INDEPENDENT_REVIEW: Decimal("0.75"),
    EvidenceSourceType.THIRD_PARTY_SELLER: Decimal("0.65"),
    EvidenceSourceType.PLATFORM_LISTING: Decimal("0.60"),
    EvidenceSourceType.USER_COMMENT: Decimal("0.45"),
    EvidenceSourceType.SEARCH_SNIPPET: Decimal("0.30"),
}
_CHECK_CONSISTENCY = {
    EvidenceCheckStatus.MATCH: _ONE,
    EvidenceCheckStatus.CONFLICT: _ZERO,
    EvidenceCheckStatus.PLATFORM_ONLY: Decimal("0.60"),
    EvidenceCheckStatus.OFFICIAL_ONLY: Decimal("0.75"),
    EvidenceCheckStatus.MISSING_OFFICIAL_SOURCE: Decimal("0.50"),
}


class ConfidenceModel(BaseModel):
    """Strict immutable base for explainable evidence-confidence outputs."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CriterionEvidenceAssessment(ConfidenceModel):
    """Evidence-quality components for one request criterion."""

    key: str = Field(min_length=1, max_length=120)
    weight: Decimal = Field(gt=0)
    complete: bool
    source_reliability: Decimal = Field(ge=0, le=1)
    freshness: Decimal = Field(ge=0, le=1)
    consistency: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    evidence_ids: tuple[UUID, ...] = ()
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def components_are_consistent(self) -> "CriterionEvidenceAssessment":
        """Keep evidence identity and the declared product formula internally valid."""

        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("criterion evidence identifiers must be unique")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("criterion evidence reason codes must be unique")
        if not self.complete and any(
            value != 0 for value in (self.source_reliability, self.freshness, self.consistency)
        ):
            raise ValueError("incomplete criterion evidence components must be zero")
        expected = _quantize(
            self.source_reliability * self.freshness * self.consistency if self.complete else _ZERO
        )
        if self.confidence != expected:
            raise ValueError("criterion confidence must equal its evidence component product")
        return self


class CandidateEvidenceConfidence(ConfidenceModel):
    """Aggregate evidence confidence and utility adjusted before final ranking."""

    request_id: UUID
    product_id: UUID
    criteria: tuple[CriterionEvidenceAssessment, ...]
    completeness: Decimal = Field(ge=0, le=1)
    source_reliability: Decimal = Field(ge=0, le=1)
    freshness: Decimal = Field(ge=0, le=1)
    consistency: Decimal = Field(ge=0, le=1)
    evidence_confidence: Decimal = Field(ge=0, le=1)
    base_utility: Decimal | None = Field(default=None, ge=0, le=1)
    adjusted_utility: Decimal | None = Field(default=None, ge=0, le=1)
    assessed_at: AwareDatetime

    @model_validator(mode="after")
    def aggregates_are_consistent(self) -> "CandidateEvidenceConfidence":
        """Reject duplicate criteria or aggregate values that disagree with formulas."""

        keys = [_key_token(item.key) for item in self.criteria]
        if len(keys) != len(set(keys)):
            raise ValueError("criterion confidence keys must be unique after normalization")
        if bool(self.criteria) != (self.base_utility is not None):
            raise ValueError("base utility exists exactly when criterion confidence exists")
        if (self.base_utility is None) != (self.adjusted_utility is None):
            raise ValueError("base and adjusted utility must be present together")
        total_weight = sum((item.weight for item in self.criteria), _ZERO)
        complete_weight = sum((item.weight for item in self.criteria if item.complete), _ZERO)
        expected_completeness = (
            _quantize(complete_weight / total_weight) if self.criteria else _ZERO
        )
        expected_components = (
            expected_completeness,
            _weighted_average(self.criteria, "source_reliability"),
            _weighted_average(self.criteria, "freshness"),
            _weighted_average(self.criteria, "consistency"),
        )
        if (
            self.completeness,
            self.source_reliability,
            self.freshness,
            self.consistency,
        ) != expected_components:
            raise ValueError("aggregate evidence components must match criterion evidence")
        expected_confidence = _quantize(
            self.completeness * self.source_reliability * self.freshness * self.consistency
        )
        if self.evidence_confidence != expected_confidence:
            raise ValueError("evidence confidence must equal the aggregate component product")
        expected_adjusted = (
            _quantize(self.base_utility * self.evidence_confidence)
            if self.base_utility is not None
            else None
        )
        if self.adjusted_utility != expected_adjusted:
            raise ValueError("adjusted utility must equal utility times evidence confidence")
        return self


class EvidenceScopeMismatchError(ValueError):
    """Raised when scoring inputs do not describe one request/candidate scope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InvalidEvidenceTimelineError(ValueError):
    """Raised when confidence would depend on a record from the future."""

    def __init__(self, record_id: UUID | None, code: str, message: str) -> None:
        super().__init__(message)
        self.record_id = record_id
        self.code = code


def _key_token(value: str) -> str:
    return " ".join(value.casefold().replace("_", " ").split())


def _quantize(value: Decimal) -> Decimal:
    return min(_ONE, max(_ZERO, value)).quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_UP)


def _weighted_average(
    items: tuple[CriterionEvidenceAssessment, ...],
    attribute: str,
) -> Decimal:
    complete = tuple(item for item in items if item.complete)
    if not complete:
        return _ZERO
    weighted = sum(
        (item.weight * getattr(item, attribute) for item in complete),
        _ZERO,
    )
    total_weight = sum((item.weight for item in complete), _ZERO)
    return _quantize(weighted / total_weight)


def _freshness(evidence: Evidence, assessed_at: datetime) -> Decimal:
    if evidence.captured_at > assessed_at:
        raise InvalidEvidenceTimelineError(
            evidence.id,
            "evidence_from_future",
            "Evidence capture time cannot be later than confidence assessment time.",
        )
    if evidence.freshness is not None:
        return evidence.freshness
    age = assessed_at - evidence.captured_at
    if age <= timedelta(days=180):
        return _ONE
    if age <= timedelta(days=365):
        return Decimal("0.90")
    if age <= timedelta(days=730):
        return Decimal("0.80")
    return Decimal("0.70")


def _source_reliability(evidence: Evidence) -> Decimal:
    return (
        evidence.reliability
        if evidence.reliability is not None
        else _SOURCE_RELIABILITY[evidence.source_type]
    )


def _relevant_checks(
    checks: tuple[EvidenceCheck, ...],
    field_paths: frozenset[str],
) -> tuple[EvidenceCheck, ...]:
    return tuple(item for item in checks if item.field_path in field_paths)


def _consistency(
    *,
    complete: bool,
    linked_evidence: tuple[Evidence, ...],
    checks: tuple[EvidenceCheck, ...],
) -> tuple[Decimal, str]:
    if not complete:
        return _ZERO, "consistency:unresolved_fact"
    if not linked_evidence:
        return _ZERO, "consistency:no_linked_evidence"
    independent_sources = {str(item.source_url) for item in linked_evidence}
    if len(independent_sources) >= 2:
        return _ONE, "consistency:normalized_multi_source_agreement"
    statuses = {item.status for item in checks}
    if EvidenceCheckStatus.MATCH in statuses:
        return _ONE, "consistency:official_match"
    if EvidenceCheckStatus.CONFLICT in statuses:
        return _ZERO, "consistency:official_conflict"
    if statuses:
        score = min(_CHECK_CONSISTENCY[status] for status in statuses)
        status = min(statuses, key=lambda item: (_CHECK_CONSISTENCY[item], item.value))
        return score, f"consistency:{status.value}"
    return Decimal("0.50"), "consistency:single_source_unverified"


class EvidenceConfidenceEvaluator:
    """Assess criterion evidence without mutating facts or claiming a final ranking."""

    def assess(
        self,
        *,
        request: ShoppingRequest,
        foundation: CandidateScoringFoundation,
        specifications: tuple[NormalizedSpecification, ...],
        evidence: tuple[Evidence, ...],
        checks: tuple[EvidenceCheck, ...],
        assessed_at: datetime,
    ) -> CandidateEvidenceConfidence:
        """Calculate evidence components for exactly one request and candidate product."""

        if assessed_at.tzinfo is None or assessed_at.utcoffset() is None:
            raise InvalidEvidenceTimelineError(
                None,
                "assessment_time_naive",
                "Confidence assessment time must include a timezone.",
            )
        if foundation.request_id != request.id:
            raise EvidenceScopeMismatchError(
                "request_mismatch",
                "Candidate scoring foundation must belong to the shopping request.",
            )
        expected = tuple(
            (_key_token(item.key), item.weight, item.hard_requirement) for item in request.criteria
        )
        actual = tuple(
            (_key_token(item.key), item.weight, item.hard_requirement)
            for item in foundation.criterion_evaluations
        )
        if expected != actual:
            raise EvidenceScopeMismatchError(
                "criteria_mismatch",
                "Candidate criterion evaluations must match the shopping request order.",
            )

        scoped_specifications = tuple(
            item
            for item in specifications
            if item.request_id == request.id and item.product_id == foundation.product_id
        )
        scoped_evidence_items = tuple(
            item
            for item in evidence
            if item.request_id == request.id
            and item.subject_type is EvidenceSubjectType.PRODUCT
            and item.subject_id == foundation.product_id
        )
        evidence_ids = [item.id for item in scoped_evidence_items]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise EvidenceScopeMismatchError(
                "duplicate_evidence_id",
                "Scoped evidence identifiers must be unique.",
            )
        scoped_evidence = {item.id: item for item in scoped_evidence_items}
        scoped_checks = tuple(
            item
            for item in checks
            if item.request_id == request.id and item.product_id == foundation.product_id
        )
        assessments = tuple(
            self._assess_one(
                evaluation,
                tuple(
                    item
                    for item in scoped_specifications
                    if _key_token(item.canonical_key) == _key_token(evaluation.key)
                ),
                scoped_evidence,
                scoped_checks,
                assessed_at,
            )
            for evaluation in foundation.criterion_evaluations
        )

        total_weight = sum((item.weight for item in assessments), _ZERO)
        complete_weight = sum((item.weight for item in assessments if item.complete), _ZERO)
        completeness = _quantize(complete_weight / total_weight) if assessments else _ZERO
        source_reliability = _weighted_average(assessments, "source_reliability")
        freshness = _weighted_average(assessments, "freshness")
        consistency = _weighted_average(assessments, "consistency")
        confidence = _quantize(completeness * source_reliability * freshness * consistency)
        adjusted = (
            _quantize(foundation.utility * confidence) if foundation.utility is not None else None
        )
        return CandidateEvidenceConfidence(
            request_id=request.id,
            product_id=foundation.product_id,
            criteria=assessments,
            completeness=completeness,
            source_reliability=source_reliability,
            freshness=freshness,
            consistency=consistency,
            evidence_confidence=confidence,
            base_utility=foundation.utility,
            adjusted_utility=adjusted,
            assessed_at=assessed_at,
        )

    @staticmethod
    def _assess_one(
        evaluation: CriterionEvaluation,
        specifications: tuple[NormalizedSpecification, ...],
        evidence_by_id: dict[UUID, Evidence],
        checks: tuple[EvidenceCheck, ...],
        assessed_at: datetime,
    ) -> CriterionEvidenceAssessment:
        complete = evaluation.status in {
            CriterionEvaluationStatus.SATISFIED,
            CriterionEvaluationStatus.UNSATISFIED,
        }
        for specification in specifications:
            if specification.normalized_at > assessed_at:
                raise InvalidEvidenceTimelineError(
                    specification.id,
                    "derived_record_from_future",
                    "Derived evidence record cannot be later than confidence assessment time.",
                )
        referenced_ids = tuple(
            dict.fromkeys(
                evidence_id
                for specification in specifications
                for evidence_id in specification.evidence_ids
            )
        )
        linked_evidence = tuple(
            evidence_by_id[item] for item in referenced_ids if item in evidence_by_id
        )
        reliability = (
            max((_source_reliability(item) for item in linked_evidence), default=_ZERO)
            if complete
            else _ZERO
        )
        freshness = (
            max((_freshness(item, assessed_at) for item in linked_evidence), default=_ZERO)
            if complete
            else _ZERO
        )
        field_paths = frozenset(
            (
                *(path for item in specifications for path in item.source_field_paths),
                *(item.field_path for item in linked_evidence),
            )
        )
        relevant_checks = _relevant_checks(checks, field_paths)
        for check in relevant_checks:
            if check.checked_at > assessed_at:
                raise InvalidEvidenceTimelineError(
                    check.id,
                    "derived_record_from_future",
                    "Derived evidence record cannot be later than confidence assessment time.",
                )
        consistency, consistency_reason = _consistency(
            complete=complete,
            linked_evidence=linked_evidence,
            checks=relevant_checks,
        )
        reasons = [f"fact:{evaluation.status.value}"]
        reasons.append(f"evidence:linked:{len(linked_evidence)}")
        if len(linked_evidence) != len(referenced_ids):
            reasons.append("evidence:missing_reference")
        reasons.append(consistency_reason)
        confidence = _quantize(reliability * freshness * consistency if complete else _ZERO)
        return CriterionEvidenceAssessment(
            key=evaluation.key,
            weight=evaluation.weight,
            complete=complete,
            source_reliability=reliability,
            freshness=freshness,
            consistency=consistency,
            confidence=confidence,
            evidence_ids=tuple(item.id for item in linked_evidence),
            reason_codes=tuple(reasons),
        )
