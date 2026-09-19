from decimal import MAX_EMAX, MIN_ETINY, Decimal

import pytest

from nexa.domain.scheduling import AllocationState
from nexa.infrastructure.persistence.values import (
    PersistenceValueError,
    allocation_state_from_db,
    allocation_state_to_db,
    decimal_from_db,
    decimal_to_db,
)


@pytest.mark.parametrize(
    ("state", "stored"),
    [
        (AllocationState.HELD, "HELD"),
        (AllocationState.QUARANTINED, "QUARANTINED"),
    ],
)
def test_allocation_state_mapping_is_explicit_and_reversible(
    state: AllocationState, stored: str
) -> None:
    assert allocation_state_to_db(state) == stored
    assert allocation_state_from_db(stored) is state


def test_allocation_state_mapping_rejects_unknown_database_value() -> None:
    with pytest.raises(PersistenceValueError, match="allocation state"):
        allocation_state_from_db("RELEASED")


@pytest.mark.parametrize(
    "value",
    [
        Decimal("0"),
        Decimal("0.33333333333333333333333333333333333333333333333333"),
        Decimal("1E+1000"),
        Decimal("1E-20000"),
        Decimal("1.2300"),
    ],
)
def test_decimal_to_db_preserves_finite_non_negative_values(value: Decimal) -> None:
    stored = decimal_to_db(value)
    converted = decimal_from_db(stored)

    assert converted.as_tuple() == value.as_tuple()


@pytest.mark.parametrize("value", ["", "1", "0:01:0", "0:1:+2", "0:1:-0", "x:1:0"])
def test_decimal_from_db_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(PersistenceValueError, match="canonical exact Decimal"):
        decimal_from_db(value)


def test_decimal_from_db_rejects_exponent_outside_python_decimal_domain() -> None:
    with pytest.raises(PersistenceValueError, match="Decimal exponent"):
        decimal_from_db("0:1:1000000000000000000")


@pytest.mark.parametrize(
    ("stored", "expected_tuple"),
    [
        (f"0:1:{MAX_EMAX}", (0, (1,), MAX_EMAX)),
        (f"0:12:{MAX_EMAX - 1}", (0, (1, 2), MAX_EMAX - 1)),
        (f"0:10:{MAX_EMAX - 1}", (0, (1, 0), MAX_EMAX - 1)),
        (f"0:0:{MAX_EMAX}", (0, (0,), MAX_EMAX)),
        (f"0:1:{MIN_ETINY}", (0, (1,), MIN_ETINY)),
    ],
)
def test_decimal_from_db_accepts_python_decimal_domain_boundaries(
    stored: str,
    expected_tuple: tuple[int, tuple[int, ...], int],
) -> None:
    assert decimal_from_db(stored).as_tuple() == expected_tuple


@pytest.mark.parametrize(
    "stored",
    [
        f"0:12:{MAX_EMAX}",
        f"0:10:{MAX_EMAX}",
    ],
)
def test_decimal_from_db_rejects_adjusted_exponent_above_python_domain(
    stored: str,
) -> None:
    with pytest.raises(PersistenceValueError, match="decodable exact Decimal"):
        decimal_from_db(stored)


@pytest.mark.parametrize(
    "value",
    [Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")],
)
def test_decimal_to_db_rejects_values_outside_scheduler_domain(value: Decimal) -> None:
    with pytest.raises(PersistenceValueError, match="finite and non-negative"):
        decimal_to_db(value)


def test_decimal_to_db_rejects_non_decimal_input() -> None:
    with pytest.raises(PersistenceValueError, match="Decimal"):
        decimal_to_db(0.5)  # type: ignore[arg-type]
