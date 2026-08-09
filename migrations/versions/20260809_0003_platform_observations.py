"""Add structured platform search and detail observations.

Revision ID: 20260809_0003
Revises: 20260809_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0003"
down_revision: str | None = "20260809_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create append-only structured platform observation storage."""

    op.create_table(
        "platform_observations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("platform", sa.String(length=40), nullable=False),
        sa.Column("external_id", sa.String(length=120), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["shopping_requests.id"],
            name="fk_platform_observations_request_id_shopping_requests",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_platform_observations"),
    )
    op.create_index(
        "ix_platform_observations_platform_external_captured",
        "platform_observations",
        ["platform", "external_id", "captured_at"],
        unique=False,
    )
    op.create_index(
        "ix_platform_observations_request_kind_captured",
        "platform_observations",
        ["request_id", "kind", "captured_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove structured observations while preserving earlier milestones."""

    op.drop_index(
        "ix_platform_observations_request_kind_captured",
        table_name="platform_observations",
    )
    op.drop_index(
        "ix_platform_observations_platform_external_captured",
        table_name="platform_observations",
    )
    op.drop_table("platform_observations")
