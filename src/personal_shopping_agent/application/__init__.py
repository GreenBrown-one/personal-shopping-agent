"""Application use cases and deterministic orchestration contracts."""

from personal_shopping_agent.application.collection import (
    CandidateDiscovery,
    NoCandidatesDiscoveredError,
    ProductDetailCollector,
    SearchDetailCollectionResult,
    SearchDetailCollectionService,
    SearchDetailUnitOfWork,
    SearchDetailUnitOfWorkFactory,
)
from personal_shopping_agent.application.details import (
    PlatformDetailAdapter,
    PlatformDetailParseError,
    PlatformProductDetail,
    PlatformSpecificationObservation,
    PlatformVariantOption,
    ProductDetailService,
)
from personal_shopping_agent.application.discovery import (
    CandidateDiscoveryService,
    CollectedPage,
    PageCollector,
    PlatformAccessRestrictedError,
    PlatformCandidate,
    PlatformSearchAdapter,
    PlatformSearchResult,
)
from personal_shopping_agent.application.observations import (
    DetailObservation,
    PlatformObservationKind,
    PlatformObservationService,
    PlatformObservationStore,
    SearchObservation,
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
    "CandidateDiscovery",
    "CandidateDiscoveryService",
    "CollectedPage",
    "DetailObservation",
    "InvalidWorkflowTransitionError",
    "NoCandidatesDiscoveredError",
    "PageCollector",
    "PlatformAccessRestrictedError",
    "PlatformCandidate",
    "PlatformDetailAdapter",
    "PlatformDetailParseError",
    "PlatformObservationKind",
    "PlatformObservationService",
    "PlatformObservationStore",
    "PlatformProductDetail",
    "PlatformSearchAdapter",
    "PlatformSearchResult",
    "PlatformSpecificationObservation",
    "PlatformVariantOption",
    "ProductDetailCollector",
    "ProductDetailService",
    "SearchDetailCollectionResult",
    "SearchDetailCollectionService",
    "SearchDetailUnitOfWork",
    "SearchDetailUnitOfWorkFactory",
    "SearchObservation",
    "ShoppingWorkflow",
    "ShoppingWorkflowService",
    "WorkflowEvent",
    "WorkflowSnapshot",
    "WorkflowState",
    "WorkflowStateMachine",
    "next_workflow_state",
]
