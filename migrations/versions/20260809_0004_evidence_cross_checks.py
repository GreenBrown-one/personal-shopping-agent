"""Add request-scoped evidence and durable field cross-checks.

Revision ID: 20260809_0004
Revises: 20260809_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0004"
down_revision: str | None = "20260809_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Link evidence to requests and add append-only field check results."""

    with op.batch_alter_table("evidence_items") as batch_op:
        batch_op.add_column(sa.Column("request_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_evidence_items_request_id_shopping_requests",
            "shopping_requests",
            ["request_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index("ix_evidence_items_request_id", ["request_id"], unique=False)

    op.create_table(
        "evidence_checks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("product_id", sa.String(length=36), nullable=False),
        sa.Column("field_path", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["shopping_requests.id"],
            name="fk_evidence_checks_request_id_shopping_requests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["shopping_workflows.id"],
            name="fk_evidence_checks_workflow_id_shopping_workflows",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_evidence_checks_product_id_products",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evidence_checks"),
    )
    op.create_index(
        "ix_evidence_checks_request_product_field",
        "evidence_checks",
        ["request_id", "product_id", "field_path"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_checks_workflow_status",
        "evidence_checks",
        ["workflow_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    """Remove cross-check results and request linkage from evidence."""

    op.drop_index("ix_evidence_checks_workflow_status", table_name="evidence_checks")
    op.drop_index("ix_evidence_checks_request_product_field", table_name="evidence_checks")
    op.drop_table("evidence_checks")
    with op.batch_alter_table("evidence_items") as batch_op:
        batch_op.drop_index("ix_evidence_items_request_id")
        batch_op.drop_constraint(
            "fk_evidence_items_request_id_shopping_requests",
            type_="foreignkey",
        )
        batch_op.drop_column("request_id")
