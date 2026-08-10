"""MCP-specific input and capability models kept outside the core domain."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from personal_shopping_agent.domain import (
    Budget,
    Money,
    ShoppingCriterion,
    ShoppingRequest,
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

    query: str = Field(min_length=1, max_length=2_000)
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


class AgentCapabilities(MCPModel):
    """Honest feature status so the host cannot assume unfinished abilities exist."""

    milestone: str
    local_workflows: bool
    local_storage: bool
    platform_collection: bool
    cross_platform_comparison: bool
    ranking: bool
    deterministic_reports: bool
    html_reports: bool
    llm_explanations: bool
    llm_explanation_provider: str
    automatic_purchase: bool
    message: str
