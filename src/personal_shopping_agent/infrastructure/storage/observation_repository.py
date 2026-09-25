"""SQLite persistence for structured platform search and detail observations."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from personal_shopping_agent.application import (
    DetailObservation,
    PlatformObservationKind,
    SearchObservation,
)
from personal_shopping_agent.storage.database import session_scope
from personal_shopping_agent.storage.repository import (
    DuplicateEntityError,
    EntityNotFoundError,
    InvalidReferenceError,
    domain_payload,
)
from personal_shopping_agent.storage.tables import PlatformObservationRecord


class ObservationKindMismatchError(ValueError):
    """Raised when an observation is loaded through the wrong typed method."""


def search_observation_record(observation: SearchObservation) -> PlatformObservationRecord:
    """Map one validated search envelope to its indexed database record."""

    return PlatformObservationRecord(
        id=str(observation.id),
        request_id=str(observation.request_id),
        kind=PlatformObservationKind.SEARCH.value,
        platform=observation.result.platform,
        external_id=None,
        captured_at=observation.result.captured_at,
        payload=domain_payload(observation),
    )


def detail_observation_record(observation: DetailObservation) -> PlatformObservationRecord:
    """Map one validated detail envelope to its indexed database record."""

    return PlatformObservationRecord(
        id=str(observation.id),
        request_id=str(observation.request_id),
        kind=PlatformObservationKind.DETAIL.value,
        platform=observation.detail.platform,
        external_id=observation.detail.external_id,
        captured_at=observation.detail.captured_at,
        payload=domain_payload(observation),
    )


class SQLitePlatformObservationRepository:
    """Store structured observations while deliberately excluding raw HTML and screenshots."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def _insert(self, record: PlatformObservationRecord) -> None:
        try:
            with session_scope(self._session_factory) as session:
                session.add(record)
        except IntegrityError as exc:
            if "FOREIGN KEY constraint failed" in str(exc.orig):
                raise InvalidReferenceError(
                    "platform observation references an unknown shopping request"
                ) from exc
            raise DuplicateEntityError("platform observation identifier already exists") from exc

    def _get_record(self, observation_id: UUID) -> PlatformObservationRecord:
        with self._session_factory() as session:
            record = session.get(PlatformObservationRecord, str(observation_id))
            if record is None:
                raise EntityNotFoundError("platform_observations entity was not found")
            session.expunge(record)
            return record

    @staticmethod
    def _require_kind(
        record: PlatformObservationRecord,
        expected: PlatformObservationKind,
    ) -> None:
        if record.kind != expected.value:
            raise ObservationKindMismatchError(
                f"observation is {record.kind!r}, not {expected.value!r}"
            )

    def add_search_observation(self, observation: SearchObservation) -> None:
        """Append one structured search result without replacing earlier results."""

        self._insert(search_observation_record(observation))

    def get_search_observation(self, observation_id: UUID) -> SearchObservation:
        """Load and revalidate one structured search result."""

        record = self._get_record(observation_id)
        self._require_kind(record, PlatformObservationKind.SEARCH)
        return SearchObservation.model_validate(record.payload)

    def list_search_observations(self, request_id: UUID) -> tuple[SearchObservation, ...]:
        """Return a request's search observations in deterministic capture order."""

        records = self._list_records(request_id, PlatformObservationKind.SEARCH)
        return tuple(SearchObservation.model_validate(record.payload) for record in records)

    def add_detail_observation(self, observation: DetailObservation) -> None:
        """Append one unverified detail result without overwriting conflicts."""

        self._insert(detail_observation_record(observation))

    def get_detail_observation(self, observation_id: UUID) -> DetailObservation:
        """Load and revalidate one structured detail result."""

        record = self._get_record(observation_id)
        self._require_kind(record, PlatformObservationKind.DETAIL)
        return DetailObservation.model_validate(record.payload)

    def list_detail_observations(
        self,
        request_id: UUID,
        *,
        platform: str | None = None,
        external_id: str | None = None,
    ) -> tuple[DetailObservation, ...]:
        """Return detail history, optionally narrowed to one platform product identity."""

        statement = select(PlatformObservationRecord).where(
            PlatformObservationRecord.request_id == str(request_id),
            PlatformObservationRecord.kind == PlatformObservationKind.DETAIL.value,
        )
        if platform is not None:
            statement = statement.where(PlatformObservationRecord.platform == platform)
        if external_id is not None:
            statement = statement.where(PlatformObservationRecord.external_id == external_id)
        statement = statement.order_by(
            PlatformObservationRecord.captured_at,
            PlatformObservationRecord.id,
        )
        with self._session_factory() as session:
            records = session.scalars(statement).all()
            return tuple(DetailObservation.model_validate(record.payload) for record in records)

    def _list_records(
        self,
        request_id: UUID,
        kind: PlatformObservationKind,
    ) -> list[PlatformObservationRecord]:
        statement = (
            select(PlatformObservationRecord)
            .where(
                PlatformObservationRecord.request_id == str(request_id),
                PlatformObservationRecord.kind == kind.value,
            )
            .order_by(PlatformObservationRecord.captured_at, PlatformObservationRecord.id)
        )
        with self._session_factory() as session:
            return list(session.scalars(statement).all())
