from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from nexa.application.json_codec import (
    JsonRequestError,
    decode_json_object,
    jcs_request_hash,
    json_wire_value,
)


def test_duplicate_json_member_is_rejected_before_model_validation() -> None:
    with pytest.raises(JsonRequestError, match="Duplicate JSON member"):
        decode_json_object(b'{"name":"first","name":"second"}', max_bytes=1024)


def test_body_limit_applies_without_content_length() -> None:
    with pytest.raises(JsonRequestError, match="too large"):
        decode_json_object(b'{"value":"1234567890"}', max_bytes=8)


def test_jcs_normalizes_numbers_to_same_behavior_used_by_application() -> None:
    first = decode_json_object(b'{"weight":0.10000000000000001}', max_bytes=1024)
    second = decode_json_object(b'{"weight":0.1}', max_bytes=1024)
    distinct = decode_json_object(b'{"weight":0.10000000000000002}', max_bytes=1024)

    assert first["weight"] == Decimal("0.1")
    assert second["weight"] == Decimal("0.1")
    assert distinct["weight"] == Decimal("0.10000000000000002")
    assert jcs_request_hash(first) == jcs_request_hash(second)
    assert jcs_request_hash(first) != jcs_request_hash(distinct)


def test_non_object_and_non_finite_or_out_of_binary64_number_are_rejected() -> None:
    with pytest.raises(JsonRequestError, match="object"):
        decode_json_object(b"[]", max_bytes=1024)
    with pytest.raises(JsonRequestError, match="finite binary64"):
        decode_json_object(b'{"weight":1e9999}', max_bytes=1024)


def test_decoder_rejects_integers_and_unicode_outside_the_jcs_domain() -> None:
    with pytest.raises(JsonRequestError, match="RFC 8785"):
        decode_json_object(b'{"limit":9007199254740992}', max_bytes=1024)
    with pytest.raises(JsonRequestError, match="Unicode"):
        decode_json_object(b'{"value":"\\ud800"}', max_bytes=1024)
    with pytest.raises(JsonRequestError, match="Unicode"):
        decode_json_object(b'{"\\ud800":"value"}', max_bytes=1024)


def test_json_wire_value_normalizes_persistence_types_for_exact_response_replay() -> None:
    value = {
        "id": UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f1"),
        "created_at": datetime(2026, 9, 20, 3, 4, 5, 6000, tzinfo=UTC),
        "weight": Decimal("0.10000000000000002"),
        "nested": [UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f2")],
    }

    assert json_wire_value(value) == {
        "id": "018f05c4-a922-7d0d-9f55-f9084a72d0f1",
        "created_at": "2026-09-20T03:04:05.006Z",
        "weight": 0.10000000000000002,
        "nested": ["018f05c4-a922-7d0d-9f55-f9084a72d0f2"],
    }
