"""Read and optionally explain already persisted deterministic shopping reports."""

from typing import Protocol
from uuid import UUID

from personal_shopping_agent.application.explanation import (
    RenderedShoppingReportPresentation,
    ShoppingReportPresentationService,
)
from personal_shopping_agent.application.reporting import RenderedShoppingReport


class ShoppingReportNotFoundError(LookupError):
    """Raised when a workflow has no persisted deterministic report."""


class StoredShoppingReportIntegrityError(ValueError):
    """Raised when indexed storage fields disagree with the validated report snapshot."""


class ShoppingReportReader(Protocol):
    """Provider-neutral read port for one report addressed by workflow identity."""

    def get(self, workflow_id: UUID) -> RenderedShoppingReport: ...


class ShoppingReportAccessService:
    """Expose deterministic reads and explicit, non-persistent explanation reads."""

    def __init__(
        self,
        reader: ShoppingReportReader,
        presentation_service: ShoppingReportPresentationService,
    ) -> None:
        self._reader = reader
        self._presentation_service = presentation_service

    def get(self, workflow_id: UUID) -> RenderedShoppingReport:
        """Return the stored deterministic report without invoking any model."""

        return self._reader.get(workflow_id)

    def explain(self, workflow_id: UUID) -> RenderedShoppingReportPresentation:
        """Read one report and explicitly request its optional explanation overlay."""

        return self._presentation_service.present(self._reader.get(workflow_id))
