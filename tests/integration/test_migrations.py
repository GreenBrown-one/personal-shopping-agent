"""Integration test for upgrading and downgrading the Alembic schema."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_initial_migration_upgrades_and_downgrades(tmp_path: Path) -> None:
    database_path = tmp_path / "migrated.db"
    database_url = f"sqlite:///{database_path}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == {
        "alembic_version",
        "evidence_items",
        "evidence_checks",
        "normalized_specifications",
        "offers",
        "platform_observations",
        "products",
        "shopping_requests",
        "shopping_workflows",
        "workflow_events",
    }
    assert inspector.get_foreign_keys("offers")[0]["referred_table"] == "products"
    assert inspector.get_foreign_keys("shopping_workflows")[0]["referred_table"] == (
        "shopping_requests"
    )
    assert inspector.get_foreign_keys("workflow_events")[0]["referred_table"] == (
        "shopping_workflows"
    )
    assert inspector.get_foreign_keys("platform_observations")[0]["referred_table"] == (
        "shopping_requests"
    )
    assert {item["referred_table"] for item in inspector.get_foreign_keys("evidence_checks")} == {
        "products",
        "shopping_requests",
        "shopping_workflows",
    }
    assert {
        item["referred_table"] for item in inspector.get_foreign_keys("normalized_specifications")
    } == {
        "products",
        "shopping_requests",
        "shopping_workflows",
    }
    assert inspector.get_foreign_keys("evidence_items")[0]["referred_table"] == (
        "shopping_requests"
    )
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260809_0005"
    engine.dispose()

    command.downgrade(config, "base")

    downgraded_engine = create_engine(database_url)
    assert inspect(downgraded_engine).get_table_names() == ["alembic_version"]
    downgraded_engine.dispose()
