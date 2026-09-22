"""Worker-side authority deadline control for the trusted runner."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .protocol import (
    FrameDecoder,
    ProtocolError,
    encode_frame,
    validate_ack,
    validate_envelope,
)


class RunnerControlError(RuntimeError):
    """The runner did not accept a deadline control safely."""


class ControlChannel(Protocol):
    def settimeout(self, timeout: float | None) -> None: ...

    def sendall(self, payload: bytes) -> None: ...

    def recv(self, maximum_bytes: int) -> bytes: ...


@dataclass(frozen=True, slots=True)
class DeadlineResult:
    control_sequence: int
    deadline_monotonic_ns: int


class RunnerControl:
    """Apply a candidate deadline only after a timely, valid runner ACK.

    The sequence counter is process-local but is normally initialized from the
    attempt journal before this object is constructed. Callers persist the
    returned sequence alongside the runner state before considering the control
    complete.
    """

    def __init__(self, *, next_sequence: int = 1, monotonic_ns=time.monotonic_ns) -> None:
        if next_sequence < 1:
            raise ValueError("next_sequence must be positive")
        self.next_sequence = next_sequence
        self.monotonic_ns = monotonic_ns

    def set_authority_deadline(
        self,
        channel: ControlChannel,
        *,
        source_callback_id: str,
        first_send_monotonic_ns: int,
        lease_duration_seconds: int = 45,
        safety_margin_seconds: int = 5,
        timeout_seconds: float = 5.0,
    ) -> DeadlineResult:
        if first_send_monotonic_ns < 0:
            raise ValueError("first_send_monotonic_ns must be non-negative")
        if lease_duration_seconds <= safety_margin_seconds:
            raise ValueError("lease duration must exceed safety margin")
        candidate = (
            first_send_monotonic_ns
            + (lease_duration_seconds - safety_margin_seconds) * 1_000_000_000
        )
        if self.monotonic_ns() >= candidate:
            raise RunnerControlError("runner deadline candidate deadline has elapsed")
        sequence = self.next_sequence
        frame = {
            "schema_version": 1,
            "control_sequence": sequence,
            "type": "SET_AUTHORITY_DEADLINE",
            "payload": {
                "source_callback_id": source_callback_id,
                "deadline_monotonic_ns": candidate,
            },
        }
        channel.settimeout(timeout_seconds)
        channel.sendall(encode_frame(frame))
        decoder = FrameDecoder()
        try:
            ack = self._receive_ack(channel, decoder, sequence, timeout_seconds)
            validated = validate_ack(ack)
        except (ProtocolError, TimeoutError, OSError, RuntimeError) as exc:
            raise RunnerControlError("runner deadline acknowledgment unavailable") from exc
        if validated["ack_sequence"] != sequence or not validated["accepted"]:
            raise RunnerControlError("runner deadline acknowledgment was not accepted")
        self.next_sequence += 1
        return DeadlineResult(sequence, candidate)

    @staticmethod
    def _receive_ack(
        channel: ControlChannel, decoder: FrameDecoder, sequence: int, timeout_seconds: float
    ) -> dict:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("runner deadline acknowledgment timed out")
            channel.settimeout(remaining)
            data = channel.recv(64 * 1024)
            for frame in decoder.feed(data):
                if frame.get("ack_sequence") == sequence:
                    return frame

    def receive_messages(
        self,
        channel: ControlChannel,
        handler: Callable[[dict[str, object]], str],
        *,
        timeout_seconds: float = 0.1,
    ) -> int:
        """Receive one bounded batch and ACK only durably handled messages."""
        decoder = FrameDecoder()
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 0
            channel.settimeout(remaining)
            try:
                data = channel.recv(64 * 1024)
            except TimeoutError:
                return 0
            handled = 0
            for raw in decoder.feed(data):
                if "ack_sequence" in raw:
                    validate_ack(raw)
                    continue
                envelope = validate_envelope(raw)
                sequence = int(envelope["message_sequence"])
                code = handler(envelope)
                if code not in {"ACCEPTED", "DUPLICATE", "OUT_OF_ORDER", "INVALID"}:
                    raise RunnerControlError("runner message handler returned an invalid code")
                channel.sendall(
                    encode_frame(
                        {
                            "schema_version": 1,
                            "ack_sequence": sequence,
                            "accepted": code in {"ACCEPTED", "DUPLICATE"},
                            "code": code,
                        }
                    )
                )
                handled += 1
            if handled:
                return handled


__all__ = ["DeadlineResult", "RunnerControl", "RunnerControlError"]
