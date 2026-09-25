"""Read-only SQLite adapter for integrity-checked shopping report snapshots."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from personal_shopping_agent.infrastructure.storage.tables import ShoppingReportRecord
from personal_shopping_agent.presentation import (
    RenderedShoppingReport,
    ShoppingReportNotFoundError,
    StoredShoppingReportIntegrityError,
)


class SQLiteShoppingReportRepository:
    """Load reports by workflow while treating the validated payload as source of truth."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, workflow_id: UUID) -> RenderedShoppingReport:
        """Load, revalidate, and cross-check one deterministic report snapshot."""

        statement = select(ShoppingReportRecord).where(
            ShoppingReportRecord.workflow_id == str(workflow_id)
        )
        with self._session_factory() as session:
            record = session.scalar(statement)
            if record is None:
                raise ShoppingReportNotFoundError("shopping report was not found")
            rendered = RenderedShoppingReport.model_validate(record.payload)
            indexed = (
                record.id,
                record.request_id,
                record.workflow_id,
                record.format,
                record.content_sha256,
                record.content,
            )

        expected = (
            str(rendered.report.id),
            str(rendered.report.request.id),
            str(rendered.report.workflow_id),
            rendered.format.value,
            rendered.content_sha256,
            rendered.content,
        )
        if indexed != expected:
            raise StoredShoppingReportIntegrityError(
                "stored report indexes must match the validated snapshot"
            )
        return rendered
