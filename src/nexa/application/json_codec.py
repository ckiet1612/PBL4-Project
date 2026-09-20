import hashlib
import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any
from uuid import UUID

import rfc8785


class JsonRequestError(ValueError):
    """Raised for bounded strict-JSON or JCS-domain violations."""


def json_wire_value(value: Any) -> Any:
    """Convert application and persistence values to deterministic JSON wire types."""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JsonRequestError("JSON number must be finite")
        return value
    if isinstance(value, Decimal):
        binary64 = float(value)
        if not math.isfinite(binary64):
            raise JsonRequestError("JSON number must be finite binary64")
        return binary64
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise JsonRequestError("JSON timestamp must include a timezone")
        utc = value.astimezone(UTC)
        return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"
    if isinstance(value, Enum):
        return json_wire_value(value.value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise JsonRequestError("JSON object members must have string names")
        return {key: json_wire_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_wire_value(item) for item in value]
    raise JsonRequestError(f"Unsupported JSON response value: {type(value).__name__}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JsonRequestError(f"Duplicate JSON member: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise JsonRequestError(f"JSON number must be finite binary64: {value}")


def _normalize_number(value: Decimal) -> Decimal:
    try:
        binary64 = float(value)
    except (OverflowError, ValueError):
        raise JsonRequestError("JSON number must be finite binary64") from None
    if not math.isfinite(binary64):
        raise JsonRequestError("JSON number must be finite binary64")
    try:
        return Decimal(rfc8785.dumps(binary64).decode("ascii"))
    except (InvalidOperation, rfc8785.FloatDomainError):
        raise JsonRequestError("JSON number must be finite binary64") from None


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise JsonRequestError("JSON string contains invalid Unicode") from None
        return value
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        if abs(value) > 9_007_199_254_740_991:
            raise JsonRequestError("JSON integer is outside the RFC 8785 domain")
        return value
    if isinstance(value, Decimal):
        return _normalize_number(value)
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {_normalize(key): _normalize(item) for key, item in value.items()}
    return value


def decode_json_object(body: bytes, *, max_bytes: int) -> dict[str, Any]:
    if len(body) > max_bytes:
        raise JsonRequestError("JSON request body is too large")
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError:
        raise JsonRequestError("JSON request body must be UTF-8") from None
    try:
        value = json.loads(
            decoded,
            object_pairs_hook=_object_without_duplicates,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_constant,
        )
    except JsonRequestError:
        raise
    except json.JSONDecodeError:
        raise JsonRequestError("Malformed JSON request body") from None
    if not isinstance(value, dict):
        raise JsonRequestError("JSON request body must be an object")
    return _normalize(value)


def _jcs_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        binary64 = float(value)
        if not math.isfinite(binary64):
            raise JsonRequestError("JSON number must be finite binary64")
        return binary64
    if isinstance(value, list):
        return [_jcs_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _jcs_value(item) for key, item in value.items()}
    return value


def jcs_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return rfc8785.dumps(_jcs_value(dict(value)))
    except (rfc8785.FloatDomainError, rfc8785.IntegerDomainError, UnicodeError):
        raise JsonRequestError("JSON value is outside the RFC 8785 domain") from None


def jcs_request_hash(value: Mapping[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(jcs_bytes(value)).hexdigest()}"
