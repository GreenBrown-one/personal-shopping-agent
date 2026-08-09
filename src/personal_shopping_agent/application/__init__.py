"""Application use cases and deterministic orchestration contracts."""

from personal_shopping_agent.application.discovery import (
    CandidateDiscoveryService,
    CollectedPage,
    PageCollector,
    PlatformAccessRestrictedError,
    PlatformCandidate,
    PlatformSearchAdapter,
    PlatformSearchResult,
)
from personal_shopping_agent.application.service import (
    ShoppingWorkflowService,
    next_workflow_state,
)
from personal_shopping_agent.application.workflow import (
    InvalidWorkflowTransitionError,
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStateMachine,
)

__all__ = [
    "CandidateDiscoveryService",
    "CollectedPage",
    "InvalidWorkflowTransitionError",
    "PageCollector",
    "PlatformAccessRestrictedError",
    "PlatformCandidate",
    "PlatformSearchAdapter",
    "PlatformSearchResult",
    "ShoppingWorkflow",
    "ShoppingWorkflowService",
    "WorkflowEvent",
    "WorkflowSnapshot",
    "WorkflowState",
    "WorkflowStateMachine",
    "next_workflow_state",
]
