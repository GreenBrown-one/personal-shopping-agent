"""Deterministic, provider-neutral shopping report construction and rendering."""

# ruff: noqa: RUF001 -- Chinese user-facing copy intentionally uses full-width punctuation.

import hashlib
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self, TypeVar
from uuid import UUID, uuid4

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from personal_shopping_agent.application.ranking import CandidateRankingBatch, CandidateScore
from personal_shopping_agent.application.workflow import (
    InvalidWorkflowTransitionError,
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStateMachine,
    workflow_now,
)
from personal_shopping_agent.domain import (
    Evidence,
    EvidenceSubjectType,
    Money,
    Offer,
    Product,
    ShoppingRequest,
)
from personal_shopping_agent.serialization import JsonContractModel

REPORT_METHODOLOGY_VERSION = "m5-report-v1"
REPORT_DISCLAIMER = (
    "本报告依据已采集且可追溯的数据生成，仅用于辅助比较；价格、库存和优惠可能变化，"
    "购买前请在平台确认最终商品、卖家、价格、配送和售后条件。系统不会自动下单或支付。"
)


class ReportModel(JsonContractModel):
    """Strict immutable base for report inputs and durable outputs."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ReportFormat(StrEnum):
    """Supported deterministic report serialization formats."""

    MARKDOWN = "markdown"
    HTML = "html"


class ReportCandidate(ReportModel):
    """One ranked or excluded candidate with its exact display facts and sources."""

    product: Product
    score: CandidateScore
    offer: Offer | None = None
    evidence: tuple[Evidence, ...] = ()

    @model_validator(mode="after")
    def facts_match_score(self) -> "ReportCandidate":
        """Prevent a report from attaching another product, offer, or evidence chain."""

        if self.product.id != self.score.product_id:
            raise ValueError("report candidate Product must match its score")
        if (self.offer is None) != (self.score.best_offer_id is None):
            raise ValueError("report candidate Offer presence must match its score")
        if self.offer is not None and (
            self.offer.id != self.score.best_offer_id or self.offer.product_id != self.product.id
        ):
            raise ValueError("report candidate Offer must match the scored Product and offer")
        evidence_ids = [item.id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("report candidate Evidence identifiers must be unique")
        if any(
            item.request_id != self.score.request_id
            or item.subject_type is not EvidenceSubjectType.PRODUCT
            or item.subject_id != self.product.id
            for item in self.evidence
        ):
            raise ValueError("report candidate Evidence must match its request and Product")
        expected_evidence_ids = {
            evidence_id
            for criterion in self.score.confidence.criteria
            for evidence_id in criterion.evidence_ids
        }
        if set(evidence_ids) != expected_evidence_ids:
            raise ValueError("report candidate Evidence must exactly cover scored evidence")
        evaluation_signature = tuple(
            (item.key.casefold(), item.weight)
            for item in self.score.foundation.criterion_evaluations
        )
        confidence_signature = tuple(
            (item.key.casefold(), item.weight) for item in self.score.confidence.criteria
        )
        if evaluation_signature != confidence_signature:
            raise ValueError("report candidate criterion confidence must match evaluations")
        return self


class ShoppingDecisionReport(ReportModel):
    """Auditable report snapshot independent from any model-provider response."""

    id: UUID = Field(default_factory=uuid4)
    request: ShoppingRequest
    workflow_id: UUID
    candidates: tuple[ReportCandidate, ...] = Field(min_length=1)
    recommended_product_id: UUID | None = None
    methodology_version: str = Field(default=REPORT_METHODOLOGY_VERSION, min_length=1)
    disclaimer: str = Field(default=REPORT_DISCLAIMER, min_length=1)
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def candidate_set_is_consistent(self) -> "ShoppingDecisionReport":
        """Require exact scope, stable order, and a mechanically derived top candidate."""

        if any(
            item.score.request_id != self.request.id
            or item.score.workflow_id != self.workflow_id
            or item.score.scored_at > self.generated_at
            for item in self.candidates
        ):
            raise ValueError("report candidates must share report scope and precede generation")
        product_ids = [item.product.id for item in self.candidates]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("report candidate Product identifiers must be unique")
        eligible = tuple(item for item in self.candidates if item.score.eligible)
        excluded = tuple(item for item in self.candidates if not item.score.eligible)
        if self.candidates != (*eligible, *excluded):
            raise ValueError("eligible report candidates must precede excluded candidates")
        if tuple(item.score.rank for item in eligible) != tuple(range(1, len(eligible) + 1)):
            raise ValueError("eligible report candidates must remain in rank order")
        if tuple(str(item.product.id) for item in excluded) != tuple(
            sorted(str(item.product.id) for item in excluded)
        ):
            raise ValueError("excluded report candidates must remain in stable Product order")
        expected_recommendation = eligible[0].product.id if eligible else None
        if self.recommended_product_id != expected_recommendation:
            raise ValueError("report recommendation must equal the first eligible candidate")
        return self


class RenderedShoppingReport(ReportModel):
    """A report snapshot plus an integrity-checked deterministic representation."""

    report: ShoppingDecisionReport
    format: ReportFormat
    content: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rendered_at: AwareDatetime

    @model_validator(mode="after")
    def content_is_consistent(self) -> "RenderedShoppingReport":
        """Bind the rendered bytes and time to the validated report snapshot."""

        if self.rendered_at != self.report.generated_at:
            raise ValueError("rendered report time must equal report generation time")
        expected = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_sha256 != expected:
            raise ValueError("rendered report hash must match its UTF-8 content")
        return self


class ShoppingReportRenderer(Protocol):
    """Provider-neutral deterministic serialization boundary for one report snapshot."""

    def render(self, report: ShoppingDecisionReport) -> RenderedShoppingReport: ...


class ShoppingReportResult(ReportModel):
    """Committed report and its advanced workflow snapshot."""

    snapshot: WorkflowSnapshot
    rendered: RenderedShoppingReport


class NoCandidateScoresForReportError(RuntimeError):
    """Raised when a scored workflow has no durable candidate results."""


class ReportInputMismatchError(ValueError):
    """Raised when report facts do not exactly cover the ranking batch."""


EntityT = TypeVar("EntityT", Product, Offer, Evidence)


def _stable_scores(scores: tuple[CandidateScore, ...]) -> tuple[CandidateScore, ...]:
    return tuple(
        sorted(
            scores,
            key=lambda item: (
                0 if item.eligible else 1,
                item.rank if item.rank is not None else 0,
                str(item.product_id),
            ),
        )
    )


class ShoppingReportBuilder:
    """Join validated ranking results to only the Product, Offer, and Evidence they cite."""

    def build(
        self,
        *,
        request: ShoppingRequest,
        batch: CandidateRankingBatch,
        products: tuple[Product, ...],
        offers: tuple[Offer, ...],
        evidence: tuple[Evidence, ...],
        generated_at: datetime,
    ) -> ShoppingDecisionReport:
        """Build one provider-neutral report snapshot with exact input coverage."""

        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ValueError("report generation time must include a timezone")
        if generated_at < batch.scored_at:
            raise ValueError("report generation cannot precede candidate scoring")
        if request.id != batch.request_id:
            raise ReportInputMismatchError("report shopping request must match the ranking batch")
        product_map = self._unique_map(products, "Product")
        offer_map = self._unique_map(offers, "Offer")
        evidence_map = self._unique_map(evidence, "Evidence")
        expected_product_ids = {item.product_id for item in batch.scores}
        expected_offer_ids = {
            item.best_offer_id for item in batch.scores if item.best_offer_id is not None
        }
        expected_evidence_ids = {
            evidence_id
            for score in batch.scores
            for criterion in score.confidence.criteria
            for evidence_id in criterion.evidence_ids
        }
        self._require_exact_ids(product_map, expected_product_ids, "Product")
        self._require_exact_ids(offer_map, expected_offer_ids, "Offer")
        self._require_exact_ids(evidence_map, expected_evidence_ids, "Evidence")

        candidates = tuple(
            ReportCandidate(
                product=product_map[score.product_id],
                score=score,
                offer=(offer_map[score.best_offer_id] if score.best_offer_id is not None else None),
                evidence=tuple(
                    sorted(
                        (
                            evidence_map[evidence_id]
                            for evidence_id in dict.fromkeys(
                                evidence_id
                                for criterion in score.confidence.criteria
                                for evidence_id in criterion.evidence_ids
                            )
                        ),
                        key=lambda item: (item.captured_at, str(item.id)),
                    )
                ),
            )
            for score in batch.scores
        )
        recommendation = next(
            (item.product.id for item in candidates if item.score.eligible),
            None,
        )
        return ShoppingDecisionReport(
            request=request,
            workflow_id=batch.workflow_id,
            candidates=candidates,
            recommended_product_id=recommendation,
            generated_at=generated_at,
        )

    @staticmethod
    def _unique_map(items: tuple[EntityT, ...], label: str) -> dict[UUID, EntityT]:
        keyed = {item.id: item for item in items}
        if len(keyed) != len(items):
            raise ReportInputMismatchError(f"report {label} identifiers must be unique")
        return keyed

    @staticmethod
    def _require_exact_ids(actual: Mapping[UUID, object], expected: set[UUID], label: str) -> None:
        if set(actual) != expected:
            raise ReportInputMismatchError(
                f"report {label} inputs must exactly cover the ranking batch"
            )


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]<>()#+\-.!|])")


def escape_markdown(value: object) -> str:
    """Normalize and escape untrusted text before inserting it into Markdown."""

    normalized = " ".join(str(value).split())
    return _MARKDOWN_SPECIAL.sub(r"\\\1", normalized)


def _decimal(value: Decimal, places: int) -> str:
    return format(value, f".{places}f")


def _money(value: Money) -> str:
    return f"{escape_markdown(value.currency)} {escape_markdown(format(value.amount, 'f'))}"


REPORT_BUDGET_LABELS = {
    "within_budget": "正常预算内",
    "within_stretch": "弹性预算内",
    "over_budget": "超出预算",
    "not_assessed": "未评估",
}
REPORT_STATUS_LABELS = {
    "satisfied": "满足",
    "unsatisfied": "未满足",
    "missing": "缺失",
    "conflict": "冲突",
    "unresolved": "未解决",
}
REPORT_EXCLUSION_LABELS = {
    "criteria_not_defined": "请求没有可评分条件",
    "hard_requirements_unmet": "硬性条件未满足",
    "no_comparable_offer": "没有可比报价",
    "nonpositive_effective_cost": "有效成本不是正数",
    "over_budget": "选定价格超出预算上限",
    "evidence_confidence_zero": "证据置信度为零",
}


class MarkdownShoppingReportRenderer:
    """Render a complete report without an LLM or model-provider SDK."""

    def render(self, report: ShoppingDecisionReport) -> RenderedShoppingReport:
        """Create deterministic, escaped Markdown from a validated report snapshot."""

        eligible = tuple(item for item in report.candidates if item.score.eligible)
        excluded = tuple(item for item in report.candidates if not item.score.eligible)
        lines = [
            "# 购物比较报告",
            "",
            f"- 工作流：`{report.workflow_id}`",
            f"- 生成时间：`{report.generated_at.isoformat()}`",
            f"- 评分方法：`{escape_markdown(report.methodology_version)}`",
            "",
            f"> {escape_markdown(report.disclaimer)}",
            "",
            "## 需求摘要",
            "",
            f"- 商品需求：{escape_markdown(report.request.query)}",
        ]
        lines.extend(
            (
                f"- 类别：{escape_markdown(report.request.category)}",
                f"- 正常预算：{_money(report.request.budget.maximum)}",
                f"- 弹性预算：{self._stretch_budget(report.request)}",
                f"- 地区：{escape_markdown(report.request.region or '未指定')}",
                "",
                "## 当前结论",
                "",
            )
        )
        if eligible:
            top = eligible[0]
            lines.extend(
                (
                    f"当前排序第一候选为 **{escape_markdown(top.product.canonical_name)}**。",
                    "该结论来自下述确定性规则，不代表自动购买指令。",
                )
            )
        else:
            lines.append("当前没有通过全部资格闸门的候选，报告不生成购买候选。")

        lines.extend(("", "## 合格候选排名", ""))
        if eligible:
            lines.extend(
                (
                    "| 名次 | 商品 | 预算层 | 选定价格 | 有效成本 | 综合分 | "
                    "每百元价值 | 证据置信度 | Pareto |",
                    "|---:|---|---|---:|---:|---:|---:|---:|---:|",
                )
            )
            lines.extend(self._ranking_row(item) for item in eligible)
        else:
            lines.append("无。")

        lines.extend(("", "## 候选详情", ""))
        for candidate in eligible:
            self._append_candidate_details(lines, candidate)

        lines.extend(("## 未进入排名", ""))
        if excluded:
            lines.extend(
                f"- **{escape_markdown(item.product.canonical_name)}**："
                + "；".join(
                    escape_markdown(REPORT_EXCLUSION_LABELS.get(code, code))
                    for code in item.score.exclusion_codes
                )
                for item in excluded
            )
        else:
            lines.append("无。")

        lines.extend(
            (
                "",
                "## 方法与限制",
                "",
                "- 正常预算候选始终排在弹性预算候选之前；预算层按选定价格判断。",
                "- Pareto 前沿同时比较调整后效用、证据置信度和有效成本。",
                "- 综合分用于稳定排序，不替代参数、来源、价格和风险的分项检查。",
                "- 页面价格、库存、优惠、配送和售后可能在采集后变化，请在购买前复核。",
                "- 本系统不执行下单、支付或其他不可逆消费操作。",
                "",
            )
        )
        content = "\n".join(lines).rstrip()
        return RenderedShoppingReport(
            report=report,
            format=ReportFormat.MARKDOWN,
            content=content,
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            rendered_at=report.generated_at,
        )

    @staticmethod
    def _stretch_budget(request: ShoppingRequest) -> str:
        if request.budget.stretch_maximum is None:
            return "未设置"
        return _money(request.budget.stretch_maximum)

    @staticmethod
    def _ranking_row(candidate: ReportCandidate) -> str:
        score = candidate.score
        assert score.rank is not None
        assert score.selected_price is not None
        assert score.effective_cost is not None
        assert score.final_score is not None
        assert score.value_per_100 is not None
        assert score.pareto_front is not None
        return (
            f"| {score.rank} | {escape_markdown(candidate.product.canonical_name)} | "
            f"{REPORT_BUDGET_LABELS[score.budget_status.value]} | "
            f"{_money(score.selected_price)} | "
            f"{_money(score.effective_cost)} | {_decimal(score.final_score, 6)} | "
            f"{_decimal(score.value_per_100, 6)} | "
            f"{_decimal(score.confidence.evidence_confidence, 6)} | "
            f"{score.pareto_front} |"
        )

    @staticmethod
    def _append_candidate_details(lines: list[str], candidate: ReportCandidate) -> None:
        score = candidate.score
        assert score.rank is not None
        assert score.risk_penalty is not None
        lines.extend(
            (
                f"### {score.rank}. {escape_markdown(candidate.product.canonical_name)}",
                "",
                f"- 品牌 / 型号：{escape_markdown(candidate.product.brand)} / "
                f"{escape_markdown(candidate.product.model)}",
                f"- 风险惩罚：{_decimal(score.risk_penalty, 6)}",
                f"- 调整后效用：{_decimal(score.confidence.adjusted_utility or Decimal('0'), 6)}",
            )
        )
        if candidate.offer is not None:  # pragma: no branch - eligible scores have a best offer
            offer = candidate.offer
            lines.extend(
                (
                    f"- 报价：{escape_markdown(offer.platform)} / {escape_markdown(offer.seller)}",
                    f"- 报价采集时间：`{offer.captured_at.isoformat()}`",
                    f"- 商品页面：<{offer.url}>",
                )
            )
        lines.extend(
            (
                "",
                "| 条件 | 状态 | 能力分 | 证据分 | 硬性 |",
                "|---|---|---:|---:|---|",
            )
        )
        for evaluation, confidence in zip(
            score.foundation.criterion_evaluations,
            score.confidence.criteria,
            strict=True,
        ):
            lines.append(
                f"| {escape_markdown(evaluation.key)} | "
                f"{REPORT_STATUS_LABELS[evaluation.status.value]} | "
                f"{_decimal(evaluation.score, 6)} | {_decimal(confidence.confidence, 6)} | "
                f"{'是' if evaluation.hard_requirement else '否'} |"
            )
        lines.extend(("", "证据来源：", ""))
        if candidate.evidence:
            lines.extend(
                f"- {escape_markdown(item.source_title)}（"
                f"{escape_markdown(item.source_type.value)}，`{item.captured_at.isoformat()}`）："
                f"<{item.source_url}>"
                for item in candidate.evidence
            )
        else:
            lines.append("- 无可链接证据。")
        lines.append("")


class ShoppingReportUnitOfWork(Protocol):
    """Transaction port for scored inputs, rendered report, and workflow state."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def get_snapshot(self, workflow_id: UUID) -> WorkflowSnapshot: ...

    def list_scores(self, workflow_id: UUID) -> tuple[CandidateScore, ...]: ...

    def list_products(self, product_ids: tuple[UUID, ...]) -> tuple[Product, ...]: ...

    def list_offers(self, offer_ids: tuple[UUID, ...]) -> tuple[Offer, ...]: ...

    def list_evidence(self, evidence_ids: tuple[UUID, ...]) -> tuple[Evidence, ...]: ...

    def add_report(self, rendered: RenderedShoppingReport) -> None: ...

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None: ...

    def commit(self) -> None: ...


class ShoppingReportUnitOfWorkFactory(Protocol):
    """Create one isolated report-rendering transaction."""

    def __call__(self) -> ShoppingReportUnitOfWork: ...


class ShoppingReportService:
    """Build, render, persist, and advance one report without invoking an LLM."""

    def __init__(
        self,
        unit_of_work_factory: ShoppingReportUnitOfWorkFactory,
        *,
        builder: ShoppingReportBuilder | None = None,
        renderer: ShoppingReportRenderer | None = None,
        state_machine: WorkflowStateMachine | None = None,
        clock: Callable[[], datetime] = workflow_now,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._builder = builder or ShoppingReportBuilder()
        self._renderer = renderer or MarkdownShoppingReportRenderer()
        self._state_machine = state_machine or WorkflowStateMachine()
        self._clock = clock

    def render(self, workflow_id: UUID) -> ShoppingReportResult:
        """Commit deterministic Markdown together with the report_rendered event."""

        with self._unit_of_work_factory() as unit_of_work:
            snapshot = unit_of_work.get_snapshot(workflow_id)
            if (
                self._state_machine.next_state(snapshot.workflow.state)
                is not WorkflowState.REPORT_RENDERED
            ):
                raise InvalidWorkflowTransitionError(
                    "report rendering requires a candidates_scored workflow"
                )
            scores = _stable_scores(unit_of_work.list_scores(workflow_id))
            if not scores:
                raise NoCandidateScoresForReportError(
                    "No durable candidate scores are available for reporting."
                )
            batch = CandidateRankingBatch(
                request_id=snapshot.request.id,
                workflow_id=snapshot.workflow.id,
                scores=scores,
                scored_at=scores[0].scored_at,
            )
            product_ids = tuple(item.product_id for item in scores)
            offer_ids = tuple(
                item.best_offer_id for item in scores if item.best_offer_id is not None
            )
            evidence_ids = tuple(
                dict.fromkeys(
                    evidence_id
                    for score in scores
                    for criterion in score.confidence.criteria
                    for evidence_id in criterion.evidence_ids
                )
            )
            generated_at = self._clock()
            report = self._builder.build(
                request=snapshot.request,
                batch=batch,
                products=unit_of_work.list_products(product_ids),
                offers=unit_of_work.list_offers(offer_ids),
                evidence=unit_of_work.list_evidence(evidence_ids),
                generated_at=generated_at,
            )
            rendered = self._renderer.render(report)
            unit_of_work.add_report(rendered)
            workflow, event = self._state_machine.advance(
                snapshot.workflow,
                WorkflowState.REPORT_RENDERED,
                reason="shopping_report_rendered",
                occurred_at=generated_at,
            )
            unit_of_work.save_transition(
                workflow,
                event,
                expected_revision=snapshot.workflow.revision,
            )
            unit_of_work.commit()
            return ShoppingReportResult(
                snapshot=WorkflowSnapshot(
                    request=snapshot.request,
                    workflow=workflow,
                    events=(*snapshot.events, event),
                ),
                rendered=rendered,
            )
