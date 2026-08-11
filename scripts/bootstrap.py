"""Initialize and verify a source checkout through the supported public CLI."""

from __future__ import annotations

import json
from collections.abc import Sequence

from personal_shopping_agent.cli import main as cli_main

_BOOTSTRAP_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("health",),
    ("migrate",),
    ("doctor",),
)


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
    raise SystemExit(bootstrap())
