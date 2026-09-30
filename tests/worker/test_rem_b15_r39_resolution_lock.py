"""Remediation B15-R39/B15-OBS-01: one attempt's callbacks and journal are serialized.

The reconciliation loop replays pending failure/cleanup callbacks while the result thread may
be sending the same callback. Each test interleaves the two senders deterministically with
events (no sleeps) and checks that one callback is POSTed once, no sender sees a vanished
operation (``KeyError``) and the journal agrees with the operation store afterwards.
"""

import threading
from dataclasses import asdict

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.worker.agent import WorkerAgent
from nexa.worker.client import WorkerTransportError
from nexa.worker.docker_client import DockerCli
from nexa.worker.executor import DockerExecutor
from nexa.worker.journal import ExecutionJournal
from nexa.worker.state import PendingOperationStore
from tests.worker.test_docker_config import start
from tests.worker.test_rem_b11_h01_fenced_claim import INSTALLATION_ID, LabelDocker

GUARD_SECONDS = 10


class ObservedJournal(ExecutionJournal):
    """Reports which threads asked for an attempt lock, before they may block on it."""

    def __init__(self, root) -> None:
        super().__init__(root)
        self.on_lock = None

    def lock(self, attempt_id: str):
        if self.on_lock is not None:
            self.on_lock(attempt_id)
        return super().lock(attempt_id)


class GatedPeer:
    """A control plane whose first matching callback blocks until the test releases it."""

    def __init__(self, gated: str) -> None:
        self.gated = gated
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[tuple[str, str]] = []
        self.fail_transport_once = False

    def reconciliation(self, *_args, **_kwargs) -> dict:
        return {"items": [], "page": {"next_cursor": None}}

    def _call(self, operation: str, callback_id: str) -> None:
        self.calls.append((operation, callback_id))
        if operation == self.gated and not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(GUARD_SECONDS)

    def cleanup(self, attempt_id: str, callback_id: str, body: dict) -> dict:
        self._call("cleanup", callback_id)
        return {"callback_id": callback_id, "verified": True, "allocation_state": "RELEASED"}

    def fail(self, attempt_id: str, callback_id: str, body: dict) -> dict:
        if self.fail_transport_once:
            self.fail_transport_once = False
            self.calls.append(("failure-lost", callback_id))
            raise WorkerTransportError("failure response lost")
        self._call("failure", callback_id)
        return {"callback_id": callback_id, "accepted": True}


def _running_attempt(tmp_path, peer: GatedPeer):
    request = start()
    docker = LabelDocker()
    journal = ObservedJournal(tmp_path / "journal")
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    executor = DockerExecutor(
        journal, docker, image_ref="registry.invalid/cpu", installation_id=INSTALLATION_ID
    )
    identity = executor.start(executor.prepare(request))
    agent = WorkerAgent(
        worker_id=request.context.authority.worker_id,
        incarnation_id=request.context.authority.worker_incarnation_id,
        installation_id=INSTALLATION_ID,
        client=peer,
        state=state,
        journal=journal,
        docker=DockerCli(docker),
        executor=executor,
        provider=object(),
    )
    agent._adopted[request.context.authority.attempt_id] = asdict(request.context.authority)
    return request, identity, journal, state, agent


def _thread(target, outcome: dict, key: str) -> threading.Thread:
    """An unstarted thread recording ``target``'s result or exception under ``key``."""

    def body() -> None:
        try:
            outcome[key] = target()
        except BaseException as exc:  # recorded for the assertion, never swallowed
            outcome[key] = exc

    return threading.Thread(target=body)


def _replay_while_first_send_is_in_flight(journal, peer, agent, attempt_id, first):
    """Start ``first``; once its POST is in flight run a reconciliation replay beside it.

    The replay is released only after it either finished on its own or asked for the
    attempt lock, so both orders are forced deterministically.
    """
    outcome: dict[str, object] = {}
    progressed = threading.Condition()
    replay_thread: list[threading.Thread] = []

    def note_lock(locked_attempt: str) -> None:
        if (
            replay_thread
            and threading.current_thread() is replay_thread[0]
            and locked_attempt == attempt_id
        ):
            with progressed:
                outcome.setdefault("replay_asked_for_lock", True)
                progressed.notify_all()

    journal.on_lock = note_lock
    first_thread = _thread(first, outcome, "first")
    first_thread.start()
    assert peer.entered.wait(GUARD_SECONDS)

    def replay():
        try:
            return agent.reconcile_once()
        finally:
            with progressed:
                progressed.notify_all()

    # Registered before it starts, so its first lock request is always observed.
    replay_thread.append(_thread(replay, outcome, "replay"))
    replay_thread[0].start()
    with progressed:
        assert progressed.wait_for(
            lambda: "replay" in outcome or "replay_asked_for_lock" in outcome, GUARD_SECONDS
        )
    peer.release.set()
    first_thread.join(GUARD_SECONDS)
    replay_thread[0].join(GUARD_SECONDS)
    assert not first_thread.is_alive() and not replay_thread[0].is_alive()
    return outcome


def test_replay_waits_for_the_in_flight_cleanup_and_never_posts_it_twice(tmp_path) -> None:
    peer = GatedPeer("cleanup")
    request, identity, journal, state, agent = _running_attempt(tmp_path, peer)
    attempt_id = request.context.authority.attempt_id

    outcome = _replay_while_first_send_is_in_flight(
        journal, peer, agent, attempt_id, lambda: agent._stop_orphan(identity)
    )

    assert outcome["first"] is True
    assert not isinstance(outcome["replay"], BaseException), outcome["replay"]
    assert outcome["replay"].complete
    assert [operation for operation, _ in peer.calls] == ["cleanup"]
    assert state.operations == {}


def test_replay_of_an_in_flight_failure_is_serialized_with_the_result_thread(tmp_path) -> None:
    peer = GatedPeer("failure")
    request, identity, journal, state, agent = _running_attempt(tmp_path, peer)
    attempt_id = request.context.authority.attempt_id

    outcome = _replay_while_first_send_is_in_flight(
        journal,
        peer,
        agent,
        attempt_id,
        lambda: agent._execution_failed(
            attempt_id, failure_class="INFRASTRUCTURE", reason_code="RUNNER_UNAVAILABLE"
        ),
    )

    assert not isinstance(outcome["first"], BaseException), outcome["first"]
    assert not isinstance(outcome["replay"], BaseException), outcome["replay"]
    assert [operation for operation, _ in peer.calls] == ["failure", "cleanup"]
    assert state.operations == {}
    resolution = journal.load(attempt_id).runner_state["failure_resolution"]
    assert resolution["acknowledged"] is True


def test_a_failure_finished_by_the_replay_is_not_posted_again(tmp_path) -> None:
    peer = GatedPeer("none")
    peer.fail_transport_once = True
    request, identity, journal, state, agent = _running_attempt(tmp_path, peer)
    attempt_id = request.context.authority.attempt_id

    try:
        agent._execution_failed(
            attempt_id, failure_class="INFRASTRUCTURE", reason_code="RUNNER_UNAVAILABLE"
        )
    except WorkerTransportError:
        pass
    else:
        raise AssertionError("the first failure response must be lost")
    [(callback_id, pending)] = state.operations.items()
    assert pending["operation"] == "failure"

    # The reconciliation loop replays the pending failure before the result thread retries.
    assert agent._send_resolution(callback_id) is True
    assert journal.load(attempt_id).runner_state["failure_resolution"]["acknowledged"] is True

    assert agent._execution_failed(
        attempt_id, failure_class="INFRASTRUCTURE", reason_code="RUNNER_UNAVAILABLE"
    )
    assert [operation for operation, _ in peer.calls] == [
        "failure-lost",
        "failure",
        "cleanup",
    ]
    assert state.operations == {}


def test_a_resolution_that_vanished_under_the_lock_counts_as_resolved(tmp_path) -> None:
    peer = GatedPeer("none")
    request, identity, journal, state, agent = _running_attempt(tmp_path, peer)
    attempt_id = request.context.authority.attempt_id
    callback_id = str(new_uuid7())
    state.begin(
        callback_id,
        operation="cleanup",
        payload={"attempt_id": attempt_id, "body": {"attempt_id": attempt_id}},
    )
    state.first_send(callback_id)
    state.acknowledge(callback_id, {"verified": True, "allocation_state": "RELEASED"})

    def finish_while_waiting(locked_attempt: str) -> None:
        # Another sender finishes the callback while this one waits for the lock.
        journal.on_lock = None
        state.finish(callback_id)

    journal.on_lock = finish_while_waiting
    assert agent._send_resolution(callback_id) is True
    assert peer.calls == []
