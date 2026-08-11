"""Deterministic criterion utility and risk-adjusted offer cost foundations."""

from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from personal_shopping_agent.application.normalization import (
    NormalizedSpecification,
    SpecificationNormalizationStatus,
)
from personal_shopping_agent.domain import (
    Money,
    Offer,
    PriceKind,
    Product,
    ShoppingCriterion,
    ShoppingRequest,
    StoreType,
)

_SCORE_QUANTUM = Decimal("0.000001")
_HALF = Decimal("0.5")
_ONE = Decimal("1")
_RISK_LIMIT = Decimal("0.30")


class DecisionModel(BaseModel):
    """Strict immutable base for explainable decision-engine outputs."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CriterionEvaluationStatus(StrEnum):
    """Resolution state for one user criterion against normalized product facts."""

    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    MISSING = "missing"
    CONFLICT = "conflict"
    UNRESOLVED = "unresolved"


class CriterionEvaluation(DecisionModel):
    """One weighted criterion score with explicit failure or uncertainty semantics."""

    key: str = Field(min_length=1, max_length=120)
    weight: Decimal = Field(gt=0)
    hard_requirement: bool
    observed_values: tuple[Decimal, ...] = ()
    status: CriterionEvaluationStatus
    score: Decimal = Field(ge=0, le=1)
    reason_code: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def observed_values_match_status(self) -> "CriterionEvaluation":
        """Require one resolved value, multiple conflicts, or no missing value."""

        if len(self.observed_values) != len(set(self.observed_values)):
            raise ValueError("criterion observed values must be unique")
        if self.status in {
            CriterionEvaluationStatus.SATISFIED,
            CriterionEvaluationStatus.UNSATISFIED,
        }:
            if len(self.observed_values) != 1:
                raise ValueError("resolved criterion status requires exactly one value")
        elif self.status is CriterionEvaluationStatus.CONFLICT:
            if len(self.observed_values) < 2:
                raise ValueError("conflict criterion status requires multiple values")
        elif self.status is CriterionEvaluationStatus.MISSING and self.observed_values:
            raise ValueError("missing criterion status cannot contain observed values")
        if (
            self.status
            in {
                CriterionEvaluationStatus.MISSING,
                CriterionEvaluationStatus.CONFLICT,
                CriterionEvaluationStatus.UNRESOLVED,
            }
            and self.score != 0
        ):
            raise ValueError("unresolved criterion statuses require a zero score")
        return self


class OfferCostAssessment(DecisionModel):
    """Selected price layer and deterministic uncertainty-adjusted comparison cost."""

    offer_id: UUID
    product_id: UUID
    selected_price_kind: PriceKind
    selected_price: Money
    risk_coefficient: Decimal = Field(ge=0, le=_RISK_LIMIT)
    effective_cost: Money
    comparable: bool
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def cost_matches_price_and_risk(self) -> "OfferCostAssessment":
        """Prevent a stored assessment from disagreeing with its declared formula."""

        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("offer cost reason codes must be unique")
        if self.selected_price.currency != self.effective_cost.currency:
            raise ValueError("selected price and effective cost currencies must match")
        expected = self.selected_price.amount * (_ONE + self.risk_coefficient)
        if self.effective_cost.amount != expected:
            raise ValueError("effective cost must equal selected price adjusted by risk")
        return self


class CandidateScoringFoundation(DecisionModel):
    """Pre-confidence utility and offer costs; intentionally not a final ranking."""

    request_id: UUID
    product_id: UUID
    criterion_evaluations: tuple[CriterionEvaluation, ...]
    utility: Decimal | None = Field(default=None, ge=0, le=1)
    hard_requirements_met: bool
    offer_costs: tuple[OfferCostAssessment, ...]
    best_offer_id: UUID | None = None

    @model_validator(mode="after")
    def aggregate_fields_match_children(self) -> "CandidateScoringFoundation":
        """Keep utility, hard gates, and best-offer selection internally consistent."""

        criterion_keys = [_key_token(item.key) for item in self.criterion_evaluations]
        if len(criterion_keys) != len(set(criterion_keys)):
            raise ValueError("criterion evaluation keys must be unique after normalization")
        if bool(self.criterion_evaluations) != (self.utility is not None):
            raise ValueError("utility exists exactly when criterion evaluations exist")
        expected_hard = all(
            not item.hard_requirement or item.status is CriterionEvaluationStatus.SATISFIED
            for item in self.criterion_evaluations
        )
        if self.hard_requirements_met is not expected_hard:
            raise ValueError("hard requirement aggregate does not match evaluations")
        offer_ids = [item.offer_id for item in self.offer_costs]
        if len(offer_ids) != len(set(offer_ids)):
            raise ValueError("assessed offer identifiers must be unique")
        if any(item.product_id != self.product_id for item in self.offer_costs):
            raise ValueError("assessed offers must belong to the candidate product")
        comparable = tuple(item for item in self.offer_costs if item.comparable)
        expected_best_offer_id = (
            min(
                comparable, key=lambda item: (item.effective_cost.amount, str(item.offer_id))
            ).offer_id
            if comparable
            else None
        )
        if self.best_offer_id != expected_best_offer_id:
            raise ValueError("best offer must be the lowest-cost comparable assessed offer")
        return self


class InvalidCriterionDefinitionError(ValueError):
    """Raised when criterion bounds cannot be interpreted without guessing direction."""

    def __init__(self, key: str, code: str, message: str) -> None:
        super().__init__(message)
        self.key = key
        self.code = code


def _key_token(value: str) -> str:
    return " ".join(value.casefold().replace("_", " ").split())


def _quantize_score(value: Decimal) -> Decimal:
    return min(_ONE, max(Decimal("0"), value)).quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_UP)


def _numeric_bounds(
    criterion: ShoppingCriterion,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    raw_values = (criterion.minimum, criterion.preferred, criterion.maximum)
    if any(value is not None and not isinstance(value, Decimal) for value in raw_values):
        raise InvalidCriterionDefinitionError(
            criterion.key,
            "non_numeric_bound",
            "Normalized numeric specifications require numeric criterion bounds.",
        )
    minimum, preferred, maximum = cast(
        tuple[Decimal | None, Decimal | None, Decimal | None], raw_values
    )
    if any(value is not None and value < 0 for value in (minimum, preferred, maximum)):
        raise InvalidCriterionDefinitionError(
            criterion.key,
            "negative_bound",
            "Normalized measurement criterion bounds cannot be negative.",
        )
    if minimum is not None and maximum is not None and minimum > maximum:
        raise InvalidCriterionDefinitionError(
            criterion.key,
            "bounds_reversed",
            "Criterion minimum cannot exceed maximum.",
        )
    if preferred is not None and (
        (minimum is not None and preferred < minimum)
        or (maximum is not None and preferred > maximum)
    ):
        raise InvalidCriterionDefinitionError(
            criterion.key,
            "preferred_outside_bounds",
            "Criterion preferred value must remain inside declared bounds.",
        )
    if minimum is None and maximum is None:
        code = "preferred_direction_undefined" if preferred is not None else "bounds_missing"
        raise InvalidCriterionDefinitionError(
            criterion.key,
            code,
            "A numeric criterion requires a minimum or maximum to define direction.",
        )
    return minimum, preferred, maximum


def _score_value(
    value: Decimal,
    minimum: Decimal | None,
    preferred: Decimal | None,
    maximum: Decimal | None,
) -> tuple[Decimal, CriterionEvaluationStatus, str]:
    if minimum is not None and value < minimum:
        return (
            _quantize_score(_HALF * value / minimum),
            CriterionEvaluationStatus.UNSATISFIED,
            "below_minimum",
        )
    if maximum is not None and value > maximum:
        return (
            _quantize_score(_HALF * maximum / value),
            CriterionEvaluationStatus.UNSATISFIED,
            "above_maximum",
        )
    if preferred is None:
        return _ONE, CriterionEvaluationStatus.SATISFIED, "within_bounds"
    if minimum is not None and value < preferred:
        score = _HALF + _HALF * (value - minimum) / (preferred - minimum)
        return _quantize_score(score), CriterionEvaluationStatus.SATISFIED, "below_preferred"
    if maximum is not None and value > preferred:
        score = _HALF + _HALF * (maximum - value) / (maximum - preferred)
        return _quantize_score(score), CriterionEvaluationStatus.SATISFIED, "above_preferred"
    return _ONE, CriterionEvaluationStatus.SATISFIED, "preferred_met"


class CriterionEvaluator:
    """Evaluate numeric normalized facts against explicit request bounds."""

    def evaluate(
        self,
        criteria: tuple[ShoppingCriterion, ...],
        specifications: tuple[NormalizedSpecification, ...],
    ) -> tuple[CriterionEvaluation, ...]:
        """Return deterministic partial scores without resolving conflicts or missing data."""

        normalized_keys = [_key_token(criterion.key) for criterion in criteria]
        if len(normalized_keys) != len(set(normalized_keys)):
            raise InvalidCriterionDefinitionError(
                "*",
                "normalized_key_duplicate",
                "Criterion keys collide after normalization.",
            )
        by_key: dict[str, list[NormalizedSpecification]] = {}
        for specification in specifications:
            by_key.setdefault(_key_token(specification.canonical_key), []).append(specification)
        return tuple(
            self._evaluate_one(criterion, tuple(by_key.get(_key_token(criterion.key), ())))
            for criterion in criteria
        )

    @staticmethod
    def _evaluate_one(
        criterion: ShoppingCriterion,
        facts: tuple[NormalizedSpecification, ...],
    ) -> CriterionEvaluation:
        minimum, preferred, maximum = _numeric_bounds(criterion)
        if not facts:
            return CriterionEvaluation(
                key=criterion.key,
                weight=criterion.weight,
                hard_requirement=criterion.hard_requirement,
                status=CriterionEvaluationStatus.MISSING,
                score=Decimal("0"),
                reason_code="normalized_fact_missing",
            )
        observed_values = tuple(
            dict.fromkeys(value for fact in facts for value in fact.normalized_values)
        )
        if (
            any(fact.status is SpecificationNormalizationStatus.CONFLICT for fact in facts)
            or len(observed_values) > 1
        ):
            return CriterionEvaluation(
                key=criterion.key,
                weight=criterion.weight,
                hard_requirement=criterion.hard_requirement,
                observed_values=observed_values,
                status=CriterionEvaluationStatus.CONFLICT,
                score=Decimal("0"),
                reason_code="normalized_values_conflict",
            )
        if (
            any(
                fact.status
                in {
                    SpecificationNormalizationStatus.UNPARSEABLE_VALUE,
                    SpecificationNormalizationStatus.UNSUPPORTED_KEY,
                }
                for fact in facts
            )
            or len(observed_values) != 1
        ):
            return CriterionEvaluation(
                key=criterion.key,
                weight=criterion.weight,
                hard_requirement=criterion.hard_requirement,
                observed_values=observed_values,
                status=CriterionEvaluationStatus.UNRESOLVED,
                score=Decimal("0"),
                reason_code="normalized_value_unresolved",
            )
        canonical_units = {fact.canonical_unit for fact in facts}
        if criterion.unit is None or canonical_units != {criterion.unit}:
            return CriterionEvaluation(
                key=criterion.key,
                weight=criterion.weight,
                hard_requirement=criterion.hard_requirement,
                observed_values=observed_values,
                status=CriterionEvaluationStatus.UNRESOLVED,
                score=Decimal("0"),
                reason_code="normalized_unit_mismatch",
            )
        score, status, reason = _score_value(observed_values[0], minimum, preferred, maximum)
        return CriterionEvaluation(
            key=criterion.key,
            weight=criterion.weight,
            hard_requirement=criterion.hard_requirement,
            observed_values=observed_values,
            status=status,
            score=score,
            reason_code=reason,
        )


_STORE_RISK = {
    StoreType.PLATFORM_SELF_OPERATED: Decimal("0"),
    StoreType.BRAND_FLAGSHIP: Decimal("0.01"),
    StoreType.AUTHORIZED_RETAILER: Decimal("0.02"),
    StoreType.THIRD_PARTY: Decimal("0.06"),
    StoreType.UNKNOWN: Decimal("0.08"),
}
_PRICE_RISK = {
    PriceKind.ESTIMATED_TOTAL: Decimal("0"),
    PriceKind.UNCONDITIONAL: Decimal("0.01"),
    PriceKind.DISPLAYED: Decimal("0.04"),
    PriceKind.CONDITIONAL: Decimal("0.08"),
    PriceKind.LIST: Decimal("0.10"),
}


class OfferCostEstimator:
    """Select an explicit price layer and apply documented uncertainty penalties."""

    def assess(self, offer: Offer, *, expected_currency: str) -> OfferCostAssessment:
        """Assess one offer without inventing shipping, discounts, or final checkout totals."""

        price_kind, selected_price = offer.price.comparison_price
        risk = _STORE_RISK[offer.store_type] + _PRICE_RISK[price_kind]
        reasons = [f"store:{offer.store_type.value}", f"price:{price_kind.value}"]
        comparable = True
        if offer.in_stock is None:
            risk += Decimal("0.04")
            reasons.append("stock:unknown")
        elif offer.in_stock is False:
            risk += Decimal("0.10")
            reasons.append("stock:unavailable")
            comparable = False
        if offer.region is None:
            risk += Decimal("0.03")
            reasons.append("region:missing")
        if price_kind is PriceKind.CONDITIONAL and not offer.promotion_conditions:
            risk += Decimal("0.05")
            reasons.append("conditional_terms:missing")
            comparable = False
        if selected_price.currency != expected_currency:
            reasons.append("currency:mismatch")
            comparable = False
        risk = min(_RISK_LIMIT, risk)
        return OfferCostAssessment(
            offer_id=offer.id,
            product_id=offer.product_id,
            selected_price_kind=price_kind,
            selected_price=selected_price,
            risk_coefficient=risk,
            effective_cost=Money(
                amount=selected_price.amount * (_ONE + risk),
                currency=selected_price.currency,
            ),
            comparable=comparable,
            reason_codes=tuple(reasons),
        )


class CandidateScoringFoundationBuilder:
    """Combine pre-confidence criterion utility with assessed offer costs."""

    def __init__(
        self,
        *,
        criterion_evaluator: CriterionEvaluator | None = None,
        cost_estimator: OfferCostEstimator | None = None,
    ) -> None:
        self._criterion_evaluator = criterion_evaluator or CriterionEvaluator()
        self._cost_estimator = cost_estimator or OfferCostEstimator()

    def build(
        self,
        request: ShoppingRequest,
        product: Product,
        specifications: tuple[NormalizedSpecification, ...],
        offers: tuple[Offer, ...],
    ) -> CandidateScoringFoundation:
        """Build explainable inputs while withholding evidence-adjusted final ranking."""

        evaluations = self._criterion_evaluator.evaluate(
            request.criteria,
            tuple(
                item
                for item in specifications
                if item.request_id == request.id and item.product_id == product.id
            ),
        )
        utility: Decimal | None = None
        if evaluations:
            weighted = sum((item.weight * item.score for item in evaluations), Decimal("0"))
            total_weight = sum((item.weight for item in evaluations), Decimal("0"))
            utility = _quantize_score(weighted / total_weight)
        hard_requirements_met = all(
            not item.hard_requirement or item.status is CriterionEvaluationStatus.SATISFIED
            for item in evaluations
        )
        expected_currency = request.budget.maximum.currency
        costs = tuple(
            self._cost_estimator.assess(offer, expected_currency=expected_currency)
            for offer in offers
            if offer.product_id == product.id
        )
        comparable = tuple(item for item in costs if item.comparable)
        best_offer_id = (
            min(
                comparable, key=lambda item: (item.effective_cost.amount, str(item.offer_id))
            ).offer_id
            if comparable
            else None
        )
        return CandidateScoringFoundation(
            request_id=request.id,
            product_id=product.id,
            criterion_evaluations=evaluations,
            utility=utility,
            hard_requirements_met=hard_requirements_met,
            offer_costs=costs,
            best_offer_id=best_offer_id,
        )
