"""Add durable candidate score results.

Revision ID: 20260809_0006
Revises: 20260809_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0006"
down_revision: str | None = "20260809_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create one auditable score per workflow candidate Product."""

    op.create_table(
        "candidate_scores",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("product_id", sa.String(length=36), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("pareto_front", sa.Integer(), nullable=True),
        sa.Column("final_score", sa.String(length=40), nullable=True),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_candidate_scores_product_id_products",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["shopping_requests.id"],
            name="fk_candidate_scores_request_id_shopping_requests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["shopping_workflows.id"],
            name="fk_candidate_scores_workflow_id_shopping_workflows",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_candidate_scores"),
        sa.UniqueConstraint(
            "workflow_id",
            "product_id",
            name="uq_candidate_scores_workflow_id_product_id",
        ),
    )
    op.create_index(
        "ix_candidate_scores_request_eligible_rank",
        "candidate_scores",
        ["request_id", "eligible", "rank"],
        unique=False,
    )
    op.create_index(
        "ix_candidate_scores_workflow_pareto",
        "candidate_scores",
        ["workflow_id", "pareto_front"],
        unique=False,
    )


def downgrade() -> None:
    """Remove durable candidate score results."""

    op.drop_index(
        "ix_candidate_scores_workflow_pareto",
        table_name="candidate_scores",
    )
    op.drop_index(
        "ix_candidate_scores_request_eligible_rank",
        table_name="candidate_scores",
    )
    op.drop_table("candidate_scores")
