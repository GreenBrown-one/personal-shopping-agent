"""MCP-specific inputs, capability status, and compact outputs kept outside the core layers."""

from decimal import Decimal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from personal_shopping_agent.automation import ShoppingPipelineOptions, ShoppingPipelineResult
from personal_shopping_agent.domain import (
    Budget,
    JsonContractModel,
    Money,
    ShoppingCriterion,
    ShoppingRequest,
    WorkflowState,
)
from personal_shopping_agent.presentation import (
    ExplanationFallbackReason,
    ExplanationStatus,
    RenderedShoppingReport,
    RenderedShoppingReportPresentation,
    ReportFormat,
)


class MCPModel(BaseModel):
    """Strict schema base for model-controlled MCP tool boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CriterionInput(MCPModel):
    """Convenient criterion shape accepted by the workflow-start tool."""

    key: str = Field(min_length=1, max_length=120)
    weight: Decimal = Field(default=Decimal("1"), gt=0)
    hard_requirement: bool = False
    minimum: Decimal | bool | str | None = None
    preferred: Decimal | bool | str | None = None
    maximum: Decimal | bool | str | None = None
    unit: str | None = Field(default=None, max_length=40)

    def to_domain(self) -> ShoppingCriterion:
        """Convert validated interface input into the provider-neutral domain model."""

        return ShoppingCriterion.model_validate(self.model_dump(mode="python"))


class StartShoppingWorkflowInput(MCPModel):
    """Minimal structured input the AI derives from the user's shopping request."""

    query: str = Field(
        min_length=1,
        max_length=2_000,
        description=(
            "Short shopping-platform search keywords, sent verbatim to the platform search box "
            "(for example '大电池手机'). Keep preferences and constraints out of this field."
        ),
    )
    category: str = Field(min_length=1, max_length=160)
    budget_maximum: Decimal = Field(gt=0)
    budget_currency: str = Field(default="CNY", min_length=3, max_length=3)
    stretch_budget_maximum: Decimal | None = Field(default=None, gt=0)
    region: str | None = Field(default=None, max_length=160)
    criteria: tuple[CriterionInput, ...] = ()

    def to_domain(self) -> ShoppingRequest:
        """Build a new validated request with server-owned identity and timestamp."""

        stretch = (
            Money(amount=self.stretch_budget_maximum, currency=self.budget_currency)
            if self.stretch_budget_maximum is not None
            else None
        )
        return ShoppingRequest(
            query=self.query,
            category=self.category,
            budget=Budget(
                maximum=Money(amount=self.budget_maximum, currency=self.budget_currency),
                stretch_maximum=stretch,
            ),
            region=self.region,
            criteria=tuple(criterion.to_domain() for criterion in self.criteria),
        )


class RunShoppingPipelineInput(MCPModel):
    """Explicit bounded request to resume all configured shopping stages."""

    workflow_id: UUID
    maximum_candidates: int = Field(default=20, ge=1, le=100)
    maximum_details: int = Field(default=5, ge=1, le=10)

    def to_options(self) -> ShoppingPipelineOptions:
        """Reuse the application boundary for cross-field limit validation."""

        return ShoppingPipelineOptions(
            maximum_candidates=self.maximum_candidates,
            maximum_details=self.maximum_details,
        )


class AgentCapabilities(MCPModel):
    """Honest feature status so the host cannot assume unfinished abilities exist."""

    milestone: str
    requirement_review: bool
    local_workflows: bool
    local_storage: bool
    end_to_end_pipeline: bool
    platform_collection: bool
    cross_platform_comparison: bool
    ranking: bool
    chip_benchmark: bool
    deterministic_reports: bool
    html_reports: bool
    llm_explanations: bool
    llm_explanation_provider: str
    automatic_purchase: bool
    message: str


class MCPViewModel(JsonContractModel):
    """Compact tool output; unlike inputs, text is never stripped so hashes stay exact."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ShoppingReportView(MCPViewModel):
    """The deterministic report text and its identity, without the full JSON snapshot."""

    workflow_id: UUID
    report_id: UUID
    format: ReportFormat
    candidates: int = Field(ge=1)
    eligible_candidates: int = Field(ge=0)
    recommended_product_id: UUID | None
    content_sha256: str
    rendered_at: AwareDatetime
    content: str

    @classmethod
    def from_rendered(cls, rendered: RenderedShoppingReport) -> "ShoppingReportView":
        """Project an already integrity-checked report without altering its bytes."""

        report = rendered.report
        return cls(
            workflow_id=report.workflow_id,
            report_id=report.id,
            format=rendered.format,
            candidates=len(report.candidates),
            eligible_candidates=sum(1 for item in report.candidates if item.score.eligible),
            recommended_product_id=report.recommended_product_id,
            content_sha256=rendered.content_sha256,
            rendered_at=rendered.rendered_at,
            content=rendered.content,
        )


class ReportRenderView(MCPViewModel):
    """Workflow state after rendering, plus the compact report."""

    workflow_state: WorkflowState
    report: ShoppingReportView


class ReportExplanationView(MCPViewModel):
    """Explanation outcome and the presented text (the deterministic report on fallback)."""

    workflow_id: UUID
    report_id: UUID
    status: ExplanationStatus
    fallback_reason: ExplanationFallbackReason | None
    provider_name: str | None
    model_name: str | None
    content_sha256: str
    content: str

    @classmethod
    def from_presentation(
        cls, presentation: RenderedShoppingReportPresentation
    ) -> "ReportExplanationView":
        """Project an already validated presentation without altering its bytes."""

        result = presentation.result
        return cls(
            workflow_id=result.rendered.report.workflow_id,
            report_id=result.rendered.report.id,
            status=result.status,
            fallback_reason=result.fallback_reason,
            provider_name=result.provider_name,
            model_name=result.model_name,
            content_sha256=presentation.content_sha256,
            content=presentation.content,
        )


class PipelineRunView(MCPViewModel):
    """Stages this call executed and the final report, ready to show the user."""

    workflow_id: UUID
    started_from: WorkflowState
    executed_stages: tuple[WorkflowState, ...]
    workflow_state: WorkflowState
    report: ShoppingReportView

    @classmethod
    def from_result(cls, result: ShoppingPipelineResult) -> "PipelineRunView":
        """Summarize a validated pipeline result."""

        return cls(
            workflow_id=result.snapshot.workflow.id,
            started_from=result.started_from,
            executed_stages=result.executed_stages,
            workflow_state=result.snapshot.workflow.state,
            report=ShoppingReportView.from_rendered(result.rendered),
        )
