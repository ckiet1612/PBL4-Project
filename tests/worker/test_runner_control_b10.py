import json
import struct

import pytest

from nexa.worker.runner_control import RunnerControl, RunnerControlError


class FakeChannel:
    def __init__(self, ack: dict | None = None, *, receive_error: Exception | None = None):
        self.sent: list[bytes] = []
        self.ack = ack
        self.receive_error = receive_error

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)

    def recv(self, _maximum: int) -> bytes:
        if self.receive_error:
            raise self.receive_error
        assert self.ack is not None
        raw = json.dumps(self.ack, separators=(",", ":")).encode()
        return struct.pack("!I", len(raw)) + raw


class RunnerMessageChannel(FakeChannel):
    def __init__(self, message: dict):
        super().__init__()
        self.message = message

    def recv(self, _maximum: int) -> bytes:
        raw = json.dumps(self.message, separators=(",", ":")).encode()
        return struct.pack("!I", len(raw)) + raw


class SplitRunnerMessageChannel(FakeChannel):
    def __init__(self, message: dict):
        super().__init__()
        raw = json.dumps(message, separators=(",", ":")).encode()
        frame = struct.pack("!I", len(raw)) + raw
        self.chunks = [frame[:2], frame[2:9], frame[9:]]

    def recv(self, _maximum: int) -> bytes:
        return self.chunks.pop(0)


def _ack(sequence: int, *, accepted: bool = True, code: str = "ACCEPTED") -> dict:
    return {
        "schema_version": 1,
        "ack_sequence": sequence,
        "accepted": accepted,
        "code": code,
    }


def test_runner_control_uses_first_send_candidate_and_persists_sequence() -> None:
    channel = FakeChannel(_ack(7))
    control = RunnerControl(next_sequence=7, monotonic_ns=lambda: 1_000_000_001)

    result = control.set_authority_deadline(
        channel,
        source_callback_id="018f05c4-a922-7d0d-9f55-f9084a72d998",
        first_send_monotonic_ns=1_000_000_000,
        lease_duration_seconds=45,
        safety_margin_seconds=5,
    )

    assert result.control_sequence == 7
    assert result.deadline_monotonic_ns == 41_000_000_000
    assert control.next_sequence == 8
    assert len(channel.sent) == 1
    assert json.loads(channel.sent[0][4:]) == {
        "schema_version": 1,
        "control_sequence": 7,
        "type": "SET_AUTHORITY_DEADLINE",
        "payload": {
            "source_callback_id": "018f05c4-a922-7d0d-9f55-f9084a72d998",
            "deadline_monotonic_ns": 41_000_000_000,
        },
    }


def test_runner_control_rejects_late_ack_without_sending_control() -> None:
    channel = FakeChannel(_ack(1))
    control = RunnerControl(next_sequence=1, monotonic_ns=lambda: 45_000_000_000)

    with pytest.raises(RunnerControlError, match="candidate deadline"):
        control.set_authority_deadline(
            channel,
            source_callback_id="018f05c4-a922-7d0d-9f55-f9084a72d998",
            first_send_monotonic_ns=1_000_000_000,
            lease_duration_seconds=45,
            safety_margin_seconds=5,
            timeout_seconds=0.01,
        )
    assert channel.sent == []


def test_runner_control_requires_matching_accepted_ack() -> None:
    channel = FakeChannel(_ack(2, accepted=False, code="INVALID"))
    control = RunnerControl(next_sequence=1, monotonic_ns=lambda: 2_000_000_000)

    with pytest.raises(RunnerControlError, match="acknowledgment"):
        control.set_authority_deadline(
            channel,
            source_callback_id="018f05c4-a922-7d0d-9f55-f9084a72d998",
            first_send_monotonic_ns=1_000_000_000,
            lease_duration_seconds=45,
            safety_margin_seconds=5,
            timeout_seconds=0.01,
        )


def test_runner_control_acknowledges_only_handler_accepted_message() -> None:
    channel = RunnerMessageChannel(
        {
            "schema_version": 1,
            "message_sequence": 3,
            "type": "PROGRESS",
            "payload": {
                "progress_sequence": 1,
                "fraction": 0.5,
                "step": 1,
                "epoch": None,
                "item_cursor": None,
            },
        }
    )
    control = RunnerControl()

    assert control.receive_messages(channel, lambda _message: "ACCEPTED") == 1
    assert json.loads(channel.sent[0][4:]) == {
        "schema_version": 1,
        "ack_sequence": 3,
        "accepted": True,
        "code": "ACCEPTED",
    }


def test_runner_control_preserves_partial_frame_until_complete() -> None:
    channel = SplitRunnerMessageChannel(
        {
            "schema_version": 1,
            "message_sequence": 3,
            "type": "PROGRESS",
            "payload": {
                "progress_sequence": 1,
                "fraction": 0.5,
                "step": 1,
                "epoch": None,
                "item_cursor": None,
            },
        }
    )

    assert RunnerControl().receive_messages(channel, lambda _message: "ACCEPTED") == 1
    assert len(channel.sent) == 1
