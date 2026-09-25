"""Private local JSON storage for dated chip benchmark references."""

import json
import os
from contextlib import suppress
from pathlib import Path

from pydantic import ValidationError

from personal_shopping_agent.infrastructure.local_security import (
    LocalFileSecurityError,
    inspect_private_file,
    require_private_directory,
)
from personal_shopping_agent.infrastructure.settings import DEFAULT_BENCHMARK_FILE
from personal_shopping_agent.sourcing.benchmarks import ChipBenchmarkReference


class BenchmarkStoreError(OSError):
    """Sanitized failure to save or load the private benchmark reference."""


class LocalJsonBenchmarkStore:
    """Replace one owner-only reference file atomically; never follow unsafe targets."""

    def __init__(self, path: Path = Path(DEFAULT_BENCHMARK_FILE)) -> None:
        self._path = path

    def save(self, reference: ChipBenchmarkReference) -> None:
        """Write the validated reference to a private temporary file, then replace."""

        payload = (
            json.dumps(reference.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        try:
            require_private_directory(self._path.parent)
            temporary.unlink(missing_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(payload)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, self._path)
        except OSError as error:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise BenchmarkStoreError("The benchmark reference could not be saved.") from error

    def load(self) -> ChipBenchmarkReference | None:
        """Return the stored reference, None when absent, or fail closed when unsafe."""

        try:
            status = inspect_private_file(self._path)
        except LocalFileSecurityError as error:
            raise BenchmarkStoreError("The benchmark reference could not be read.") from error
        if not status.exists:
            return None
        if status.private_permissions is False:
            raise BenchmarkStoreError("The benchmark reference file is not private.")
        try:
            return ChipBenchmarkReference.model_validate_json(self._path.read_bytes())
        except (OSError, ValidationError) as error:
            raise BenchmarkStoreError("The benchmark reference is unreadable.") from error
