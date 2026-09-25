"""Resumable end-to-end orchestration across the deterministic shopping use cases."""

from typing import Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from personal_shopping_agent.automation.workflow_service import ShoppingWorkflowService
from personal_shopping_agent.domain.serialization import JsonContractModel
from personal_shopping_agent.domain.workflow import WorkflowSnapshot, WorkflowState
from personal_shopping_agent.presentation.ranking import CandidateScoringService
from personal_shopping_agent.presentation.report_access import ShoppingReportReader
from personal_shopping_agent.presentation.reporting import (
    RenderedShoppingReport,
    ShoppingReportService,
)
from personal_shopping_agent.sourcing.collection import SearchDetailCollectionService
from personal_shopping_agent.sourcing.cross_check import EvidenceCrossCheckService
from personal_shopping_agent.sourcing.ingestion import OfferIngestionService
from personal_shopping_agent.sourcing.normalization import SpecificationNormalizationService

PIPELINE_STATE_SEQUENCE = (
    WorkflowState.REQUEST_VALIDATED,
    WorkflowState.CANDIDATES_DISCOVERED,
    WorkflowState.OFFERS_COLLECTED,
    WorkflowState.EVIDENCE_CROSS_CHECKED,
    WorkflowState.DATA_NORMALIZED,
    WorkflowState.CANDIDATES_SCORED,
    WorkflowState.REPORT_RENDERED,
    WorkflowState.COMPLETED,
)


class PipelineModel(JsonContractModel):
    """Strict immutable base for end-to-end options and results."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ShoppingPipelineOptions(PipelineModel):
    """Hard-bounded collection limits for one explicit pipeline run."""

    maximum_candidates: int = Field(default=20, ge=1, le=100)
    maximum_details: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def detail_limit_fits_candidate_limit(self) -> Self:
        if self.maximum_details > self.maximum_candidates:
            raise ValueError("maximum_details cannot exceed maximum_candidates")
        return self


class ShoppingPipelineResult(PipelineModel):
    """Completed workflow, canonical report, and stages executed by this invocation."""

    started_from: WorkflowState
    executed_stages: tuple[WorkflowState, ...]
    snapshot: WorkflowSnapshot
    rendered: RenderedShoppingReport

    @model_validator(mode="after")
    def result_matches_completed_pipeline(self) -> Self:
        if self.snapshot.workflow.state is not WorkflowState.COMPLETED:
            raise ValueError("shopping pipeline result must contain a completed workflow")
        if self.rendered.report.workflow_id != self.snapshot.workflow.id:
            raise ValueError("shopping pipeline report must match its workflow")
        if self.started_from not in PIPELINE_STATE_SEQUENCE:
            raise ValueError("shopping pipeline start state is not resumable")
        start_index = PIPELINE_STATE_SEQUENCE.index(self.started_from)
        if self.executed_stages != PIPELINE_STATE_SEQUENCE[start_index + 1 :]:
            raise ValueError("shopping pipeline executed stages must be the exact remaining suffix")
        return self


class ShoppingPipelineStateError(ValueError):
    """Raised when a failed or pre-validation workflow cannot be resumed."""


class ShoppingDecisionPipelineService:
    """Resume and complete every real application step from the durable current state."""

    def __init__(
        self,
        workflow_service: ShoppingWorkflowService,
        collection_service: SearchDetailCollectionService,
        ingestion_service: OfferIngestionService,
        cross_check_service: EvidenceCrossCheckService,
        normalization_service: SpecificationNormalizationService,
        scoring_service: CandidateScoringService,
        report_service: ShoppingReportService,
        report_reader: ShoppingReportReader,
    ) -> None:
        self._workflow_service = workflow_service
        self._collection_service = collection_service
        self._ingestion_service = ingestion_service
        self._cross_check_service = cross_check_service
        self._normalization_service = normalization_service
        self._scoring_service = scoring_service
        self._report_service = report_service
        self._report_reader = report_reader

    async def run(
        self,
        workflow_id: UUID,
        *,
        options: ShoppingPipelineOptions,
    ) -> ShoppingPipelineResult:
        """Run only the stages remaining after the last durable successful transition."""

        snapshot = self._workflow_service.get(workflow_id)
        started_from = snapshot.workflow.state
        if started_from not in PIPELINE_STATE_SEQUENCE:
            raise ShoppingPipelineStateError(
                f"workflow state {started_from.value} cannot enter the shopping pipeline"
            )

        executed: list[WorkflowState] = []
        rendered: RenderedShoppingReport | None = None
        if snapshot.workflow.state is WorkflowState.REQUEST_VALIDATED:
            collected = await self._collection_service.collect(
                workflow_id,
                maximum_candidates=options.maximum_candidates,
                maximum_details=options.maximum_details,
                screenshot=False,
            )
            snapshot = collected.snapshot
            executed.append(WorkflowState.CANDIDATES_DISCOVERED)

        if snapshot.workflow.state is WorkflowState.CANDIDATES_DISCOVERED:
            ingested = self._ingestion_service.ingest(workflow_id)
            snapshot = ingested.snapshot
            executed.append(WorkflowState.OFFERS_COLLECTED)

        if snapshot.workflow.state is WorkflowState.OFFERS_COLLECTED:
            cross_checked = await self._cross_check_service.cross_check(workflow_id)
            snapshot = cross_checked.snapshot
            executed.append(WorkflowState.EVIDENCE_CROSS_CHECKED)

        if snapshot.workflow.state is WorkflowState.EVIDENCE_CROSS_CHECKED:
            normalized = self._normalization_service.normalize(workflow_id)
            snapshot = normalized.snapshot
            executed.append(WorkflowState.DATA_NORMALIZED)

        if snapshot.workflow.state is WorkflowState.DATA_NORMALIZED:
            scored = self._scoring_service.score(workflow_id)
            snapshot = scored.snapshot
            executed.append(WorkflowState.CANDIDATES_SCORED)

        if snapshot.workflow.state is WorkflowState.CANDIDATES_SCORED:
            reported = self._report_service.render(workflow_id)
            snapshot = reported.snapshot
            rendered = reported.rendered
            executed.append(WorkflowState.REPORT_RENDERED)

        if rendered is None:
            rendered = self._report_reader.get(workflow_id)

        if snapshot.workflow.state is WorkflowState.REPORT_RENDERED:
            snapshot = self._workflow_service.advance(
                workflow_id,
                WorkflowState.COMPLETED,
                reason="shopping_pipeline_completed",
            )
            executed.append(WorkflowState.COMPLETED)

        return ShoppingPipelineResult(
            started_from=started_from,
            executed_stages=tuple(executed),
            snapshot=snapshot,
            rendered=rendered,
        )
