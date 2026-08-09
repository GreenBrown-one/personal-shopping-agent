"""Unit tests for provider-neutral deterministic shopping reports."""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from types import TracebackType
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import HttpUrl, ValidationError

from personal_shopping_agent.application import (
    BudgetStatus,
    CandidateDecisionInput,
    CandidateEvidenceConfidence,
    CandidateExplanation,
    CandidateRankingBatch,
    CandidateRankingEngine,
    CandidateScore,
    CandidateScoringFoundation,
    CriterionEvaluation,
    CriterionEvaluationStatus,
    CriterionEvidenceAssessment,
    ExplanationFact,
    ExplanationFallbackReason,
    ExplanationStatement,
    ExplanationStatus,
    InvalidExplanationOutputError,
    InvalidWorkflowTransitionError,
    MarkdownShoppingReportRenderer,
    NoCandidateScoresForReportError,
    OfferCostAssessment,
    RenderedShoppingReport,
    ReportCandidate,
    ReportExplanationProviderError,
    ReportExplanationRequest,
    ReportExplanationRequestBuilder,
    ReportFormat,
    ReportInputMismatchError,
    ShoppingDecisionReport,
    ShoppingReportBuilder,
    ShoppingReportExplanation,
    ShoppingReportExplanationResult,
    ShoppingReportExplanationService,
    ShoppingReportService,
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStateMachine,
    validate_explanation_scope,
)
from personal_shopping_agent.domain import (
    Budget,
    Evidence,
    EvidenceSourceType,
    EvidenceSubjectType,
    Money,
    Offer,
    PriceBreakdown,
    PriceKind,
    Product,
    ShoppingCriterion,
    ShoppingRequest,
    StoreType,
)

NOW = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
GENERATED_AT = NOW + timedelta(seconds=1)
SCORE_QUANTUM = Decimal("0.000001")


def q(value: Decimal) -> Decimal:
    return value.quantize(SCORE_QUANTUM, rounding=ROUND_HALF_UP)


def build_request(
    *, request_id: UUID | None = None, include_stretch: bool = True
) -> ShoppingRequest:
    return ShoppingRequest(
        id=request_id or uuid4(),
        query="phone [safe] | # comparison",
        category="smartphone",
        budget=Budget(
            maximum=Money(amount=Decimal("100")),
            stretch_maximum=(Money(amount=Decimal("150")) if include_stretch else None),
        ),
        region="云南省曲靖市",
        criteria=(
            ShoppingCriterion(
                key="battery_capacity",
                weight=Decimal("1"),
                minimum=Decimal("4000"),
                preferred=Decimal("6000"),
                unit="mAh",
            ),
        ),
        created_at=NOW,
    )


@dataclass(frozen=True)
class ReportFixture:
    request: ShoppingRequest
    batch: CandidateRankingBatch
    products: tuple[Product, ...]
    offers: tuple[Offer, ...]
    evidence: tuple[Evidence, ...]


def build_fixture(
    *,
    amounts: tuple[str | None, ...] = ("80", "151"),
    request: ShoppingRequest | None = None,
    workflow_id: UUID | None = None,
) -> ReportFixture:
    shopping_request = request or build_request()
    target_workflow_id = workflow_id or uuid4()
    products: list[Product] = []
    offers: list[Offer] = []
    evidence: list[Evidence] = []
    decisions: list[CandidateDecisionInput] = []
    for index, amount in enumerate(amounts, start=1):
        product = Product(
            id=UUID(f"00000000-0000-0000-0000-{index:012d}"),
            brand="Example [brand]",
            model=f"X{index}",
            category="smartphone",
            canonical_name=f"Example [X{index}] | phone",
        )
        source = Evidence(
            request_id=shopping_request.id,
            subject_type=EvidenceSubjectType.PRODUCT,
            subject_id=product.id,
            field_path="specifications.battery_capacity",
            source_type=EvidenceSourceType.MANUFACTURER_OFFICIAL,
            source_url=HttpUrl(f"https://manufacturer.example.com/x{index}"),
            source_title="Official [spec] | source",
            captured_at=NOW,
            observed_value="5000 mAh",
        )
        evaluation = CriterionEvaluation(
            key="battery_capacity",
            weight=Decimal("1"),
            hard_requirement=False,
            observed_values=(Decimal("5000"),),
            status=CriterionEvaluationStatus.SATISFIED,
            score=Decimal("0.8"),
            reason_code="test",
        )
        offer: Offer | None = None
        costs: tuple[OfferCostAssessment, ...] = ()
        if amount is not None:
            price = Money(amount=Decimal(amount))
            offer = Offer(
                product_id=product.id,
                platform="JD",
                seller="Seller [safe] | shop",
                store_type=StoreType.PLATFORM_SELF_OPERATED,
                url=HttpUrl(f"https://item.example.com/{index}"),
                sku=str(index),
                region="云南省曲靖市",
                captured_at=NOW,
                price=PriceBreakdown(estimated_total_cost=price),
                in_stock=True,
            )
            costs = (
                OfferCostAssessment(
                    offer_id=offer.id,
                    product_id=product.id,
                    selected_price_kind=PriceKind.ESTIMATED_TOTAL,
                    selected_price=price,
                    risk_coefficient=Decimal("0"),
                    effective_cost=price,
                    comparable=True,
                    reason_codes=("test",),
                ),
            )
            offers.append(offer)
        foundation = CandidateScoringFoundation(
            request_id=shopping_request.id,
            product_id=product.id,
            criterion_evaluations=(evaluation,),
            utility=Decimal("0.8"),
            hard_requirements_met=True,
            offer_costs=costs,
            best_offer_id=costs[0].offer_id if costs else None,
        )
        confidence = CandidateEvidenceConfidence(
            request_id=shopping_request.id,
            product_id=product.id,
            criteria=(
                CriterionEvidenceAssessment(
                    key="battery_capacity",
                    weight=Decimal("1"),
                    complete=True,
                    source_reliability=Decimal("0.9"),
                    freshness=Decimal("1"),
                    consistency=Decimal("1"),
                    confidence=Decimal("0.9"),
                    evidence_ids=(source.id,),
                    reason_codes=("test",),
                ),
            ),
            completeness=Decimal("1"),
            source_reliability=Decimal("0.9"),
            freshness=Decimal("1"),
            consistency=Decimal("1"),
            evidence_confidence=Decimal("0.9"),
            base_utility=Decimal("0.8"),
            adjusted_utility=q(Decimal("0.72")),
            assessed_at=NOW,
        )
        products.append(product)
        evidence.append(source)
        decisions.append(CandidateDecisionInput(foundation=foundation, confidence=confidence))
    batch = CandidateRankingEngine().rank(
        request=shopping_request,
        workflow_id=target_workflow_id,
        candidates=tuple(decisions),
        scored_at=NOW,
    )
    return ReportFixture(
        request=shopping_request,
        batch=batch,
        products=tuple(products),
        offers=tuple(offers),
        evidence=tuple(evidence),
    )


def build_report(fixture: ReportFixture) -> ShoppingDecisionReport:
    return ShoppingReportBuilder().build(
        request=fixture.request,
        batch=fixture.batch,
        products=fixture.products,
        offers=fixture.offers,
        evidence=fixture.evidence,
        generated_at=GENERATED_AT,
    )


def test_builder_and_renderer_create_auditable_escaped_markdown() -> None:
    fixture = build_fixture()

    report = build_report(fixture)
    rendered = MarkdownShoppingReportRenderer().render(report)

    assert report.recommended_product_id == fixture.products[0].id
    assert report.candidates[0].score.budget_status is BudgetStatus.WITHIN_BUDGET
    assert report.candidates[1].score.exclusion_codes == ("over_budget",)
    assert rendered.format is ReportFormat.MARKDOWN
    assert rendered.content_sha256 == hashlib.sha256(rendered.content.encode("utf-8")).hexdigest()
    assert "phone \\[safe\\] \\| \\# comparison" in rendered.content
    assert "Example \\[X1\\] \\| phone" in rendered.content
    assert "Official \\[spec\\] \\| source" in rendered.content
    assert "https://manufacturer.example.com/x1" in rendered.content
    assert "选定价格超出预算上限" in rendered.content
    assert "系统不会自动下单或支付" in rendered.content


def test_report_without_eligible_candidate_is_explicit() -> None:
    fixture = build_fixture(amounts=("151",))

    report = build_report(fixture)
    content = MarkdownShoppingReportRenderer().render(report).content

    assert report.recommended_product_id is None
    assert "当前没有通过全部资格闸门" in content
    assert "## 合格候选排名\n\n无。" in content


def test_renderer_handles_no_exclusions_stretch_or_linked_evidence() -> None:
    request = build_request(include_stretch=False)
    fixture = build_fixture(amounts=("80",), request=request)
    valid = build_report(fixture).candidates[0]
    criterion = valid.score.confidence.criteria[0].model_copy(update={"evidence_ids": ()})
    confidence = valid.score.confidence.model_copy(update={"criteria": (criterion,)})
    score = CandidateScore.model_validate(valid.score.model_dump() | {"confidence": confidence})
    candidate = ReportCandidate(
        product=valid.product,
        score=score,
        offer=valid.offer,
        evidence=(),
    )
    report = ShoppingDecisionReport(
        request=request,
        workflow_id=fixture.batch.workflow_id,
        candidates=(candidate,),
        recommended_product_id=candidate.product.id,
        generated_at=GENERATED_AT,
    )

    content = MarkdownShoppingReportRenderer().render(report).content

    assert "弹性预算" in content and "未设置" in content
    assert "## 未进入排名\n\n无。" in content
    assert "无可链接证据" in content


def test_builder_rejects_time_scope_duplicate_and_incomplete_inputs() -> None:
    fixture = build_fixture()
    builder = ShoppingReportBuilder()

    with pytest.raises(ValueError, match="timezone"):
        builder.build(
            request=fixture.request,
            batch=fixture.batch,
            products=fixture.products,
            offers=fixture.offers,
            evidence=fixture.evidence,
            generated_at=datetime(2026, 8, 10, 2, 0),
        )
    with pytest.raises(ValueError, match="cannot precede"):
        builder.build(
            request=fixture.request,
            batch=fixture.batch,
            products=fixture.products,
            offers=fixture.offers,
            evidence=fixture.evidence,
            generated_at=NOW - timedelta(seconds=1),
        )
    with pytest.raises(ReportInputMismatchError, match="shopping request"):
        builder.build(
            request=build_request(),
            batch=fixture.batch,
            products=fixture.products,
            offers=fixture.offers,
            evidence=fixture.evidence,
            generated_at=GENERATED_AT,
        )
    with pytest.raises(ReportInputMismatchError, match="identifiers must be unique"):
        builder.build(
            request=fixture.request,
            batch=fixture.batch,
            products=(fixture.products[0],) * 2,
            offers=fixture.offers,
            evidence=fixture.evidence,
            generated_at=GENERATED_AT,
        )
    with pytest.raises(ReportInputMismatchError, match="Offer inputs"):
        builder.build(
            request=fixture.request,
            batch=fixture.batch,
            products=fixture.products,
            offers=fixture.offers[:1],
            evidence=fixture.evidence,
            generated_at=GENERATED_AT,
        )
    with pytest.raises(ReportInputMismatchError, match="Evidence inputs"):
        builder.build(
            request=fixture.request,
            batch=fixture.batch,
            products=fixture.products,
            offers=fixture.offers,
            evidence=fixture.evidence[:1],
            generated_at=GENERATED_AT,
        )


def test_report_candidate_rejects_mismatched_facts_and_criteria() -> None:
    fixture = build_fixture(amounts=("80",))
    valid = build_report(fixture).candidates[0]
    base = valid.model_dump()
    other_product = valid.product.model_copy(update={"id": uuid4()})
    wrong_offer = valid.offer.model_copy(update={"id": uuid4()}) if valid.offer else None
    wrong_evidence = valid.evidence[0].model_copy(update={"request_id": uuid4()})
    invalid = (
        ({"product": other_product}, "Product must match"),
        ({"offer": None}, "Offer presence"),
        ({"offer": wrong_offer}, "Offer must match"),
        ({"evidence": (valid.evidence[0], valid.evidence[0])}, "must be unique"),
        ({"evidence": (wrong_evidence,)}, "Evidence must match"),
        ({"evidence": ()}, "exactly cover"),
    )
    for updates, message in invalid:
        with pytest.raises(ValidationError, match=message):
            ReportCandidate.model_validate(base | updates)

    confidence_criterion = valid.score.confidence.criteria[0].model_copy(update={"key": "weight"})
    confidence = valid.score.confidence.model_copy(update={"criteria": (confidence_criterion,)})
    changed_score = valid.score.model_copy(update={"confidence": confidence})
    with pytest.raises(ValidationError, match="criterion confidence"):
        ReportCandidate.model_validate(base | {"score": changed_score})

    no_offer_fixture = build_fixture(amounts=(None,))
    no_offer = build_report(no_offer_fixture).candidates[0]
    with pytest.raises(ValidationError, match="Offer presence"):
        ReportCandidate.model_validate(
            no_offer.model_dump() | {"offer": no_offer_fixture.offers or valid.offer}
        )


def test_report_model_rejects_scope_order_duplicates_and_recommendation() -> None:
    fixture = build_fixture()
    report = build_report(fixture)
    first, second = report.candidates
    base = report.model_dump()
    invalid = (
        ({"workflow_id": uuid4()}, "share report scope"),
        ({"candidates": (first, first)}, "Product identifiers"),
        ({"candidates": (second, first)}, "eligible report candidates must precede"),
        (
            {
                "candidates": (
                    first.model_copy(update={"score": first.score.model_copy(update={"rank": 2})}),
                    second,
                )
            },
            "remain in rank order",
        ),
        ({"recommended_product_id": None}, "recommendation"),
        ({"generated_at": NOW - timedelta(seconds=1)}, "precede generation"),
    )
    for updates, message in invalid:
        with pytest.raises(ValidationError, match=message):
            ShoppingDecisionReport.model_validate(base | updates)

    excluded_fixture = build_fixture(amounts=("151", "152"))
    excluded_report = build_report(excluded_fixture)
    with pytest.raises(ValidationError, match="stable Product order"):
        ShoppingDecisionReport.model_validate(
            excluded_report.model_dump()
            | {"candidates": tuple(reversed(excluded_report.candidates))}
        )


def test_rendered_report_rejects_wrong_time_and_content_hash() -> None:
    rendered = MarkdownShoppingReportRenderer().render(build_report(build_fixture()))
    base = rendered.model_dump()

    with pytest.raises(ValidationError, match="time must equal"):
        RenderedShoppingReport.model_validate(
            base | {"rendered_at": GENERATED_AT + timedelta(seconds=1)}
        )
    with pytest.raises(ValidationError, match="hash must match"):
        RenderedShoppingReport.model_validate(base | {"content": rendered.content + "x"})


def build_snapshot(request: ShoppingRequest, workflow_id: UUID) -> WorkflowSnapshot:
    machine = WorkflowStateMachine()
    workflow, events = machine.initialize(request.id, occurred_at=NOW)
    workflow = workflow.model_copy(update={"id": workflow_id})
    events = tuple(item.model_copy(update={"workflow_id": workflow_id}) for item in events)
    for state in (
        WorkflowState.CANDIDATES_DISCOVERED,
        WorkflowState.OFFERS_COLLECTED,
        WorkflowState.EVIDENCE_CROSS_CHECKED,
        WorkflowState.DATA_NORMALIZED,
        WorkflowState.CANDIDATES_SCORED,
    ):
        workflow, event = machine.advance(
            workflow,
            state,
            reason=f"seed_{state.value}",
            occurred_at=NOW,
        )
        events = (*events, event)
    return WorkflowSnapshot(request=request, workflow=workflow, events=events)


class FakeReportUnitOfWork:
    def __init__(
        self,
        snapshot: WorkflowSnapshot,
        fixture: ReportFixture | None,
        *,
        fail_on_add: bool = False,
    ) -> None:
        self.snapshot = snapshot
        self.fixture = fixture
        self.fail_on_add = fail_on_add
        self.report: RenderedShoppingReport | None = None
        self.transition: tuple[ShoppingWorkflow, WorkflowEvent, int] | None = None
        self.committed = False
        self.exited = False
        self.rolled_back = False

    def __enter__(self) -> "FakeReportUnitOfWork":
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

    def list_scores(self, workflow_id: UUID) -> tuple[CandidateScore, ...]:
        assert workflow_id == self.snapshot.workflow.id
        return tuple(reversed(self.fixture.batch.scores)) if self.fixture else ()

    def list_products(self, product_ids: tuple[UUID, ...]) -> tuple[Product, ...]:
        fixture = self.fixture
        assert fixture is not None
        assert set(product_ids) == {item.id for item in fixture.products}
        return fixture.products

    def list_offers(self, offer_ids: tuple[UUID, ...]) -> tuple[Offer, ...]:
        fixture = self.fixture
        assert fixture is not None
        assert set(offer_ids) == {item.id for item in fixture.offers}
        return fixture.offers

    def list_evidence(self, evidence_ids: tuple[UUID, ...]) -> tuple[Evidence, ...]:
        fixture = self.fixture
        assert fixture is not None
        assert set(evidence_ids) == {item.id for item in fixture.evidence}
        return fixture.evidence

    def add_report(self, rendered: RenderedShoppingReport) -> None:
        if self.fail_on_add:
            raise RuntimeError("report persistence failed")
        self.report = rendered

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


def test_service_renders_all_scores_and_advances_atomically() -> None:
    request = build_request()
    workflow_id = uuid4()
    fixture = build_fixture(request=request, workflow_id=workflow_id)
    snapshot = build_snapshot(request, workflow_id)
    unit = FakeReportUnitOfWork(snapshot, fixture)

    result = ShoppingReportService(lambda: unit, clock=lambda: GENERATED_AT).render(workflow_id)

    assert unit.exited and unit.committed and not unit.rolled_back
    assert unit.report == result.rendered
    assert result.rendered.report.candidates[0].score.rank == 1
    assert result.snapshot.workflow.state is WorkflowState.REPORT_RENDERED
    assert result.snapshot.events[-1].reason == "shopping_report_rendered"
    assert unit.transition is not None
    assert unit.transition[2] == snapshot.workflow.revision


def test_service_rejects_wrong_state_and_missing_scores() -> None:
    request = build_request()
    workflow_id = uuid4()
    snapshot = build_snapshot(request, workflow_id)
    wrong_snapshot = snapshot.model_copy(
        update={
            "workflow": snapshot.workflow.model_copy(
                update={"state": WorkflowState.DATA_NORMALIZED}
            )
        }
    )
    wrong = FakeReportUnitOfWork(wrong_snapshot, None)

    with pytest.raises(InvalidWorkflowTransitionError, match="candidates_scored"):
        ShoppingReportService(lambda: wrong).render(workflow_id)
    assert wrong.exited and wrong.rolled_back

    empty = FakeReportUnitOfWork(snapshot, None)
    with pytest.raises(NoCandidateScoresForReportError, match="No durable"):
        ShoppingReportService(lambda: empty).render(workflow_id)
    assert empty.exited and empty.rolled_back


def test_service_rolls_back_report_and_state_when_staging_fails() -> None:
    request = build_request()
    workflow_id = uuid4()
    fixture = build_fixture(request=request, workflow_id=workflow_id)
    snapshot = build_snapshot(request, workflow_id)
    unit = FakeReportUnitOfWork(snapshot, fixture, fail_on_add=True)

    with pytest.raises(RuntimeError, match="persistence failed"):
        ShoppingReportService(lambda: unit, clock=lambda: GENERATED_AT).render(workflow_id)

    assert unit.exited and unit.rolled_back and not unit.committed
    assert unit.transition is None


def build_explanation(request: ReportExplanationRequest) -> ShoppingReportExplanation:
    candidate_explanations = tuple(
        CandidateExplanation(
            product_id=product_id,
            summary=ExplanationStatement(
                text="该候选的解释只引用报告事实。",
                fact_ids=(f"candidate.{product_id}.name",),
            ),
        )
        for product_id in request.candidate_product_ids
    )
    return ShoppingReportExplanation(
        report_id=request.report_id,
        report_content_sha256=request.report_content_sha256,
        overview=ExplanationStatement(
            text="这是确定性报告的可选语言说明。",
            fact_ids=("report.recommendation",),
        ),
        candidates=candidate_explanations,
        cautions=(
            ExplanationStatement(
                text="购买前仍需在平台复核。",
                fact_ids=("report.disclaimer",),
            ),
        ),
    )


def explanation_request(*, maximum_candidates: int = 3) -> ReportExplanationRequest:
    fixture = build_fixture(amounts=("80", "151", None))
    rendered = MarkdownShoppingReportRenderer().render(build_report(fixture))
    return ReportExplanationRequestBuilder(maximum_candidates=maximum_candidates).build(rendered)


def test_explanation_request_builder_projects_bounded_exact_facts() -> None:
    request = explanation_request(maximum_candidates=2)

    assert len(request.candidate_product_ids) == 2
    assert request.recommended_product_id == request.candidate_product_ids[0]
    assert "report.disclaimer" in {fact.id for fact in request.facts}
    assert all("http" not in fact.value for fact in request.facts)
    assert any(fact.value == "未提供" for fact in request.facts)
    assert any(fact.value == "未排名" for fact in request.facts)
    assert any(fact.value == "over_budget" for fact in request.facts)


def test_explanation_request_builder_handles_no_recommendation_and_no_stretch_budget() -> None:
    fixture = build_fixture(
        amounts=(None,),
        request=build_request(include_stretch=False),
    )
    rendered = MarkdownShoppingReportRenderer().render(build_report(fixture))

    request = ReportExplanationRequestBuilder(maximum_candidates=1).build(rendered)

    assert request.recommended_product_id is None
    assert next(fact for fact in request.facts if fact.id == "report.budget.stretch").value == (
        "未提供"
    )
    assert (
        next(fact for fact in request.facts if fact.id.endswith("selected_price")).value == "未提供"
    )


@pytest.mark.parametrize("maximum_candidates", [0, 11])
def test_explanation_request_builder_rejects_invalid_candidate_limit(
    maximum_candidates: int,
) -> None:
    with pytest.raises(ValueError, match="between 1 and 10"):
        ReportExplanationRequestBuilder(maximum_candidates=maximum_candidates)


def base_request_parts() -> dict[str, object]:
    product_id = uuid4()
    return {
        "report_id": uuid4(),
        "report_content_sha256": "a" * 64,
        "candidate_product_ids": (product_id,),
        "recommended_product_id": product_id,
        "facts": (
            ExplanationFact(
                id="report.disclaimer",
                label="限制",
                value="请复核",
            ),
            ExplanationFact(
                id=f"candidate.{product_id}.name",
                label="名称",
                value="Example",
                candidate_product_id=product_id,
            ),
        ),
    }


def test_explanation_request_rejects_inconsistent_scopes() -> None:
    parts = base_request_parts()
    product_id = cast(tuple[UUID, ...], parts["candidate_product_ids"])[0]
    with pytest.raises(ValidationError, match="must be unique"):
        ReportExplanationRequest.model_validate(
            {**parts, "candidate_product_ids": (product_id, product_id)}
        )

    with pytest.raises(ValidationError, match="recommendation must be inside"):
        ReportExplanationRequest.model_validate({**parts, "recommended_product_id": uuid4()})

    facts = cast(tuple[ExplanationFact, ...], parts["facts"])
    with pytest.raises(ValidationError, match="Fact identifiers must be unique"):
        ReportExplanationRequest.model_validate({**parts, "facts": (*facts, facts[0])})

    other_id = uuid4()
    outside_fact = ExplanationFact(
        id=f"candidate.{other_id}.name",
        label="名称",
        value="Outside",
        candidate_product_id=other_id,
    )
    with pytest.raises(ValidationError, match="inside candidate scope"):
        ReportExplanationRequest.model_validate({**parts, "facts": (*facts, outside_fact)})

    with pytest.raises(ValidationError, match="cover every candidate"):
        ReportExplanationRequest.model_validate({**parts, "facts": (facts[0],)})

    with pytest.raises(ValidationError, match="include the deterministic disclaimer"):
        ReportExplanationRequest.model_validate({**parts, "facts": (facts[1],)})


def test_explanation_statement_rejects_links_and_duplicate_fact_ids() -> None:
    with pytest.raises(ValidationError, match="must not contain links"):
        ExplanationStatement(text="访问 https://example.com", fact_ids=("report.disclaimer",))
    with pytest.raises(ValidationError, match="must be unique"):
        ExplanationStatement(
            text="重复引用",
            fact_ids=("report.disclaimer", "report.disclaimer"),
        )


def test_validate_explanation_scope_accepts_exact_output() -> None:
    request = explanation_request(maximum_candidates=2)
    explanation = build_explanation(request)

    validate_explanation_scope(request, explanation)


def test_validate_explanation_scope_rejects_snapshot_and_candidate_changes() -> None:
    request = explanation_request(maximum_candidates=2)
    explanation = build_explanation(request)
    with pytest.raises(InvalidExplanationOutputError, match="exact report"):
        validate_explanation_scope(
            request,
            explanation.model_copy(update={"report_id": uuid4()}),
        )
    with pytest.raises(InvalidExplanationOutputError, match="scope and order"):
        validate_explanation_scope(
            request,
            explanation.model_copy(update={"candidates": tuple(reversed(explanation.candidates))}),
        )


def test_validate_explanation_scope_rejects_unknown_and_cross_candidate_facts() -> None:
    request = explanation_request(maximum_candidates=2)
    explanation = build_explanation(request)
    first, second = explanation.candidates

    unknown = first.model_copy(
        update={"summary": first.summary.model_copy(update={"fact_ids": ("unknown.fact",)})}
    )
    with pytest.raises(InvalidExplanationOutputError, match="outside the allowlist"):
        validate_explanation_scope(
            request,
            explanation.model_copy(update={"candidates": (unknown, second)}),
        )

    global_only = first.model_copy(
        update={
            "summary": first.summary.model_copy(update={"fact_ids": ("report.recommendation",)})
        }
    )
    with pytest.raises(InvalidExplanationOutputError, match="at least one fact"):
        validate_explanation_scope(
            request,
            explanation.model_copy(update={"candidates": (global_only, second)}),
        )

    other_fact_id = f"candidate.{second.product_id}.name"
    mixed = first.model_copy(
        update={
            "summary": first.summary.model_copy(
                update={
                    "fact_ids": (
                        f"candidate.{first.product_id}.name",
                        other_fact_id,
                    )
                }
            )
        }
    )
    with pytest.raises(InvalidExplanationOutputError, match="another Product"):
        validate_explanation_scope(
            request,
            explanation.model_copy(update={"candidates": (mixed, second)}),
        )


def test_validate_explanation_scope_requires_disclaimer_caution() -> None:
    request = explanation_request(maximum_candidates=1)
    explanation = build_explanation(request)
    invalid_caution = ExplanationStatement(
        text="只写预算。",
        fact_ids=("report.budget.maximum",),
    )

    with pytest.raises(InvalidExplanationOutputError, match="preserve the deterministic"):
        validate_explanation_scope(
            request,
            explanation.model_copy(update={"cautions": (invalid_caution,)}),
        )


@dataclass
class FakeExplanationProvider:
    output: ShoppingReportExplanation | None = None
    failure: ExplanationFallbackReason | None = None
    provider_name: str = "fake"
    model_name: str = "fake-model"

    def explain(self, request: ReportExplanationRequest) -> ShoppingReportExplanation:
        if self.failure is not None:
            raise ReportExplanationProviderError(self.failure)
        return self.output or build_explanation(request)


def test_explanation_service_returns_valid_overlay_or_deterministic_fallback() -> None:
    fixture = build_fixture()
    rendered = MarkdownShoppingReportRenderer().render(build_report(fixture))

    success = ShoppingReportExplanationService(FakeExplanationProvider()).explain(rendered)
    assert success.status is ExplanationStatus.EXPLAINED
    assert success.explanation is not None
    assert success.provider_name == "fake"

    unconfigured = ShoppingReportExplanationService(None).explain(rendered)
    assert unconfigured.status is ExplanationStatus.DETERMINISTIC_FALLBACK
    assert unconfigured.rendered == rendered
    assert unconfigured.fallback_reason is ExplanationFallbackReason.NOT_CONFIGURED

    unavailable = ShoppingReportExplanationService(
        FakeExplanationProvider(failure=ExplanationFallbackReason.PROVIDER_UNAVAILABLE)
    ).explain(rendered)
    assert unavailable.fallback_reason is ExplanationFallbackReason.PROVIDER_UNAVAILABLE


def test_explanation_service_falls_back_on_locally_invalid_output() -> None:
    fixture = build_fixture()
    rendered = MarkdownShoppingReportRenderer().render(build_report(fixture))
    request = ReportExplanationRequestBuilder().build(rendered)
    output = build_explanation(request).model_copy(update={"report_id": uuid4()})

    result = ShoppingReportExplanationService(FakeExplanationProvider(output=output)).explain(
        rendered
    )

    assert result.status is ExplanationStatus.DETERMINISTIC_FALLBACK
    assert result.fallback_reason is ExplanationFallbackReason.INVALID_OUTPUT


def test_explanation_result_and_provider_error_reject_inconsistent_states() -> None:
    fixture = build_fixture()
    rendered = MarkdownShoppingReportRenderer().render(build_report(fixture))
    with pytest.raises(ValidationError, match="fields must match"):
        ShoppingReportExplanationResult(
            rendered=rendered,
            status=ExplanationStatus.EXPLAINED,
            fallback_reason=ExplanationFallbackReason.INVALID_OUTPUT,
        )
    with pytest.raises(ValidationError, match="fields must match"):
        ShoppingReportExplanationResult(
            rendered=rendered,
            status=ExplanationStatus.DETERMINISTIC_FALLBACK,
            explanation=build_explanation(ReportExplanationRequestBuilder().build(rendered)),
            provider_name="fake",
            model_name="fake-model",
        )
    with pytest.raises(ValueError, match="cannot report"):
        ReportExplanationProviderError(ExplanationFallbackReason.NOT_CONFIGURED)
