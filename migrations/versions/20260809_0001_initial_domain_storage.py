"""Create the initial shopping domain storage schema.

Revision ID: 20260809_0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create request, product, offer, and evidence tables."""

    op.create_table(
        "products",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("brand", sa.String(length=160), nullable=False),
        sa.Column("model", sa.String(length=240), nullable=False),
        sa.Column("category", sa.String(length=160), nullable=False),
        sa.Column("canonical_name", sa.String(length=400), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_products"),
    )
    op.create_index("ix_products_brand", "products", ["brand"], unique=False)
    op.create_index("ix_products_category", "products", ["category"], unique=False)

    op.create_table(
        "shopping_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=160), nullable=False),
        sa.Column("region", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_shopping_requests"),
    )
    op.create_index(
        "ix_shopping_requests_category", "shopping_requests", ["category"], unique=False
    )

    op.create_table(
        "offers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("product_id", sa.String(length=36), nullable=False),
        sa.Column("platform", sa.String(length=120), nullable=False),
        sa.Column("seller", sa.String(length=240), nullable=False),
        sa.Column("sku", sa.String(length=240), nullable=True),
        sa.Column("variant", sa.String(length=400), nullable=True),
        sa.Column("region", sa.String(length=160), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_offers_product_id_products",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_offers"),
    )
    op.create_index("ix_offers_platform_sku", "offers", ["platform", "sku"], unique=False)
    op.create_index(
        "ix_offers_product_captured", "offers", ["product_id", "captured_at"], unique=False
    )

    op.create_table(
        "evidence_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.String(length=36), nullable=False),
        sa.Column("field_path", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_evidence_items"),
    )
    op.create_index(
        "ix_evidence_source_captured",
        "evidence_items",
        ["source_type", "captured_at"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_subject_field",
        "evidence_items",
        ["subject_type", "subject_id", "field_path"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the M1 storage schema in reverse dependency order."""

    op.drop_index("ix_evidence_subject_field", table_name="evidence_items")
    op.drop_index("ix_evidence_source_captured", table_name="evidence_items")
    op.drop_table("evidence_items")
    op.drop_index("ix_offers_product_captured", table_name="offers")
    op.drop_index("ix_offers_platform_sku", table_name="offers")
    op.drop_table("offers")
    op.drop_index("ix_shopping_requests_category", table_name="shopping_requests")
    op.drop_table("shopping_requests")
    op.drop_index("ix_products_category", table_name="products")
    op.drop_index("ix_products_brand", table_name="products")
    op.drop_table("products")
