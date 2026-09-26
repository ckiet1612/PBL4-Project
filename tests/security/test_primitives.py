from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from nexa.infrastructure.security import (
    CsrfCodec,
    CursorCodec,
    CursorError,
    PasswordHasher,
    SecretCodec,
    SecurityConfigurationError,
    read_header_secret_file,
    read_secret_file,
)

SESSION_ID = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f1")
OTHER_SESSION_ID = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f2")


def test_secret_file_requires_at_least_32_random_bytes(tmp_path) -> None:
    secret_file = tmp_path / "server-secret"
    secret_file.write_bytes(b"short-secret\n")

    with pytest.raises(SecurityConfigurationError, match="at least 32 bytes") as exc_info:
        read_secret_file(secret_file)

    assert str(secret_file) not in str(exc_info.value)


def test_secret_file_strips_only_trailing_line_ending(tmp_path) -> None:
    secret_file = tmp_path / "server-secret"
    expected = b"a stable server secret with spaces  "
    secret_file.write_bytes(expected + b"\r\n")

    assert read_secret_file(secret_file) == expected


def test_bootstrap_secret_file_must_round_trip_through_an_http_header(tmp_path) -> None:
    secret_file = tmp_path / "bootstrap-secret"
    secret_file.write_bytes(b"a" * 31 + b"\x80")

    with pytest.raises(SecurityConfigurationError, match="visible ASCII"):
        read_header_secret_file(secret_file)

    expected = b"base64url-bootstrap-secret-value-1234567890"
    secret_file.write_bytes(expected + b"\n")
    assert read_header_secret_file(secret_file) == expected


def test_password_hasher_uses_argon2id_and_rejects_out_of_contract_length() -> None:
    hasher = PasswordHasher(memory_kib=19_456, time_cost=2, parallelism=1, max_concurrency=1)

    encoded = hasher.hash("correct-horse-battery-staple")

    assert encoded.startswith("$argon2id$")
    assert hasher.verify(encoded, "correct-horse-battery-staple") is True
    assert hasher.verify(encoded, "wrong-password-value") is False
    with pytest.raises(ValueError, match="12 and 1024"):
        hasher.hash("too-short")


def test_opaque_secret_contains_indexable_id_but_hashes_only_random_material() -> None:
    codec = SecretCodec()

    encoded = codec.issue(SESSION_ID)
    parsed = codec.parse(encoded)

    assert parsed.identifier == SESSION_ID
    assert len(parsed.secret) == 32
    assert codec.digest(encoded) == codec.digest(encoded)
    assert codec.digest(codec.issue(SESSION_ID)) != codec.digest(encoded)
    assert "secret=" not in repr(parsed)


def test_csrf_token_is_bound_to_the_exact_session_cookie() -> None:
    secrets = SecretCodec()
    session_cookie = secrets.issue(SESSION_ID)
    other_cookie = secrets.issue(OTHER_SESSION_ID)
    codec = CsrfCodec(b"s" * 32)

    token = codec.issue(session_cookie)
    stored_hash = codec.digest(token)

    assert codec.verify(session_cookie, token, stored_hash) is True
    assert codec.verify(other_cookie, token, stored_hash) is False
    assert codec.verify(session_cookie, token + "x", stored_hash) is False


def test_signed_cursor_binds_actor_operation_filters_and_expiry() -> None:
    now = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    codec = CursorCodec(b"c" * 32, ttl_seconds=86_400, now=lambda: now)
    binding = {"actor": "user-1", "operation": "adminListUsers", "filter": "enabled"}

    cursor = codec.encode(binding=binding, position={"created_at": "x", "id": "user-9"})

    assert codec.decode(cursor, expected_binding=binding) == {
        "created_at": "x",
        "id": "user-9",
    }
    with pytest.raises(CursorError, match="binding"):
        codec.decode(cursor, expected_binding={**binding, "actor": "user-2"})
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last_index = alphabet.index(cursor[-1])
    noncanonical_last = alphabet[(last_index & ~15) | ((last_index + 1) & 15)]
    with pytest.raises(CursorError, match="signature"):
        codec.decode(cursor[:-1] + noncanonical_last, expected_binding=binding)

    expired = CursorCodec(
        b"c" * 32,
        ttl_seconds=86_400,
        now=lambda: now + timedelta(days=1, seconds=1),
    )
    with pytest.raises(CursorError, match="expired"):
        expired.decode(cursor, expected_binding=binding)
