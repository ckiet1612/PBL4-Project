"""Worker-side monotonic authority deadline candidate tracking."""


class DeadlineStateError(ValueError):
    pass


class DeadlineController:
    def __init__(self, *, lease_duration_seconds: float, safety_margin_seconds: float) -> None:
        if lease_duration_seconds <= safety_margin_seconds:
            raise ValueError("lease duration must exceed safety margin")
        self.lease_duration_seconds = lease_duration_seconds
        self.safety_margin_seconds = safety_margin_seconds
        self._first_send: dict[str, float] = {}
        self._accepted: dict[str, float] = {}
        self._failed: set[str] = set()
        self._last_deadline: float | None = None

    def first_send(self, callback_id: str, sent_at: float) -> float:
        if callback_id not in self._first_send:
            self._first_send[callback_id] = sent_at
        return self._candidate(callback_id)

    def retry_first_send(self, callback_id: str, *, now: float) -> float:
        del now
        if callback_id not in self._first_send:
            raise DeadlineStateError("callback has no first send")
        return self._first_send[callback_id]

    def accept_ack(self, callback_id: str, *, sent_at: float, ack_at: float) -> float:
        candidate = self.first_send(callback_id, sent_at)
        if callback_id in self._failed:
            raise DeadlineStateError("callback failed")
        if ack_at >= candidate:
            raise DeadlineStateError("late authority acknowledgment")
        existing = self._accepted.get(callback_id)
        if existing is not None and existing != candidate:
            raise DeadlineStateError("authority acknowledgment candidate changed")
        self._accepted[callback_id] = candidate
        return candidate

    def fail(self, callback_id: str) -> None:
        self._failed.add(callback_id)
        self._accepted.pop(callback_id, None)
        return None

    def set_authority_deadline(
        self, callback_id: str, deadline: float, *, applied_at: float
    ) -> float:
        candidate = self._accepted.get(callback_id)
        if candidate is None:
            raise DeadlineStateError("callback has no acknowledged authority deadline")
        if deadline != candidate:
            raise DeadlineStateError("deadline does not match acknowledged candidate")
        if applied_at >= candidate:
            raise DeadlineStateError("acknowledged authority deadline already expired")
        if self._last_deadline is not None and deadline < self._last_deadline:
            raise DeadlineStateError("authority deadline cannot move backwards")
        self._last_deadline = deadline
        return deadline

    @property
    def last_deadline(self) -> float | None:
        return self._last_deadline

    def _candidate(self, callback_id: str) -> float:
        return (
            self._first_send[callback_id] + self.lease_duration_seconds - self.safety_margin_seconds
        )
