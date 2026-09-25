"""Unit tests for final M4 ranking rules and atomic scoring orchestration."""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
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
    Offer,
    PriceBreakdown,
    PriceKind,
    Product,
    ShoppingCriterion,
    ShoppingRequest,
    ShoppingWorkflow,
    StoreType,
    WorkflowEvent,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStateMachine,
)
from personal_shopping_agent.presentation import (
    BudgetStatus,
    CandidateDecisionInput,
    CandidateEvidenceConfidence,
    CandidateRankingBatch,
    CandidateRankingEngine,
    CandidateScore,
    CandidateScoringFoundation,
    CandidateScoringService,
    CriterionEvaluation,
    CriterionEvaluationStatus,
    CriterionEvidenceAssessment,
    DuplicateCandidateError,
    NoCandidatesForScoringError,
    OfferCostAssessment,
)
from personal_shopping_agent.sourcing import (
    EvidenceCheck,
    EvidenceCheckStatus,
    NormalizedSpecification,
    SpecificationNormalizationStatus,
)

NOW = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
SCORE_QUANTUM = Decimal("0.000001")


def q(value: Decimal) -> Decimal:
    return value.quantize(SCORE_QUANTUM, rounding=ROUND_HALF_UP)


def criterion(*, hard_requirement: bool = False) -> ShoppingCriterion:
    return ShoppingCriterion(
        key="battery_capacity",
        weight=Decimal("1"),
        hard_requirement=hard_requirement,
        minimum=Decimal("4000"),
        preferred=Decimal("6000"),
        unit="mAh",
    )


def build_request(
    *,
    maximum: str = "100",
    stretch: str | None = None,
    criteria: tuple[ShoppingCriterion, ...] | None = None,
    request_id: UUID | None = None,
) -> ShoppingRequest:
    return ShoppingRequest(
        id=request_id or uuid4(),
        query="example phone",
        category="smartphone",
        budget=Budget(
            maximum=Money(amount=Decimal(maximum)),
            stretch_maximum=(Money(amount=Decimal(stretch)) if stretch is not None else None),
        ),
        criteria=(criterion(),) if criteria is None else criteria,
        created_at=NOW,
    )


def build_offer_cost(
    product_id: UUID,
    *,
    amount: str = "80",
    risk: str = "0",
    currency: str = "CNY",
    offer_id: UUID | None = None,
) -> OfferCostAssessment:
    price = Money(amount=Decimal(amount), currency=currency)
    coefficient = Decimal(risk)
    return OfferCostAssessment(
        offer_id=offer_id or uuid4(),
        product_id=product_id,
        selected_price_kind=PriceKind.ESTIMATED_TOTAL,
        selected_price=price,
        risk_coefficient=coefficient,
        effective_cost=Money(
            amount=price.amount * (Decimal("1") + coefficient),
            currency=currency,
        ),
        comparable=True,
        reason_codes=("test",),
    )


def build_decision(
    request: ShoppingRequest,
    product_id: UUID,
    *,
    utility: str | None = "0.8",
    confidence: str = "0.9",
    amount: str | None = "80",
    risk: str = "0",
    currency: str = "CNY",
    hard_met: bool = True,
    offer_id: UUID | None = None,
) -> CandidateDecisionInput:
    if utility is None:
        evaluations: tuple[CriterionEvaluation, ...] = ()
        evidence_criteria: tuple[CriterionEvidenceAssessment, ...] = ()
        utility_value = None
        adjusted = None
    else:
        utility_value = Decimal(utility)
        evaluation = CriterionEvaluation(
            key="battery_capacity",
            weight=Decimal("1"),
            hard_requirement=request.criteria[0].hard_requirement,
            observed_values=(Decimal("5000"),),
            status=(
                CriterionEvaluationStatus.SATISFIED
                if hard_met
                else CriterionEvaluationStatus.UNSATISFIED
            ),
            score=utility_value,
            reason_code="test",
        )
        evaluations = (evaluation,)
        confidence_value = Decimal(confidence)
        evidence_criteria = (
            CriterionEvidenceAssessment(
                key="battery_capacity",
                weight=Decimal("1"),
                complete=True,
                source_reliability=confidence_value,
                freshness=Decimal("1"),
                consistency=Decimal("1"),
                confidence=q(confidence_value),
                reason_codes=("test",),
            ),
        )
        adjusted = q(utility_value * confidence_value)

    costs = (
        (
            build_offer_cost(
                product_id,
                amount=amount,
                risk=risk,
                currency=currency,
                offer_id=offer_id,
            ),
        )
        if amount is not None
        else ()
    )
    foundation = CandidateScoringFoundation(
        request_id=request.id,
        product_id=product_id,
        criterion_evaluations=evaluations,
        utility=utility_value,
        hard_requirements_met=hard_met,
        offer_costs=costs,
        best_offer_id=costs[0].offer_id if costs else None,
    )
    confidence_value = Decimal(confidence) if utility is not None else Decimal("0")
    evidence_confidence = CandidateEvidenceConfidence(
        request_id=request.id,
        product_id=product_id,
        criteria=evidence_criteria,
        completeness=Decimal("1") if evidence_criteria else Decimal("0"),
        source_reliability=confidence_value,
        freshness=Decimal("1") if evidence_criteria else Decimal("0"),
        consistency=Decimal("1") if evidence_criteria else Decimal("0"),
        evidence_confidence=q(confidence_value) if evidence_criteria else Decimal("0"),
        base_utility=utility_value,
        adjusted_utility=adjusted,
        assessed_at=NOW,
    )
    return CandidateDecisionInput(
        foundation=foundation,
        confidence=evidence_confidence,
    )


def rank(
    request: ShoppingRequest,
    *decisions: CandidateDecisionInput,
    scored_at: datetime = NOW,
) -> CandidateRankingBatch:
    return CandidateRankingEngine().rank(
        request=request,
        workflow_id=uuid4(),
        candidates=decisions,
        scored_at=scored_at,
    )


def test_ranking_calculates_budget_value_risk_and_final_score() -> None:
    request = build_request(maximum="100")
    decision = build_decision(
        request,
        uuid4(),
        utility="0.8",
        confidence="0.9",
        amount="100",
        risk="0.10",
    )

    result = rank(request, decision).scores[0]

    assert result.eligible is True
    assert result.budget_status is BudgetStatus.WITHIN_BUDGET
    assert result.selected_price == Money(amount=Decimal("100"))
    assert result.effective_cost == Money(amount=Decimal("110.00"))
    assert result.risk_penalty == Decimal("10.000000")
    assert result.value_index == Decimal("0.006545454545")
    assert result.value_per_100 == Decimal("0.654545")
    assert result.final_score == Decimal("72.500000")
    assert result.pareto_front == result.rank == 1


def test_selected_price_sets_budget_tier_while_effective_cost_keeps_risk() -> None:
    request = build_request(maximum="100", stretch="120")
    within = build_decision(request, uuid4(), amount="100", risk="0.20")
    stretch = build_decision(request, uuid4(), amount="110", risk="0")
    over = build_decision(request, uuid4(), amount="121", risk="0")

    batch = rank(request, within, stretch, over)
    by_product = {item.product_id: item for item in batch.scores}
    within_cost = by_product[within.foundation.product_id].effective_cost

    assert by_product[within.foundation.product_id].budget_status is BudgetStatus.WITHIN_BUDGET
    assert within_cost is not None
    assert within_cost.amount == Decimal("120.00")
    assert by_product[stretch.foundation.product_id].budget_status is BudgetStatus.WITHIN_STRETCH
    assert by_product[stretch.foundation.product_id].eligible is True
    assert by_product[over.foundation.product_id].budget_status is BudgetStatus.OVER_BUDGET
    assert by_product[over.foundation.product_id].exclusion_codes == ("over_budget",)


def test_hard_gates_and_missing_inputs_produce_stable_exclusions() -> None:
    hard_criterion = criterion(hard_requirement=True)
    request = build_request(criteria=(hard_criterion,))
    hard_failed = build_decision(request, uuid4(), hard_met=False)
    no_offer = build_decision(request, uuid4(), amount=None)
    zero_cost = build_decision(request, uuid4(), amount="0")
    zero_confidence = build_decision(request, uuid4(), confidence="0")

    batch = rank(request, hard_failed, no_offer, zero_cost, zero_confidence)
    by_product = {item.product_id: item for item in batch.scores}

    assert by_product[hard_failed.foundation.product_id].exclusion_codes == (
        "hard_requirements_unmet",
    )
    assert by_product[no_offer.foundation.product_id].exclusion_codes == ("no_comparable_offer",)
    assert by_product[zero_cost.foundation.product_id].exclusion_codes == (
        "nonpositive_effective_cost",
    )
    assert by_product[zero_confidence.foundation.product_id].exclusion_codes == (
        "evidence_confidence_zero",
    )
    assert all(item.rank is None and item.final_score is None for item in batch.scores)


def test_request_without_criteria_is_not_ranked() -> None:
    request = build_request(criteria=())
    decision = build_decision(request, uuid4(), utility=None, amount=None)

    score = rank(request, decision).scores[0]

    assert score.exclusion_codes == (
        "criteria_not_defined",
        "no_comparable_offer",
        "evidence_confidence_zero",
    )
    assert score.budget_status is BudgetStatus.NOT_ASSESSED


def test_pareto_fronts_preserve_tradeoffs_and_mark_dominated_candidates() -> None:
    request = build_request(maximum="200")
    dominant = build_decision(
        request,
        UUID("00000000-0000-0000-0000-000000000001"),
        utility="1",
        confidence="0.9",
        amount="100",
    )
    dominated = build_decision(
        request,
        UUID("00000000-0000-0000-0000-000000000002"),
        utility="0.9",
        confidence="0.8",
        amount="120",
    )
    tradeoff = build_decision(
        request,
        UUID("00000000-0000-0000-0000-000000000003"),
        utility="0.8",
        confidence="0.95",
        amount="90",
    )

    batch = rank(request, dominated, tradeoff, dominant)
    by_product = {item.product_id: item for item in batch.scores}

    assert by_product[dominant.foundation.product_id].pareto_front == 1
    assert by_product[tradeoff.foundation.product_id].pareto_front == 1
    assert by_product[dominated.foundation.product_id].pareto_front == 2
    assert [item.product_id for item in batch.scores] == [
        dominant.foundation.product_id,
        tradeoff.foundation.product_id,
        dominated.foundation.product_id,
    ]


def test_normal_budget_tier_precedes_a_stronger_stretch_candidate() -> None:
    request = build_request(maximum="100", stretch="150")
    normal = build_decision(request, uuid4(), utility="0.5", confidence="0.5", amount="100")
    stretch = build_decision(request, uuid4(), utility="1", confidence="1", amount="101")

    batch = rank(request, stretch, normal)

    assert batch.scores[0].product_id == normal.foundation.product_id
    assert batch.scores[0].rank == 1
    assert batch.scores[1].product_id == stretch.foundation.product_id
    assert batch.scores[0].pareto_front == batch.scores[1].pareto_front == 1


def test_exact_ranking_ties_use_stable_product_identifier() -> None:
    request = build_request()
    lower = build_decision(request, UUID("00000000-0000-0000-0000-000000000001"))
    higher = build_decision(request, UUID("00000000-0000-0000-0000-000000000002"))

    batch = rank(request, higher, lower)

    assert [item.product_id for item in batch.scores] == [
        lower.foundation.product_id,
        higher.foundation.product_id,
    ]


def test_engine_rejects_empty_duplicate_mismatched_and_future_inputs() -> None:
    request = build_request()
    decision = build_decision(request, uuid4())
    engine = CandidateRankingEngine()

    with pytest.raises(NoCandidatesForScoringError):
        engine.rank(request=request, workflow_id=uuid4(), candidates=(), scored_at=NOW)
    with pytest.raises(DuplicateCandidateError):
        rank(request, decision, decision)
    wrong_request_id = uuid4()
    with pytest.raises(ValueError, match="belong to the shopping request"):
        rank(
            request,
            CandidateDecisionInput(
                foundation=decision.foundation.model_copy(update={"request_id": wrong_request_id}),
                confidence=decision.confidence.model_copy(update={"request_id": wrong_request_id}),
            ),
        )
    changed_request = build_request(
        request_id=request.id,
        criteria=(criterion(hard_requirement=True),),
    )
    with pytest.raises(ValueError, match="match the request criteria"):
        rank(changed_request, decision)
    usd = build_decision(request, uuid4(), currency="USD")
    with pytest.raises(ValueError, match="budget currency"):
        rank(request, usd)
    with pytest.raises(ValueError, match="timezone"):
        rank(request, decision, scored_at=datetime(2026, 8, 10, 0, 0))
    future = CandidateDecisionInput(
        foundation=decision.foundation,
        confidence=decision.confidence.model_copy(
            update={"assessed_at": NOW + timedelta(seconds=1)}
        ),
    )
    with pytest.raises(ValueError, match="after ranking"):
        rank(request, future)


def test_decision_input_rejects_mismatched_foundations() -> None:
    request = build_request()
    decision = build_decision(request, uuid4())
    invalid: tuple[tuple[dict[str, object], dict[str, object], str], ...] = (
        (
            {},
            {"request_id": uuid4()},
            "request identifiers",
        ),
        (
            {},
            {"product_id": uuid4()},
            "product identifiers",
        ),
        (
            {},
            {
                "base_utility": Decimal("0.7"),
                "adjusted_utility": Decimal("0.63"),
            },
            "utility must match",
        ),
    )
    for foundation_updates, confidence_updates, message in invalid:
        with pytest.raises(ValidationError, match=message):
            CandidateDecisionInput(
                foundation=decision.foundation.model_copy(update=foundation_updates),
                confidence=decision.confidence.model_copy(update=confidence_updates),
            )


def test_candidate_score_rejects_inconsistent_scope_offer_and_metrics() -> None:
    request = build_request()
    valid = rank(request, build_decision(request, uuid4())).scores[0]
    base = valid.model_dump()
    invalid = (
        ({"exclusion_codes": ("x", "x"), "eligible": False}, "must be unique"),
        ({"eligible": False}, "eligibility must agree"),
        ({"request_id": uuid4()}, "request scope"),
        ({"product_id": uuid4()}, "product scope"),
        (
            {
                "confidence": valid.confidence.model_copy(
                    update={"assessed_at": NOW + timedelta(seconds=1)}
                )
            },
            "after scoring",
        ),
        ({"risk_penalty": Decimal("1")}, "offer metrics"),
        ({"value_index": Decimal("0")}, "ranking metrics"),
    )
    for updates, message in invalid:
        with pytest.raises(ValidationError, match=message):
            CandidateScore.model_validate(base | updates)

    no_offer = rank(request, build_decision(request, uuid4(), amount=None)).scores[0]
    with pytest.raises(ValidationError, match="without a best offer"):
        CandidateScore.model_validate(
            no_offer.model_dump() | {"selected_price": Money(amount=Decimal("1"))}
        )
    with pytest.raises(ValidationError, match="ineligible candidate"):
        CandidateScore.model_validate(no_offer.model_dump() | {"rank": 1})


def test_candidate_score_rejects_eligible_missing_or_zero_cost_metrics() -> None:
    request = build_request()
    valid = rank(request, build_decision(request, uuid4())).scores[0]
    with pytest.raises(ValidationError, match="requires every ranking metric"):
        CandidateScore.model_validate(valid.model_dump() | {"rank": None})

    request_without_criteria = build_request(criteria=())
    no_utility = rank(
        request_without_criteria,
        build_decision(request_without_criteria, uuid4(), utility=None),
    ).scores[0]
    with pytest.raises(ValidationError, match="comparable cost and adjusted utility"):
        CandidateScore.model_validate(
            no_utility.model_dump()
            | {
                "eligible": True,
                "exclusion_codes": (),
                "value_index": Decimal("0"),
                "value_per_100": Decimal("0"),
                "final_score": Decimal("0"),
                "pareto_front": 1,
                "rank": 1,
            }
        )

    zero_decision = build_decision(request, uuid4(), amount="0")
    zero_score = rank(request, zero_decision).scores[0]
    with pytest.raises(ValidationError, match="positive effective cost"):
        CandidateScore.model_validate(
            zero_score.model_dump()
            | {
                "eligible": True,
                "exclusion_codes": (),
                "risk_penalty": Decimal("0"),
                "value_index": Decimal("0"),
                "value_per_100": Decimal("0"),
                "final_score": Decimal("0"),
                "pareto_front": 1,
                "rank": 1,
            }
        )


def test_ranking_batch_rejects_inconsistent_sets() -> None:
    request = build_request()
    batch = rank(
        request,
        build_decision(request, uuid4()),
        build_decision(request, uuid4()),
    )
    first, second = batch.scores
    invalid = (
        (
            (
                first.model_copy(update={"scored_at": NOW + timedelta(seconds=1)}),
                second,
            ),
            "share batch scope",
        ),
        (
            (first, first),
            "product identifiers",
        ),
        (
            (first, second.model_copy(update={"id": first.id})),
            "score identifiers",
        ),
        (
            (first, second.model_copy(update={"rank": 3})),
            "ranks must be contiguous",
        ),
        (
            (first, second.model_copy(update={"pareto_front": 3})),
            "fronts must be contiguous",
        ),
    )
    for scores, message in invalid:
        with pytest.raises(ValidationError, match=message):
            CandidateRankingBatch(
                request_id=batch.request_id,
                workflow_id=batch.workflow_id,
                scores=scores,
                scored_at=NOW,
            )


def build_product(*, product_id: UUID | None = None) -> Product:
    return Product(
        id=product_id or uuid4(),
        brand="Example",
        model="X1",
        category="smartphone",
        canonical_name="Example X1",
    )


def build_service_inputs(
    request: ShoppingRequest,
    product: Product,
) -> tuple[
    tuple[NormalizedSpecification, ...],
    tuple[Evidence, ...],
    tuple[Offer, ...],
    tuple[EvidenceCheck, ...],
]:
    evidence = Evidence(
        request_id=request.id,
        subject_type=EvidenceSubjectType.PRODUCT,
        subject_id=product.id,
        field_path="specifications.battery_capacity",
        source_type=EvidenceSourceType.MANUFACTURER_OFFICIAL,
        source_url=HttpUrl("https://manufacturer.example.com/x1"),
        source_title="Official specifications",
        captured_at=NOW,
        observed_value="5000 mAh",
    )
    specification = NormalizedSpecification(
        request_id=request.id,
        workflow_id=uuid4(),
        product_id=product.id,
        canonical_key="battery_capacity",
        source_field_paths=("specifications.battery_capacity",),
        raw_values=("5000 mAh",),
        normalized_values=(Decimal("5000"),),
        canonical_unit="mAh",
        evidence_ids=(evidence.id,),
        status=SpecificationNormalizationStatus.NORMALIZED,
        normalized_at=NOW,
    )
    offer = Offer(
        product_id=product.id,
        platform="JD",
        seller="JD self operated",
        store_type=StoreType.PLATFORM_SELF_OPERATED,
        url=HttpUrl("https://item.example.com/1"),
        region="云南省曲靖市",
        captured_at=NOW,
        price=PriceBreakdown(estimated_total_cost=Money(amount=Decimal("80"))),
        in_stock=True,
    )
    check = EvidenceCheck(
        request_id=request.id,
        workflow_id=specification.workflow_id,
        product_id=product.id,
        field_path="specifications.battery_capacity",
        status=EvidenceCheckStatus.MATCH,
        platform_values=("5000 mAh",),
        official_values=("5000 mAh",),
        checked_at=NOW,
    )
    return (specification,), (evidence,), (offer,), (check,)


def build_snapshot(
    request: ShoppingRequest,
    *,
    data_normalized: bool = True,
) -> WorkflowSnapshot:
    machine = WorkflowStateMachine()
    workflow, events = machine.initialize(request.id, occurred_at=NOW)
    states = (
        WorkflowState.CANDIDATES_DISCOVERED,
        WorkflowState.OFFERS_COLLECTED,
        WorkflowState.EVIDENCE_CROSS_CHECKED,
        *((WorkflowState.DATA_NORMALIZED,) if data_normalized else ()),
    )
    for state in states:
        workflow, event = machine.advance(
            workflow,
            state,
            reason=f"seed_{state.value}",
            occurred_at=NOW,
        )
        events = (*events, event)
    return WorkflowSnapshot(request=request, workflow=workflow, events=events)


class FakeScoringUnitOfWork:
    def __init__(
        self,
        snapshot: WorkflowSnapshot,
        products: tuple[Product, ...],
        inputs: dict[
            UUID,
            tuple[
                tuple[NormalizedSpecification, ...],
                tuple[Evidence, ...],
                tuple[Offer, ...],
                tuple[EvidenceCheck, ...],
            ],
        ],
        *,
        fail_on_add: bool = False,
    ) -> None:
        self.snapshot = snapshot
        self.products = products
        self.inputs = inputs
        self.fail_on_add = fail_on_add
        self.scores: list[CandidateScore] = []
        self.transition: tuple[ShoppingWorkflow, WorkflowEvent, int] | None = None
        self.entered = False
        self.exited = False
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> "FakeScoringUnitOfWork":
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

    def list_offers_for_request_product(
        self, request_id: UUID, product_id: UUID
    ) -> tuple[Offer, ...]:
        assert request_id == self.snapshot.request.id
        return self.inputs[product_id][2]

    def list_specifications(
        self, request_id: UUID, workflow_id: UUID, product_id: UUID
    ) -> tuple[NormalizedSpecification, ...]:
        assert request_id == self.snapshot.request.id
        assert workflow_id == self.snapshot.workflow.id
        return tuple(
            item.model_copy(update={"workflow_id": workflow_id})
            for item in self.inputs[product_id][0]
        )

    def list_product_evidence(self, request_id: UUID, product_id: UUID) -> tuple[Evidence, ...]:
        assert request_id == self.snapshot.request.id
        return self.inputs[product_id][1]

    def list_evidence_checks(
        self, request_id: UUID, workflow_id: UUID, product_id: UUID
    ) -> tuple[EvidenceCheck, ...]:
        assert request_id == self.snapshot.request.id
        return tuple(
            item.model_copy(update={"workflow_id": workflow_id})
            for item in self.inputs[product_id][3]
        )

    def add_score(self, score: CandidateScore) -> None:
        if self.fail_on_add:
            raise RuntimeError("score persistence failed")
        self.scores.append(score)

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


def test_service_scores_all_candidates_and_advances_atomically() -> None:
    request = build_request()
    product = build_product()
    snapshot = build_snapshot(request)
    unit = FakeScoringUnitOfWork(
        snapshot,
        (product,),
        {product.id: build_service_inputs(request, product)},
    )

    result = CandidateScoringService(lambda: unit, clock=lambda: NOW).score(snapshot.workflow.id)

    assert unit.entered and unit.exited and unit.committed and not unit.rolled_back
    assert unit.scores == list(result.batch.scores)
    assert result.batch.scores[0].eligible is True
    assert result.snapshot.workflow.state is WorkflowState.CANDIDATES_SCORED
    assert result.snapshot.events[-1].reason == "candidates_ranked"
    assert unit.transition is not None
    assert unit.transition[2] == snapshot.workflow.revision


def test_service_rejects_wrong_state_and_missing_products() -> None:
    request = build_request()
    wrong_snapshot = build_snapshot(request, data_normalized=False)
    wrong = FakeScoringUnitOfWork(wrong_snapshot, (), {})

    with pytest.raises(InvalidWorkflowTransitionError, match="data_normalized"):
        CandidateScoringService(lambda: wrong).score(wrong_snapshot.workflow.id)
    assert wrong.exited and wrong.rolled_back

    snapshot = build_snapshot(request)
    empty = FakeScoringUnitOfWork(snapshot, (), {})
    with pytest.raises(NoCandidatesForScoringError, match="No request-linked"):
        CandidateScoringService(lambda: empty).score(snapshot.workflow.id)
    assert empty.exited and empty.rolled_back


def test_service_rolls_back_scores_and_state_when_staging_fails() -> None:
    request = build_request()
    product = build_product()
    snapshot = build_snapshot(request)
    unit = FakeScoringUnitOfWork(
        snapshot,
        (product,),
        {product.id: build_service_inputs(request, product)},
        fail_on_add=True,
    )

    with pytest.raises(RuntimeError, match="persistence failed"):
        CandidateScoringService(lambda: unit, clock=lambda: NOW).score(snapshot.workflow.id)

    assert unit.exited and unit.rolled_back and not unit.committed
    assert unit.transition is None
