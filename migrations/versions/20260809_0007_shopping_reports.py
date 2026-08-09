"""Add durable deterministic shopping reports.

Revision ID: 20260809_0007
Revises: 20260809_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0007"
down_revision: str | None = "20260809_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create one integrity-checked report per scored workflow."""

    op.create_table(
        "shopping_reports",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("format", sa.String(length=32), nullable=False),
        sa.Column("rendered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["shopping_requests.id"],
            name="fk_shopping_reports_request_id_shopping_requests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["shopping_workflows.id"],
            name="fk_shopping_reports_workflow_id_shopping_workflows",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_shopping_reports"),
        sa.UniqueConstraint(
            "workflow_id",
            name="uq_shopping_reports_workflow_id",
        ),
    )
    op.create_index(
        "ix_shopping_reports_request_rendered",
        "shopping_reports",
        ["request_id", "rendered_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove durable shopping reports."""

    op.drop_index(
        "ix_shopping_reports_request_rendered",
        table_name="shopping_reports",
    )
    op.drop_table("shopping_reports")
