import asyncio
import json
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

import pytest

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.worker.agent import WorkerAgent
from nexa.worker.client import WorkerApiError, WorkerTransportError
from nexa.worker.docker_client import DockerCli
from nexa.worker.executor import DockerExecutor, runtime_identity_digest
from nexa.worker.journal import ExecutionJournal
from nexa.worker.models import Authority, ContainerIdentity, ResourceVector
from nexa.worker.state import PendingOperationStore


@dataclass
class PageClient:
    pages: list[dict | Exception]

    def __post_init__(self) -> None:
        self.calls: list[str | None] = []

    def reconciliation(self, _worker_id: str, _incarnation_id: str, *, cursor=None) -> dict:
        self.calls.append(cursor)
        result = self.pages[len(self.calls) - 1]
        if isinstance(result, Exception):
            raise result
        return result


def test_reconciliation_drains_more_than_one_hundred_rows() -> None:
    client = PageClient(
        [
            {
                "items": [{"attempt_id": str(index)} for index in range(100)],
                "page": {"next_cursor": "c1"},
            },
            {"items": [{"attempt_id": "100"}], "page": {"next_cursor": None}},
        ]
    )
    agent = WorkerAgent.for_test(client, worker_id="w", incarnation_id="i")

    result = agent.reconcile_once()

    assert result.complete is True
    assert result.items_seen == 101
    assert client.calls == [None, "c1"]


def test_reconciliation_restarts_from_first_page_after_snapshot_conflict() -> None:
    client = PageClient(
        [
            {"items": [], "page": {"next_cursor": "stale"}},
            WorkerApiError(409, "state_conflict"),
            {"items": [], "page": {"next_cursor": None}},
        ]
    )
    agent = WorkerAgent.for_test(client, worker_id="w", incarnation_id="i")

    assert agent.reconcile_once().complete is True
    assert client.calls == [None, "stale", None]


class FakeDocker:
    def __init__(self, payload: dict):
        self.payload = payload
        self.stopped: list[str] = []
        self.removed: list[str] = []

    def find_by_labels(self, labels, *, timeout_seconds: float):
        assert labels == {
            "nexa.managed": "true",
            "nexa.installation_id": self.payload["Config"]["Labels"]["nexa.installation_id"],
        }
        return (self.payload["Id"],)

    def inspect(self, container_id: str, *, timeout_seconds: float):
        assert container_id == self.payload["Id"]
        return type("Inspection", (), {"payload": self.payload})()

    def stop(self, container_id: str, *, timeout_seconds: float):
        self.stopped.append(container_id)

    def remove(self, container_id: str, *, timeout_seconds: float):
        self.removed.append(container_id)


class AckChannel:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.sequence = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def settimeout(self, _timeout):
        pass

    def sendall(self, payload: bytes):
        self.sequence = json.loads(payload[4:])["control_sequence"]

    def recv(self, _maximum: int):
        if self.fail:
            raise TimeoutError("lost runner acknowledgment")
        raw = json.dumps(
            {
                "schema_version": 1,
                "ack_sequence": self.sequence,
                "accepted": True,
                "code": "ACCEPTED",
            },
            separators=(",", ":"),
        ).encode()
        return struct.pack("!I", len(raw)) + raw


class AdoptionClient:
    def __init__(self, page: dict, current: Authority):
        self.page = page
        self.current = current
        self.adopt_calls = 0
        self.renew_calls = 0
        self.adopt_bodies: list[dict] = []

    def reconciliation(self, *_args, **_kwargs):
        return self.page

    def adopt(self, attempt_id: str, callback_id: str, body: dict):
        self.adopt_calls += 1
        self.adopt_bodies.append(body)
        assert attempt_id == self.current.attempt_id
        return {
            "callback_id": callback_id,
            "accepted": True,
            "authority": asdict(self.current),
            "transferred_checkpoint_reservation": None,
            "transferred_result_reservation": None,
            "lease_duration_seconds": 45,
            "renew_interval_seconds": 5,
            "safety_margin_seconds": 5,
        }

    def renew(self, attempt_id: str, callback_id: str, body: dict):
        self.renew_calls += 1
        assert attempt_id == self.current.attempt_id
        assert body["authority"] == asdict(self.current)
        return {
            "server_time": "2026-09-21T00:00:00Z",
            "lease_expires_at": "2026-09-21T00:00:45Z",
            "desired_state": "RUNNING",
            "renew_interval_seconds": 5,
            "safety_margin_seconds": 5,
        }


class CleanupPeer:
    def __init__(self, page: dict, *, cleanup_verified: bool = True):
        self.page = page
        self.cleanup_verified = cleanup_verified
        self.cleanup_calls: list[tuple[str, str, dict]] = []

    def reconciliation(self, *_args, **_kwargs):
        return self.page

    def cleanup(self, attempt_id: str, callback_id: str, body: dict):
        self.cleanup_calls.append((attempt_id, callback_id, body))
        return {
            "callback_id": callback_id,
            "verified": self.cleanup_verified,
            "allocation_state": "RELEASED" if self.cleanup_verified else "QUARANTINED",
            "server_time": "2026-09-21T00:00:00Z",
        }


def _adoption_fixture(tmp_path, *, architecture: str = "linux/amd64"):
    worker_id = str(new_uuid7())
    prior_incarnation = str(new_uuid7())
    current_incarnation = str(new_uuid7())
    attempt_id = str(new_uuid7())
    allocation_id = str(new_uuid7())
    lease_id = str(new_uuid7())
    nonce = str(new_uuid7())
    installation_id = str(new_uuid7())
    prior = Authority(
        worker_id=worker_id,
        worker_incarnation_id=prior_incarnation,
        attempt_id=attempt_id,
        allocation_id=allocation_id,
        lease_id=lease_id,
        job_fence=1,
    )
    current = Authority(
        worker_id=worker_id,
        worker_incarnation_id=current_incarnation,
        attempt_id=attempt_id,
        allocation_id=allocation_id,
        lease_id=lease_id,
        job_fence=1,
    )
    container_id = "a" * 64
    payload = {
        "Id": container_id,
        "Image": "sha256:" + "b" * 64,
        "Config": {
            "Image": "worker@sha256:" + "c" * 64,
            "User": "1000:1000",
            "Labels": {
                "nexa.managed": "true",
                "nexa.installation_id": installation_id,
                "nexa.attempt_id": attempt_id,
                "nexa.allocation_id": allocation_id,
                "nexa.startup_nonce": nonce,
            },
        },
        "HostConfig": {},
    }
    identity = ContainerIdentity(
        container_id=container_id,
        runtime_identity_digest=runtime_identity_digest(payload),
        attempt_id=attempt_id,
        allocation_id=allocation_id,
        startup_nonce=nonce,
        architecture=architecture,
        image_id=payload["Image"],
    )
    journal = ExecutionJournal(tmp_path / "journal")
    prepared = journal.prepare(
        attempt_id=attempt_id,
        allocation_id=allocation_id,
        startup_nonce=nonce,
        authority=prior,
        resources=ResourceVector(1000, 128 * 1024**2, 0),
        image_digest="sha256:" + "c" * 64,
        execution_binding={},
    )
    in_flight = journal.begin_create(attempt_id, expected_sequence=prepared.operation_sequence)
    journal.bind_created(attempt_id, identity)
    assert in_flight.state == "CREATE_IN_FLIGHT"
    item = {
        "authority": asdict(prior),
        "authority_state": "LIVE",
        "lease_expires_at": "2026-09-21T00:00:45Z",
        "desired_state": "RUNNING",
        "allocation": {
            "allocation_id": allocation_id,
            "tenant_id": str(new_uuid7()),
            "job_id": str(new_uuid7()),
            "attempt_id": attempt_id,
            "worker_id": worker_id,
            "resources": {
                "cpu_millis": 1000,
                "memory_bytes": 128 * 1024**2,
                "gpu_count": 0,
            },
            "gpu_uuids": [],
            "state": "HELD",
            "held_at": "2026-09-21T00:00:00Z",
            "quarantined_at": None,
            "released_at": None,
        },
        "startup_nonce": nonce,
        "claim_state": "STARTED",
        "expected_container": {
            "container_id": container_id,
            "runtime_identity_digest": identity.runtime_identity_digest,
        },
    }
    page = {"items": [item], "page": {"next_cursor": None}}
    return worker_id, current_incarnation, installation_id, current, payload, journal, page


def test_exact_live_container_is_adopted_then_renewed_while_starting(tmp_path) -> None:
    worker, incarnation, installation, current, payload, journal, page = _adoption_fixture(tmp_path)
    client = AdoptionClient(page, current)
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    agent = WorkerAgent(
        worker_id=worker,
        incarnation_id=incarnation,
        installation_id=installation,
        client=client,
        state=state,
        journal=journal,
        docker=FakeDocker(payload),
        provider=object(),
        channel_factory=lambda _container: AckChannel(),
    )

    result = agent.reconcile_once()
    agent.renew_once()

    assert result.complete is True
    assert result.adopted_attempts == (current.attempt_id,)
    assert journal.load(current.attempt_id).authority == current
    assert client.adopt_calls == 1
    assert client.renew_calls == 1
    assert state.operations == {}


def test_exact_arm64_container_does_not_fail_identity_reconciliation(tmp_path) -> None:
    worker, incarnation, installation, current, payload, journal, page = _adoption_fixture(
        tmp_path,
        architecture="linux/arm64",
    )
    client = AdoptionClient(page, current)
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    docker = FakeDocker(payload)
    agent = WorkerAgent(
        worker_id=worker,
        incarnation_id=incarnation,
        installation_id=installation,
        client=client,
        state=state,
        journal=journal,
        docker=docker,
        provider=object(),
        channel_factory=lambda _container: AckChannel(),
    )

    result = agent.reconcile_once()

    assert result.complete is True
    assert client.adopt_calls == 1
    assert docker.stopped == []
    assert docker.removed == []


def test_adoption_ack_remains_pending_until_runner_ack_then_replays_exactly(tmp_path) -> None:
    worker, incarnation, installation, current, payload, journal, page = _adoption_fixture(tmp_path)
    client = AdoptionClient(page, current)
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    outcomes = iter((True, False))
    agent = WorkerAgent(
        worker_id=worker,
        incarnation_id=incarnation,
        installation_id=installation,
        client=client,
        state=state,
        journal=journal,
        docker=FakeDocker(payload),
        provider=object(),
        channel_factory=lambda _container: AckChannel(fail=next(outcomes)),
    )

    first = agent.reconcile_once()
    second = agent.reconcile_once()

    assert first.complete is False
    assert second.complete is True
    assert client.adopt_calls == 1
    assert state.operations == {}


def test_reconciliation_does_not_adopt_current_authority_twice(tmp_path) -> None:
    worker, incarnation, installation, current, payload, journal, page = _adoption_fixture(tmp_path)

    class MovingPageClient(AdoptionClient):
        def adopt(self, attempt_id: str, callback_id: str, body: dict):
            response = super().adopt(attempt_id, callback_id, body)
            self.page["items"][0]["authority"] = asdict(self.current)
            return response

    client = MovingPageClient(page, current)
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    agent = WorkerAgent(
        worker_id=worker,
        incarnation_id=incarnation,
        installation_id=installation,
        client=client,
        state=state,
        journal=journal,
        docker=FakeDocker(payload),
        provider=object(),
        channel_factory=lambda _container: AckChannel(),
    )

    assert agent.reconcile_once().complete is True
    assert agent.reconcile_once().complete is True
    assert client.adopt_calls == 1
    assert state.operations == {}


def test_new_incarnation_supersedes_old_pending_adoption_and_adopts_again(tmp_path) -> None:
    worker, old_incarnation, installation, old_current, payload, journal, page = _adoption_fixture(
        tmp_path
    )
    prior = journal.load(old_current.attempt_id).authority
    old_callback = str(new_uuid7())
    old_body = {
        "prior_authority": asdict(prior),
        "current_worker_incarnation_id": old_incarnation,
        "container": page["items"][0]["expected_container"],
    }
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    state.begin(
        old_callback,
        operation="adopt",
        payload={"attempt_id": old_current.attempt_id, "body": old_body},
    )
    state.first_send(old_callback)
    state.acknowledge(
        old_callback,
        {
            "callback_id": old_callback,
            "accepted": True,
            "authority": asdict(old_current),
            "transferred_checkpoint_reservation": None,
            "transferred_result_reservation": None,
            "lease_duration_seconds": 45,
            "renew_interval_seconds": 5,
            "safety_margin_seconds": 5,
        },
        control_sequence=1,
    )
    journal.rebind_authority(
        old_current.attempt_id,
        prior_authority=prior,
        current_authority=old_current,
    )
    page["items"][0]["authority"] = asdict(old_current)
    new_incarnation = str(new_uuid7())
    new_current = Authority(
        worker_id=old_current.worker_id,
        worker_incarnation_id=new_incarnation,
        attempt_id=old_current.attempt_id,
        allocation_id=old_current.allocation_id,
        lease_id=old_current.lease_id,
        job_fence=old_current.job_fence,
    )
    client = AdoptionClient(page, new_current)
    agent = WorkerAgent(
        worker_id=worker,
        incarnation_id=new_incarnation,
        installation_id=installation,
        client=client,
        state=state,
        journal=journal,
        docker=FakeDocker(payload),
        provider=object(),
        channel_factory=lambda _container: AckChannel(),
    )

    result = agent.reconcile_once()

    assert result.complete is True
    assert client.adopt_calls == 1
    assert client.adopt_bodies[0]["prior_authority"] == asdict(old_current)
    assert journal.load(old_current.attempt_id).authority == new_current
    assert state.operations == {}


def test_pending_renewal_blocks_a_second_renewal_until_runner_ack(tmp_path) -> None:
    worker, incarnation, installation, current, payload, journal, page = _adoption_fixture(tmp_path)
    client = AdoptionClient(page, current)
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    agent = WorkerAgent(
        worker_id=worker,
        incarnation_id=incarnation,
        installation_id=installation,
        client=client,
        state=state,
        journal=journal,
        docker=FakeDocker(payload),
        provider=object(),
        channel_factory=lambda _container: AckChannel(),
    )
    assert agent.reconcile_once().complete is True
    agent.channel_factory = lambda _container: AckChannel(fail=True)

    agent.renew_once()
    agent.renew_once()

    assert client.renew_calls == 1
    assert len(state.operations) == 1


def test_create_in_flight_without_container_stays_unresolved(tmp_path) -> None:
    worker, incarnation, installation, current, payload, _journal, page = _adoption_fixture(
        tmp_path / "fixture"
    )
    authority = Authority(**page["items"][0]["authority"])
    item = page["items"][0]
    journal = ExecutionJournal(tmp_path / "unknown")
    prepared = journal.prepare(
        attempt_id=current.attempt_id,
        allocation_id=current.allocation_id,
        startup_nonce=item["startup_nonce"],
        authority=authority,
        resources=ResourceVector(1000, 128 * 1024**2, 0),
        image_digest="sha256:" + "c" * 64,
        execution_binding={},
    )
    journal.begin_create(current.attempt_id, expected_sequence=prepared.operation_sequence)
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    agent = WorkerAgent(
        worker_id=worker,
        incarnation_id=incarnation,
        installation_id=installation,
        client=AdoptionClient(page, current),
        state=state,
        journal=journal,
        docker=FakeDocker(payload),
        provider=object(),
    )

    result = agent.reconcile_once()

    assert result.complete is False
    assert result.unresolved_attempts == (current.attempt_id,)
    assert journal.load(current.attempt_id).state == "CREATE_IN_FLIGHT"
    assert state.operations == {}


class CleanupBackend:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.running = True
        self.removed = False
        self.stopped: list[str] = []

    def run(self, argv: tuple[str, ...], _timeout_seconds: float):
        container_id = self.payload["Id"]
        if argv[1] == "ps":
            return 0, b"" if self.removed else container_id.encode(), b""
        if argv[1] == "inspect":
            if self.removed:
                return 1, b"", f"Error: No such object: {container_id}".encode()
            inspected = json.loads(json.dumps(self.payload))
            inspected["State"] = {
                "Running": self.running,
                "Status": "running" if self.running else "exited",
                "ExitCode": 0,
                "OOMKilled": False,
            }
            return 0, json.dumps([inspected]).encode(), b""
        if argv[1] == "stop":
            self.running = False
            self.stopped.append(container_id)
            return 0, b"", b""
        if argv[1] == "rm":
            self.removed = True
            return 0, b"", b""
        raise AssertionError(argv)


def test_orphan_cleanup_uses_executor_proof_and_blocks_ready_until_verified(tmp_path) -> None:
    worker, incarnation, installation, current, payload, journal, _page = _adoption_fixture(
        tmp_path, architecture="linux/arm64"
    )
    client = CleanupPeer({"items": [], "page": {"next_cursor": None}}, cleanup_verified=False)
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    discovery = FakeDocker(payload)
    backend = CleanupBackend(payload)
    executor = DockerExecutor(
        journal,
        DockerCli(backend),
        image_ref="registry.invalid/cpu",
        installation_id=installation,
    )
    agent = WorkerAgent(
        worker_id=worker,
        incarnation_id=incarnation,
        installation_id=installation,
        client=client,
        state=state,
        journal=journal,
        docker=discovery,
        executor=executor,
        provider=object(),
    )

    result = agent.reconcile_once()

    record = journal.load(current.attempt_id)
    assert result.complete is False
    assert result.unresolved_attempts == (current.attempt_id,)
    assert record.state == "TOMBSTONED"
    assert discovery.stopped == []
    assert discovery.removed == []
    assert backend.stopped == [payload["Id"]]
    assert len(state.operations) == 1
    proof = client.cleanup_calls[0][2]["proof"]
    assert proof == {
        "proof_type": "CONTAINER_STOPPED",
        "startup_nonce": record.startup_nonce,
        "executor_operation_sequence": record.operation_sequence,
        "container": {
            "container_id": payload["Id"],
            "runtime_identity_digest": runtime_identity_digest(payload),
        },
        "stopped_at": record.stopped_at,
        "exit_code": 0,
        "inspection_checksum": record.inspection_checksum,
    }


@pytest.mark.parametrize("crash_point", [None, "after_remove", "before_pending"])
@pytest.mark.parametrize("initial_verified", [False, True])
def test_revoked_cleanup_converges_from_journal_without_stale_failure(
    tmp_path, monkeypatch, crash_point, initial_verified
) -> None:
    worker, incarnation, installation, current, payload, journal, page = _adoption_fixture(tmp_path)
    page["items"][0]["authority_state"] = "REVOKED"
    page["items"][0]["allocation"]["state"] = "QUARANTINED"
    backend = CleanupBackend(payload)

    class Peer(CleanupPeer):
        failure_calls = 0

        def fail(self, *_args):
            self.failure_calls += 1
            raise WorkerApiError(409, "stale_authority")

        def cleanup(self, attempt_id, callback_id, body):
            assert backend.removed
            assert body["attempt_id"] == current.attempt_id
            assert body["allocation_id"] == current.allocation_id
            assert body["job_fence"] == current.job_fence
            assert body["proof"]["proof_type"] == "CONTAINER_STOPPED"
            assert body["proof"]["container"] == page["items"][0]["expected_container"]
            return super().cleanup(attempt_id, callback_id, body)

    client = Peer(page, cleanup_verified=initial_verified)

    def new_agent():
        recovered_journal = ExecutionJournal(journal.root)
        docker = DockerCli(backend)
        return WorkerAgent(
            worker_id=worker,
            incarnation_id=incarnation,
            installation_id=installation,
            client=client,
            state=PendingOperationStore(tmp_path / "state.json", boot_id="boot"),
            journal=recovered_journal,
            docker=docker,
            executor=DockerExecutor(recovered_journal, docker, image_ref="registry.invalid/cpu"),
            provider=object(),
        )

    class Crash(BaseException):
        pass

    agent = new_agent()
    if crash_point is not None:
        with monkeypatch.context() as patch:
            if crash_point == "after_remove":

                def crash_finish(*_args, **_kwargs):
                    raise Crash()

                patch.setattr(agent.journal, "finish_cleanup", crash_finish)
            else:
                original_begin = agent.state.begin

                def crash_begin(callback_id, *, operation, payload):
                    if operation == "cleanup":
                        raise Crash()
                    return original_begin(callback_id, operation=operation, payload=payload)

                patch.setattr(agent.state, "begin", crash_begin)
            with pytest.raises(Crash):
                agent.reconcile_once()
        assert backend.removed
        assert agent.state.operations == {}
        assert not client.cleanup_calls
        assert journal.load(current.attempt_id).state == (
            "CLEANUP_IN_FLIGHT" if crash_point == "after_remove" else "TOMBSTONED"
        )
        agent = new_agent()

    result = agent.reconcile_once()
    assert client.failure_calls == 0
    if initial_verified:
        assert result.complete is True
        assert agent._reconcile_complete is True
        assert agent.state.operations == {}
        assert backend.stopped == [payload["Id"]]
        assert journal.load(current.attempt_id).state == "TOMBSTONED"
        assert agent._containers == {}
        return
    assert result.complete is False
    assert agent._reconcile_complete is False
    assert len(client.cleanup_calls) >= 1
    assert len(agent.state.operations) == 1
    callback_id = next(iter(agent.state.operations))
    assert agent.state.operations[callback_id]["acknowledgment"]["verified"] is False

    # Restart again with an unverified callback; reuse its durable identity.
    agent = new_agent()
    client.cleanup_verified = True
    result = agent.reconcile_once()
    assert result.complete is True
    assert agent._reconcile_complete is True
    assert agent.state.operations == {}
    assert {call[1] for call in client.cleanup_calls} == {callback_id}
    assert backend.stopped == [payload["Id"]]
    assert journal.load(current.attempt_id).state == "TOMBSTONED"
    assert agent._containers == {}


@pytest.mark.parametrize("mismatch", ["expected_digest", "missing_proof", "observed_digest"])
def test_revoked_cleanup_requires_exact_identity_and_durable_stopped_evidence(tmp_path, mismatch):
    worker, incarnation, installation, current, payload, journal, page = _adoption_fixture(tmp_path)
    page["items"][0]["authority_state"] = "REVOKED"
    backend = CleanupBackend(payload)
    docker = DockerCli(backend)
    executor = DockerExecutor(journal, docker, image_ref="registry.invalid/cpu")
    if mismatch == "missing_proof":
        backend.removed = True
    else:
        executor.cleanup(journal.load(current.attempt_id).container)
        if mismatch == "expected_digest":
            page["items"][0]["expected_container"]["runtime_identity_digest"] = "sha256:" + "f" * 64
        else:
            backend.removed = False
            backend.payload["Config"]["Labels"]["nexa.startup_nonce"] = str(new_uuid7())
    client = CleanupPeer(page)
    agent = WorkerAgent(
        worker_id=worker,
        incarnation_id=incarnation,
        installation_id=installation,
        client=client,
        state=PendingOperationStore(tmp_path / "state.json", boot_id="boot"),
        journal=journal,
        docker=docker,
        executor=executor,
        provider=object(),
    )
    assert agent.reconcile_once().complete is False
    assert agent._reconcile_complete is False
    assert client.cleanup_calls == []
    assert agent.state.operations == {}


def test_deadline_and_runner_message_preserve_each_others_state(tmp_path, monkeypatch) -> None:
    worker, incarnation, installation, current, payload, journal, page = _adoption_fixture(tmp_path)
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    agent = WorkerAgent(
        worker_id=worker,
        incarnation_id=incarnation,
        installation_id=installation,
        client=AdoptionClient(page, current),
        state=state,
        journal=journal,
        docker=FakeDocker(payload),
        provider=object(),
        channel_factory=lambda _container: AckChannel(),
    )
    append = journal.append_runner_state
    stale_write_barrier = threading.Barrier(2)

    def append_after_both_read(attempt_id, runner_state):
        stale_write_barrier.wait()
        return append(attempt_id, runner_state)

    monkeypatch.setattr(journal, "append_runner_state", append_after_both_read)
    callback_id = str(new_uuid7())
    outcomes: dict[str, object] = {}

    def deadline() -> None:
        outcomes["deadline"] = agent._apply_deadline(
            current.attempt_id,
            journal.load(current.attempt_id).container,
            callback_id,
            time.monotonic_ns(),
            {"lease_duration_seconds": 45, "safety_margin_seconds": 5},
            1,
        )

    def progress() -> None:
        outcomes["progress"] = agent._record_runner_message(
            current.attempt_id,
            {
                "schema_version": 1,
                "message_sequence": 1,
                "type": "PROGRESS",
                "payload": {
                    "progress_sequence": 1,
                    "fraction": 0.5,
                    "step": 1,
                    "epoch": None,
                    "item_cursor": None,
                },
            },
        )

    threads = [threading.Thread(target=deadline), threading.Thread(target=progress)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    runner_state = journal.load(current.attempt_id).runner_state
    assert outcomes == {"deadline": True, "progress": "ACCEPTED"}
    assert runner_state is not None
    assert runner_state["last_control_sequence"] == 1
    assert runner_state["latest_progress"]["progress_sequence"] == 1


def test_missing_container_does_not_emit_invalid_container_observation(tmp_path) -> None:
    worker_id = str(new_uuid7())
    attempt_id = str(new_uuid7())
    allocation_id = str(new_uuid7())
    authority = Authority(
        worker_id=worker_id,
        worker_incarnation_id=str(new_uuid7()),
        attempt_id=attempt_id,
        allocation_id=allocation_id,
        lease_id=str(new_uuid7()),
        job_fence=1,
    )
    item = {
        "authority": asdict(authority),
        "authority_state": "LIVE",
        "lease_expires_at": "2026-09-21T00:00:45Z",
        "desired_state": "RUNNING",
        "allocation": {
            "allocation_id": allocation_id,
            "tenant_id": str(new_uuid7()),
            "job_id": str(new_uuid7()),
            "attempt_id": attempt_id,
            "worker_id": worker_id,
            "resources": {"cpu_millis": 1000, "memory_bytes": 128 * 1024**2, "gpu_count": 0},
            "gpu_uuids": [],
            "state": "HELD",
            "held_at": "2026-09-21T00:00:00Z",
            "quarantined_at": None,
            "released_at": None,
        },
        "startup_nonce": str(new_uuid7()),
        "claim_state": "CLAIMED",
        "expected_container": None,
    }

    class MissingContainerPeer:
        def __init__(self) -> None:
            self.fail_calls = 0

        def reconciliation(self, *_args, **_kwargs):
            return {"items": [item], "page": {"next_cursor": None}}

        def fail(self, *_args, **_kwargs):
            self.fail_calls += 1
            raise AssertionError("CONTAINER observation cannot contain null")

    peer = MissingContainerPeer()
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    docker = type("EmptyDocker", (), {"find_by_labels": lambda *_args, **_kwargs: ()})()
    agent = WorkerAgent(
        worker_id=worker_id,
        incarnation_id=str(new_uuid7()),
        installation_id=str(new_uuid7()),
        client=peer,
        state=state,
        journal=ExecutionJournal(tmp_path / "journal"),
        docker=docker,
        provider=object(),
    )

    result = agent.reconcile_once()

    assert result.complete is False
    assert result.unresolved_attempts == (attempt_id,)
    assert peer.fail_calls == 0
    assert state.operations == {}


def test_fresh_unclaimed_offer_keeps_current_worker_reconciliation_ready(tmp_path) -> None:
    worker_id = str(new_uuid7())
    incarnation_id = str(new_uuid7())
    authority = Authority(
        worker_id,
        incarnation_id,
        str(new_uuid7()),
        str(new_uuid7()),
        str(new_uuid7()),
        1,
    )
    item = {
        "authority": asdict(authority),
        "authority_state": "LIVE",
        "claim_state": "UNCLAIMED",
        "expected_container": None,
        "startup_nonce": str(new_uuid7()),
    }
    agent = WorkerAgent(
        worker_id=worker_id,
        incarnation_id=incarnation_id,
        installation_id=str(new_uuid7()),
        client=PageClient([{"items": [item], "page": {"next_cursor": None}}]),
        state=PendingOperationStore(tmp_path / "state.json", boot_id="boot"),
        journal=ExecutionJournal(tmp_path / "journal"),
        docker=type("EmptyDocker", (), {"find_by_labels": lambda *_args, **_kwargs: ()})(),
        provider=object(),
    )

    assert agent.reconcile_once().complete is True


def test_pending_claim_replays_only_after_identity_and_docker_scan(tmp_path) -> None:
    worker_id, incarnation_id = str(new_uuid7()), str(new_uuid7())
    authority = Authority(
        worker_id,
        incarnation_id,
        str(new_uuid7()),
        str(new_uuid7()),
        str(new_uuid7()),
        1,
    )
    row = {
        "authority": asdict(authority),
        "authority_state": "LIVE",
        "claim_state": "CLAIMED",
        "expected_container": None,
        "startup_nonce": str(new_uuid7()),
    }

    class Peer:
        claim_calls = 0

        def reconciliation(self, *_args, **_kwargs):
            return {"items": [row], "page": {"next_cursor": None}}

        def claim(self, attempt_id, callback_id, body):
            assert attempt_id == authority.attempt_id
            assert body == {"authority": asdict(authority)}
            self.claim_calls += 1
            raise WorkerTransportError("post-commit claim response lost")

    peer = Peer()
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    callback_id = str(new_uuid7())
    state.begin(
        callback_id,
        operation="claim",
        payload={"attempt_id": authority.attempt_id, "body": {"authority": asdict(authority)}},
    )
    state.first_send(callback_id)
    agent = WorkerAgent(
        worker_id=worker_id,
        incarnation_id=incarnation_id,
        installation_id=str(new_uuid7()),
        client=peer,
        state=state,
        journal=ExecutionJournal(tmp_path / "journal"),
        docker=type("EmptyDocker", (), {"find_by_labels": lambda *_args, **_kwargs: ()})(),
        executor=object(),
        provider=object(),
    )

    agent._poll_once()
    assert peer.claim_calls == 0
    assert agent.reconcile_once().complete is False
    with pytest.raises(WorkerTransportError):
        agent._poll_once()
    assert peer.claim_calls == 1
    assert list(agent.state.operations) == [callback_id]


def test_reconciliation_does_not_stop_container_started_after_page_snapshot(tmp_path) -> None:
    worker, _next, installation, current, payload, prior_journal, _page = _adoption_fixture(
        tmp_path
    )
    previous = prior_journal.load(current.attempt_id)
    authority = previous.authority
    journal = ExecutionJournal(tmp_path / "late-journal")
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    callback_id = str(new_uuid7())
    state.begin(
        callback_id,
        operation="claim",
        payload={
            "attempt_id": authority.attempt_id,
            "body": {"authority": asdict(authority)},
        },
    )
    backend = CleanupBackend(payload)
    backend.removed = True
    snapshot_taken = threading.Event()
    launch_finished = threading.Event()

    class SnapshotPeer(CleanupPeer):
        def reconciliation(self, *_args, **_kwargs):
            snapshot_taken.set()
            assert launch_finished.wait(5), "dispatch did not finish after the snapshot"
            return self.page

    client = SnapshotPeer({"items": [], "page": {"next_cursor": None}})
    docker = DockerCli(backend)
    agent = WorkerAgent(
        worker_id=worker,
        incarnation_id=authority.worker_incarnation_id,
        installation_id=installation,
        client=client,
        state=state,
        journal=journal,
        docker=docker,
        executor=DockerExecutor(journal, docker, image_ref="registry.invalid/cpu"),
        provider=object(),
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        scan = pool.submit(agent.reconcile_once)
        assert snapshot_taken.wait(5)
        try:
            prepared = journal.prepare(
                attempt_id=authority.attempt_id,
                allocation_id=authority.allocation_id,
                startup_nonce=previous.startup_nonce,
                authority=authority,
                resources=previous.resources,
                image_digest=previous.image_digest,
                execution_binding=previous.execution_binding,
            )
            journal.begin_create(
                authority.attempt_id, expected_sequence=prepared.operation_sequence
            )
            created = journal.bind_created(authority.attempt_id, previous.container)
            starting = journal.begin_start(
                authority.attempt_id, expected_sequence=created.operation_sequence
            )
            journal.mark_started(
                authority.attempt_id, expected_sequence=starting.operation_sequence
            )
            backend.removed = False
        finally:
            launch_finished.set()
        result = scan.result(timeout=5)

    assert result.complete is False
    assert backend.removed is False
    assert client.cleanup_calls == []
    assert journal.load(authority.attempt_id).state == "STARTED"


def test_terminal_result_restart_cleans_with_original_grant_lineage(tmp_path) -> None:
    worker, incarnation, installation, current, payload, journal, page = _adoption_fixture(tmp_path)
    original = journal.load(current.attempt_id).authority
    assert original.worker_incarnation_id != incarnation
    page["items"][0]["authority_state"] = "REVOKED"
    page["items"][0]["allocation"]["state"] = "HELD"
    backend = CleanupBackend(payload)
    client = CleanupPeer(page, cleanup_verified=True)
    recovered = ExecutionJournal(journal.root)
    docker = DockerCli(backend)
    agent = WorkerAgent(
        worker_id=worker,
        incarnation_id=incarnation,
        installation_id=installation,
        client=client,
        state=PendingOperationStore(tmp_path / "new-state.json", boot_id="boot"),
        journal=recovered,
        docker=docker,
        executor=DockerExecutor(recovered, docker, image_ref="registry.invalid/cpu"),
        provider=object(),
    )

    result = agent.reconcile_once()

    assert result.complete is True
    assert backend.removed is True
    assert client.cleanup_calls[0][2]["worker_incarnation_id"] == original.worker_incarnation_id


@pytest.mark.parametrize("renewal_acknowledged", [False, True])
def test_terminal_restart_discards_pending_renewal_after_durable_completion(
    tmp_path, renewal_acknowledged
) -> None:
    worker, incarnation, installation, current, payload, journal, page = _adoption_fixture(tmp_path)
    original = journal.load(current.attempt_id).authority
    page["items"][0]["authority_state"] = "REVOKED"
    completion_ack = {"accepted": True, "job_state": "SUCCEEDED"}
    journal.update_runner_state(
        current.attempt_id,
        lambda state: {
            **state,
            "result_flow": {"completed": True, "completion_ack": completion_ack},
        },
    )
    pending_path = tmp_path / "pending.json"
    state = PendingOperationStore(pending_path, boot_id="same-boot")
    callback = str(new_uuid7())
    state.begin(
        callback,
        operation="renew",
        payload={
            "attempt_id": current.attempt_id,
            "body": {"authority": asdict(original), "progress_sequence": 0, "progress": None},
        },
    )
    state.first_send(callback)
    if renewal_acknowledged:
        state.acknowledge(callback, {"callback_id": callback, "accepted": True})
    client = CleanupPeer(page, cleanup_verified=True)
    recovered = ExecutionJournal(journal.root)
    docker = DockerCli(CleanupBackend(payload))
    agent = WorkerAgent(
        worker_id=worker,
        incarnation_id=incarnation,
        installation_id=installation,
        client=client,
        state=PendingOperationStore(pending_path, boot_id="same-boot"),
        journal=recovered,
        docker=docker,
        executor=DockerExecutor(recovered, docker, image_ref="registry.invalid/cpu"),
        provider=object(),
    )

    result = agent.reconcile_once()

    assert result.complete is True
    assert client.cleanup_calls[0][2]["worker_incarnation_id"] == original.worker_incarnation_id
    assert agent.state.operations == {}


@pytest.mark.parametrize("receipt_matches", [True, False])
def test_restart_recovers_lost_completion_ack_only_for_matching_result_receipt(
    tmp_path, receipt_matches
) -> None:
    worker, incarnation, installation, current, payload, journal, page = _adoption_fixture(tmp_path)
    prior = journal.load(current.attempt_id).authority
    page["items"][0]["authority_state"] = "REVOKED"
    callback_id, result_id, manifest_id = (str(new_uuid7()) for _ in range(3))
    acknowledgment = {
        "callback_id": callback_id,
        "accepted": True,
        "job_state": "SUCCEEDED",
        "job_version": 4,
        "server_time": "2026-09-21T00:00:00Z",
    }
    page["items"][0]["completion_receipt"] = {
        "callback_id": callback_id if receipt_matches else str(new_uuid7()),
        "result_id": result_id,
        "manifest_artifact_id": manifest_id,
        "acknowledgment": acknowledgment,
    }
    journal.update_runner_state(
        current.attempt_id,
        lambda state: {
            **state,
            "result_flow": {
                "completion_callback_id": callback_id,
                "reservation": {"result_id": result_id},
                "bindings": {
                    "manifest-key": {"artifact_id": manifest_id, "kind": "RESULT_MANIFEST"}
                },
            },
        },
    )
    state = PendingOperationStore(tmp_path / "pending.json", boot_id="boot")
    renewal = str(new_uuid7())
    state.begin(
        renewal,
        operation="renew",
        payload={
            "attempt_id": current.attempt_id,
            "body": {"authority": asdict(prior), "progress_sequence": 0, "progress": None},
        },
    )
    state.first_send(renewal)
    client = CleanupPeer(page)
    recovered = ExecutionJournal(journal.root)
    docker = DockerCli(CleanupBackend(payload))
    agent = WorkerAgent(
        worker_id=worker,
        incarnation_id=incarnation,
        installation_id=installation,
        client=client,
        state=PendingOperationStore(state.path, boot_id="boot"),
        journal=recovered,
        docker=docker,
        executor=DockerExecutor(recovered, docker, image_ref="registry.invalid/cpu"),
        provider=object(),
    )

    result = agent.reconcile_once()

    if receipt_matches:
        assert result.complete is True
        assert agent.state.operations == {}
        assert (
            recovered.load(current.attempt_id).runner_state["result_flow"]["completion_ack"]
            == acknowledgment
        )
        assert len(client.cleanup_calls) == 1
    else:
        assert result.complete is False
        assert renewal in agent.state.operations
        assert not client.cleanup_calls


def test_revoked_unclaimed_row_creates_sequence_one_tombstone_and_cleanup_peer(tmp_path) -> None:
    worker_id = str(new_uuid7())
    incarnation_id = str(new_uuid7())
    attempt_id = str(new_uuid7())
    allocation_id = str(new_uuid7())
    authority = Authority(
        worker_id=worker_id,
        worker_incarnation_id=str(new_uuid7()),
        attempt_id=attempt_id,
        allocation_id=allocation_id,
        lease_id=str(new_uuid7()),
        job_fence=2,
    )
    item = {
        "authority": asdict(authority),
        "authority_state": "REVOKED",
        "lease_expires_at": "2026-09-21T00:00:00Z",
        "desired_state": "RUNNING",
        "allocation": {
            "allocation_id": allocation_id,
            "tenant_id": str(new_uuid7()),
            "job_id": str(new_uuid7()),
            "attempt_id": attempt_id,
            "worker_id": worker_id,
            "resources": {"cpu_millis": 1000, "memory_bytes": 128 * 1024**2, "gpu_count": 0},
            "gpu_uuids": [],
            "state": "QUARANTINED",
            "held_at": "2026-09-21T00:00:00Z",
            "quarantined_at": "2026-09-21T00:00:00Z",
            "released_at": None,
        },
        "startup_nonce": str(new_uuid7()),
        "claim_state": "UNCLAIMED",
        "expected_container": None,
    }
    page = {"items": [item], "page": {"next_cursor": None}}
    client = CleanupPeer(page)
    journal = ExecutionJournal(tmp_path / "journal")
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    docker = type(
        "EmptyDocker",
        (),
        {"find_by_labels": lambda *_args, **_kwargs: ()},
    )()
    agent = WorkerAgent(
        worker_id=worker_id,
        incarnation_id=incarnation_id,
        installation_id=str(new_uuid7()),
        client=client,
        state=state,
        journal=journal,
        docker=docker,
        provider=object(),
    )

    result = agent.reconcile_once()

    record = journal.load(attempt_id)
    assert result.complete is True
    assert record.state == "TOMBSTONED"
    assert record.operation_sequence == record.tombstone_sequence == 1
    assert client.cleanup_calls[0][2]["proof"]["proof_type"] == "NO_CONTAINER"
    assert state.operations == {}


def test_slow_reconciliation_does_not_block_heartbeat_or_renewal_loop() -> None:
    client = PageClient([])
    agent = WorkerAgent.for_test(client, worker_id="w", incarnation_id="i")
    agent.loop_intervals = {name: 0.01 for name in agent.loop_intervals}
    heartbeats = 0
    renewals = 0

    def heartbeat():
        nonlocal heartbeats
        heartbeats += 1
        if heartbeats == 3:
            agent.stop()

    def slow_reconcile():
        time.sleep(0.2)

    def renew():
        nonlocal renewals
        renewals += 1

    agent.heartbeat_once = heartbeat
    agent.renew_once = renew
    agent.reconcile_once = slow_reconcile
    agent._poll_once = lambda: None
    agent._ipc_once = lambda: None

    asyncio.run(agent.run())

    assert heartbeats >= 3
    assert renewals >= 3


def test_live_attempt_renews_during_scan_longer_than_lease(tmp_path) -> None:
    worker, incarnation, installation, current, payload, journal, page = _adoption_fixture(tmp_path)
    advanced = threading.Condition()
    clock_seconds = 0
    expires_at = 45
    scanning = threading.Event()
    finished = threading.Event()

    class LeaseClient(AdoptionClient):
        def renew(self, *args):
            nonlocal expires_at
            with advanced:
                assert clock_seconds < expires_at
                response = super().renew(*args)
                expires_at = clock_seconds + 45
                advanced.notify_all()
                return response

    client = LeaseClient(page, current)

    class SlowScanDocker(FakeDocker):
        def find_by_labels(self, labels, *, timeout_seconds):
            nonlocal clock_seconds
            scanning.set()
            try:
                for _ in range(10):
                    with advanced:
                        clock_seconds += 5
                        before = client.renew_calls
                        assert advanced.wait_for(
                            lambda count=before: client.renew_calls > count, timeout=1
                        )
                        assert clock_seconds < expires_at
                return super().find_by_labels(labels, timeout_seconds=timeout_seconds)
            finally:
                finished.set()
                agent.stop()

    agent = WorkerAgent(
        worker_id=worker,
        incarnation_id=incarnation,
        installation_id=installation,
        client=client,
        state=PendingOperationStore(tmp_path / "state.json", boot_id="boot"),
        journal=journal,
        docker=SlowScanDocker(payload),
        provider=object(),
        channel_factory=lambda _container: AckChannel(),
    )
    agent.loop_intervals = {name: 0.005 for name in agent.loop_intervals}
    agent.heartbeat_once = lambda: None
    agent._ipc_once = lambda: None

    asyncio.run(agent.run())

    assert scanning.is_set() and finished.is_set()
    assert clock_seconds == 50
    assert client.adopt_calls == 1
    assert client.renew_calls >= 10
    assert journal.load(current.attempt_id).authority == current
    assert agent.state.operations == {}


def test_old_incarnation_claim_without_container_blocks_new_readiness(tmp_path) -> None:
    worker = str(new_uuid7())
    prior = str(new_uuid7())
    current = str(new_uuid7())
    attempt = str(new_uuid7())
    state = PendingOperationStore(tmp_path / "claim-state.json", boot_id="boot")
    state.begin(
        str(new_uuid7()),
        operation="claim",
        payload={
            "attempt_id": attempt,
            "body": {
                "authority": asdict(
                    Authority(worker, prior, attempt, str(new_uuid7()), str(new_uuid7()), 1)
                )
            },
        },
    )
    agent = WorkerAgent.for_test(
        PageClient([{"items": [], "page": {"next_cursor": None}}]),
        worker_id=worker,
        incarnation_id=current,
    )
    agent.state = state

    result = agent.reconcile_once()

    assert result.complete is False
    assert result.unresolved_attempts == (attempt,)
    assert agent._reconcile_complete is False
