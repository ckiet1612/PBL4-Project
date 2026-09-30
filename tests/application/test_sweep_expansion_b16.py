"""B16 hyperparameter-sweep expansion: order, RFC 8785 uniqueness, bounds and child keys."""

import json
import math
import pathlib
import time
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nexa.application import sweep_expansion as sweep
from nexa.application.json_codec import decode_json_object, jcs_request_hash

FIXTURE = pathlib.Path(__file__).parents[1] / "fixtures/workloads/hyperparameter-sweep-v1"
BASE = {
    "epochs": 2,
    "batch_size": 32,
    "learning_rate": Decimal("0.01"),
    "seed": 7,
    "subset_size": 500,
}


def test_expansion_order_is_dimension_order_then_value_order():
    children = sweep.expand(
        BASE,
        [("learning_rate", [Decimal("0.1"), Decimal("0.01")]), ("seed", [1, 2, 3])],
    )
    assert [(c["learning_rate"], c["seed"]) for c in children] == [
        (Decimal("0.1"), 1),
        (Decimal("0.1"), 2),
        (Decimal("0.1"), 3),
        (Decimal("0.01"), 1),
        (Decimal("0.01"), 2),
        (Decimal("0.01"), 3),
    ]
    # Base parameters are kept and only the swept names are replaced.
    assert all(c["epochs"] == 2 and c["subset_size"] == 500 for c in children)
    assert BASE["seed"] == 7


def _decoded(values: bytes):
    return decode_json_object(b'{"v":' + values + b"}", max_bytes=100)["v"]


@pytest.mark.parametrize(
    "values",
    [
        [1, Decimal("1.0")],
        [1, 2, Decimal("1")],
        [Decimal("2.50"), Decimal("2.5")],
        [True, True],
        ["1", "1"],
        # The request decoder already normalizes these spellings to one JCS number.
        _decoded(b"[1.0,1,1e0]"),
        _decoded(b"[0.10,0.1]"),
        _decoded(b"[0.1,0.01,1e-2,0.001]"),
    ],
)
def test_equal_json_values_in_one_dimension_reject_the_request(values):
    with pytest.raises(sweep.SweepExpansionError, match="values must be unique"):
        sweep.expand(BASE, [("learning_rate", [Decimal("0.1")]), ("seed", values)])


def test_values_equal_only_in_python_are_distinct_and_canonical_values_is_a_no_op():
    # 1 == True in Python, but RFC 8785 keeps a number, a boolean and a string apart.
    values = [1, True, "1", Decimal("2.5"), 2]
    assert sweep.canonical_values(values) == values
    assert [child["seed"] for child in sweep.expand(BASE, [("seed", values)])] == values


def test_product_is_counted_after_dedup_without_materializing():
    assert sweep.child_count([("seed", [1, Decimal("1.0"), 2]), ("epochs", [1, 2])]) == 4
    assert sweep.child_count([("seed", list(range(100)))]) == 100
    assert sweep.child_count([("seed", list(range(101)))]) is None
    assert sweep.child_count([("seed", list(range(11))), ("epochs", list(range(10)))]) is None
    started = time.perf_counter()
    huge = [(f"d{index}", list(range(100))) for index in range(16)]
    assert sweep.child_count(huge) is None
    with pytest.raises(sweep.SweepExpansionError, match="1..100 children"):
        sweep.expand(BASE, huge)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    ("dimensions", "message"),
    [
        ([], "1..16 dimensions"),
        ([(f"d{i}", [1]) for i in range(17)], "1..16 dimensions"),
        ([("seed", [])], "1..100 values"),
        ([("seed", list(range(101)))], "1..100 values"),
        ([("seed", [1]), ("seed", [2])], "names must be unique"),
        ([("seed", [1]), ("seed", [1])], "names must be unique"),
    ],
)
def test_request_level_bounds_raise(dimensions, message):
    with pytest.raises(sweep.SweepExpansionError, match=message):
        sweep.expand(BASE, dimensions)


def test_parameter_hash_is_rfc8785_of_the_complete_child_parameters():
    (child,) = sweep.expand(BASE, [("learning_rate", [Decimal("1.0E-2")])])
    expected = jcs_request_hash({**BASE, "learning_rate": 0.01})
    assert sweep.parameter_hash(child) == expected
    # Member order and number spelling do not change the hash.
    reordered = dict(reversed(list(child.items())))
    assert sweep.parameter_hash(reordered) == expected


def test_child_key_is_deterministic_bounded_and_scoped_to_sweep_index_and_hash():
    sweep_id = UUID("01923456-789a-7bcd-8ef0-123456789abc")
    other = UUID("01923456-789a-7bcd-8ef0-123456789abd")
    digest = "sha256:" + "a" * 64
    key = sweep.child_idempotency_key(sweep_id, 3, digest)
    assert key == sweep.child_idempotency_key(sweep_id, 3, digest)
    assert len(key) <= 128 and all("!" <= ch <= "~" for ch in key) and len(key) >= 16
    assert key != sweep.child_idempotency_key(other, 3, digest)
    assert key != sweep.child_idempotency_key(sweep_id, 4, digest)
    assert key != sweep.child_idempotency_key(sweep_id, 3, "sha256:" + "b" * 64)


_UNIQUE_DIMENSIONS = st.lists(
    st.lists(st.integers(min_value=0, max_value=5), min_size=1, max_size=6, unique=True),
    min_size=1,
    max_size=4,
)


@given(_UNIQUE_DIMENSIONS)
def test_expansion_is_the_ordered_cartesian_product_of_unique_values(raw):
    dimensions = [(f"p{index}", values) for index, values in enumerate(raw)]
    count = sweep.child_count(dimensions)
    product = math.prod(len(values) for values in raw)
    if product > 100:
        assert count is None
        return
    assert count == product
    children = sweep.expand({}, dimensions)
    assert len(children) == product
    hashes = [sweep.parameter_hash(child) for child in children]
    assert len(set(hashes)) == product
    # The last dimension varies fastest.
    assert [child[f"p{len(raw) - 1}"] for child in children[: len(raw[-1])]] == raw[-1]


@given(_UNIQUE_DIMENSIONS, st.data())
def test_one_repeated_value_in_any_dimension_rejects_the_whole_request(raw, data):
    index = data.draw(st.integers(min_value=0, max_value=len(raw) - 1))
    repeated = data.draw(st.sampled_from(raw[index]))
    spelling = data.draw(st.sampled_from([repeated, Decimal(repeated), Decimal(f"{repeated}.0")]))
    position = data.draw(st.integers(min_value=0, max_value=len(raw[index])))
    raw[index] = raw[index][:position] + [spelling] + raw[index][position:]
    dimensions = [(f"p{number}", values) for number, values in enumerate(raw)]
    # Rejected before the product bound, whatever the product is.
    with pytest.raises(sweep.SweepExpansionError, match="values must be unique"):
        sweep.expand({}, dimensions)


def test_golden_expansion_fixture():
    request = decode_json_object((FIXTURE / "request.json").read_bytes(), max_bytes=1 << 20)
    expected = json.loads((FIXTURE / "expansion.json").read_text())
    dimensions = [(item["name"], item["values"]) for item in request["dimensions"]]
    children = sweep.expand(request["base_spec"]["parameters"], dimensions)
    sweep_id = UUID(expected["sweep_id"])
    assert [
        {
            "child_index": index,
            "parameters": sweep.wire_parameters(child),
            "parameter_hash": sweep.parameter_hash(child),
            "idempotency_key": sweep.child_idempotency_key(
                sweep_id, index, sweep.parameter_hash(child)
            ),
        }
        for index, child in enumerate(children)
    ] == expected["children"]
