from datetime import UTC, datetime

import pytest

from nexa.api.http import format_timestamp, parse_if_match, strong_etag
from nexa.application.errors import ApplicationError


def test_strong_etag_round_trips_positive_version() -> None:
    assert strong_etag(12) == '"v12"'
    assert parse_if_match('"v12"') == 12


def test_missing_and_malformed_if_match_are_distinct_safe_errors() -> None:
    with pytest.raises(ApplicationError) as missing:
        parse_if_match(None)
    assert missing.value.status == 428
    assert missing.value.code == "precondition_required"

    with pytest.raises(ApplicationError) as malformed:
        parse_if_match("v12")
    assert malformed.value.status == 400
    assert malformed.value.code == "validation_failed"


def test_timestamp_is_utc_with_exact_millisecond_precision() -> None:
    value = datetime(2026, 9, 20, 10, 11, 12, 345678, tzinfo=UTC)

    assert format_timestamp(value) == "2026-09-20T10:11:12.345Z"
