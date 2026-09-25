"""Provider-neutral, fact-scoped explanations for deterministic shopping reports."""

# ruff: noqa: RUF001 -- Chinese user-facing copy intentionally uses full-width punctuation.

import hashlib
import re
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, Self
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from personal_shopping_agent.application.reporting import (
    RenderedShoppingReport,
    ReportCandidate,
    ReportFormat,
    escape_markdown,
)
from personal_shopping_agent.domain import Money
from personal_shopping_agent.serialization import JsonContractModel

EXPLANATION_SCHEMA_VERSION = "m5-explanation-v1"
DISCLAIMER_FACT_ID = "report.disclaimer"
_FACT_ID_PATTERN = r"^[a-z0-9][a-z0-9._:-]{0,199}$"
_LINK_PATTERN = re.compile(r"(?:https?://|www\.|\[[^\]]+\]\([^\)]+\))", re.IGNORECASE)


class ExplanationModel(JsonContractModel):
    """Strict immutable base for requests and untrusted provider output."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ExplanationFact(ExplanationModel):
    """One exact report fact that an explanation may cite."""

    id: str = Field(pattern=_FACT_ID_PATTERN)
    label: str = Field(min_length=1, max_length=160)
    value: str = Field(min_length=1, max_length=1_000)
    candidate_product_id: UUID | None = None


class ReportExplanationRequest(ExplanationModel):
    """Minimal structured projection sent to an LLM instead of raw page content."""

    schema_version: str = Field(default=EXPLANATION_SCHEMA_VERSION, min_length=1)
    report_id: UUID
    report_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    language: str = Field(default="zh-CN", pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")
    candidate_product_ids: tuple[UUID, ...] = Field(min_length=1, max_length=10)
    recommended_product_id: UUID | None = None
    facts: tuple[ExplanationFact, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def scope_is_consistent(self) -> Self:
        """Reject duplicate or cross-candidate facts before any provider call."""

        if len(self.candidate_product_ids) != len(set(self.candidate_product_ids)):
            raise ValueError("explanation candidate Product identifiers must be unique")
        if (
            self.recommended_product_id is not None
            and self.recommended_product_id not in self.candidate_product_ids
        ):
            raise ValueError("explanation recommendation must be inside candidate scope")
        fact_ids = [fact.id for fact in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("explanation Fact identifiers must be unique")
        candidate_ids = set(self.candidate_product_ids)
        if any(
            fact.candidate_product_id is not None and fact.candidate_product_id not in candidate_ids
            for fact in self.facts
        ):
            raise ValueError("explanation Facts must remain inside candidate scope")
        covered_candidates = {
            fact.candidate_product_id
            for fact in self.facts
            if fact.candidate_product_id is not None
        }
        if covered_candidates != candidate_ids:
            raise ValueError("explanation Facts must cover every candidate")
        if DISCLAIMER_FACT_ID not in fact_ids:
            raise ValueError("explanation Facts must include the deterministic disclaimer")
        return self


class ExplanationStatement(ExplanationModel):
    """Natural-language text plus the exact report facts claimed as support."""

    text: str = Field(min_length=1, max_length=1_000)
    fact_ids: tuple[str, ...] = Field(min_length=1, max_length=20)

    @field_validator("text")
    @classmethod
    def reject_generated_links(cls, value: str) -> str:
        """Keep links and source titles in the deterministic report only."""

        if _LINK_PATTERN.search(value):
            raise ValueError("LLM explanation text must not contain links")
        return value

    @model_validator(mode="after")
    def fact_ids_are_unique(self) -> Self:
        """Make every citation list deterministic and unambiguous."""

        if len(self.fact_ids) != len(set(self.fact_ids)):
            raise ValueError("explanation statement Fact identifiers must be unique")
        return self


class CandidateExplanation(ExplanationModel):
    """One explanation paragraph bound to a candidate in deterministic order."""

    product_id: UUID
    summary: ExplanationStatement


class ShoppingReportExplanation(ExplanationModel):
    """Typed provider output that cannot replace the underlying report."""

    report_id: UUID
    report_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    overview: ExplanationStatement
    candidates: tuple[CandidateExplanation, ...] = Field(min_length=1, max_length=10)
    cautions: tuple[ExplanationStatement, ...] = Field(min_length=1, max_length=5)


class ExplanationFallbackReason(StrEnum):
    """Sanitized reason for returning the deterministic report without LLM text."""

    NOT_CONFIGURED = "not_configured"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INVALID_OUTPUT = "invalid_output"


class ExplanationStatus(StrEnum):
    """Whether an optional explanation passed every local boundary."""

    EXPLAINED = "explained"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"


class ShoppingReportExplanationResult(ExplanationModel):
    """The original report plus either a validated overlay or a safe fallback."""

    rendered: RenderedShoppingReport
    status: ExplanationStatus
    request: ReportExplanationRequest | None = None
    explanation: ShoppingReportExplanation | None = None
    provider_name: str | None = Field(default=None, min_length=1, max_length=80)
    model_name: str | None = Field(default=None, min_length=1, max_length=160)
    fallback_reason: ExplanationFallbackReason | None = None

    @model_validator(mode="after")
    def outcome_is_consistent(self) -> Self:
        """Prevent a fallback from masquerading as a provider-backed explanation."""

        explained = self.status is ExplanationStatus.EXPLAINED
        has_provider_output = (
            self.request is not None
            and self.explanation is not None
            and self.provider_name is not None
            and self.model_name is not None
            and self.fallback_reason is None
        )
        fallback = (
            self.request is None
            and self.explanation is None
            and self.provider_name is None
            and self.model_name is None
            and self.fallback_reason is not None
        )
        if (explained and not has_provider_output) or (not explained and not fallback):
            raise ValueError("explanation result fields must match its status")
        if has_provider_output:
            assert self.request is not None
            assert self.explanation is not None
            expected_request = ReportExplanationRequestBuilder(
                maximum_candidates=len(self.request.candidate_product_ids)
            ).build(self.rendered)
            if self.request != expected_request:
                raise ValueError("explanation request must be the exact deterministic projection")
            validate_explanation_scope(self.request, self.explanation)
        return self


class RenderedShoppingReportPresentation(ExplanationModel):
    """Integrity-checked Markdown presentation with an optional explanation overlay."""

    result: ShoppingReportExplanationResult
    format: ReportFormat = ReportFormat.MARKDOWN
    content: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def content_is_bound_to_result(self) -> Self:
        """Require fallback identity or an append-only explanation presentation."""

        expected_hash = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_sha256 != expected_hash:
            raise ValueError("presented report hash must match its UTF-8 content")
        base = self.result.rendered
        if self.result.status is ExplanationStatus.DETERMINISTIC_FALLBACK:
            if self.content != base.content or self.content_sha256 != base.content_sha256:
                raise ValueError(
                    "fallback presentation must exactly equal the deterministic report"
                )
        elif not self.content.startswith(f"{base.content}\n\n---\n\n"):
            raise ValueError("explanation presentation must append to the deterministic report")
        return self


class InvalidExplanationOutputError(ValueError):
    """Raised when typed provider output still violates the current report scope."""


class ReportExplanationProviderError(RuntimeError):
    """Sanitized external failure that is safe to convert into a fallback reason."""

    def __init__(self, reason: ExplanationFallbackReason) -> None:
        if reason is ExplanationFallbackReason.NOT_CONFIGURED:
            raise ValueError("provider adapters cannot report application configuration state")
        self.reason = reason
        super().__init__(reason.value)


class ReportExplanationProvider(Protocol):
    """Replaceable LLM boundary implemented by provider-specific adapters."""

    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def explain(self, request: ReportExplanationRequest) -> ShoppingReportExplanation: ...


def _decimal(value: Decimal | None, places: int = 6) -> str:
    return "未提供" if value is None else format(value, f".{places}f")


def _money(value: Money | None) -> str:
    return "未提供" if value is None else f"{value.currency} {format(value.amount, 'f')}"


class ReportExplanationRequestBuilder:
    """Project only bounded, validated report facts into the provider request."""

    def __init__(self, *, maximum_candidates: int = 3) -> None:
        if not 1 <= maximum_candidates <= 10:
            raise ValueError("maximum explanation candidates must be between 1 and 10")
        self._maximum_candidates = maximum_candidates

    def build(self, rendered: RenderedShoppingReport) -> ReportExplanationRequest:
        """Select candidates deterministically and create stable fact identifiers."""

        report = rendered.report
        candidates = report.candidates[: self._maximum_candidates]
        candidate_ids = tuple(candidate.product.id for candidate in candidates)
        recommendation = (
            report.recommended_product_id
            if report.recommended_product_id in candidate_ids
            else None
        )
        facts = [
            ExplanationFact(id="report.query", label="用户需求", value=report.request.query),
            ExplanationFact(
                id="report.budget.maximum",
                label="正常预算",
                value=_money(report.request.budget.maximum),
            ),
            ExplanationFact(
                id="report.budget.stretch",
                label="弹性预算",
                value=_money(report.request.budget.stretch_maximum),
            ),
            ExplanationFact(
                id="report.recommendation",
                label="确定性第一候选",
                value=str(report.recommended_product_id or "无合格候选"),
            ),
            ExplanationFact(
                id=DISCLAIMER_FACT_ID,
                label="固定限制说明",
                value=report.disclaimer,
            ),
        ]
        for candidate in candidates:
            facts.extend(self._candidate_facts(candidate))
        return ReportExplanationRequest(
            report_id=report.id,
            report_content_sha256=rendered.content_sha256,
            candidate_product_ids=candidate_ids,
            recommended_product_id=recommendation,
            facts=tuple(facts),
        )

    @staticmethod
    def _candidate_facts(candidate: ReportCandidate) -> tuple[ExplanationFact, ...]:
        product_id = candidate.product.id
        prefix = f"candidate.{product_id}"
        score = candidate.score
        facts = [
            ExplanationFact(
                id=f"{prefix}.name",
                label="商品名称",
                value=candidate.product.canonical_name,
                candidate_product_id=product_id,
            ),
            ExplanationFact(
                id=f"{prefix}.eligible",
                label="是否合格",
                value="是" if score.eligible else "否",
                candidate_product_id=product_id,
            ),
            ExplanationFact(
                id=f"{prefix}.rank",
                label="确定性名次",
                value=str(score.rank or "未排名"),
                candidate_product_id=product_id,
            ),
            ExplanationFact(
                id=f"{prefix}.budget_status",
                label="预算层",
                value=score.budget_status.value,
                candidate_product_id=product_id,
            ),
            ExplanationFact(
                id=f"{prefix}.selected_price",
                label="选定价格",
                value=_money(score.selected_price),
                candidate_product_id=product_id,
            ),
            ExplanationFact(
                id=f"{prefix}.effective_cost",
                label="有效成本",
                value=_money(score.effective_cost),
                candidate_product_id=product_id,
            ),
            ExplanationFact(
                id=f"{prefix}.final_score",
                label="综合分",
                value=_decimal(score.final_score),
                candidate_product_id=product_id,
            ),
            ExplanationFact(
                id=f"{prefix}.value_per_100",
                label="每百元价值",
                value=_decimal(score.value_per_100),
                candidate_product_id=product_id,
            ),
            ExplanationFact(
                id=f"{prefix}.evidence_confidence",
                label="证据置信度",
                value=_decimal(score.confidence.evidence_confidence),
                candidate_product_id=product_id,
            ),
            ExplanationFact(
                id=f"{prefix}.risk_penalty",
                label="风险惩罚",
                value=_decimal(score.risk_penalty),
                candidate_product_id=product_id,
            ),
            ExplanationFact(
                id=f"{prefix}.pareto_front",
                label="Pareto 前沿",
                value=str(score.pareto_front or "未提供"),
                candidate_product_id=product_id,
            ),
            ExplanationFact(
                id=f"{prefix}.exclusions",
                label="排除原因码",
                value="、".join(score.exclusion_codes) or "无",
                candidate_product_id=product_id,
            ),
        ]
        for index, (evaluation, confidence) in enumerate(
            zip(
                score.foundation.criterion_evaluations,
                score.confidence.criteria,
                strict=True,
            )
        ):
            criterion_prefix = f"{prefix}.criterion.{index}"
            facts.extend(
                (
                    ExplanationFact(
                        id=f"{criterion_prefix}.key",
                        label="条件名称",
                        value=evaluation.key,
                        candidate_product_id=product_id,
                    ),
                    ExplanationFact(
                        id=f"{criterion_prefix}.status",
                        label="条件状态",
                        value=evaluation.status.value,
                        candidate_product_id=product_id,
                    ),
                    ExplanationFact(
                        id=f"{criterion_prefix}.ability_score",
                        label="条件能力分",
                        value=_decimal(evaluation.score),
                        candidate_product_id=product_id,
                    ),
                    ExplanationFact(
                        id=f"{criterion_prefix}.evidence_score",
                        label="条件证据分",
                        value=_decimal(confidence.confidence),
                        candidate_product_id=product_id,
                    ),
                )
            )
        return tuple(facts)


def validate_explanation_scope(
    request: ReportExplanationRequest,
    explanation: ShoppingReportExplanation,
) -> None:
    """Validate provider output against facts and ordering the provider cannot define."""

    if (
        explanation.report_id != request.report_id
        or explanation.report_content_sha256 != request.report_content_sha256
    ):
        raise InvalidExplanationOutputError("explanation must match the exact report snapshot")
    output_candidate_ids = tuple(item.product_id for item in explanation.candidates)
    if output_candidate_ids != request.candidate_product_ids:
        raise InvalidExplanationOutputError(
            "explanation candidates must preserve the deterministic candidate scope and order"
        )

    fact_map = {fact.id: fact for fact in request.facts}
    statements = (
        explanation.overview,
        *(candidate.summary for candidate in explanation.candidates),
        *explanation.cautions,
    )
    cited_ids = {fact_id for statement in statements for fact_id in statement.fact_ids}
    if not cited_ids <= set(fact_map):
        raise InvalidExplanationOutputError("explanation cites Facts outside the allowlist")

    for candidate in explanation.candidates:
        cited_facts = tuple(fact_map[fact_id] for fact_id in candidate.summary.fact_ids)
        if not any(fact.candidate_product_id == candidate.product_id for fact in cited_facts):
            raise InvalidExplanationOutputError(
                "candidate explanations must cite at least one fact for that Product"
            )
        if any(
            fact.candidate_product_id not in (None, candidate.product_id) for fact in cited_facts
        ):
            raise InvalidExplanationOutputError(
                "candidate explanations cannot cite another Product's facts"
            )
    caution_fact_ids = {fact_id for caution in explanation.cautions for fact_id in caution.fact_ids}
    if DISCLAIMER_FACT_ID not in caution_fact_ids:
        raise InvalidExplanationOutputError(
            "explanation cautions must preserve the deterministic disclaimer"
        )


class MarkdownShoppingReportExplanationRenderer:
    """Append validated LLM text as escaped, fact-cited Markdown."""

    def render(
        self,
        result: ShoppingReportExplanationResult,
    ) -> RenderedShoppingReportPresentation:
        """Keep fallbacks byte-identical and make successful overlays visibly secondary."""

        base = result.rendered
        if result.status is ExplanationStatus.DETERMINISTIC_FALLBACK:
            return RenderedShoppingReportPresentation(
                result=result,
                content=base.content,
                content_sha256=base.content_sha256,
            )

        request = result.request
        explanation = result.explanation
        assert request is not None
        assert explanation is not None
        assert result.provider_name is not None
        assert result.model_name is not None
        validate_explanation_scope(request, explanation)
        facts = {fact.id: fact for fact in request.facts}
        product_names = {
            candidate.product.id: candidate.product.canonical_name
            for candidate in base.report.candidates
        }
        lines = [
            base.content,
            "",
            "---",
            "",
            "## AI 辅助解释",
            "",
            "> 本节仅是对上方确定性报告的语言说明，不是新的事实来源，也不会改变排名。",
            "",
            f"- 提供方：{escape_markdown(result.provider_name)}",
            f"- 模型：{escape_markdown(result.model_name)}",
            f"- 解释输入版本：{escape_markdown(request.schema_version)}",
            "",
            "### 总览",
            "",
        ]
        self._append_statement(lines, explanation.overview, facts)
        lines.extend(("### 候选说明", ""))
        for candidate in explanation.candidates:
            lines.extend(
                (
                    f"#### {escape_markdown(product_names[candidate.product_id])}",
                    "",
                )
            )
            self._append_statement(lines, candidate.summary, facts)
        lines.extend(("### 限制提醒", ""))
        for caution in explanation.cautions:
            self._append_statement(lines, caution, facts)
        content = "\n".join(lines).rstrip()
        return RenderedShoppingReportPresentation(
            result=result,
            content=content,
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _append_statement(
        lines: list[str],
        statement: ExplanationStatement,
        facts: dict[str, ExplanationFact],
    ) -> None:
        lines.extend((escape_markdown(statement.text), "", "引用事实：", ""))
        lines.extend(
            f"- `{fact_id}` — {escape_markdown(facts[fact_id].label)}："
            f"{escape_markdown(facts[fact_id].value)}"
            for fact_id in statement.fact_ids
        )
        lines.append("")


class ShoppingReportExplanationService:
    """Add an optional validated LLM overlay without changing the durable report."""

    def __init__(
        self,
        provider: ReportExplanationProvider | None,
        *,
        request_builder: ReportExplanationRequestBuilder | None = None,
    ) -> None:
        self._provider = provider
        self._request_builder = request_builder or ReportExplanationRequestBuilder()

    def explain(self, rendered: RenderedShoppingReport) -> ShoppingReportExplanationResult:
        """Return provider text only after schema and local fact-scope validation."""

        if self._provider is None:
            return self._fallback(rendered, ExplanationFallbackReason.NOT_CONFIGURED)
        request = self._request_builder.build(rendered)
        try:
            explanation = self._provider.explain(request)
            validate_explanation_scope(request, explanation)
        except ReportExplanationProviderError as error:
            return self._fallback(rendered, error.reason)
        except InvalidExplanationOutputError:
            return self._fallback(rendered, ExplanationFallbackReason.INVALID_OUTPUT)
        return ShoppingReportExplanationResult(
            rendered=rendered,
            status=ExplanationStatus.EXPLAINED,
            request=request,
            explanation=explanation,
            provider_name=self._provider.provider_name,
            model_name=self._provider.model_name,
        )

    @staticmethod
    def _fallback(
        rendered: RenderedShoppingReport,
        reason: ExplanationFallbackReason,
    ) -> ShoppingReportExplanationResult:
        return ShoppingReportExplanationResult(
            rendered=rendered,
            status=ExplanationStatus.DETERMINISTIC_FALLBACK,
            fallback_reason=reason,
        )


class ShoppingReportPresentationService:
    """Create a user-visible report while preserving the deterministic base."""

    def __init__(
        self,
        explanation_service: ShoppingReportExplanationService,
        *,
        renderer: MarkdownShoppingReportExplanationRenderer | None = None,
    ) -> None:
        self._explanation_service = explanation_service
        self._renderer = renderer or MarkdownShoppingReportExplanationRenderer()

    def present(self, rendered: RenderedShoppingReport) -> RenderedShoppingReportPresentation:
        """Explain if configured, then render a safe append-only presentation."""

        return self._renderer.render(self._explanation_service.explain(rendered))
