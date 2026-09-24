import asyncio
import logging
import sys
import threading
import time
from pathlib import Path

import pytest

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.worker.agent import WorkerAgent
from nexa.worker.client import WorkerApiError, WorkerTransportError
from nexa.worker.main import WorkerConfig, _credential, _replay_pending, main, run
from nexa.worker.state import PendingOperationStore


def _config(tmp_path: Path) -> WorkerConfig:
    return WorkerConfig(
        api_url="https://api.test",
        installation_id=str(new_uuid7()),
        worker_id=str(new_uuid7()),
        fingerprint="sha256:" + "a" * 64,
        state_root=tmp_path / "state",
        bootstrap_secret_file=tmp_path / "bootstrap-secret",
    )


def test_bootstrap_validates_secret_before_intent_and_retains_uncertain_send(
    tmp_path, monkeypatch
) -> None:
    config = _config(tmp_path)
    with pytest.raises(FileNotFoundError):
        _credential(config, bootstrap=True)
    assert not (config.state_root / "bootstrap-intent.json").exists()

    config.bootstrap_secret_file.write_text("x" * 32 + "\nembedded\n", encoding="ascii")
    with pytest.raises(ValueError, match="bootstrap secret"):
        _credential(config, bootstrap=True)
    assert not (config.state_root / "bootstrap-intent.json").exists()

    config.bootstrap_secret_file.write_text("s" * 32 + "\n", encoding="ascii")

    class TimeoutClient:
        def __init__(self, _url: str) -> None:
            pass

        def bootstrap(self, **_kwargs):
            raise WorkerTransportError("worker API transport unavailable")

        def close(self) -> None:
            pass

    monkeypatch.setattr("nexa.worker.main.WorkerApiClient", TimeoutClient)
    with pytest.raises(WorkerTransportError):
        _credential(config, bootstrap=True)
    assert (config.state_root / "bootstrap-intent.json").exists()


def test_pending_operations_replay_exact_identity_and_first_send(tmp_path) -> None:
    config = _config(tmp_path)
    state = PendingOperationStore(config.state_root / "agent-state.json", boot_id="boot")
    callback_id = str(new_uuid7())
    nonce = str(new_uuid7())
    state.begin(callback_id, operation="create_incarnation", payload={"nonce": nonce})
    first_send = state.first_send(callback_id)
    calls = []

    class Client:
        def create_incarnation(self, worker_id: str, *, nonce: str, key: str) -> dict:
            calls.append((worker_id, nonce, key))
            return {
                "worker_id": worker_id,
                "worker_incarnation_id": str(new_uuid7()),
                "process_start_nonce": nonce,
            }

    _replay_pending(config, Client(), state)
    assert calls == [(config.worker_id, nonce, callback_id)]
    assert state.operations == {}
    recovered = PendingOperationStore(config.state_root / "agent-state.json", boot_id="boot")
    assert recovered.operations == {}
    assert first_send > 0


def test_main_returns_unavailable_and_never_logs_transport_secret(
    tmp_path, monkeypatch, caplog
) -> None:
    config = _config(tmp_path)
    secret = "raw-bootstrap-secret-must-not-appear"
    monkeypatch.setattr("nexa.worker.main.WorkerConfig.from_environment", lambda: config)
    monkeypatch.setattr(
        "nexa.worker.main.run",
        lambda _config, *, bootstrap: (_ for _ in ()).throw(
            WorkerTransportError("worker API transport unavailable")
        ),
    )
    monkeypatch.setattr(logging.getLogger("nexa.worker"), "disabled", False)
    monkeypatch.setattr(sys, "argv", ["nexa-worker", "--bootstrap"])
    with caplog.at_level(logging.ERROR, logger="nexa.worker"):
        assert main() == 75
    assert secret not in caplog.text
    assert "worker API transport unavailable" in caplog.text


def test_periodic_reconciliation_preserves_prior_ready_until_scan_finishes() -> None:
    scanning = threading.Event()
    finish = threading.Event()

    class Client:
        def reconciliation(self, *_args, **_kwargs):
            scanning.set()
            assert finish.wait(2)
            return {"items": [], "page": {"next_cursor": None}}

    agent = WorkerAgent.for_test(Client(), worker_id="worker", incarnation_id="incarnation")
    agent._reconcile_complete = True
    agent._server_ready = True
    thread = threading.Thread(target=agent.reconcile_once)
    thread.start()
    try:
        assert scanning.wait(2)
        assert agent._reconcile_complete is True
        assert agent._server_ready is True
    finally:
        finish.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert agent._reconcile_complete is True


def test_background_loop_error_clears_reconciliation_readiness() -> None:
    agent = WorkerAgent.for_test(object(), worker_id="worker", incarnation_id="incarnation")
    agent._reconcile_complete = True

    def fail() -> None:
        agent.stop()
        raise RuntimeError("dependency failed")

    asyncio.run(agent._loop(fail, 0.01))

    assert agent._reconcile_complete is False


def test_poll_rejection_waits_for_next_ready_heartbeat_without_invalidating_reconcile() -> None:
    class Client:
        def poll(self, _worker_id, _incarnation_id):
            agent.stop()
            raise WorkerApiError(409, "state_conflict")

    agent = WorkerAgent.for_test(Client(), worker_id="worker", incarnation_id="incarnation")
    agent._reconcile_complete = True
    agent._server_ready = True

    asyncio.run(agent._loop(agent._poll_once, 0.0))

    assert agent._reconcile_complete is True
    assert agent._readiness_blocked is False
    assert agent._server_ready is False


def test_loop_converges_timed_out_thread_before_next_operation() -> None:
    agent = WorkerAgent.for_test(object(), worker_id="worker", incarnation_id="incarnation")
    agent.operation_timeout_seconds = 0.01
    agent._reconcile_complete = True
    active = 0
    peak_active = 0
    calls = 0
    lock = threading.Lock()
    started = threading.Event()

    def slow_operation() -> None:
        nonlocal active, peak_active, calls
        with lock:
            calls += 1
            active += 1
            peak_active = max(peak_active, active)
            started.set()
        time.sleep(0.03)
        with lock:
            active -= 1
            if calls >= 2:
                agent.stop()

    async def exercise() -> None:
        loop = asyncio.create_task(agent._loop(slow_operation, 0.0))
        assert await asyncio.to_thread(started.wait, 0.2)
        await asyncio.sleep(0.02)
        assert agent._reconcile_complete is False
        await loop

    asyncio.run(exercise())

    assert calls == 2
    assert peak_active == 1


def test_late_timed_out_reconciliation_cannot_advertise_ready() -> None:
    agent = WorkerAgent.for_test(object(), worker_id="worker", incarnation_id="incarnation")
    agent.operation_timeout_seconds = 0.01
    calls = 0

    def slow_reconcile() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            time.sleep(0.03)
            agent._reconcile_complete = True
        else:
            agent.stop()

    async def exercise() -> None:
        loop = asyncio.create_task(agent._loop(slow_reconcile, 0.0))
        while calls < 2:
            await asyncio.sleep(0.001)
        assert agent._reconcile_complete is False
        await loop

    asyncio.run(exercise())


@pytest.mark.parametrize("first_delay", [0.0, 0.03])
def test_docker_timeout_retries_after_thread_finishes_without_overlap(first_delay) -> None:
    class Client:
        def reconciliation(self, *_args, **_kwargs):
            return {"items": [], "page": {"next_cursor": None}}

    agent = WorkerAgent.for_test(Client(), worker_id="w", incarnation_id="i")
    agent.operation_timeout_seconds = 0.005
    calls = active = peak_active = 0

    class Docker:
        def find_by_labels(self, _labels, *, timeout_seconds):
            nonlocal calls, active, peak_active
            calls += 1
            active += 1
            peak_active = max(peak_active, active)
            try:
                if calls == 1:
                    time.sleep(first_delay)
                    raise TimeoutError("transient Docker timeout")
                agent.stop()
                return ()
            finally:
                active -= 1

    agent.docker = Docker()

    async def exercise():
        task = asyncio.create_task(agent._loop(agent.reconcile_once, 0.001))
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except TimeoutError:
            agent.stop()

    asyncio.run(exercise())
    assert calls == 2
    assert peak_active == 1
    assert active == 0
    assert agent._reconcile_complete is True
    assert agent._readiness_blocked is False


def test_malformed_container_labels_reset_readiness_without_killing_loop() -> None:
    class Client:
        def reconciliation(self, *_args, **_kwargs):
            return {"items": [], "page": {"next_cursor": None}}

    agent = WorkerAgent.for_test(Client(), worker_id="worker", incarnation_id="incarnation")
    agent._reconcile_complete = True

    class Docker:
        def find_by_labels(self, _labels, *, timeout_seconds):
            agent.stop()
            return ("container",)

        def inspect(self, _container_id, *, timeout_seconds):
            return type(
                "Inspection",
                (),
                {
                    "payload": {
                        "Id": "container",
                        "Config": {
                            "Labels": {
                                "nexa.managed": "true",
                                "nexa.installation_id": agent.installation_id,
                            }
                        },
                    }
                },
            )()

    agent.docker = Docker()
    asyncio.run(agent._loop(agent.reconcile_once, 0.0))

    assert agent._reconcile_complete is False


def test_replay_keeps_unverified_cleanup_pending_after_restart(tmp_path) -> None:
    config = _config(tmp_path)
    state = PendingOperationStore(config.state_root / "agent-state.json", boot_id="boot")
    callback_id = str(new_uuid7())
    state.begin(
        callback_id,
        operation="cleanup",
        payload={
            "attempt_id": str(new_uuid7()),
            "body": {"proof": {"proof_type": "CONTAINER_STOPPED"}},
        },
    )
    state.first_send(callback_id)

    class Client:
        def cleanup(self, _attempt_id, _callback_id, _body):
            return {"verified": False, "allocation_state": "QUARANTINED"}

    _replay_pending(config, Client(), state)

    assert callback_id in state.operations
    assert state.operations[callback_id]["acknowledgment"]["verified"] is False

    recovered = PendingOperationStore(config.state_root / "agent-state.json", boot_id="boot")
    _replay_pending(config, Client(), recovered)
    assert callback_id in recovered.operations


def test_run_starts_supervision_before_reconciliation(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path)
    config.state_root.mkdir(parents=True)
    monkeypatch.setenv("NEXA_CPU_IMAGE_REF", "registry.invalid/cpu")
    monkeypatch.setattr("nexa.worker.main.platform.system", lambda: "Linux")
    monkeypatch.setattr("nexa.worker.state.linux_boot_id", lambda: "boot")
    monkeypatch.setattr("nexa.worker.main._credential", lambda *_args, **_kwargs: "credential")
    monkeypatch.setattr("nexa.worker.main.live_provider", object)
    monkeypatch.setattr("nexa.worker.main.signal.signal", lambda *_args: None)

    class Client:
        def __init__(self, *_args):
            pass

        def create_incarnation(self, worker_id, *, nonce, key):
            return {
                "worker_id": worker_id,
                "worker_incarnation_id": str(new_uuid7()),
                "process_start_nonce": nonce,
            }

        def close(self):
            pass

    class Agent:
        def __init__(self, **_kwargs):
            pass

        def reconcile_once(self):
            raise AssertionError("startup must not reconcile before supervision starts")

        async def run(self):
            return None

        def stop(self):
            pass

    monkeypatch.setattr("nexa.worker.main.WorkerApiClient", Client)
    monkeypatch.setattr("nexa.worker.main.DockerCli", object)

    def capture_executor(*_args, **kwargs):
        assert kwargs["staging_root"] == config.state_root / "staging"
        return object()

    monkeypatch.setattr("nexa.worker.main.DockerExecutor", capture_executor)
    monkeypatch.setattr("nexa.worker.main.WorkerAgent", Agent)

    assert run(config) == 0


def test_heartbeat_replays_uncertain_callback_before_sampling_new_inventory(tmp_path) -> None:
    worker_id = str(new_uuid7())
    incarnation_id = str(new_uuid7())
    callback_id = str(new_uuid7())
    body = {
        "worker_incarnation_id": incarnation_id,
        "observed_health": "STARTING",
        "reconcile_complete": False,
        "inventory": {"exact": "persisted"},
        "observed_containers": [],
    }
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    state.begin(callback_id, operation="heartbeat", payload=body)
    first_send = state.first_send(callback_id)
    calls = []

    class Client:
        def heartbeat(self, request_worker_id: str, request_callback_id: str, payload: dict):
            calls.append((request_worker_id, request_callback_id, payload))
            return {"accepted_incarnation_id": incarnation_id}

    class Provider:
        def discover(self):
            raise AssertionError("pending heartbeat must replay before new inventory discovery")

    agent = WorkerAgent(
        worker_id=worker_id,
        incarnation_id=incarnation_id,
        installation_id=str(new_uuid7()),
        client=Client(),
        state=state,
        journal=object(),
        docker=object(),
        provider=Provider(),
    )

    response = agent.heartbeat_once()

    assert response == {"accepted_incarnation_id": incarnation_id}
    assert calls == [(worker_id, callback_id, body)]
    assert state.operations == {}
    assert first_send > 0
