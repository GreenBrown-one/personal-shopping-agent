"""SQLAlchemy persistence records kept separate from validated domain models."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
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
    request_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("shopping_requests.id", ondelete="CASCADE"), index=True
    )
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(36), nullable=False)
    field_path: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class EvidenceCheckRecord(Base):
    """Append-only result of comparing platform and official product facts."""

    __tablename__ = "evidence_checks"
    __table_args__ = (
        Index(
            "ix_evidence_checks_request_product_field",
            "request_id",
            "product_id",
            "field_path",
        ),
        Index("ix_evidence_checks_workflow_status", "workflow_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("shopping_requests.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("shopping_workflows.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    field_path: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class NormalizedSpecificationRecord(Base):
    """Append-only normalized values with raw evidence retained in the payload."""

    __tablename__ = "normalized_specifications"
    __table_args__ = (
        Index(
            "ix_normalized_specifications_request_product_key",
            "request_id",
            "product_id",
            "canonical_key",
        ),
        Index(
            "ix_normalized_specifications_workflow_status",
            "workflow_id",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("shopping_requests.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("shopping_workflows.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    canonical_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_unit: Mapped[str | None] = mapped_column(String(40))
    normalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class CandidateScoreRecord(Base):
    """Auditable final M4 candidate score linked to one workflow and product."""

    __tablename__ = "candidate_scores"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id",
            "product_id",
            name="uq_candidate_scores_workflow_id_product_id",
        ),
        Index(
            "ix_candidate_scores_request_eligible_rank",
            "request_id",
            "eligible",
            "rank",
        ),
        Index(
            "ix_candidate_scores_workflow_pareto",
            "workflow_id",
            "pareto_front",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("shopping_requests.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("shopping_workflows.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    pareto_front: Mapped[int | None] = mapped_column(Integer)
    final_score: Mapped[str | None] = mapped_column(String(40))
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class PlatformObservationRecord(Base):
    """Structured parser output linked to its originating shopping request."""

    __tablename__ = "platform_observations"
    __table_args__ = (
        Index(
            "ix_platform_observations_request_kind_captured",
            "request_id",
            "kind",
            "captured_at",
        ),
        Index(
            "ix_platform_observations_platform_external_captured",
            "platform",
            "external_id",
            "captured_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("shopping_requests.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(120))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class WorkflowRecord(Base):
    """Current orchestration state linked to one validated shopping request."""

    __tablename__ = "shopping_workflows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("shopping_requests.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(String(1_000))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class WorkflowEventRecord(Base):
    """Append-only ordered workflow transition audit record."""

    __tablename__ = "workflow_events"
    __table_args__ = (
        UniqueConstraint("workflow_id", "sequence", name="uq_workflow_events_workflow_id_sequence"),
        Index("ix_workflow_events_workflow_sequence", "workflow_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("shopping_workflows.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(64))
    to_state: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(240), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
