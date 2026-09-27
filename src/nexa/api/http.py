from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from nexa.application.json_codec import json_wire_value
from nexa.application.preconditions import VersionPrecondition, resolve_expected_version


def strong_etag(version: int) -> str:
    if version < 1:
        raise ValueError("ETag version must be positive")
    return f'"v{version}"'


def parse_if_match(value: str | None) -> int:
    return resolve_expected_version(VersionPrecondition(value))


def format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware")
    utc = value.astimezone(UTC)
    milliseconds = utc.microsecond // 1000
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


def wire_response(
    model: type[BaseModel],
    result: Any,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Validate and filter like ``response_model``, keeping three-digit timestamps.

    FastAPI's own serialisation writes six fractional digits; the contract fixes
    exactly three (B15-R19/R29).
    """
    body = model.model_validate(result).model_dump(by_alias=True)
    return JSONResponse(json_wire_value(body), status_code=status_code, headers=headers)
