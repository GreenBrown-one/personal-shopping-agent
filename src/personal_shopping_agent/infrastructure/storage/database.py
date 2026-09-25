"""SQLite engine and session setup with foreign-key enforcement."""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from typing import cast

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from personal_shopping_agent.infrastructure.storage.tables import Base


def _enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
    """Enable SQLite foreign-key constraints for every new DB-API connection."""

    connection = cast(sqlite3.Connection, dbapi_connection)
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def create_sqlite_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create a SQLite engine suitable for a local file or shared in-memory tests."""

    if not database_url.startswith("sqlite:"):
        raise ValueError("only SQLite database URLs are supported")

    if database_url in {"sqlite://", "sqlite:///:memory:"}:
        engine = create_engine(
            database_url,
            echo=echo,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_engine(database_url, echo=echo)

    event.listen(engine, "connect", _enable_foreign_keys)

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return sessions that keep domain lifetimes independent from ORM state."""

    return sessionmaker(bind=engine, expire_on_commit=False)


def create_schema(engine: Engine) -> None:
    """Create the current schema for tests; deployed databases use Alembic migrations."""

    Base.metadata.create_all(engine)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Generator[Session]:
    """Commit one unit of work or roll it back on failure."""

    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
