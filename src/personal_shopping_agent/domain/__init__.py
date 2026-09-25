"""Shared kernel: domain contracts, invariants, and the deterministic workflow state machine.

Every capability layer depends on this package; it depends on none of them.
"""

from personal_shopping_agent.domain.enums import (
    EvidenceSourceType,
    EvidenceSubjectType,
    PriceKind,
    StoreType,
)
from personal_shopping_agent.domain.models import (
    Budget,
    Evidence,
    Money,
    Offer,
    PriceBreakdown,
    Product,
    ShoppingCriterion,
    ShoppingRequest,
    Specification,
)
from personal_shopping_agent.domain.serialization import JsonContractModel
from personal_shopping_agent.domain.workflow import (
    InvalidWorkflowTransitionError,
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStateMachine,
)

__all__ = [
    "Budget",
    "Evidence",
    "EvidenceSourceType",
    "EvidenceSubjectType",
    "InvalidWorkflowTransitionError",
    "JsonContractModel",
    "Money",
    "Offer",
    "PriceBreakdown",
    "PriceKind",
    "Product",
    "ShoppingCriterion",
    "ShoppingRequest",
    "ShoppingWorkflow",
    "Specification",
    "StoreType",
    "WorkflowEvent",
    "WorkflowSnapshot",
    "WorkflowState",
    "WorkflowStateMachine",
]
