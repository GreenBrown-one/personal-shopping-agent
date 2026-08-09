"""SQLite repository that revalidates every stored domain snapshot on read."""

from typing import TypeVar, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from personal_shopping_agent.domain import (
    Evidence,
    EvidenceSubjectType,
    Offer,
    Product,
    ShoppingRequest,
)
from personal_shopping_agent.storage.database import session_scope
from personal_shopping_agent.storage.tables import (
    Base,
    EvidenceRecord,
    OfferRecord,
    ProductRecord,
    ShoppingRequestRecord,
)

RecordT = TypeVar("RecordT", bound=Base)


class DuplicateEntityError(ValueError):
    """Raised when a caller attempts to reuse an existing entity identifier."""


class InvalidReferenceError(ValueError):
    """Raised when an entity refers to a required parent that does not exist."""


class EntityNotFoundError(LookupError):
    """Raised when an entity identifier is not present in local storage."""


def domain_payload(model: BaseModel) -> dict[str, object]:
    """Serialize a validated domain object to a JSON-compatible dictionary."""

    return cast(dict[str, object], model.model_dump(mode="json"))


class SQLiteShoppingRepository:
    """Persistence adapter for the core M1 domain objects."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def _insert(self, record: Base) -> None:
        try:
            with session_scope(self._session_factory) as session:
                session.add(record)
        except IntegrityError as exc:
            if "FOREIGN KEY constraint failed" in str(exc.orig):
                raise InvalidReferenceError("referenced parent entity does not exist") from exc
            raise DuplicateEntityError("entity identifier already exists") from exc

    def _get_record(self, record_type: type[RecordT], entity_id: UUID) -> RecordT:
        with self._session_factory() as session:
            record = session.get(record_type, str(entity_id))
            if record is None:
                raise EntityNotFoundError(f"{record_type.__tablename__} entity was not found")
            session.expunge(record)
            return record

    def add_request(self, request: ShoppingRequest) -> None:
        """Persist a validated shopping request."""

        self._insert(
            ShoppingRequestRecord(
                id=str(request.id),
                query=request.query,
                category=request.category,
                region=request.region,
                created_at=request.created_at,
                payload=domain_payload(request),
            )
        )

    def get_request(self, request_id: UUID) -> ShoppingRequest:
        """Load and revalidate a shopping request."""

        record = self._get_record(ShoppingRequestRecord, request_id)
        return ShoppingRequest.model_validate(record.payload)

    def add_product(self, product: Product) -> None:
        """Persist stable product identity without offer pricing."""

        self._insert(
            ProductRecord(
                id=str(product.id),
                brand=product.brand,
                model=product.model,
                category=product.category,
                canonical_name=product.canonical_name,
                payload=domain_payload(product),
            )
        )

    def get_product(self, product_id: UUID) -> Product:
        """Load and revalidate stable product identity."""

        record = self._get_record(ProductRecord, product_id)
        return Product.model_validate(record.payload)

    def add_offer(self, offer: Offer) -> None:
        """Persist a time-specific offer linked to an existing product."""

        self._insert(
            OfferRecord(
                id=str(offer.id),
                product_id=str(offer.product_id),
                platform=offer.platform,
                seller=offer.seller,
                sku=offer.sku,
                variant=offer.variant,
                region=offer.region,
                captured_at=offer.captured_at,
                payload=domain_payload(offer),
            )
        )

    def get_offer(self, offer_id: UUID) -> Offer:
        """Load and revalidate an offer snapshot."""

        record = self._get_record(OfferRecord, offer_id)
        return Offer.model_validate(record.payload)

    def list_offers_for_product(self, product_id: UUID) -> tuple[Offer, ...]:
        """Return all offer snapshots in capture order for one product."""

        statement = (
            select(OfferRecord)
            .where(OfferRecord.product_id == str(product_id))
            .order_by(OfferRecord.captured_at, OfferRecord.id)
        )
        with self._session_factory() as session:
            records = session.scalars(statement).all()
            return tuple(Offer.model_validate(record.payload) for record in records)

    def add_evidence(self, evidence: Evidence) -> None:
        """Persist one observation without replacing possible conflicts."""

        self._insert(
            EvidenceRecord(
                id=str(evidence.id),
                subject_type=evidence.subject_type.value,
                subject_id=str(evidence.subject_id),
                field_path=evidence.field_path,
                source_type=evidence.source_type.value,
                source_url=str(evidence.source_url),
                captured_at=evidence.captured_at,
                payload=domain_payload(evidence),
            )
        )

    def get_evidence(self, evidence_id: UUID) -> Evidence:
        """Load and revalidate one evidence observation."""

        record = self._get_record(EvidenceRecord, evidence_id)
        return Evidence.model_validate(record.payload)

    def list_evidence(
        self,
        subject_type: EvidenceSubjectType,
        subject_id: UUID,
        *,
        field_path: str | None = None,
    ) -> tuple[Evidence, ...]:
        """Return all matching evidence, optionally narrowed to one field."""

        statement = select(EvidenceRecord).where(
            EvidenceRecord.subject_type == subject_type.value,
            EvidenceRecord.subject_id == str(subject_id),
        )
        if field_path is not None:
            statement = statement.where(EvidenceRecord.field_path == field_path)
        statement = statement.order_by(EvidenceRecord.captured_at, EvidenceRecord.id)
        with self._session_factory() as session:
            records = session.scalars(statement).all()
            return tuple(Evidence.model_validate(record.payload) for record in records)
