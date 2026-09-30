"""REM-R06: no worker path acts again on an attempt whose cleanup the server verified.

A verified cleanup ends the attempt's lease and releases its allocation; the server accepts
no renewal, failure or claim for it any more. On VPS1 (B15 C1) a reconciliation scan
fetched its page while the paused attempt was still LIVE, inspected the container, then
waited on the attempt's journal lock while the result thread removed the container and
had the cleanup verified. The scan then re-adopted the released attempt: it failed with
``adopted container changed``, and the IPC and result loops kept failing on the removed
container, which held the worker out of READY until the next offer's lease expired.

Every interleaving is forced with events, without sleeps.
"""

import threading
from dataclasses import asdict

from nexa.infrastructure.persistence.ids import new_uuid7
from tests.worker.test_rem_b11_h01_fenced_claim import LabelDocker
from tests.worker.test_rem_b15_r39_resolution_lock import (
    GUARD_SECONDS,
    GatedPeer,
    _running_attempt,
    _thread,
)


class StopGatedDocker(LabelDocker):
    """Blocks the first ``docker stop`` until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.stopping = threading.Event()
        self.release_stop = threading.Event()

    def run(self, argv, timeout_seconds):
        if argv[1] == "stop" and not self.stopping.is_set():
            self.stopping.set()
            assert self.release_stop.wait(GUARD_SECONDS)
        return super().run(argv, timeout_seconds)


class ReconcilingPeer(GatedPeer):
    """Serves one page listing the attempt LIVE, as fetched before the cleanup."""

    def __init__(self) -> None:
        super().__init__("none")
        self.page = {"items": [], "page": {"next_cursor": None}}

    def reconciliation(self, *_args, **_kwargs) -> dict:
        return self.page

    def claim(self, attempt_id: str, callback_id: str, body: dict) -> dict:
        self.calls.append(("claim", callback_id))
        raise AssertionError("a released attempt must not be claimed")


def _live_item(request, identity) -> dict:
    authority = request.context.authority
    return {
        "authority": asdict(authority),
        "authority_state": "LIVE",
        "lease_expires_at": "2026-09-29T00:00:45Z",
        "desired_state": "PAUSED",
        "allocation": {
            "allocation_id": authority.allocation_id,
            "tenant_id": str(new_uuid7()),
            "job_id": str(new_uuid7()),
            "attempt_id": authority.attempt_id,
            "worker_id": authority.worker_id,
            "resources": asdict(request.context.resources),
            "gpu_uuids": [],
            "state": "HELD",
            "held_at": "2026-09-29T00:00:00Z",
            "quarantined_at": None,
            "released_at": None,
        },
        "startup_nonce": request.startup_nonce,
        "claim_state": "STARTED",
        "expected_container": {
            "container_id": identity.container_id,
            "runtime_identity_digest": identity.runtime_identity_digest,
        },
    }


def _paused_attempt(tmp_path, peer, monkeypatch, docker=None):
    """A running attempt of this incarnation with a runner deadline, pausing."""
    if docker is not None:
        monkeypatch.setattr(
            "tests.worker.test_rem_b15_r39_resolution_lock.LabelDocker", lambda: docker
        )
    request, identity, journal, state, agent = _running_attempt(tmp_path, peer)
    attempt_id = request.context.authority.attempt_id
    agent._adopted[attempt_id] = request.context.authority
    journal.update_runner_state(
        attempt_id,
        lambda local: {
            **local,
            "authority_deadline_monotonic_ns": 1,
            "control_desired": "PAUSED",
        },
    )
    return request, identity, journal, state, agent, attempt_id


def _verified(journal, attempt_id) -> bool:
    return journal.load(attempt_id).runner_state.get("cleanup_verified") is not None


def test_a_scan_waiting_on_a_verified_cleanup_does_not_readopt_the_attempt(
    tmp_path, monkeypatch
) -> None:
    docker = StopGatedDocker()
    peer = ReconcilingPeer()
    request, identity, journal, state, agent, attempt_id = _paused_attempt(
        tmp_path, peer, monkeypatch, docker
    )
    peer.page["items"] = [_live_item(request, identity)]
    outcome: dict[str, object] = {}
    scan: list[threading.Thread] = []
    waiting = threading.Event()

    def note_lock(locked_attempt: str) -> None:
        if scan and threading.current_thread() is scan[0] and locked_attempt == attempt_id:
            waiting.set()

    journal.on_lock = note_lock
    # The result thread proves the paused workload stopped; its docker stop is held.
    paused = _thread(lambda: agent._paused(attempt_id, journal.load(attempt_id)), outcome, "paused")
    paused.start()
    assert docker.stopping.wait(GUARD_SECONDS)
    # The scan lists the attempt LIVE, inspects the still-present container and waits.
    scan.append(_thread(agent.reconcile_once, outcome, "scan"))
    scan[0].start()
    assert waiting.wait(GUARD_SECONDS)
    docker.release_stop.set()
    paused.join(GUARD_SECONDS)
    scan[0].join(GUARD_SECONDS)
    assert not paused.is_alive() and not scan[0].is_alive()

    assert not isinstance(outcome["paused"], BaseException), outcome["paused"]
    assert _verified(journal, attempt_id)
    assert not isinstance(outcome["scan"], BaseException), outcome["scan"]
    assert outcome["scan"].complete
    assert outcome["scan"].adopted_attempts == ()
    assert attempt_id not in agent._adopted
    assert [operation for operation, _ in peer.calls] == ["cleanup"]
    assert state.operations == {}
    assert agent._blocking_pending_attempts() == []


def _released(tmp_path, monkeypatch):
    """An attempt whose cleanup was verified, still in a stale adopted snapshot."""
    peer = ReconcilingPeer()
    request, identity, journal, state, agent, attempt_id = _paused_attempt(
        tmp_path, peer, monkeypatch
    )
    agent._paused(attempt_id, journal.load(attempt_id))
    assert _verified(journal, attempt_id) and attempt_id not in agent._adopted
    agent._adopted[attempt_id] = request.context.authority
    return request, journal, state, agent, attempt_id, peer


def test_no_failure_is_sent_for_a_verified_attempt(tmp_path, monkeypatch) -> None:
    _request, _journal, state, agent, attempt_id, peer = _released(tmp_path, monkeypatch)

    released = agent._execution_failed(
        attempt_id, failure_class="INFRASTRUCTURE", reason_code="RUNNER_UNAVAILABLE"
    )

    assert released is True
    assert [operation for operation, _ in peer.calls] == ["cleanup"]
    assert state.operations == {}
    assert attempt_id not in agent._adopted


def test_the_ipc_and_result_loops_drop_a_verified_attempt(tmp_path, monkeypatch) -> None:
    _request, _journal, state, agent, attempt_id, peer = _released(tmp_path, monkeypatch)

    agent._ipc_once()
    assert attempt_id not in agent._adopted
    agent._adopted[attempt_id] = _request.context.authority
    agent._result_once()

    assert attempt_id not in agent._adopted
    assert [operation for operation, _ in peer.calls] == ["cleanup"]
    assert state.operations == {}
    assert agent.reconcile_once().complete


def test_an_offer_for_a_verified_attempt_is_not_claimed(tmp_path, monkeypatch) -> None:
    request, _journal, state, agent, attempt_id, peer = _released(tmp_path, monkeypatch)
    agent._adopted.pop(attempt_id)

    agent._dispatch_offer({"authority": asdict(request.context.authority)})

    assert [operation for operation, _ in peer.calls] == ["cleanup"]
    assert state.operations == {}
    assert attempt_id not in agent._adopted
