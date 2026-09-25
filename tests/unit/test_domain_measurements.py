"""Shared measurement catalog lookups used by intake, normalization, and scoring."""

import pytest

from personal_shopping_agent.domain import (
    MEASUREMENT_DEFINITIONS,
    is_declared_unit,
    measurement_for_criterion_key,
)


@pytest.mark.parametrize(
    ("key", "canonical_key"),
    [
        ("battery_capacity", "battery_capacity"),
        ("Battery Capacity", "battery_capacity"),
        ("memory_capacity", "memory_capacity"),
        ("内存", "memory_capacity"),
        ("屏幕尺寸", "display_size"),
    ],
)
def test_criterion_keys_resolve_by_canonical_key_or_declared_alias(
    key: str, canonical_key: str
) -> None:
    definition = measurement_for_criterion_key(key)

    assert definition is not None
    assert definition.canonical_key == canonical_key


def test_unknown_criterion_keys_are_not_guessed() -> None:
    assert measurement_for_criterion_key("camera quality") is None
    assert measurement_for_criterion_key("battery life") is None


def test_declared_units_are_case_and_space_insensitive_but_exact() -> None:
    battery = next(
        item for item in MEASUREMENT_DEFINITIONS if item.canonical_key == "battery_capacity"
    )

    assert is_declared_unit(battery, "mAh")
    assert is_declared_unit(battery, " MAH ")
    assert is_declared_unit(battery, "Ah")
    assert not is_declared_unit(battery, "Wh")
