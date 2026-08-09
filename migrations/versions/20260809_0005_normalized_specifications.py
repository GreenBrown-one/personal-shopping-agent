"""Add durable normalized product specifications.

Revision ID: 20260809_0005
Revises: 20260809_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0005"
down_revision: str | None = "20260809_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create append-only normalized specification results and query indexes."""

    op.create_table(
        "normalized_specifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("product_id", sa.String(length=36), nullable=False),
        sa.Column("canonical_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("canonical_unit", sa.String(length=40), nullable=True),
        sa.Column("normalized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["shopping_requests.id"],
            name="fk_normalized_specifications_request_id_shopping_requests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["shopping_workflows.id"],
            name="fk_normalized_specifications_workflow_id_shopping_workflows",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_normalized_specifications_product_id_products",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_normalized_specifications"),
    )
    op.create_index(
        "ix_normalized_specifications_request_product_key",
        "normalized_specifications",
        ["request_id", "product_id", "canonical_key"],
        unique=False,
    )
    op.create_index(
        "ix_normalized_specifications_workflow_status",
        "normalized_specifications",
        ["workflow_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    """Remove normalized specification results."""

    op.drop_index(
        "ix_normalized_specifications_workflow_status",
        table_name="normalized_specifications",
    )
    op.drop_index(
        "ix_normalized_specifications_request_product_key",
        table_name="normalized_specifications",
    )
    op.drop_table("normalized_specifications")
