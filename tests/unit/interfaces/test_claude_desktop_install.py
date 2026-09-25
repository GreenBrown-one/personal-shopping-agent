"""The Claude Desktop installer merges one entry, backs up first, and never leaks other servers."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from personal_shopping_agent.interfaces import host_config
from personal_shopping_agent.interfaces.cli import main
from personal_shopping_agent.interfaces.host_config import (
    SERVER_NAME,
    ClaudeDesktopConfigError,
    InstallStatus,
    claude_desktop_config_path,
    install_claude_desktop_server,
)

SERVER: dict[str, object] = {"command": "uv", "args": ["run"], "env": {"A": "1"}}
OTHER_SECRET = "sk-other-server-secret"
STAMP = datetime(2026, 9, 25, 8, 30, tzinfo=UTC)


def _existing(path: Path, document: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
    path.write_bytes(raw)
    return raw


def test_config_paths_follow_the_documented_locations(tmp_path: Path) -> None:
    assert claude_desktop_config_path(
        {"APPDATA": "C:/Users/me/AppData/Roaming"}, platform="win32", home=tmp_path
    ) == Path("C:/Users/me/AppData/Roaming/Claude/claude_desktop_config.json")
    assert claude_desktop_config_path({}, platform="darwin", home=tmp_path) == (
        tmp_path / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    )
    for platform, code in (("win32", "appdata_missing"), ("linux", "unsupported_platform")):
        with pytest.raises(ClaudeDesktopConfigError) as raised:
            claude_desktop_config_path({}, platform=platform, home=tmp_path)
        assert raised.value.code == code


def test_creates_a_new_config_when_claude_desktop_has_none(tmp_path: Path) -> None:
    path = tmp_path / "Claude" / "claude_desktop_config.json"

    result = install_claude_desktop_server(path, SERVER)

    assert result.status is InstallStatus.CREATED
    assert result.backup_path is None
    assert json.loads(path.read_text(encoding="utf-8")) == {"mcpServers": {SERVER_NAME: SERVER}}
    assert sorted(item.name for item in path.parent.iterdir()) == [path.name]


def test_update_keeps_other_servers_and_settings_and_backs_up_the_original(
    tmp_path: Path,
) -> None:
    path = tmp_path / "claude_desktop_config.json"
    original = _existing(
        path,
        {
            "globalShortcut": "Ctrl+Space",
            "mcpServers": {
                "other": {"command": "x", "env": {"API_KEY": OTHER_SECRET}},
                SERVER_NAME: {"command": "old"},
            },
        },
    )

    result = install_claude_desktop_server(path, SERVER, clock=lambda: STAMP)

    assert result.status is InstallStatus.UPDATED
    backup = tmp_path / "claude_desktop_config.json.backup-20260925T083000"
    assert result.backup_path == backup
    assert backup.read_bytes() == original
    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["globalShortcut"] == "Ctrl+Space"
    assert updated["mcpServers"]["other"]["env"]["API_KEY"] == OTHER_SECRET
    assert updated["mcpServers"][SERVER_NAME] == SERVER
    assert OTHER_SECRET not in repr(result)

    again = install_claude_desktop_server(path, SERVER, clock=lambda: STAMP)
    assert again.status is InstallStatus.UNCHANGED
    assert again.backup_path is None


def test_dry_run_and_utf8_bom_or_empty_files_are_handled(tmp_path: Path) -> None:
    path = tmp_path / "claude_desktop_config.json"
    path.write_bytes(b"\xef\xbb\xbf{}")

    preview = install_claude_desktop_server(path, SERVER, dry_run=True)
    assert preview.status is InstallStatus.PREVIEW
    assert path.read_bytes() == b"\xef\xbb\xbf{}"

    path.write_bytes(b"")
    assert install_claude_desktop_server(path, SERVER).status is InstallStatus.UPDATED


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[1, 2]", b'{"mcpServers": []}', b"\xff\xfe\x00"],
)
def test_damaged_configs_are_left_untouched(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "claude_desktop_config.json"
    path.write_bytes(content)

    with pytest.raises(ClaudeDesktopConfigError) as raised:
        install_claude_desktop_server(path, SERVER)

    assert raised.value.code == "config_invalid"
    assert path.read_bytes() == content
    assert [item.name for item in tmp_path.iterdir()] == [path.name]


def test_unreadable_and_unwritable_targets_fail_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory_target = tmp_path / "is-a-directory"
    directory_target.mkdir()
    with pytest.raises(ClaudeDesktopConfigError) as unreadable:
        install_claude_desktop_server(directory_target, SERVER)
    assert unreadable.value.code == "config_unreadable"

    path = tmp_path / "claude_desktop_config.json"
    original = _existing(path, {"mcpServers": {}})

    def fail_replace(*_: object) -> None:
        raise PermissionError("locked by another process")

    monkeypatch.setattr(host_config.os, "replace", fail_replace)
    with pytest.raises(ClaudeDesktopConfigError) as unwritable:
        install_claude_desktop_server(path, SERVER, clock=lambda: STAMP)
    assert unwritable.value.code == "config_write_failed"
    assert path.read_bytes() == original
    assert not (tmp_path / f".{path.name}.tmp").exists()


def _checkout(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='personal-shopping-agent'\n")
    (tmp_path / "uv.lock").write_text("version = 1\n")
    (tmp_path / "src" / "personal_shopping_agent").mkdir(parents=True)
    return tmp_path.resolve()


def _output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_cli_installs_the_live_entry_with_the_running_uv_and_hides_other_servers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = _checkout(tmp_path / "project")
    config = tmp_path / "Claude" / "claude_desktop_config.json"
    _existing(config, {"mcpServers": {"other": {"env": {"API_KEY": OTHER_SECRET}}}})
    arguments = [
        "install-claude-desktop",
        "--project-directory",
        str(checkout),
        "--config-path",
        str(config),
        "--live-jd",
    ]

    assert main([*arguments, "--dry-run"], environment={"UV": "C:/tools/uv.exe"}) == 0
    preview = capsys.readouterr().out
    assert json.loads(preview)["status"] == "preview"
    assert OTHER_SECRET not in preview

    assert main(arguments, environment={"UV": "C:/tools/uv.exe"}) == 0
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    assert OTHER_SECRET not in raw
    assert payload["status"] == "updated"
    assert payload["live_jd"] is True
    server = cast(dict[str, object], payload["server"])
    assert server["command"] == "C:/tools/uv.exe"
    assert cast(list[str], server["args"])[-1] == "personal-shopping-agent-mcp-jd"
    installed = json.loads(config.read_text(encoding="utf-8"))
    assert installed["mcpServers"][SERVER_NAME] == server
    assert installed["mcpServers"]["other"]["env"]["API_KEY"] == OTHER_SECRET


def test_cli_reports_invalid_checkouts_configs_and_platforms(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "claude_desktop_config.json"
    assert (
        main(
            [
                "install-claude-desktop",
                "--project-directory",
                str(tmp_path / "missing"),
                "--config-path",
                str(config),
            ],
            environment={},
        )
        == 2
    )
    assert _output(capsys)["error_code"] == "source_checkout_invalid"

    checkout = _checkout(tmp_path / "project")
    config.write_text("{broken", encoding="utf-8")
    arguments = ["install-claude-desktop", "--project-directory", str(checkout)]
    assert main([*arguments, "--config-path", str(config)], environment={}) == 2
    assert _output(capsys)["error_code"] == "config_invalid"

    if __import__("sys").platform not in {"win32", "darwin"}:
        assert main(arguments, environment={}) == 2
        assert _output(capsys)["error_code"] == "unsupported_platform"
