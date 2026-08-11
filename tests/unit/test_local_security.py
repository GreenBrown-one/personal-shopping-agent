"""Unit tests for fail-closed private local file boundaries."""

import os
import stat
from pathlib import Path

import pytest

import personal_shopping_agent.local_security as security_module
from personal_shopping_agent.local_security import (
    LocalFileSecurityError,
    inspect_private_file,
    prepare_private_file,
    require_private_directory,
    secure_existing_private_file,
)


def test_private_directory_is_created_once_with_owner_only_access(tmp_path: Path) -> None:
    directory = tmp_path / "nested" / "private"

    require_private_directory(directory)
    require_private_directory(directory)

    assert directory.is_dir()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize("target_kind", ["file", "directory", "symlink"])
def test_private_directory_rejects_unsafe_existing_targets(
    tmp_path: Path,
    target_kind: str,
) -> None:
    target = tmp_path / "target"
    if target_kind == "file":
        target.write_text("not a directory")
    elif target_kind == "directory":
        target.mkdir(mode=0o755)
        target.chmod(0o755)
    else:
        destination = tmp_path / "destination"
        destination.mkdir(mode=0o700)
        target.symlink_to(destination, target_is_directory=True)

    with pytest.raises(LocalFileSecurityError):
        require_private_directory(target)


def test_private_directory_sanitizes_filesystem_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_: Path) -> os.stat_result:
        raise PermissionError("sensitive directory detail")

    monkeypatch.setattr(Path, "lstat", fail)
    with pytest.raises(LocalFileSecurityError, match="could not be secured") as captured:
        require_private_directory(tmp_path / "private")
    assert "sensitive directory detail" not in str(captured.value)


def test_private_file_creation_inspection_and_permission_repair(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.db"

    assert inspect_private_file(path).exists is False
    prepare_private_file(path)
    assert inspect_private_file(path).private_permissions is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    path.chmod(0o644)
    assert inspect_private_file(path).private_permissions is False
    prepare_private_file(path)
    assert inspect_private_file(path).private_permissions is True


def test_private_file_rejects_nonregular_symlink_and_hardlink_targets(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    assert inspect_private_file(directory).private_permissions is False
    with pytest.raises(LocalFileSecurityError, match="regular file"):
        prepare_private_file(directory)

    source = tmp_path / "source"
    prepare_private_file(source)
    symlink = tmp_path / "symlink"
    symlink.symlink_to(source)
    assert inspect_private_file(symlink).private_permissions is False
    with pytest.raises(LocalFileSecurityError, match="regular file"):
        prepare_private_file(symlink)

    hardlink = tmp_path / "hardlink"
    hardlink.hardlink_to(source)
    assert inspect_private_file(source).private_permissions is False
    with pytest.raises(LocalFileSecurityError, match="hard links"):
        secure_existing_private_file(hardlink)


def test_existing_private_file_is_required_and_parent_must_be_a_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(LocalFileSecurityError, match="does not exist"):
        secure_existing_private_file(tmp_path / "missing")

    parent = tmp_path / "parent"
    parent.write_text("not a directory")
    with pytest.raises(LocalFileSecurityError, match="parent"):
        prepare_private_file(parent / "state.db")


def test_private_file_sanitizes_inspection_creation_and_permission_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"

    def fail_lstat(_: Path) -> os.stat_result:
        raise PermissionError("sensitive inspection detail")

    monkeypatch.setattr(Path, "lstat", fail_lstat)
    with pytest.raises(LocalFileSecurityError, match="could not be inspected") as captured:
        inspect_private_file(target)
    assert "sensitive inspection detail" not in str(captured.value)

    monkeypatch.undo()

    def fail_open(_: object, __: int, ___: int) -> int:
        raise PermissionError("sensitive creation detail")

    monkeypatch.setattr(security_module.os, "open", fail_open)
    with pytest.raises(LocalFileSecurityError, match="could not be secured") as captured:
        prepare_private_file(target)
    assert "sensitive creation detail" not in str(captured.value)

    monkeypatch.undo()
    target.write_text("state")
    target.chmod(0o644)

    def ignore_chmod(_: Path, __: int) -> None:
        return None

    monkeypatch.setattr(Path, "chmod", ignore_chmod)
    with pytest.raises(LocalFileSecurityError, match="could not be restricted"):
        secure_existing_private_file(target)

    monkeypatch.undo()

    def fail_chmod(_: Path, __: int) -> None:
        raise PermissionError("sensitive permission detail")

    monkeypatch.setattr(Path, "chmod", fail_chmod)
    with pytest.raises(LocalFileSecurityError, match="could not be secured") as captured:
        secure_existing_private_file(target)
    assert "sensitive permission detail" not in str(captured.value)


def test_non_posix_hosts_report_permissions_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.db"
    target.write_text("state")
    target.chmod(0o644)
    monkeypatch.setattr(security_module, "POSIX_PERMISSIONS", False)

    assert inspect_private_file(target).private_permissions is None
    secure_existing_private_file(target)
    assert inspect_private_file(target).private_permissions is None

    directory = tmp_path / "private-directory"
    require_private_directory(directory)
    assert directory.is_dir()

    nested_file = tmp_path / "non-posix-parent" / "state.db"
    prepare_private_file(nested_file)
    assert inspect_private_file(nested_file).private_permissions is None
