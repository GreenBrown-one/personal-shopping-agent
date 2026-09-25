"""Declared specification measurement catalog shared by intake, normalization, and scoring.

Only exact declared aliases and units are recognized; nothing is inferred from context.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

_MEASUREMENT_PATTERN = re.compile(
    r"^(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?P<unit>[^\d\s].*?)$"
)


@dataclass(frozen=True, slots=True)
class MeasurementDefinition:
    """One canonical field, its aliases, base unit, and exact conversion factors."""

    canonical_key: str
    aliases: frozenset[str]
    canonical_unit: str
    unit_factors: Mapping[str, Decimal]


def specification_key_token(value: str) -> str:
    """Fold case, underscores, and whitespace so equivalent keys compare equal."""

    return " ".join(value.casefold().replace("_", " ").split())


def _unit_token(value: str) -> str:
    return "".join(value.casefold().split())


MEASUREMENT_DEFINITIONS = (
    MeasurementDefinition(
        canonical_key="battery_capacity",
        aliases=frozenset({"battery", "battery capacity", "电池", "电池容量"}),
        canonical_unit="mAh",
        unit_factors={
            "mah": Decimal("1"),
            "毫安时": Decimal("1"),
            "ah": Decimal("1000"),
            "安时": Decimal("1000"),
        },
    ),
    MeasurementDefinition(
        canonical_key="weight",
        aliases=frozenset({"weight", "机身重量", "重量"}),
        canonical_unit="g",
        unit_factors={
            "g": Decimal("1"),
            "gram": Decimal("1"),
            "grams": Decimal("1"),
            "克": Decimal("1"),
            "kg": Decimal("1000"),
            "公斤": Decimal("1000"),
            "千克": Decimal("1000"),
        },
    ),
    MeasurementDefinition(
        canonical_key="display_size",
        aliases=frozenset({"display size", "screen size", "屏幕尺寸"}),
        canonical_unit="inch",
        unit_factors={
            '"': Decimal("1"),
            "in": Decimal("1"),
            "inch": Decimal("1"),
            "inches": Decimal("1"),
            "英寸": Decimal("1"),
        },
    ),
    MeasurementDefinition(
        canonical_key="storage_capacity",
        aliases=frozenset({"rom", "storage", "storage capacity", "存储", "存储容量"}),
        canonical_unit="GB",
        unit_factors={
            "gb": Decimal("1"),
            "吉字节": Decimal("1"),
            "mb": Decimal("0.0009765625"),
            "tb": Decimal("1024"),
        },
    ),
    MeasurementDefinition(
        canonical_key="memory_capacity",
        aliases=frozenset({"memory", "ram", "内存", "运行内存"}),
        canonical_unit="GB",
        unit_factors={
            "gb": Decimal("1"),
            "吉字节": Decimal("1"),
            "mb": Decimal("0.0009765625"),
            "tb": Decimal("1024"),
        },
    ),
)

_DEFINITIONS_BY_ALIAS = {
    specification_key_token(alias): definition
    for definition in MEASUREMENT_DEFINITIONS
    for alias in definition.aliases
}

_DEFINITIONS_BY_CANONICAL_KEY = {
    specification_key_token(definition.canonical_key): definition
    for definition in MEASUREMENT_DEFINITIONS
}


def measurement_definition(field_path: str) -> MeasurementDefinition | None:
    """Resolve only exact declared aliases from one specification evidence path."""

    prefix = "specifications."
    if not field_path.startswith(prefix):
        return None
    return _DEFINITIONS_BY_ALIAS.get(specification_key_token(field_path.removeprefix(prefix)))


def measurement_for_criterion_key(key: str) -> MeasurementDefinition | None:
    """Resolve a criterion key by its exact canonical key or a declared alias."""

    token = specification_key_token(key)
    return _DEFINITIONS_BY_CANONICAL_KEY.get(token) or _DEFINITIONS_BY_ALIAS.get(token)


def is_declared_unit(definition: MeasurementDefinition, unit: str) -> bool:
    """Return whether a user-supplied unit is a declared unit for the definition."""

    return _unit_token(unit) in definition.unit_factors


def normalize_measurement(raw_value: str, definition: MeasurementDefinition) -> Decimal | None:
    """Convert one complete number/unit expression without guessing omitted units."""

    match = _MEASUREMENT_PATTERN.fullmatch(raw_value.strip())
    if match is None:
        return None
    factor = definition.unit_factors.get(_unit_token(match.group("unit")))
    if factor is None:
        return None
    value = Decimal(match.group("number").replace(",", ""))
    return (value * factor).normalize()
