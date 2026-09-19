import re
from decimal import MAX_EMAX, MIN_ETINY, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from nexa.domain.scheduling import AllocationState


class PersistenceValueError(ValueError):
    """Raised when a domain value cannot be represented by the schema."""


_ALLOCATION_STATE_TO_DB = {
    AllocationState.HELD: "HELD",
    AllocationState.QUARANTINED: "QUARANTINED",
}
_ALLOCATION_STATE_FROM_DB = {value: key for key, value in _ALLOCATION_STATE_TO_DB.items()}
_EXACT_DECIMAL_PATTERN = re.compile(r"^[01]:(?:0|[1-9][0-9]*):(?:0|-?[1-9][0-9]*)$")


def _parse_decimal_exponent(exponent_text: str) -> int:
    unsigned = exponent_text.removeprefix("-")
    if len(unsigned) > len(str(abs(MIN_ETINY))):
        raise PersistenceValueError("Decimal exponent is outside Python's supported domain")
    try:
        exponent = int(exponent_text)
    except ValueError as exc:
        raise PersistenceValueError(
            "Decimal exponent is outside Python's supported domain"
        ) from exc
    if not MIN_ETINY <= exponent <= MAX_EMAX:
        raise PersistenceValueError("Decimal exponent is outside Python's supported domain")
    return exponent


def allocation_state_to_db(state: AllocationState) -> str:
    try:
        return _ALLOCATION_STATE_TO_DB[state]
    except (KeyError, TypeError) as exc:
        raise PersistenceValueError(f"Unsupported allocation state: {state!r}") from exc


def allocation_state_from_db(value: str) -> AllocationState:
    try:
        return _ALLOCATION_STATE_FROM_DB[value]
    except (KeyError, TypeError) as exc:
        raise PersistenceValueError(f"Unknown database allocation state: {value!r}") from exc


def decimal_to_db(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        raise PersistenceValueError("Scheduler persistence values must be Decimal instances")
    if not value.is_finite() or value < 0:
        raise PersistenceValueError("Scheduler persistence values must be finite and non-negative")
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise PersistenceValueError("Decimal exponent is outside Python's supported domain")
    _parse_decimal_exponent(str(exponent))
    return f"{sign}:{''.join(str(digit) for digit in digits)}:{exponent}"


def decimal_from_db(value: str) -> Decimal:
    if not isinstance(value, str) or _EXACT_DECIMAL_PATTERN.fullmatch(value) is None:
        raise PersistenceValueError("Database value is not a canonical exact Decimal")
    sign_text, digits_text, exponent_text = value.split(":")
    exponent = _parse_decimal_exponent(exponent_text)
    try:
        decoded = Decimal(
            (
                int(sign_text),
                tuple(int(digit) for digit in digits_text),
                exponent,
            )
        )
    except (InvalidOperation, ValueError) as exc:
        raise PersistenceValueError("Database value is not a decodable exact Decimal") from exc
    if not decoded.is_finite() or decoded < 0:
        raise PersistenceValueError("Scheduler persistence values must be finite and non-negative")
    return decoded


class ExactDecimalText(TypeDecorator[Decimal]):
    """Persist a finite non-negative Decimal without PostgreSQL NUMERIC range loss."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        del dialect
        if value is None:
            return None
        return decimal_to_db(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        del dialect
        if value is None:
            return None
        return decimal_from_db(value)
