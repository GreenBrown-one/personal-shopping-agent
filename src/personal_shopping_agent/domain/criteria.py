"""Numeric criterion rules shared by requirement review and deterministic scoring."""

from decimal import Decimal
from typing import cast

from personal_shopping_agent.domain.models import ShoppingCriterion


class InvalidCriterionDefinitionError(ValueError):
    """Raised when criterion bounds cannot be interpreted without guessing direction."""

    def __init__(self, key: str, code: str, message: str) -> None:
        super().__init__(message)
        self.key = key
        self.code = code


def numeric_criterion_bounds(
    criterion: ShoppingCriterion,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Return numeric bounds or raise when direction or order would have to be guessed."""

    raw_values = (criterion.minimum, criterion.preferred, criterion.maximum)
    if any(value is not None and not isinstance(value, Decimal) for value in raw_values):
        raise InvalidCriterionDefinitionError(
            criterion.key,
            "non_numeric_bound",
            "Normalized numeric specifications require numeric criterion bounds.",
        )
    minimum, preferred, maximum = cast(
        tuple[Decimal | None, Decimal | None, Decimal | None], raw_values
    )
    if any(value is not None and value < 0 for value in (minimum, preferred, maximum)):
        raise InvalidCriterionDefinitionError(
            criterion.key,
            "negative_bound",
            "Normalized measurement criterion bounds cannot be negative.",
        )
    if minimum is not None and maximum is not None and minimum > maximum:
        raise InvalidCriterionDefinitionError(
            criterion.key,
            "bounds_reversed",
            "Criterion minimum cannot exceed maximum.",
        )
    if preferred is not None and (
        (minimum is not None and preferred < minimum)
        or (maximum is not None and preferred > maximum)
    ):
        raise InvalidCriterionDefinitionError(
            criterion.key,
            "preferred_outside_bounds",
            "Criterion preferred value must remain inside declared bounds.",
        )
    if minimum is None and maximum is None:
        code = "preferred_direction_undefined" if preferred is not None else "bounds_missing"
        raise InvalidCriterionDefinitionError(
            criterion.key,
            code,
            "A numeric criterion requires a minimum or maximum to define direction.",
        )
    return minimum, preferred, maximum
