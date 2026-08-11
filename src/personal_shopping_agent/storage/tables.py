"""SQLAlchemy persistence records kept separate from validated domain models."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, MetaData, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative metadata root used by runtime setup and Alembic."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class ShoppingRequestRecord(Base):
    """Indexed request fields plus the complete validated domain snapshot."""

    __tablename__ = "shopping_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    region: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ProductRecord(Base):
    """Stable product identity snapshot with no offer price columns."""

    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    brand: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(240), nullable=False)
    category: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    canonical_name: Mapped[str] = mapped_column(String(400), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class OfferRecord(Base):
    """Time-specific offer snapshot linked to one stable product."""

    __tablename__ = "offers"
    __table_args__ = (
        Index("ix_offers_product_captured", "product_id", "captured_at"),
        Index("ix_offers_platform_sku", "platform", "sku"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(120), nullable=False)
    seller: Mapped[str] = mapped_column(String(240), nullable=False)
    sku: Mapped[str | None] = mapped_column(String(240))
    variant: Mapped[str | None] = mapped_column(String(400))
    region: Mapped[str | None] = mapped_column(String(160))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class EvidenceRecord(Base):
    """Polymorphic evidence snapshot; duplicate field observations are intentional."""

    __tablename__ = "evidence_items"
    __table_args__ = (
        Index("ix_evidence_subject_field", "subject_type", "subject_id", "field_path"),
        Index("ix_evidence_source_captured", "source_type", "captured_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(36), nullable=False)
    field_path: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
