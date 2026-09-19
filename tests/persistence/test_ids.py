import time
import uuid

from nexa.infrastructure.persistence.ids import new_uuid7


def test_new_uuid7_has_rfc9562_layout_and_server_time_prefix() -> None:
    before_ms = time.time_ns() // 1_000_000

    value = new_uuid7()

    after_ms = time.time_ns() // 1_000_000
    encoded_ms = value.int >> 80
    assert value.version == 7
    assert value.variant == uuid.RFC_4122
    assert before_ms <= encoded_ms <= after_ms


def test_new_uuid7_is_unique_and_monotonic_in_generation_order() -> None:
    values = [new_uuid7() for _ in range(1_024)]

    assert len(set(values)) == len(values)
    assert values == sorted(values)
