"""Read and optionally explain already persisted deterministic shopping reports."""

from typing import Protocol
from uuid import UUID

from personal_shopping_agent.presentation.explanation import (
    RenderedShoppingReportPresentation,
    ShoppingReportPresentationService,
)
from personal_shopping_agent.presentation.reporting import (
    RenderedShoppingReport,
    ShoppingReportRenderer,
)


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
        html_renderer: ShoppingReportRenderer,
    ) -> None:
        self._reader = reader
        self._presentation_service = presentation_service
        self._html_renderer = html_renderer

    def get(self, workflow_id: UUID) -> RenderedShoppingReport:
        """Return the stored deterministic report without invoking any model."""

        return self._reader.get(workflow_id)

    def explain(self, workflow_id: UUID) -> RenderedShoppingReportPresentation:
        """Read one report and explicitly request its optional explanation overlay."""

        return self._presentation_service.present(self._reader.get(workflow_id))

    def get_html(self, workflow_id: UUID) -> RenderedShoppingReport:
        """Derive deterministic HTML from the same validated report snapshot."""

        return self._html_renderer.render(self._reader.get(workflow_id).report)
