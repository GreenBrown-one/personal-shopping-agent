"""Static enforcement of the five-layer dependency rules in DESIGN.md section 3."""

import ast
from pathlib import Path

import pytest

PACKAGE = "personal_shopping_agent"
PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / PACKAGE

# Every module belongs to exactly one area, resolved by longest dotted prefix.
AREAS = {
    "": "root",
    "__main__": "root",
    "__about__": "about",
    "domain": "domain",
    "intake": "intake",
    "sourcing": "sourcing",
    "sourcing.browser": "sourcing.browser",
    "sourcing.platforms": "sourcing.platforms",
    "presentation": "presentation",
    "presentation.llm": "presentation.llm",
    "presentation.rendering": "presentation.rendering",
    "automation": "automation",
    "evolution": "evolution",
    "infrastructure": "infrastructure",
    "infrastructure.local_security": "infrastructure.local_security",
    "interfaces": "interfaces",
    "interfaces.health": "interfaces.health",
}

# Project areas each area may import (itself is always allowed).
ALLOWED_AREAS = {
    "root": {"about", "interfaces.health"},
    "about": set[str](),
    "domain": set[str](),
    "intake": {"domain"},
    "evolution": {"domain", "about"},
    "sourcing": {"domain"},
    "sourcing.browser": {"domain", "sourcing", "infrastructure.local_security"},
    "sourcing.platforms": {"domain", "sourcing"},
    "presentation": {"domain", "sourcing"},
    "presentation.llm": {"domain", "presentation"},
    "presentation.rendering": {"domain", "presentation"},
    "automation": {"domain", "intake", "sourcing", "presentation", "about"},
    "infrastructure": {
        "domain",
        "sourcing",
        "presentation",
        "automation",
        "infrastructure.local_security",
    },
    "infrastructure.local_security": set[str](),
    "interfaces": set(AREAS.values()),
    "interfaces.health": {"about"},
}

TECHNICAL_SDKS = {"alembic", "jinja2", "mcp", "openai", "playwright", "sqlalchemy"}
ALLOWED_SDKS = {
    "sourcing.browser": {"playwright"},
    "presentation.llm": {"openai"},
    "presentation.rendering": {"jinja2"},
    "infrastructure": {"alembic", "sqlalchemy"},
    "interfaces": TECHNICAL_SDKS,
}


def _area(module: str) -> str:
    if module == PACKAGE:
        return AREAS[""]
    if not module.startswith(f"{PACKAGE}."):
        raise AssertionError(f"{module} is not assigned to an architecture area")
    parts = module.removeprefix(f"{PACKAGE}.").split(".")
    for length in range(len(parts), 0, -1):
        prefix = ".".join(parts[:length])
        if prefix in AREAS:
            return AREAS[prefix]
    raise AssertionError(f"{module} is not assigned to an architecture area")


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join([PACKAGE, *parts])


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{path} uses a relative import"
            assert node.module is not None
            found.append(node.module)
    return found


MODULES = sorted(PACKAGE_ROOT.rglob("*.py"))


def test_every_module_is_assigned_to_an_area() -> None:
    assert MODULES
    for path in MODULES:
        _area(_module_name(path))


@pytest.mark.parametrize("path", MODULES, ids=lambda path: _module_name(path))
def test_module_respects_layer_dependency_direction(path: Path) -> None:
    module = _module_name(path)
    area = _area(module)
    for imported in _imports(path):
        top_level = imported.split(".")[0]
        if top_level == PACKAGE:
            target = _area(imported)
            assert target == area or target in ALLOWED_AREAS[area], (
                f"{module} ({area}) must not import {imported} ({target})"
            )
        elif top_level in TECHNICAL_SDKS:
            assert top_level in ALLOWED_SDKS.get(area, set()), (
                f"{module} ({area}) must not import the technical SDK {imported}"
            )


def test_rule_table_covers_every_area() -> None:
    assert set(ALLOWED_AREAS) == set(AREAS.values())
    assert _area(f"{PACKAGE}.sourcing.browser.manager") == "sourcing.browser"
    assert _area(f"{PACKAGE}.sourcing.collection") == "sourcing"
    for unknown in ("other_package.module", f"{PACKAGE}.unclassified.module"):
        with pytest.raises(AssertionError, match="not assigned"):
            _area(unknown)
