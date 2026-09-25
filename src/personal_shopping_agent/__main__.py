"""Run the package health check from the command line."""

import json
from dataclasses import asdict

from personal_shopping_agent.interfaces.health import health_check


def main() -> None:
    """Print the health status as stable JSON."""

    print(json.dumps(asdict(health_check()), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover - exercised by the process entry point
    main()
