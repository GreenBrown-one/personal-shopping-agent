"""Deterministic candidate ranking, Pareto fronts, and atomic scoring orchestration."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from personal_shopping_agent.domain import Evidence, Money, Offer, Product, ShoppingRequest
from personal_shopping_agent.domain.measurements import specification_key_token
from personal_shopping_agent.domain.workflow import (
    InvalidWorkflowTransitionError,
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStateMachine,
    workflow_now,
)
from personal_shopping_agent.presentation.confidence import (
    CandidateEvidenceConfidence,
    EvidenceConfidenceEvaluator,
)
from personal_shopping_agent.presentation.decision import (
    CandidateScoringFoundation,
    CandidateScoringFoundationBuilder,
    OfferCostAssessment,
)
from personal_shopping_agent.sourcing.cross_check import EvidenceCheck
from personal_shopping_agent.sourcing.normalization import NormalizedSpecification

_SCORE_QUANTUM = Decimal("0.000001")
_INDEX_QUANTUM = Decimal("0.000000000001")
_ZERO = Decimal("0")
_ONE_HUNDRED = Decimal("100")
_UTILITY_WEIGHT = Decimal("0.75")
_CONFIDENCE_WEIGHT = Decimal("0.25")


class RankingModel(BaseModel):
    """Strict immutable base for ranking inputs and auditable outputs."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class BudgetStatus(StrEnum):
    """Position of the selected price relative to normal and optional stretch limits."""

    WITHIN_BUDGET = "within_budget"
    WITHIN_STRETCH = "within_stretch"
    OVER_BUDGET = "over_budget"
    NOT_ASSESSED = "not_assessed"


class CandidateDecisionInput(RankingModel):
    """One candidate's compatible ability, offer-cost, and confidence foundations."""

    foundation: CandidateScoringFoundation
    confidence: CandidateEvidenceConfidence

    @model_validator(mode="after")
    def scopes_and_utility_match(self) -> "CandidateDecisionInput":
        """Prevent mixing foundations from different requests, products, or utility runs."""

        if self.foundation.request_id != self.confidence.request_id:
            raise ValueError("candidate foundation and confidence request identifiers must match")
        if self.foundation.product_id != self.confidence.product_id:
            raise ValueError("candidate foundation and confidence product identifiers must match")
        if self.foundation.utility != self.confidence.base_utility:
            raise ValueError("candidate foundation and confidence utility must match")
        return self


class CandidateScore(RankingModel):
    """One fully auditable candidate result, including exclusions and ranking metrics."""

    id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    workflow_id: UUID
    product_id: UUID
    foundation: CandidateScoringFoundation
    confidence: CandidateEvidenceConfidence
    budget_status: BudgetStatus
    eligible: bool
    exclusion_codes: tuple[str, ...] = ()
    best_offer_id: UUID | None = None
    selected_price: Money | None = None
    effective_cost: Money | None = None
    risk_coefficient: Decimal | None = Field(default=None, ge=0, le=Decimal("0.30"))
    risk_penalty: Decimal | None = Field(default=None, ge=0, le=30)
    value_index: Decimal | None = Field(default=None, ge=0)
    value_per_100: Decimal | None = Field(default=None, ge=0)
    final_score: Decimal | None = Field(default=None, ge=0, le=100)
    pareto_front: int | None = Field(default=None, ge=1)
    rank: int | None = Field(default=None, ge=1)
    scored_at: AwareDatetime

    @model_validator(mode="after")
    def result_is_internally_consistent(self) -> "CandidateScore":
        """Recalculate scope, offer, eligibility, and every published metric."""

        if len(self.exclusion_codes) != len(set(self.exclusion_codes)):
            raise ValueError("candidate exclusion codes must be unique")
        if self.eligible != (not self.exclusion_codes):
            raise ValueError("candidate eligibility must agree with exclusion codes")
        if (
            self.foundation.request_id != self.request_id
            or self.confidence.request_id != self.request_id
        ):
            raise ValueError("candidate score request scope must match its foundations")
        if (
            self.foundation.product_id != self.product_id
            or self.confidence.product_id != self.product_id
        ):
            raise ValueError("candidate score product scope must match its foundations")
        if self.confidence.assessed_at > self.scored_at:
            raise ValueError("candidate confidence cannot be assessed after scoring time")
        CandidateDecisionInput(foundation=self.foundation, confidence=self.confidence)

        best = _best_offer(self.foundation)
        if best is None:
            if self.budget_status is not BudgetStatus.NOT_ASSESSED or any(
                value is not None
                for value in (
                    self.best_offer_id,
                    self.selected_price,
                    self.effective_cost,
                    self.risk_coefficient,
                    self.risk_penalty,
                )
            ):
                raise ValueError("candidate without a best offer cannot contain offer metrics")
        elif (
            self.best_offer_id != best.offer_id
            or self.selected_price != best.selected_price
            or self.effective_cost != best.effective_cost
            or self.risk_coefficient != best.risk_coefficient
            or self.risk_penalty != _score(best.risk_coefficient * _ONE_HUNDRED)
            or self.budget_status is BudgetStatus.NOT_ASSESSED
        ):
            raise ValueError("candidate offer metrics must match its selected foundation offer")

        ranking_values = (
            self.value_index,
            self.value_per_100,
            self.final_score,
            self.pareto_front,
            self.rank,
        )
        if self.eligible:
            if any(value is None for value in ranking_values):
                raise ValueError("eligible candidate requires every ranking metric")
            if best is None or self.confidence.adjusted_utility is None:
                raise ValueError("eligible candidate requires comparable cost and adjusted utility")
            if best.effective_cost.amount <= 0:
                raise ValueError("eligible candidate requires positive effective cost")
            expected_index = _index(self.confidence.adjusted_utility / best.effective_cost.amount)
            expected_per_100 = _nonnegative_score(expected_index * _ONE_HUNDRED)
            expected_final = _final_score(
                self.foundation,
                self.confidence,
                best.risk_coefficient,
            )
            if (
                self.value_index != expected_index
                or self.value_per_100 != expected_per_100
                or self.final_score != expected_final
            ):
                raise ValueError("candidate ranking metrics must match declared formulas")
        elif any(value is not None for value in ranking_values):
            raise ValueError("ineligible candidate cannot contain ranking metrics")
        return self


class CandidateRankingBatch(RankingModel):
    """All candidate scores produced by one request/workflow transaction."""

    request_id: UUID
    workflow_id: UUID
    scores: tuple[CandidateScore, ...] = Field(min_length=1)
    scored_at: AwareDatetime

    @model_validator(mode="after")
    def score_set_is_consistent(self) -> "CandidateRankingBatch":
        """Require one score per product and contiguous ranks/front numbers."""

        if any(
            item.request_id != self.request_id
            or item.workflow_id != self.workflow_id
            or item.scored_at != self.scored_at
            for item in self.scores
        ):
            raise ValueError("candidate scores must share batch scope and timestamp")
        product_ids = [item.product_id for item in self.scores]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("candidate score product identifiers must be unique")
        score_ids = [item.id for item in self.scores]
        if len(score_ids) != len(set(score_ids)):
            raise ValueError("candidate score identifiers must be unique")
        eligible = tuple(item for item in self.scores if item.eligible)
        if tuple(item.rank for item in eligible) != tuple(range(1, len(eligible) + 1)):
            raise ValueError("eligible candidate ranks must be contiguous and ordered")
        fronts = {item.pareto_front for item in eligible if item.pareto_front is not None}
        if fronts and fronts != set(range(1, max(fronts) + 1)):
            raise ValueError("eligible candidate Pareto fronts must be contiguous")
        return self


class CandidateScoringResult(RankingModel):
    """Committed candidate scores and their advanced workflow snapshot."""

    snapshot: WorkflowSnapshot
    batch: CandidateRankingBatch


class NoCandidatesForScoringError(RuntimeError):
    """Raised when a normalized request has no request-linked Products."""


class DuplicateCandidateError(ValueError):
    """Raised when one product appears more than once in ranking inputs."""


def _score(value: Decimal) -> Decimal:
    return max(_ZERO, min(_ONE_HUNDRED, value)).quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_UP)


def _index(value: Decimal) -> Decimal:
    return max(_ZERO, value).quantize(_INDEX_QUANTUM, rounding=ROUND_HALF_UP)


def _nonnegative_score(value: Decimal) -> Decimal:
    return max(_ZERO, value).quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_UP)


def _best_offer(foundation: CandidateScoringFoundation) -> OfferCostAssessment | None:
    if foundation.best_offer_id is None:
        return None
    return next(
        item for item in foundation.offer_costs if item.offer_id == foundation.best_offer_id
    )


def _budget_status(request: ShoppingRequest, best: OfferCostAssessment | None) -> BudgetStatus:
    if best is None:
        return BudgetStatus.NOT_ASSESSED
    amount = best.selected_price.amount
    if amount <= request.budget.maximum.amount:
        return BudgetStatus.WITHIN_BUDGET
    if (
        request.budget.stretch_maximum is not None
        and amount <= request.budget.stretch_maximum.amount
    ):
        return BudgetStatus.WITHIN_STRETCH
    return BudgetStatus.OVER_BUDGET


def _final_score(
    foundation: CandidateScoringFoundation,
    confidence: CandidateEvidenceConfidence,
    risk_coefficient: Decimal,
) -> Decimal:
    if foundation.utility is None:  # pragma: no cover - eligible drafts always have utility
        return _ZERO
    raw = _ONE_HUNDRED * (
        _UTILITY_WEIGHT * foundation.utility
        + _CONFIDENCE_WEIGHT * confidence.evidence_confidence
        - risk_coefficient
    )
    return _score(raw)


@dataclass(frozen=True, slots=True)
class _Draft:
    decision: CandidateDecisionInput
    budget_status: BudgetStatus
    exclusions: tuple[str, ...]
    best: OfferCostAssessment | None
    value_index: Decimal | None
    value_per_100: Decimal | None
    final_score: Decimal | None

    @property
    def product_id(self) -> UUID:
        return self.decision.foundation.product_id

    @property
    def eligible(self) -> bool:
        return not self.exclusions


def _draft(request: ShoppingRequest, decision: CandidateDecisionInput) -> _Draft:
    foundation = decision.foundation
    confidence = decision.confidence
    best = _best_offer(foundation)
    budget_status = _budget_status(request, best)
    exclusions: list[str] = []
    if foundation.utility is None:
        exclusions.append("criteria_not_defined")
    if not foundation.hard_requirements_met:
        exclusions.append("hard_requirements_unmet")
    if best is None:
        exclusions.append("no_comparable_offer")
    elif best.effective_cost.amount <= 0:
        exclusions.append("nonpositive_effective_cost")
    if budget_status is BudgetStatus.OVER_BUDGET:
        exclusions.append("over_budget")
    if confidence.evidence_confidence <= 0:
        exclusions.append("evidence_confidence_zero")

    if exclusions or best is None or confidence.adjusted_utility is None:
        return _Draft(
            decision=decision,
            budget_status=budget_status,
            exclusions=tuple(exclusions),
            best=best,
            value_index=None,
            value_per_100=None,
            final_score=None,
        )
    value_index = _index(confidence.adjusted_utility / best.effective_cost.amount)
    return _Draft(
        decision=decision,
        budget_status=budget_status,
        exclusions=(),
        best=best,
        value_index=value_index,
        value_per_100=_nonnegative_score(value_index * _ONE_HUNDRED),
        final_score=_final_score(foundation, confidence, best.risk_coefficient),
    )


def _dominates(left: _Draft, right: _Draft) -> bool:
    left_confidence = left.decision.confidence
    right_confidence = right.decision.confidence
    if (  # pragma: no cover - Pareto input contains eligible drafts only
        left.best is None
        or right.best is None
        or left_confidence.adjusted_utility is None
        or right_confidence.adjusted_utility is None
    ):
        return False
    no_worse = (
        left_confidence.adjusted_utility >= right_confidence.adjusted_utility
        and left_confidence.evidence_confidence >= right_confidence.evidence_confidence
        and left.best.effective_cost.amount <= right.best.effective_cost.amount
    )
    strictly_better = (
        left_confidence.adjusted_utility > right_confidence.adjusted_utility
        or left_confidence.evidence_confidence > right_confidence.evidence_confidence
        or left.best.effective_cost.amount < right.best.effective_cost.amount
    )
    return no_worse and strictly_better


def _pareto_fronts(drafts: tuple[_Draft, ...]) -> dict[UUID, int]:
    result: dict[UUID, int] = {}
    for budget_status in (BudgetStatus.WITHIN_BUDGET, BudgetStatus.WITHIN_STRETCH):
        remaining = [item for item in drafts if item.budget_status is budget_status]
        front_number = 1
        while remaining:
            front = tuple(
                candidate
                for candidate in remaining
                if not any(
                    _dominates(other, candidate)
                    for other in remaining
                    if other.product_id != candidate.product_id
                )
            )
            for candidate in front:
                result[candidate.product_id] = front_number
                remaining.remove(candidate)
            front_number += 1
    return result


def _ranking_key(draft: _Draft, front: int) -> tuple[object, ...]:
    if (  # pragma: no cover - sorting receives eligible drafts only
        draft.best is None or draft.final_score is None or draft.value_per_100 is None
    ):
        raise RuntimeError("ranking key requires an eligible candidate")
    budget_priority = 0 if draft.budget_status is BudgetStatus.WITHIN_BUDGET else 1
    return (
        budget_priority,
        front,
        -draft.final_score,
        -draft.value_per_100,
        draft.best.effective_cost.amount,
        str(draft.product_id),
    )


class CandidateRankingEngine:
    """Apply hard gates, budget tiers, Pareto fronts, and stable ranking rules."""

    def rank(
        self,
        *,
        request: ShoppingRequest,
        workflow_id: UUID,
        candidates: tuple[CandidateDecisionInput, ...],
        scored_at: datetime,
    ) -> CandidateRankingBatch:
        """Rank candidates without mutating facts or performing persistence."""

        if not candidates:
            raise NoCandidatesForScoringError("No candidates are available for scoring.")
        if scored_at.tzinfo is None or scored_at.utcoffset() is None:
            raise ValueError("Candidate scoring time must include a timezone.")
        product_ids = [item.foundation.product_id for item in candidates]
        if len(product_ids) != len(set(product_ids)):
            raise DuplicateCandidateError("Candidate product identifiers must be unique.")
        if any(item.foundation.request_id != request.id for item in candidates):
            raise ValueError("Every ranking candidate must belong to the shopping request.")
        expected_criteria = tuple(
            (specification_key_token(item.key), item.weight, item.hard_requirement)
            for item in request.criteria
        )
        if any(
            tuple(
                (specification_key_token(item.key), item.weight, item.hard_requirement)
                for item in candidate.foundation.criterion_evaluations
            )
            != expected_criteria
            for candidate in candidates
        ):
            raise ValueError("Every ranking foundation must match the request criteria.")
        if any(
            best is not None and best.selected_price.currency != request.budget.maximum.currency
            for best in (_best_offer(item.foundation) for item in candidates)
        ):
            raise ValueError("Every selected offer must use the request budget currency.")
        if any(item.confidence.assessed_at > scored_at for item in candidates):
            raise ValueError("Candidate confidence cannot be assessed after ranking time.")

        drafts = tuple(_draft(request, item) for item in candidates)
        eligible = tuple(item for item in drafts if item.eligible)
        fronts = _pareto_fronts(eligible)
        ordered_eligible = tuple(
            sorted(eligible, key=lambda item: _ranking_key(item, fronts[item.product_id]))
        )
        rank_by_product = {
            item.product_id: rank for rank, item in enumerate(ordered_eligible, start=1)
        }
        ordered = (
            *ordered_eligible,
            *sorted(
                (item for item in drafts if not item.eligible),
                key=lambda item: str(item.product_id),
            ),
        )
        scores = tuple(
            self._score_from_draft(
                item,
                request_id=request.id,
                workflow_id=workflow_id,
                scored_at=scored_at,
                pareto_front=fronts.get(item.product_id),
                rank=rank_by_product.get(item.product_id),
            )
            for item in ordered
        )
        return CandidateRankingBatch(
            request_id=request.id,
            workflow_id=workflow_id,
            scores=scores,
            scored_at=scored_at,
        )

    @staticmethod
    def _score_from_draft(
        draft: _Draft,
        *,
        request_id: UUID,
        workflow_id: UUID,
        scored_at: datetime,
        pareto_front: int | None,
        rank: int | None,
    ) -> CandidateScore:
        best = draft.best
        return CandidateScore(
            request_id=request_id,
            workflow_id=workflow_id,
            product_id=draft.product_id,
            foundation=draft.decision.foundation,
            confidence=draft.decision.confidence,
            budget_status=draft.budget_status,
            eligible=draft.eligible,
            exclusion_codes=draft.exclusions,
            best_offer_id=best.offer_id if best is not None else None,
            selected_price=best.selected_price if best is not None else None,
            effective_cost=best.effective_cost if best is not None else None,
            risk_coefficient=best.risk_coefficient if best is not None else None,
            risk_penalty=(
                _score(best.risk_coefficient * _ONE_HUNDRED) if best is not None else None
            ),
            value_index=draft.value_index,
            value_per_100=draft.value_per_100,
            final_score=draft.final_score,
            pareto_front=pareto_front,
            rank=rank,
            scored_at=scored_at,
        )


class CandidateScoringUnitOfWork(Protocol):
    """Transaction port for scoring inputs, candidate scores, and workflow state."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def get_snapshot(self, workflow_id: UUID) -> WorkflowSnapshot: ...

    def list_products_for_request(self, request_id: UUID) -> tuple[Product, ...]: ...

    def list_offers_for_request_product(
        self, request_id: UUID, product_id: UUID
    ) -> tuple[Offer, ...]: ...

    def list_specifications(
        self, request_id: UUID, workflow_id: UUID, product_id: UUID
    ) -> tuple[NormalizedSpecification, ...]: ...

    def list_product_evidence(self, request_id: UUID, product_id: UUID) -> tuple[Evidence, ...]: ...

    def list_evidence_checks(
        self, request_id: UUID, workflow_id: UUID, product_id: UUID
    ) -> tuple[EvidenceCheck, ...]: ...

    def add_score(self, score: CandidateScore) -> None: ...

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None: ...

    def commit(self) -> None: ...


class CandidateScoringUnitOfWorkFactory(Protocol):
    """Create one isolated candidate-scoring transaction."""

    def __call__(self) -> CandidateScoringUnitOfWork: ...


class CandidateScoringService:
    """Build, rank, persist, and advance all request candidates atomically."""

    def __init__(
        self,
        unit_of_work_factory: CandidateScoringUnitOfWorkFactory,
        *,
        foundation_builder: CandidateScoringFoundationBuilder | None = None,
        confidence_evaluator: EvidenceConfidenceEvaluator | None = None,
        ranking_engine: CandidateRankingEngine | None = None,
        state_machine: WorkflowStateMachine | None = None,
        clock: Callable[[], datetime] = workflow_now,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._foundation_builder = foundation_builder or CandidateScoringFoundationBuilder()
        self._confidence_evaluator = confidence_evaluator or EvidenceConfidenceEvaluator()
        self._ranking_engine = ranking_engine or CandidateRankingEngine()
        self._state_machine = state_machine or WorkflowStateMachine()
        self._clock = clock

    def score(self, workflow_id: UUID) -> CandidateScoringResult:
        """Commit every candidate score together with the candidates_scored event."""

        with self._unit_of_work_factory() as unit_of_work:
            snapshot = unit_of_work.get_snapshot(workflow_id)
            if (
                self._state_machine.next_state(snapshot.workflow.state)
                is not WorkflowState.CANDIDATES_SCORED
            ):
                raise InvalidWorkflowTransitionError(
                    "candidate scoring requires a data_normalized workflow"
                )
            products = unit_of_work.list_products_for_request(snapshot.request.id)
            if not products:
                raise NoCandidatesForScoringError(
                    "No request-linked products are available for scoring."
                )
            scored_at = self._clock()
            candidates: list[CandidateDecisionInput] = []
            for product in products:
                specifications = unit_of_work.list_specifications(
                    snapshot.request.id,
                    snapshot.workflow.id,
                    product.id,
                )
                evidence = unit_of_work.list_product_evidence(snapshot.request.id, product.id)
                foundation = self._foundation_builder.build(
                    snapshot.request,
                    product,
                    specifications,
                    unit_of_work.list_offers_for_request_product(snapshot.request.id, product.id),
                )
                confidence = self._confidence_evaluator.assess(
                    request=snapshot.request,
                    foundation=foundation,
                    specifications=specifications,
                    evidence=evidence,
                    checks=unit_of_work.list_evidence_checks(
                        snapshot.request.id,
                        snapshot.workflow.id,
                        product.id,
                    ),
                    assessed_at=scored_at,
                )
                candidates.append(
                    CandidateDecisionInput(
                        foundation=foundation,
                        confidence=confidence,
                    )
                )
            batch = self._ranking_engine.rank(
                request=snapshot.request,
                workflow_id=snapshot.workflow.id,
                candidates=tuple(candidates),
                scored_at=scored_at,
            )
            for score in batch.scores:
                unit_of_work.add_score(score)
            workflow, event = self._state_machine.advance(
                snapshot.workflow,
                WorkflowState.CANDIDATES_SCORED,
                reason="candidates_ranked",
                occurred_at=scored_at,
            )
            unit_of_work.save_transition(
                workflow,
                event,
                expected_revision=snapshot.workflow.revision,
            )
            unit_of_work.commit()
            return CandidateScoringResult(
                snapshot=WorkflowSnapshot(
                    request=snapshot.request,
                    workflow=workflow,
                    events=(*snapshot.events, event),
                ),
                batch=batch,
            )
