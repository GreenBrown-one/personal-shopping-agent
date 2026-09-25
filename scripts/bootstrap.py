"""Initialize and verify a source checkout through the supported public CLI.

uv run --locked python scripts/bootstrap.py                    # initialize only
uv run --locked python scripts/bootstrap.py --claude-desktop   # ...and add to Claude Desktop
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from personal_shopping_agent.interfaces.cli import main as cli_main

_BOOTSTRAP_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("health",),
    ("migrate",),
    ("doctor",),
)


def bootstrap_commands(argv: Sequence[str] | None = None) -> tuple[tuple[str, ...], ...]:
    """Translate bootstrap flags into the ordered public CLI commands to run."""

    parser = argparse.ArgumentParser(prog="bootstrap")
    parser.add_argument(
        "--claude-desktop",
        action="store_true",
        help="after initializing, add this project to Claude Desktop's MCP servers",
    )
    parser.add_argument(
        "--live-jd",
        action="store_true",
        help="with --claude-desktop, configure the explicit live-JD entry point",
    )
    options = parser.parse_args(list(argv) if argv is not None else None)
    if options.live_jd and not options.claude_desktop:
        parser.error("--live-jd requires --claude-desktop")
    if not options.claude_desktop:
        return _BOOTSTRAP_COMMANDS
    install = ("install-claude-desktop", *(("--live-jd",) if options.live_jd else ()))
    return (*_BOOTSTRAP_COMMANDS, install)


def bootstrap(commands: Sequence[Sequence[str]] = _BOOTSTRAP_COMMANDS) -> int:
    """Run supported setup checks in order and stop on the first safe failure."""

    for command in commands:
        exit_code = cli_main(tuple(command))
        if exit_code != 0:
            return exit_code
    print(
        json.dumps(
            {
                "command": "bootstrap",
                "ok": True,
                "service": "personal-shopping-agent",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(bootstrap(bootstrap_commands()))
