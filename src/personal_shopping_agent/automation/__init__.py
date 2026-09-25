"""Layer 4 - automation across the whole workflow.

Workflow lifecycle, the resumable end-to-end pipeline, and per-workflow data export/deletion.
This layer only composes real use cases from the other layers; it never skips a stage.
"""

from personal_shopping_agent.automation.data_lifecycle import (
    ARCHIVE_FORMAT,
    DELETION_WARNING,
    EXPORT_PRIVACY_WARNING,
    ArchiveTargetExistsError,
    ArchiveWriteError,
    ArchiveWriteReceipt,
    WorkflowArchiveWriter,
    WorkflowDataArchive,
    WorkflowDataCounts,
    WorkflowDataExportResult,
    WorkflowDataIntegrityError,
    WorkflowDataLifecycleService,
    WorkflowDataOwnershipError,
    WorkflowDataSnapshot,
    WorkflowDataStore,
    WorkflowDeletionConfirmationError,
    WorkflowDeletionPlan,
    WorkflowDeletionPlanStaleError,
    WorkflowDeletionResult,
    deletion_confirmation_token,
)
from personal_shopping_agent.automation.pipeline import (
    PIPELINE_STATE_SEQUENCE,
    ShoppingDecisionPipelineService,
    ShoppingPipelineOptions,
    ShoppingPipelineResult,
    ShoppingPipelineStateError,
)
from personal_shopping_agent.automation.workflow_service import (
    ShoppingWorkflowService,
    next_workflow_state,
)

__all__ = [
    "ARCHIVE_FORMAT",
    "DELETION_WARNING",
    "EXPORT_PRIVACY_WARNING",
    "PIPELINE_STATE_SEQUENCE",
    "ArchiveTargetExistsError",
    "ArchiveWriteError",
    "ArchiveWriteReceipt",
    "ShoppingDecisionPipelineService",
    "ShoppingPipelineOptions",
    "ShoppingPipelineResult",
    "ShoppingPipelineStateError",
    "ShoppingWorkflowService",
    "WorkflowArchiveWriter",
    "WorkflowDataArchive",
    "WorkflowDataCounts",
    "WorkflowDataExportResult",
    "WorkflowDataIntegrityError",
    "WorkflowDataLifecycleService",
    "WorkflowDataOwnershipError",
    "WorkflowDataSnapshot",
    "WorkflowDataStore",
    "WorkflowDeletionConfirmationError",
    "WorkflowDeletionPlan",
    "WorkflowDeletionPlanStaleError",
    "WorkflowDeletionResult",
    "deletion_confirmation_token",
    "next_workflow_state",
]
