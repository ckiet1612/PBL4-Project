"""Stdlib RFC 8785 (JCS) encoder usable inside workload images without the rfc8785 wheel.

Float-free values with BMP member names encode byte-identically to ``json.dumps(
sort_keys=True, separators=(",", ":"), ensure_ascii=False)``; finite floats use the ECMAScript
Number-to-String algorithm that RFC 8785 section 3.2.2.3 requires.
"""

from __future__ import annotations

import json
import math

# RFC 8785 inherits the I-JSON integer range; larger integers lose precision in ES6.
_MAX_SAFE_INTEGER = 2**53 - 1


def es6_number(value: float) -> str:
    """Serialize a finite float exactly like ECMAScript ``Number.prototype.toString``."""
    if not math.isfinite(value):
        raise ValueError("non-finite numbers are not valid JSON")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    # repr() yields the shortest round-tripping digits, which is what ES6 requires.
    digits, exponent = _shortest_digits(abs(value))
    k = len(digits)
    n = exponent + k
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    tail = n - 1
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    return f"{sign}{mantissa}e{'+' if tail > 0 else '-'}{abs(tail)}"


def _shortest_digits(value: float) -> tuple[str, int]:
    text = repr(value)
    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    whole, _, fraction = mantissa.partition(".")
    if fraction == "0":
        fraction = ""
    digits = (whole + fraction).lstrip("0")
    exponent -= len(fraction)
    stripped = digits.rstrip("0")
    exponent += len(digits) - len(stripped)
    return stripped, exponent


def _encode(value: object, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise ValueError("integer is outside the I-JSON safe range")
        out.append(str(value))
    elif isinstance(value, float):
        out.append(es6_number(value))
    elif isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("object keys must be strings")
        out.append("{")
        # UTF-16 code-unit order, which RFC 8785 mandates for member names.
        for index, key in enumerate(sorted(value, key=lambda item: item.encode("utf-16-be"))):
            if index:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _encode(value[key], out)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _encode(item, out)
        out.append("]")
    else:
        raise ValueError(f"unsupported JSON value type {type(value).__name__}")


def canonical_json(value: object) -> bytes:
    out: list[str] = []
    _encode(value, out)
    return "".join(out).encode("utf-8")


__all__ = ["canonical_json", "es6_number"]
