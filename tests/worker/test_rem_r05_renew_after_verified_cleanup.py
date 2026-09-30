"""REM-R05: a renewal never starts for an attempt whose cleanup the server verified.

The result thread proves a paused attempt's container stopped and sends its cleanup under
the attempt's journal lock (B15-R39). The verified cleanup ends the lease, discards the
attempt's pending renewals (B15-R09) and only then drops the attempt from the adopted set.
A renewal scan that listed the attempt and waited on that lock must not renew it: the
server rejects the renewal with 409 ``stale_authority`` and, because only a verified
cleanup discards a renewal, the pending renewal would block worker readiness for good.
The interleaving is forced with events, without sleeps.
"""

import threading

from nexa.worker.client import WorkerApiError
from tests.worker.test_rem_b15_r39_resolution_lock import (
    GUARD_SECONDS,
    GatedPeer,
    _running_attempt,
    _thread,
)


class RenewingPeer(GatedPeer):
    """The control plane after the verified cleanup: the lease is revoked."""

    def renew(self, attempt_id: str, callback_id: str, body: dict) -> dict:
        self.calls.append(("renew", callback_id))
        raise WorkerApiError(409, "stale_authority")


def test_a_renewal_waiting_on_a_verified_cleanup_does_not_renew_the_released_attempt(
    tmp_path,
) -> None:
    peer = RenewingPeer("cleanup")
    request, _identity, journal, state, agent = _running_attempt(tmp_path, peer)
    attempt_id = request.context.authority.attempt_id
    agent._adopted[attempt_id] = request.context.authority
    outcome: dict[str, object] = {}
    renewal: list[threading.Thread] = []
    waiting = threading.Event()

    def note_lock(locked_attempt: str) -> None:
        if renewal and threading.current_thread() is renewal[0] and locked_attempt == attempt_id:
            waiting.set()

    journal.on_lock = note_lock
    # The result thread: the workload stopped for PAUSE; prove it stopped and release.
    paused = _thread(lambda: agent._paused(attempt_id, journal.load(attempt_id)), outcome, "paused")
    paused.start()
    assert peer.entered.wait(GUARD_SECONDS)
    # The cleanup POST is in flight under the lock; the scan lists the attempt and waits.
    renewal.append(_thread(agent.renew_once, outcome, "renew"))
    renewal[0].start()
    assert waiting.wait(GUARD_SECONDS)
    peer.release.set()
    paused.join(GUARD_SECONDS)
    renewal[0].join(GUARD_SECONDS)
    assert not paused.is_alive() and not renewal[0].is_alive()

    assert not isinstance(outcome["paused"], BaseException), outcome["paused"]
    assert [operation for operation, _ in peer.calls] == ["cleanup"]
    assert state.operations == {}
    assert not isinstance(outcome["renew"], BaseException), outcome["renew"]
    assert journal.load(attempt_id).runner_state["cleanup_verified"] is not None
    assert attempt_id not in agent._adopted
    assert agent._blocking_pending_attempts() == []
    assert agent.reconcile_once().complete
