"""Exclusive private JSON archive writer for local workflow exports."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from personal_shopping_agent.automation.data_lifecycle import (
    ArchiveTargetExistsError,
    ArchiveWriteError,
    ArchiveWriteReceipt,
    WorkflowDataArchive,
)


class LocalJsonWorkflowArchiveWriter:
    """Create a mode-0600 JSON file while refusing every overwrite."""

    def write(self, archive: WorkflowDataArchive, output_path: Path) -> ArchiveWriteReceipt:
        """Serialize canonical UTF-8 JSON and durably write one new local file."""

        payload = (
            json.dumps(
                archive.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        content_sha256 = hashlib.sha256(payload).hexdigest()
        created = False
        try:
            descriptor = os.open(
                output_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            created = True
            descriptor_chmod = getattr(os, "fchmod", None)
            if descriptor_chmod is not None:
                descriptor_chmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(payload)
                destination.flush()
                os.fsync(destination.fileno())
        except FileExistsError as error:
            raise ArchiveTargetExistsError("archive output already exists") from error
        except OSError as error:
            if created:
                output_path.unlink(missing_ok=True)
            raise ArchiveWriteError("archive could not be written") from error
        return ArchiveWriteReceipt(
            content_sha256=content_sha256,
            bytes_written=len(payload),
        )
