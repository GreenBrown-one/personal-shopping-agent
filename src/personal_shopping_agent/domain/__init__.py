"""Shared kernel: domain contracts, invariants, and the deterministic workflow state machine.

Every capability layer depends on this package; it depends on none of them.
"""

from personal_shopping_agent.domain.criteria import (
    InvalidCriterionDefinitionError,
    numeric_criterion_bounds,
)
from personal_shopping_agent.domain.enums import (
    EvidenceSourceType,
    EvidenceSubjectType,
    PriceKind,
    StoreType,
)
from personal_shopping_agent.domain.measurements import (
    MEASUREMENT_DEFINITIONS,
    MeasurementDefinition,
    is_declared_unit,
    measurement_definition,
    measurement_for_criterion_key,
    normalize_measurement,
    specification_key_token,
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
    "MEASUREMENT_DEFINITIONS",
    "Budget",
    "Evidence",
    "EvidenceSourceType",
    "EvidenceSubjectType",
    "InvalidCriterionDefinitionError",
    "InvalidWorkflowTransitionError",
    "JsonContractModel",
    "MeasurementDefinition",
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
    "is_declared_unit",
    "measurement_definition",
    "measurement_for_criterion_key",
    "normalize_measurement",
    "numeric_criterion_bounds",
    "specification_key_token",
]
