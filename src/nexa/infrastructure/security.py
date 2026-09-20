"""Password, opaque-secret, CSRF, and signed-cursor primitives."""

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import BoundedSemaphore
from uuid import UUID

from argon2 import PasswordHasher as Argon2PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type


class SecurityConfigurationError(ValueError):
    """Raised when secret material or hashing configuration is unsafe."""


class CursorError(ValueError):
    """Raised when a pagination cursor is invalid for the current request."""


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("invalid base64url value") from None


def read_secret_file(path: Path) -> bytes:
    try:
        value = path.read_bytes()
    except OSError:
        raise SecurityConfigurationError("Secret file cannot be read") from None
    if value.endswith(b"\r\n"):
        value = value[:-2]
    elif value.endswith(b"\n"):
        value = value[:-1]
    if not 32 <= len(value) <= 4096:
        raise SecurityConfigurationError("Secret file must contain at least 32 bytes")
    if b"\x00" in value:
        raise SecurityConfigurationError("Secret file contains invalid bytes")
    return value


def read_header_secret_file(path: Path) -> bytes:
    value = read_secret_file(path)
    if any(byte < 33 or byte > 126 for byte in value):
        raise SecurityConfigurationError(
            "Bootstrap secret file must contain only visible ASCII bytes"
        )
    return value


def header_secret_bytes(value: str) -> bytes:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("Header secret must contain only visible ASCII bytes") from None
    if any(byte < 33 or byte > 126 for byte in encoded):
        raise ValueError("Header secret must contain only visible ASCII bytes")
    return encoded


class PasswordHasher:
    def __init__(
        self,
        *,
        memory_kib: int,
        time_cost: int,
        parallelism: int,
        max_concurrency: int,
    ) -> None:
        if memory_kib < 19_456 or time_cost < 2 or parallelism < 1:
            raise SecurityConfigurationError("Argon2id configuration is below the OWASP minimum")
        if max_concurrency < 1:
            raise SecurityConfigurationError("Password hash concurrency must be positive")
        self._hasher = Argon2PasswordHasher(
            time_cost=time_cost,
            memory_cost=memory_kib,
            parallelism=parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._semaphore = BoundedSemaphore(max_concurrency)

    @staticmethod
    def _validate_password(password: str) -> None:
        if not 12 <= len(password) <= 1024:
            raise ValueError("Password length must be between 12 and 1024 characters")

    def hash(self, password: str) -> str:
        self._validate_password(password)
        with self._semaphore:
            return self._hasher.hash(password)

    def verify(self, encoded: str, password: str) -> bool:
        self._validate_password(password)
        try:
            with self._semaphore:
                return self._hasher.verify(encoded, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False


@dataclass(frozen=True, slots=True, repr=False)
class ParsedSecret:
    identifier: UUID
    secret: bytes


class SecretCodec:
    def issue(self, identifier: UUID) -> str:
        return f"{identifier}.{_b64encode(secrets.token_bytes(32))}"

    def parse(self, encoded: str) -> ParsedSecret:
        identifier_text, separator, secret_text = encoded.partition(".")
        if not separator or "." in secret_text:
            raise ValueError("Invalid opaque secret")
        try:
            identifier = UUID(identifier_text)
            secret = _b64decode(secret_text)
        except ValueError:
            raise ValueError("Invalid opaque secret") from None
        if len(secret) != 32:
            raise ValueError("Invalid opaque secret")
        return ParsedSecret(identifier=identifier, secret=secret)

    def digest(self, encoded: str) -> bytes:
        return hashlib.sha256(self.parse(encoded).secret).digest()


class CsrfCodec:
    def __init__(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise SecurityConfigurationError("CSRF signing key must contain at least 32 bytes")
        self._signing_key = signing_key

    def issue(self, session_cookie: str) -> str:
        return _b64encode(
            hmac.new(self._signing_key, session_cookie.encode("ascii"), hashlib.sha256).digest()
        )

    def digest(self, token: str) -> bytes:
        return hashlib.sha256(token.encode("ascii")).digest()

    def verify(self, session_cookie: str, token: str, expected_digest: bytes) -> bool:
        expected_token = self.issue(session_cookie)
        return hmac.compare_digest(token, expected_token) and hmac.compare_digest(
            self.digest(token), expected_digest
        )


class CursorCodec:
    def __init__(
        self, signing_key: bytes, *, ttl_seconds: int, now: Callable[[], datetime]
    ) -> None:
        if len(signing_key) < 32:
            raise SecurityConfigurationError("Cursor signing key must contain at least 32 bytes")
        if ttl_seconds < 1:
            raise SecurityConfigurationError("Cursor TTL must be positive")
        self._signing_key = signing_key
        self._ttl_seconds = ttl_seconds
        self._now = now

    def encode(self, *, binding: Mapping[str, str], position: Mapping[str, str]) -> str:
        payload = {
            "binding": dict(binding),
            "expires_at": int(self._now().astimezone(UTC).timestamp()) + self._ttl_seconds,
            "position": dict(position),
        }
        payload_bytes = json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("ascii")
        signature = hmac.new(self._signing_key, payload_bytes, hashlib.sha256).digest()
        return f"{_b64encode(payload_bytes)}.{_b64encode(signature)}"

    def decode(self, cursor: str, *, expected_binding: Mapping[str, str]) -> dict[str, str]:
        payload_text, separator, signature_text = cursor.partition(".")
        if not separator or "." in signature_text:
            raise CursorError("Invalid cursor signature")
        try:
            payload_bytes = _b64decode(payload_text)
            signature = _b64decode(signature_text)
        except ValueError:
            raise CursorError("Invalid cursor signature") from None
        expected_signature = hmac.new(self._signing_key, payload_bytes, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise CursorError("Invalid cursor signature")
        try:
            payload = json.loads(payload_bytes)
            binding = payload["binding"]
            expires_at = int(payload["expires_at"])
            position = payload["position"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise CursorError("Invalid cursor payload") from None
        if binding != dict(expected_binding):
            raise CursorError("Cursor binding does not match this request")
        if int(self._now().astimezone(UTC).timestamp()) > expires_at:
            raise CursorError("Cursor has expired")
        if not isinstance(position, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in position.items()
        ):
            raise CursorError("Invalid cursor position")
        return position
