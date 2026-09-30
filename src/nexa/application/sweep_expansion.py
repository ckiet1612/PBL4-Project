"""Pure hyperparameter-sweep expansion (workloads-checkpoints.md, `hyperparameter-sweep` v1).

Children follow dimension request order, then value request order, so the last
dimension varies fastest (`itertools.product`). Values inside one dimension must be
unique by their RFC 8785 form (`uniqueItems`): `1` and `1.0` are one value and
reject the request, `1`, `true` and `"1"` are three (B16-R10). The canonical
de-duplication is kept as a defensive no-op on an accepted request.
"""

import hashlib
import itertools
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from nexa.application.json_codec import jcs_bytes, jcs_request_hash, json_wire_value

MAX_DIMENSIONS = 16
MAX_VALUES = 100
MAX_CHILDREN = 100

Dimension = tuple[str, Sequence[Any]]


class SweepExpansionError(ValueError):
    """A request-level sweep shape violation; nothing may be persisted."""


def canonical_values(values: Sequence[Any]) -> list[Any]:
    seen: set[bytes] = set()
    unique = []
    for value in values:
        key = jcs_bytes({"v": value})
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def child_count(dimensions: Sequence[Dimension]) -> int | None:
    """Product of the de-duplicated value counts, or None once it exceeds MAX_CHILDREN."""
    count = 1
    for _name, values in dimensions:
        count *= len(canonical_values(values))
        if count > MAX_CHILDREN:
            return None
    return count


def _check_shape(dimensions: Sequence[Dimension]) -> None:
    if not 1 <= len(dimensions) <= MAX_DIMENSIONS:
        raise SweepExpansionError(f"A sweep needs 1..{MAX_DIMENSIONS} dimensions")
    if any(not 1 <= len(values) <= MAX_VALUES for _name, values in dimensions):
        raise SweepExpansionError(f"Each sweep dimension needs 1..{MAX_VALUES} values")
    names = [name for name, _values in dimensions]
    if len(set(names)) != len(names):
        raise SweepExpansionError("Sweep dimension names must be unique")
    if any(len(canonical_values(values)) != len(values) for _name, values in dimensions):
        raise SweepExpansionError("Sweep dimension values must be unique (RFC 8785)")


def expand(base_parameters: Mapping[str, Any], dimensions: Sequence[Dimension]) -> list[dict]:
    """Complete child parameter objects in child_index order."""
    _check_shape(dimensions)
    if child_count(dimensions) is None:
        raise SweepExpansionError(f"A sweep must expand to 1..{MAX_CHILDREN} children")
    names = [name for name, _values in dimensions]
    unique = [canonical_values(values) for _name, values in dimensions]
    return [
        {**base_parameters, **dict(zip(names, combination, strict=True))}
        for combination in itertools.product(*unique)
    ]


def parameter_hash(parameters: Mapping[str, Any]) -> str:
    return jcs_request_hash(parameters)


def wire_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    return json_wire_value(dict(parameters))


def child_idempotency_key(sweep_id: UUID, child_index: int, digest: str) -> str:
    """Internal submitJob key of one child; normal tenant/principal/operation scope applies."""
    material = jcs_bytes(
        {"sweep_id": str(sweep_id), "child_index": child_index, "parameter_hash": digest}
    )
    return f"sweep-child-{hashlib.sha256(material).hexdigest()}"
