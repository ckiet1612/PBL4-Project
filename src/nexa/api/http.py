from datetime import UTC, datetime

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
