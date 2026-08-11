"""Add deterministic workflow state and audit events.

Revision ID: 20260809_0002
Revises: 20260809_0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0002"
down_revision: str | None = "20260809_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create current workflow and append-only event tables."""

    op.create_table(
        "shopping_workflows",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["shopping_requests.id"],
            name="fk_shopping_workflows_request_id_shopping_requests",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_shopping_workflows"),
    )
    op.create_index("ix_shopping_workflows_state", "shopping_workflows", ["state"], unique=False)

    op.create_table(
        "workflow_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(length=64), nullable=True),
        sa.Column("to_state", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=240), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["shopping_workflows.id"],
            name="fk_workflow_events_workflow_id_shopping_workflows",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_events"),
        sa.UniqueConstraint(
            "workflow_id",
            "sequence",
            name="uq_workflow_events_workflow_id_sequence",
        ),
    )
    op.create_index(
        "ix_workflow_events_workflow_sequence",
        "workflow_events",
        ["workflow_id", "sequence"],
        unique=False,
    )


def downgrade() -> None:
    """Remove workflow tables while preserving M1 shopping data."""

    op.drop_index("ix_workflow_events_workflow_sequence", table_name="workflow_events")
    op.drop_table("workflow_events")
    op.drop_index("ix_shopping_workflows_state", table_name="shopping_workflows")
    op.drop_table("shopping_workflows")
