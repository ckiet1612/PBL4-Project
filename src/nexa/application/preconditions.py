import re
from dataclasses import dataclass

from nexa.application.errors import ApplicationError

_ETAG_PATTERN = re.compile(r'^"v([1-9][0-9]*)"$')


@dataclass(frozen=True, slots=True)
class VersionPrecondition:
    raw_value: str | None


type ExpectedVersion = int | VersionPrecondition


def resolve_expected_version(value: ExpectedVersion) -> int:
    if isinstance(value, int):
        if value < 1:
            raise ValueError("Expected version must be positive")
        return value
    if value.raw_value is None:
        raise ApplicationError(
            code="precondition_required",
            status=428,
            message="If-Match is required",
        )
    match = _ETAG_PATTERN.fullmatch(value.raw_value)
    if match is None:
        raise ApplicationError(
            code="validation_failed",
            status=400,
            message="If-Match must be a strong version ETag",
        )
    return int(match.group(1))
