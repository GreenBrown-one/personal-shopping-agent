"""Fail-closed local filesystem boundaries for private application state."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
POSIX_PERMISSIONS = os.name == "posix"


class LocalFileSecurityError(OSError):
    """Sanitized failure raised when local private-state guarantees cannot be met."""


@dataclass(frozen=True, slots=True)
class PrivateFileStatus:
    """Non-sensitive file presence and host permission state."""

    exists: bool
    private_permissions: bool | None


def require_private_directory(path: Path) -> None:
    """Create a dedicated directory privately or reject an unsafe existing target."""

    try:
        try:
            details = path.lstat()
        except FileNotFoundError:
            path.mkdir(parents=True, mode=PRIVATE_DIRECTORY_MODE)
            if POSIX_PERMISSIONS:
                path.chmod(PRIVATE_DIRECTORY_MODE)
            details = path.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise LocalFileSecurityError("private directory target is not a regular directory")
        if POSIX_PERMISSIONS and stat.S_IMODE(details.st_mode) != PRIVATE_DIRECTORY_MODE:
            raise LocalFileSecurityError("private directory permissions are not owner-only")
    except LocalFileSecurityError:
        raise
    except OSError as error:
        raise LocalFileSecurityError("private directory could not be secured") from error


def inspect_private_file(path: Path) -> PrivateFileStatus:
    """Inspect a local file without following a symbolic link."""

    try:
        details = path.lstat()
    except FileNotFoundError:
        return PrivateFileStatus(exists=False, private_permissions=None)
    except OSError as error:
        raise LocalFileSecurityError("private file could not be inspected") from error

    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        return PrivateFileStatus(exists=True, private_permissions=False)
    if POSIX_PERMISSIONS:
        permissions_private = stat.S_IMODE(details.st_mode) == PRIVATE_FILE_MODE
        links_private = details.st_nlink == 1
        return PrivateFileStatus(
            exists=True,
            private_permissions=permissions_private and links_private,
        )
    return PrivateFileStatus(exists=True, private_permissions=None)


def prepare_private_file(path: Path) -> None:
    """Create or restrict one regular, unlinked file to its current OS user."""

    try:
        _prepare_parent(path.parent)
        status = inspect_private_file(path)
        if not status.exists:
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                PRIVATE_FILE_MODE,
            )
            os.close(descriptor)
        secure_existing_private_file(path)
    except LocalFileSecurityError:
        raise
    except OSError as error:
        raise LocalFileSecurityError("private file could not be secured") from error


def secure_existing_private_file(path: Path) -> None:
    """Restrict an existing regular, unlinked file without creating a replacement."""

    try:
        status = inspect_private_file(path)
        if not status.exists:
            raise LocalFileSecurityError("private file target does not exist")
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise LocalFileSecurityError("private file target is not a regular file")
        if POSIX_PERMISSIONS and details.st_nlink != 1:
            raise LocalFileSecurityError("private file target has additional hard links")
        if POSIX_PERMISSIONS:
            path.chmod(PRIVATE_FILE_MODE)
        secured = inspect_private_file(path)
        if secured.private_permissions is False:
            raise LocalFileSecurityError("private file permissions could not be restricted")
    except LocalFileSecurityError:
        raise
    except OSError as error:
        raise LocalFileSecurityError("private file could not be secured") from error


def _prepare_parent(parent: Path) -> None:
    try:
        details = parent.lstat()
    except FileNotFoundError:
        parent.mkdir(parents=True, mode=PRIVATE_DIRECTORY_MODE)
        if POSIX_PERMISSIONS:
            parent.chmod(PRIVATE_DIRECTORY_MODE)
        details = parent.lstat()
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise LocalFileSecurityError("private file parent is not a regular directory")
