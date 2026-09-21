import math
import struct

import pytest

from nexa.worker.protocol import (
    FrameDecoder,
    ProtocolError,
    SequenceState,
    canonical_envelope_effect,
    encode_frame,
    validate_control_envelope,
    validate_envelope,
)


def frame(payload: dict[str, object]) -> bytes:
    return encode_frame(payload)


def test_decoder_handles_fragmented_frame_and_rejects_oversize() -> None:
    payload = {"schema_version": 1, "message_sequence": 1, "type": "STARTED", "payload": {}}
    encoded = frame(payload)
    decoder = FrameDecoder()
    assert decoder.feed(encoded[:2]) == []
    assert decoder.feed(encoded[2:]) == [payload]
    oversized = struct.pack("!I", 64 * 1024 + 1)
    with pytest.raises(ProtocolError, match="maximum"):
        decoder.feed(oversized)


def test_decoder_rejects_invalid_utf8_and_unknown_fields() -> None:
    raw = struct.pack("!I", 1) + b"\xff"
    with pytest.raises(ProtocolError, match="UTF-8"):
        FrameDecoder().feed(raw)
    with pytest.raises(ProtocolError, match="unknown"):
        validate_envelope(
            {
                "schema_version": 1,
                "message_sequence": 1,
                "type": "STARTED",
                "payload": {},
                "extra": 1,
            }
        )
    with pytest.raises(ProtocolError, match="type"):
        validate_envelope(
            {"schema_version": 1, "message_sequence": 1, "type": "NOT_A_TYPE", "payload": {}}
        )


def test_validate_envelope_rejects_duplicate_or_bad_sequence() -> None:
    with pytest.raises(ProtocolError, match="sequence"):
        validate_envelope(
            {"schema_version": 1, "message_sequence": 0, "type": "STARTED", "payload": {}}
        )


def test_sequence_state_deduplicates_exact_payload_and_rejects_conflict() -> None:
    state = SequenceState()
    assert state.accept(1, b"a") == "ACCEPTED"
    assert state.accept(1, b"a") == "DUPLICATE"
    assert state.accept(1, b"b") == "INVALID"
    assert state.accept(3, b"c") == "OUT_OF_ORDER"


def test_decoder_and_sequence_state_bound_pending_history() -> None:
    decoder = FrameDecoder(max_pending_frames=1)
    first = frame({"schema_version": 1, "message_sequence": 1, "type": "STARTED", "payload": {}})
    second = frame({"schema_version": 1, "message_sequence": 2, "type": "STARTED", "payload": {}})
    with pytest.raises(ProtocolError, match="pending"):
        decoder.feed(first + second)

    state = SequenceState(max_entries=1)
    assert state.accept(1, b"a") == "ACCEPTED"
    assert state.accept(2, b"b") == "ACCEPTED"
    assert state.accept(1, b"a") == "OUT_OF_ORDER"


def test_control_payload_is_strictly_validated_before_sequence_commit() -> None:
    valid = {
        "schema_version": 1,
        "control_sequence": 1,
        "type": "SET_AUTHORITY_DEADLINE",
        "payload": {
            "source_callback_id": "018f0d60-7b6a-7a21-9d82-1aa39c4f30b7",
            "deadline_monotonic_ns": 10,
        },
    }
    assert validate_control_envelope(valid) == valid
    for bad_payload in (
        {"source_callback_id": None, "deadline_monotonic_ns": 10},
        {"source_callback_id": "cb", "deadline_monotonic_ns": 10},
        {
            "source_callback_id": "018f0d60-7b6a-7a21-9d82-1aa39c4f30b7",
            "deadline_monotonic_ns": math.nan,
        },
        {
            "source_callback_id": "018f0d60-7b6a-7a21-9d82-1aa39c4f30b7",
            "deadline_monotonic_ns": 2**63,
        },
    ):
        with pytest.raises(ProtocolError):
            validate_control_envelope({**valid, "payload": bad_payload})


def test_runner_payload_branches_are_closed_and_strictly_validated() -> None:
    token = "018f0d60-7b6a-7a21-9d82-1aa39c4f30b7"
    result_id = "018f0d60-7b6a-7a22-9d82-1aa39c4f30b7"
    callback_id = "018f0d60-7b6a-7a23-9d82-1aa39c4f30b7"
    checksum = "sha256:" + "a" * 64
    staged = {
        "staging_name": "result.json",
        "logical_name": "result.json",
        "kind": "RESULT_FILE",
        "media_type": "application/json",
        "size_bytes": 2,
        "checksum": checksum,
    }
    manifest = {
        "staging_name": "result-manifest.json",
        "logical_name": "result.manifest.json",
        "kind": "RESULT_MANIFEST",
        "media_type": "application/json",
        "size_bytes": 2,
        "checksum": checksum,
    }
    valid_payloads = {
        "STARTED": {"startup_nonce": token, "pid": 1, "started_monotonic_ns": 0},
        "PROGRESS": {
            "progress_sequence": 1,
            "fraction": 0.5,
            "step": 1,
            "epoch": None,
            "item_cursor": None,
        },
        "RESULT_PREPARE": {"completion_token": token},
        "RESULT_FILE_BATCH": {
            "completion_token": token,
            "reservation_callback_id": callback_id,
            "result_id": result_id,
            "batch_index": 0,
            "batch_count": 1,
            "artifacts": [staged],
        },
        "RESULT_READY": {
            "completion_token": token,
            "reservation_callback_id": callback_id,
            "result_id": result_id,
            "manifest": manifest,
        },
        "FAILED": {
            "failure_class": "INTERNAL",
            "reason_code": "WORKLOAD_EXIT_NONZERO",
            "exit_code": 1,
            "oom_killed": False,
            "runtime_limit_reached": False,
        },
        "STOPPED": {"reason": "FAILURE", "exit_code": -1, "stopped_monotonic_ns": 0},
    }
    for sequence, (message_type, payload) in enumerate(valid_payloads.items(), start=1):
        envelope = {
            "schema_version": 1,
            "message_sequence": sequence,
            "type": message_type,
            "payload": payload,
        }
        assert validate_envelope(envelope) == envelope
        with pytest.raises(ProtocolError):
            validate_envelope({**envelope, "payload": {**payload, "unexpected": None}})

    with pytest.raises(ProtocolError):
        validate_envelope(
            {
                "schema_version": 1,
                "message_sequence": 8,
                "type": "STARTED",
                "payload": {"startup_nonce": token, "pid": None, "started_monotonic_ns": 0},
            }
        )


def test_sequence_state_can_preview_commit_and_restore_durable_hashes() -> None:
    state = SequenceState()
    payload = canonical_envelope_effect(
        {
            "schema_version": 1,
            "control_sequence": 1,
            "type": "REQUEST_STOP",
            "payload": {"reason": "CANCEL", "grace_deadline_monotonic_ns": 10},
        }
    )
    assert state.classify(1, payload) == "ACCEPTED"
    assert state.classify(1, payload) == "ACCEPTED"
    state.commit(1, payload)
    restored = SequenceState.from_snapshot(state.snapshot())
    assert restored.classify(1, payload) == "DUPLICATE"
    assert restored.classify(1, payload + b"x") == "INVALID"


def test_json_codec_rejects_non_finite_numbers() -> None:
    with pytest.raises(ProtocolError, match="finite"):
        encode_frame({"value": math.inf})
    raw = (
        b'{"schema_version":1,"control_sequence":1,"type":"SET_AUTHORITY_DEADLINE",'
        b'"payload":{"source_callback_id":"018f0d60-7b6a-7a21-9d82-1aa39c4f30b7",'
        b'"deadline_monotonic_ns":NaN}}'
    )
    with pytest.raises(ProtocolError, match="finite"):
        FrameDecoder().feed(struct.pack("!I", len(raw)) + raw)
