"""The chip benchmark reference is stored privately and fails closed when unsafe."""

import os
import stat
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import HttpUrl

from personal_shopping_agent.infrastructure import benchmark_store
from personal_shopping_agent.infrastructure.benchmark_store import (
    BenchmarkStoreError,
    LocalJsonBenchmarkStore,
)
from personal_shopping_agent.infrastructure.local_security import LocalFileSecurityError
from personal_shopping_agent.sourcing import ChipBenchmarkEntry, ChipBenchmarkReference

REFERENCE = ChipBenchmarkReference(
    source_url=HttpUrl("https://www.socpk.com/allperf/?brand=phone"),
    source_title="极客湾 SOCPK",
    method="CPU 70% / GPU 30%",
    captured_at=datetime(2026, 9, 1, tzinfo=UTC),
    entries=(ChipBenchmarkEntry(name="骁龙 8 Gen3", score=Decimal("280.0")),),
)


posix_only = pytest.mark.skipif(
    os.name != "posix", reason="owner-only mode bits are enforced only on POSIX hosts"
)


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "benchmarks" / "socpk.json"
    store = LocalJsonBenchmarkStore(path)

    assert store.load() is None
    store.save(REFERENCE)
    store.save(REFERENCE)  # refreshing replaces the previous snapshot

    assert store.load() == REFERENCE
    assert [item.name for item in path.parent.iterdir()] == ["socpk.json"]


@posix_only
def test_saved_reference_has_owner_only_permissions(tmp_path: Path) -> None:
    path = tmp_path / "benchmarks" / "socpk.json"
    LocalJsonBenchmarkStore(path).save(REFERENCE)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_nonregular_or_invalid_files_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "benchmarks" / "socpk.json"
    store = LocalJsonBenchmarkStore(path)
    path.mkdir(parents=True)
    with pytest.raises(BenchmarkStoreError, match="not private"):
        store.load()

    path.rmdir()
    store.save(REFERENCE)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(BenchmarkStoreError, match="unreadable"):
        store.load()


@posix_only
def test_broadly_readable_files_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "benchmarks" / "socpk.json"
    store = LocalJsonBenchmarkStore(path)
    store.save(REFERENCE)

    path.chmod(0o644)
    with pytest.raises(BenchmarkStoreError, match="not private"):
        store.load()


def test_save_failures_are_sanitized_and_leave_no_temporary_file(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")

    with pytest.raises(BenchmarkStoreError, match="could not be saved"):
        LocalJsonBenchmarkStore(blocker / "socpk.json").save(REFERENCE)
    assert sorted(item.name for item in tmp_path.iterdir()) == ["not-a-directory"]


def test_inspection_failures_are_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_: Path) -> None:
        raise LocalFileSecurityError("raw detail")

    monkeypatch.setattr(benchmark_store, "inspect_private_file", fail)
    with pytest.raises(BenchmarkStoreError, match="could not be read"):
        LocalJsonBenchmarkStore(tmp_path / "socpk.json").load()
