"""Strict bounded trusted-runner frame codec and durable sequence state."""

import hashlib
import json
import math
import re
import struct
from typing import Any

MAX_FRAME_BYTES = 64 * 1024
MAX_INT64 = 2**63 - 1
ENVELOPE_TYPES = frozenset(
    {
        "STARTED",
        "PROGRESS",
        "CHECKPOINT_FILES_READY",
        "CHECKPOINT_READY",
        "RESULT_PREPARE",
        "RESULT_FILE_BATCH",
        "CHUNK_FILE_BATCH",
        "AUXILIARY_MANIFEST_READY",
        "RESULT_READY",
        "FAILED",
        "STOPPED",
        "SET_AUTHORITY_DEADLINE",
        "REQUEST_CHECKPOINT",
        "PREPARE_RESULT",
        "BIND_ARTIFACT_BATCH",
        "FINALIZE_CHECKPOINT_MANIFEST",
        "FINALIZE_RESULT_MANIFEST",
        "REQUEST_STOP",
    }
)
CONTROL_TYPES = frozenset(
    {
        "SET_AUTHORITY_DEADLINE",
        "REQUEST_CHECKPOINT",
        "PREPARE_RESULT",
        "BIND_ARTIFACT_BATCH",
        "FINALIZE_CHECKPOINT_MANIFEST",
        "FINALIZE_RESULT_MANIFEST",
        "REQUEST_STOP",
    }
)
_UUID_V7 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")
_STAGING_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_LOGICAL_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class ProtocolError(ValueError):
    pass


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON field")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ProtocolError(f"non-finite JSON number is invalid: {value}")


def _closed(payload: object, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ProtocolError(f"invalid {name} payload")
    return payload


def _int64(value: object, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= MAX_INT64:
        raise ProtocolError(f"{name} must be an int64 in range")
    return value


def _uuid(value: object, name: str) -> str:
    if not isinstance(value, str) or not _UUID_V7.fullmatch(value):
        raise ProtocolError(f"{name} must be a canonical UUIDv7")
    return value


def _checksum(value: object, name: str) -> str:
    if not isinstance(value, str) or not _CHECKSUM.fullmatch(value):
        raise ProtocolError(f"{name} must be a sha256 checksum")
    return value


def _nullable_int64(value: object, name: str, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    return _int64(value, name, minimum=minimum)


def _validate_staged_artifact(value: object, *, kinds: set[str]) -> None:
    artifact = _closed(
        value,
        {"staging_name", "logical_name", "kind", "media_type", "size_bytes", "checksum"},
        "staged artifact",
    )
    if not isinstance(artifact["staging_name"], str) or not _STAGING_NAME.fullmatch(
        artifact["staging_name"]
    ):
        raise ProtocolError("invalid staging_name")
    if not isinstance(artifact["logical_name"], str) or not _LOGICAL_NAME.fullmatch(
        artifact["logical_name"]
    ):
        raise ProtocolError("invalid logical_name")
    if artifact["kind"] not in kinds:
        raise ProtocolError("invalid staged artifact kind")
    if not isinstance(artifact["media_type"], str) or not 1 <= len(artifact["media_type"]) <= 127:
        raise ProtocolError("invalid media_type")
    _int64(artifact["size_bytes"], "size_bytes")
    _checksum(artifact["checksum"], "checksum")


def _validate_final_manifest(value: object, *, kind: str) -> None:
    _validate_staged_artifact(value, kinds={kind})
    assert isinstance(value, dict)
    if value["media_type"] != "application/json" or value["size_bytes"] < 1:
        raise ProtocolError("invalid final manifest artifact")


def _validate_artifact_array(value: object, *, kinds: set[str]) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise ProtocolError("artifacts must contain 1..64 entries")
    for artifact in value:
        _validate_staged_artifact(artifact, kinds=kinds)


def _validate_runner_payload(message_type: object, body: object) -> None:
    if message_type == "STARTED":
        payload = _closed(body, {"startup_nonce", "pid", "started_monotonic_ns"}, "started")
        _uuid(payload["startup_nonce"], "startup_nonce")
        pid = _int64(payload["pid"], "pid", minimum=1)
        if pid > 4_194_304:
            raise ProtocolError("pid is outside the contract bound")
        _int64(payload["started_monotonic_ns"], "started_monotonic_ns")
    elif message_type == "PROGRESS":
        payload = _closed(
            body,
            {"progress_sequence", "fraction", "step", "epoch", "item_cursor"},
            "progress",
        )
        _int64(payload["progress_sequence"], "progress_sequence", minimum=1)
        fraction = payload["fraction"]
        if (
            not isinstance(fraction, int | float)
            or isinstance(fraction, bool)
            or not math.isfinite(fraction)
            or not 0 <= fraction <= 1
        ):
            raise ProtocolError("progress fraction is outside 0..1")
        for field in ("step", "epoch", "item_cursor"):
            _nullable_int64(payload[field], field)
    elif message_type == "CHECKPOINT_FILES_READY":
        payload = _closed(
            body,
            {
                "reservation_callback_id",
                "checkpoint_id",
                "checkpoint_sequence",
                "batch_index",
                "batch_count",
                "artifacts",
            },
            "checkpoint files",
        )
        _uuid(payload["reservation_callback_id"], "reservation_callback_id")
        _uuid(payload["checkpoint_id"], "checkpoint_id")
        _int64(payload["checkpoint_sequence"], "checkpoint_sequence", minimum=1)
        if payload["batch_index"] != 0 or payload["batch_count"] != 1:
            raise ProtocolError("checkpoint batch bounds are invalid")
        _validate_artifact_array(payload["artifacts"], kinds={"CHECKPOINT_FILE", "RESULT_FILE"})
    elif message_type == "CHECKPOINT_READY":
        payload = _closed(
            body,
            {"reservation_callback_id", "checkpoint_id", "checkpoint_sequence", "manifest"},
            "checkpoint ready",
        )
        _uuid(payload["reservation_callback_id"], "reservation_callback_id")
        _uuid(payload["checkpoint_id"], "checkpoint_id")
        _int64(payload["checkpoint_sequence"], "checkpoint_sequence", minimum=1)
        _validate_final_manifest(payload["manifest"], kind="CHECKPOINT_MANIFEST")
    elif message_type == "RESULT_PREPARE":
        payload = _closed(body, {"completion_token"}, "result prepare")
        _uuid(payload["completion_token"], "completion_token")
    elif message_type == "RESULT_FILE_BATCH":
        payload = _closed(
            body,
            {
                "completion_token",
                "reservation_callback_id",
                "result_id",
                "batch_index",
                "batch_count",
                "artifacts",
            },
            "result file batch",
        )
        for field in ("completion_token", "reservation_callback_id", "result_id"):
            _uuid(payload[field], field)
        batch_index = _int64(payload["batch_index"], "batch_index")
        batch_count = _int64(payload["batch_count"], "batch_count", minimum=1)
        if batch_index > 15 or batch_count > 16 or batch_index >= batch_count:
            raise ProtocolError("result batch bounds are invalid")
        _validate_artifact_array(payload["artifacts"], kinds={"RESULT_FILE"})
    elif message_type == "CHUNK_FILE_BATCH":
        payload = _closed(
            body,
            {
                "purpose",
                "reservation_callback_id",
                "reserved_id",
                "batch_index",
                "batch_count",
                "artifacts",
            },
            "chunk file batch",
        )
        if payload["purpose"] not in {"CHECKPOINT", "RESULT"}:
            raise ProtocolError("invalid chunk purpose")
        _uuid(payload["reservation_callback_id"], "reservation_callback_id")
        _uuid(payload["reserved_id"], "reserved_id")
        batch_index = _int64(payload["batch_index"], "batch_index")
        batch_count = _int64(payload["batch_count"], "batch_count", minimum=1)
        if batch_index > 1562 or batch_count > 1563 or batch_index >= batch_count:
            raise ProtocolError("chunk batch bounds are invalid")
        _validate_artifact_array(payload["artifacts"], kinds={"RESULT_FILE"})
    elif message_type == "AUXILIARY_MANIFEST_READY":
        payload = _closed(
            body,
            {"purpose", "reservation_callback_id", "reserved_id", "artifact"},
            "auxiliary manifest",
        )
        if payload["purpose"] not in {"CHECKPOINT", "RESULT"}:
            raise ProtocolError("invalid auxiliary manifest purpose")
        _uuid(payload["reservation_callback_id"], "reservation_callback_id")
        _uuid(payload["reserved_id"], "reserved_id")
        _validate_staged_artifact(payload["artifact"], kinds={"CHUNK_OUTPUT_MANIFEST"})
    elif message_type == "RESULT_READY":
        payload = _closed(
            body,
            {"completion_token", "reservation_callback_id", "result_id", "manifest"},
            "result ready",
        )
        for field in ("completion_token", "reservation_callback_id", "result_id"):
            _uuid(payload[field], field)
        _validate_final_manifest(payload["manifest"], kind="RESULT_MANIFEST")
    elif message_type == "FAILED":
        payload = _closed(
            body,
            {
                "failure_class",
                "reason_code",
                "exit_code",
                "oom_killed",
                "runtime_limit_reached",
            },
            "failed",
        )
        if payload["failure_class"] not in {
            "INFRASTRUCTURE",
            "TIMEOUT",
            "OOM",
            "INVALID_INPUT",
            "INCOMPATIBLE",
            "INTERNAL",
        }:
            raise ProtocolError("invalid failure class")
        if not isinstance(payload["reason_code"], str) or not _SAFE_CODE.fullmatch(
            payload["reason_code"]
        ):
            raise ProtocolError("invalid failure reason code")
        _nullable_int64(payload["exit_code"], "exit_code", minimum=-1)
        if payload["exit_code"] is not None and payload["exit_code"] > 255:
            raise ProtocolError("exit_code is outside the contract bound")
        for field in ("oom_killed", "runtime_limit_reached"):
            if not isinstance(payload[field], bool):
                raise ProtocolError(f"{field} must be boolean")
    elif message_type == "STOPPED":
        payload = _closed(body, {"reason", "exit_code", "stopped_monotonic_ns"}, "stopped")
        if payload["reason"] not in {
            "PAUSE",
            "CANCEL",
            "LEASE_DEADLINE",
            "FAILURE",
            "RUNTIME_LIMIT",
            "SHUTDOWN",
        }:
            raise ProtocolError("invalid stop reason")
        exit_code = _int64(payload["exit_code"], "exit_code", minimum=-1)
        if exit_code > 255:
            raise ProtocolError("exit_code is outside the contract bound")
        _int64(payload["stopped_monotonic_ns"], "stopped_monotonic_ns")
    else:
        raise ProtocolError("runner message type is invalid")


def validate_envelope(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProtocolError("envelope must be an object")
    allowed = {"schema_version", "message_sequence", "control_sequence", "type", "payload"}
    unknown = set(payload) - allowed
    if unknown:
        raise ProtocolError("unknown envelope field")
    if payload.get("schema_version") != 1:
        raise ProtocolError("unsupported schema version")
    sequence_fields = [name for name in ("message_sequence", "control_sequence") if name in payload]
    if len(sequence_fields) != 1:
        raise ProtocolError("exactly one envelope sequence is required")
    _int64(payload[sequence_fields[0]], "sequence", minimum=1)
    if payload.get("type") not in ENVELOPE_TYPES:
        raise ProtocolError("unknown envelope type")
    if not isinstance(payload.get("payload"), dict):
        raise ProtocolError("payload must be an object")
    _assert_finite(payload["payload"])
    if "message_sequence" in payload:
        _validate_runner_payload(payload["type"], payload["payload"])
    return payload


def validate_control_envelope(payload: object) -> dict[str, Any]:
    envelope = validate_envelope(payload)
    if "control_sequence" not in envelope or envelope["type"] not in CONTROL_TYPES:
        raise ProtocolError("runner control requires a control envelope")
    body = envelope["payload"]
    message_type = envelope["type"]
    if message_type == "SET_AUTHORITY_DEADLINE":
        body = _closed(body, {"source_callback_id", "deadline_monotonic_ns"}, "deadline")
        _uuid(body["source_callback_id"], "source_callback_id")
        _int64(body["deadline_monotonic_ns"], "deadline_monotonic_ns")
    elif message_type == "REQUEST_STOP":
        body = _closed(body, {"reason", "grace_deadline_monotonic_ns"}, "stop")
        if body["reason"] not in {
            "PAUSE",
            "CANCEL",
            "LEASE_DEADLINE",
            "FAILURE",
            "RUNTIME_LIMIT",
            "SHUTDOWN",
        }:
            raise ProtocolError("invalid stop reason")
        _int64(body["grace_deadline_monotonic_ns"], "grace_deadline_monotonic_ns")
    elif message_type == "PREPARE_RESULT":
        body = _closed(
            body,
            {"completion_token", "reservation_callback_id", "result_id"},
            "prepare result",
        )
        for field in ("completion_token", "reservation_callback_id", "result_id"):
            _uuid(body[field], field)
    elif message_type == "BIND_ARTIFACT_BATCH":
        body = _closed(
            body,
            {
                "purpose",
                "reservation_callback_id",
                "reserved_id",
                "source_message_sequence",
                "bindings",
            },
            "artifact binding",
        )
        if body["purpose"] not in {"CHECKPOINT", "RESULT", "CHUNK_OUTPUT"}:
            raise ProtocolError("invalid binding purpose")
        _uuid(body["reservation_callback_id"], "reservation_callback_id")
        _uuid(body["reserved_id"], "reserved_id")
        _int64(body["source_message_sequence"], "source_message_sequence", minimum=1)
        bindings = body["bindings"]
        if not isinstance(bindings, list) or not 1 <= len(bindings) <= 64:
            raise ProtocolError("bindings must contain 1..64 entries")
        for binding in bindings:
            _validate_binding(binding)
    elif message_type == "FINALIZE_RESULT_MANIFEST":
        body = _closed(
            body,
            {
                "completion_token",
                "reservation_callback_id",
                "result_id",
                "binding_set_checksum",
            },
            "finalize result",
        )
        for field in ("completion_token", "reservation_callback_id", "result_id"):
            _uuid(body[field], field)
        _checksum(body["binding_set_checksum"], "binding_set_checksum")
    elif message_type in {
        "REQUEST_CHECKPOINT",
        "FINALIZE_CHECKPOINT_MANIFEST",
    }:
        raise ProtocolError("checkpoint control is not implemented by B09")
    else:
        raise ProtocolError("unknown control type")
    return envelope


def _validate_binding(value: object) -> None:
    binding = _closed(
        value,
        {
            "staging_name",
            "logical_name",
            "artifact_id",
            "kind",
            "media_type",
            "size_bytes",
            "checksum",
        },
        "committed artifact binding",
    )
    if not isinstance(binding["staging_name"], str) or not _STAGING_NAME.fullmatch(
        binding["staging_name"]
    ):
        raise ProtocolError("invalid staging_name")
    if not isinstance(binding["logical_name"], str) or not _LOGICAL_NAME.fullmatch(
        binding["logical_name"]
    ):
        raise ProtocolError("invalid logical_name")
    _uuid(binding["artifact_id"], "artifact_id")
    if binding["kind"] not in {
        "CHECKPOINT_FILE",
        "RESULT_FILE",
        "CHUNK_OUTPUT_MANIFEST",
        "CHECKPOINT_MANIFEST",
        "RESULT_MANIFEST",
    }:
        raise ProtocolError("invalid artifact kind")
    if not isinstance(binding["media_type"], str) or not 1 <= len(binding["media_type"]) <= 127:
        raise ProtocolError("invalid media_type")
    _int64(binding["size_bytes"], "size_bytes")
    _checksum(binding["checksum"], "checksum")


def validate_ack(payload: object) -> dict[str, Any]:
    ack = _closed(
        payload,
        {"schema_version", "ack_sequence", "accepted", "code"},
        "acknowledgment",
    )
    if ack["schema_version"] != 1:
        raise ProtocolError("unsupported schema version")
    _int64(ack["ack_sequence"], "ack_sequence", minimum=1)
    if not isinstance(ack["accepted"], bool):
        raise ProtocolError("accepted must be boolean")
    if ack["code"] not in {
        "ACCEPTED",
        "DUPLICATE",
        "OUT_OF_ORDER",
        "INVALID",
        "STALE_AUTHORITY",
    }:
        raise ProtocolError("invalid acknowledgment code")
    if ack["accepted"] != (ack["code"] in {"ACCEPTED", "DUPLICATE"}):
        raise ProtocolError("acknowledgment accepted/code mismatch")
    return ack


def canonical_envelope_effect(envelope: dict[str, object]) -> bytes:
    return json.dumps(
        {"type": envelope["type"], "payload": envelope["payload"]},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def encode_frame(payload: dict[str, object]) -> bytes:
    try:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except ValueError as exc:
        raise ProtocolError("non-finite JSON number is invalid") from exc
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds maximum size")
    return struct.pack("!I", len(raw)) + raw


class FrameDecoder:
    def __init__(
        self, *, max_frame_bytes: int = MAX_FRAME_BYTES, max_pending_frames: int = 64
    ) -> None:
        if max_frame_bytes < 1 or max_frame_bytes > MAX_FRAME_BYTES:
            raise ValueError("max_frame_bytes is outside the contract bound")
        if max_pending_frames < 1 or max_pending_frames > 1024:
            raise ValueError("max_pending_frames is outside the contract bound")
        self.max_frame_bytes = max_frame_bytes
        self.max_pending_frames = max_pending_frames
        self._buffer = bytearray()
        self._expected: int | None = None

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self._buffer.extend(data)
        frames: list[dict[str, Any]] = []
        while True:
            if self._expected is None:
                if len(self._buffer) < 4:
                    return frames
                self._expected = struct.unpack("!I", self._buffer[:4])[0]
                del self._buffer[:4]
                if self._expected > self.max_frame_bytes:
                    raise ProtocolError("frame exceeds maximum size")
            if len(self._buffer) < self._expected:
                return frames
            if len(frames) >= self.max_pending_frames:
                raise ProtocolError("pending frame limit exceeded")
            body = bytes(self._buffer[: self._expected])
            del self._buffer[: self._expected]
            self._expected = None
            try:
                value = json.loads(
                    body.decode("utf-8", "strict"),
                    object_pairs_hook=_reject_duplicates,
                    parse_constant=_reject_non_finite,
                )
            except UnicodeDecodeError as exc:
                raise ProtocolError("frame is not valid UTF-8") from exc
            except json.JSONDecodeError as exc:
                raise ProtocolError("frame is not valid JSON") from exc
            if not isinstance(value, dict):
                raise ProtocolError("frame must contain an object")
            frames.append(value)


class SequenceState:
    """Bounded receive state with explicit validate/effect/commit ordering."""

    def __init__(self, *, max_entries: int = 1024) -> None:
        if max_entries < 1 or max_entries > 65_536:
            raise ValueError("max_entries is outside the contract bound")
        self.max_entries = max_entries
        self._payloads: dict[int, bytes] = {}
        self._highest = 0

    def classify(self, sequence: int, payload: bytes) -> str:
        digest = hashlib.sha256(payload).digest()
        if sequence in self._payloads:
            return "DUPLICATE" if self._payloads[sequence] == digest else "INVALID"
        if sequence <= self._highest or sequence != self._highest + 1:
            return "OUT_OF_ORDER"
        return "ACCEPTED"

    def commit(self, sequence: int, payload: bytes) -> None:
        if self.classify(sequence, payload) != "ACCEPTED":
            raise ProtocolError("sequence cannot be committed")
        self._payloads[sequence] = hashlib.sha256(payload).digest()
        if len(self._payloads) > self.max_entries:
            oldest = next(iter(self._payloads))
            del self._payloads[oldest]
        self._highest = sequence

    def accept(self, sequence: int, payload: bytes) -> str:
        result = self.classify(sequence, payload)
        if result == "ACCEPTED":
            self.commit(sequence, payload)
        return result

    def snapshot(self) -> dict[str, object]:
        return {
            "highest": self._highest,
            "payload_hashes": {
                str(sequence): digest.hex() for sequence, digest in self._payloads.items()
            },
        }

    @classmethod
    def from_snapshot(cls, snapshot: object, *, max_entries: int = 1024) -> "SequenceState":
        if not isinstance(snapshot, dict):
            raise ProtocolError("sequence snapshot must be an object")
        highest = snapshot.get("highest")
        hashes = snapshot.get("payload_hashes")
        _int64(highest, "highest")
        if not isinstance(hashes, dict) or len(hashes) > max_entries:
            raise ProtocolError("sequence snapshot hashes are invalid")
        state = cls(max_entries=max_entries)
        for sequence_text, digest_text in hashes.items():
            try:
                sequence = int(sequence_text)
                digest = bytes.fromhex(str(digest_text))
            except (TypeError, ValueError) as exc:
                raise ProtocolError("sequence snapshot hash is invalid") from exc
            if not 1 <= sequence <= MAX_INT64 or len(digest) != 32:
                raise ProtocolError("sequence snapshot hash is invalid")
            state._payloads[sequence] = digest
        if state._payloads and max(state._payloads) > highest:
            raise ProtocolError("sequence snapshot highest is invalid")
        state._highest = highest
        return state


def _assert_finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ProtocolError("non-finite number is invalid")
    if isinstance(value, dict):
        for item in value.values():
            _assert_finite(item)
    elif isinstance(value, list):
        for item in value:
            _assert_finite(item)


__all__ = [
    "FrameDecoder",
    "MAX_FRAME_BYTES",
    "ProtocolError",
    "SequenceState",
    "canonical_envelope_effect",
    "encode_frame",
    "validate_ack",
    "validate_control_envelope",
    "validate_envelope",
]
