"""Shared JSON serialization guarantees for public validated contracts."""

from decimal import Decimal
from typing import cast

from pydantic import BaseModel, field_serializer


def _plain_decimal_value(value: object) -> object:
    """Recursively encode Decimal values without exponent notation."""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, BaseModel):
        return _plain_decimal_value(value.model_dump(mode="python", by_alias=True))
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        return {key: _plain_decimal_value(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        sequence = cast("list[object] | tuple[object, ...]", value)
        return [_plain_decimal_value(item) for item in sequence]
    return value


class JsonContractModel(BaseModel):
    """Base model whose JSON output always satisfies Pydantic's Decimal schema."""

    @field_serializer("*", when_used="json", check_fields=False)
    def serialize_json_field(self, value: object) -> object:
        """Keep exact Decimal strings inside arbitrarily nested public contracts."""

        return _plain_decimal_value(value)
