"""B16 stdlib JCS encoder: byte-equal to the rfc8785 library, including floats."""

import json
import math

import pytest
import rfc8785
from hypothesis import given, settings
from hypothesis import strategies as st

from nexa.workloads.canonical_json import canonical_json, es6_number

_SAFE = 2**53 - 1
_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-_SAFE, max_value=_SAFE)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=12)
)
_values = st.recursive(
    _scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=6), children, max_size=4)
    ),
    max_leaves=20,
)


@settings(max_examples=2000, deadline=None)
@given(st.floats(allow_nan=False, allow_infinity=False))
def test_es6_number_matches_rfc8785(value: float) -> None:
    assert es6_number(value).encode() == rfc8785.dumps(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "0"),
        (-0.0, "0"),
        (1.0, "1"),
        (100.0, "100"),
        (1e21, "1e+21"),
        (1e20, "100000000000000000000"),
        (1e-6, "0.000001"),
        (1e-7, "1e-7"),
        (0.1, "0.1"),
        (123.456, "123.456"),
        (-2.5e-10, "-2.5e-10"),
        (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
    ],
)
def test_es6_number_known_vectors(value: float, expected: str) -> None:
    assert es6_number(value) == expected


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_numbers_are_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json({"loss": value})


@settings(max_examples=500, deadline=None)
@given(_values)
def test_canonical_json_matches_rfc8785(value: object) -> None:
    assert canonical_json(value) == rfc8785.dumps(value)


@settings(max_examples=500, deadline=None)
@given(
    st.recursive(
        st.none()
        | st.booleans()
        | st.integers(min_value=-_SAFE, max_value=_SAFE)
        | st.text(
            alphabet=st.characters(max_codepoint=0xFFFF, exclude_categories=("Cs",)), max_size=12
        ),
        lambda children: (
            st.lists(children, max_size=4)
            | st.dictionaries(
                st.text(
                    alphabet=st.characters(max_codepoint=0xFFFF, exclude_categories=("Cs",)),
                    max_size=6,
                ),
                children,
                max_size=4,
            )
        ),
        max_leaves=20,
    )
)
def test_float_free_output_matches_the_legacy_runner_encoding(value: object) -> None:
    legacy = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    assert canonical_json(value) == legacy


def test_unsafe_integers_and_foreign_types_are_rejected() -> None:
    with pytest.raises(ValueError, match="safe range"):
        canonical_json(2**53)
    with pytest.raises(ValueError, match="unsupported"):
        canonical_json({"x": b"raw"})
    with pytest.raises(ValueError, match="keys"):
        canonical_json({1: 2})
