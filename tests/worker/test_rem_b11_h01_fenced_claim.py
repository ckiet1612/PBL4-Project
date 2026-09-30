"""Remediation B11-H01: a worker that dies between the claim commit and ``/start`` recovers.

The server committed the claim (``claim_state=CLAIMED``) but never a container identity, and
the reaper, a cancel or a disable later fenced the attempt (``authority_state=REVOKED``). Each
test starts a new worker incarnation over the journal and agent state the dead process left in
one death window and checks that reconciliation proves the absence of any container with the
exact startup identity, tombstones the journal first, sends one ``NO_CONTAINER`` cleanup with
the original grant lineage and completes, so the worker can become READY again.
"""

import json
from dataclasses import asdict, replace

import pytest

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.worker.agent import WorkerAgent
from nexa.worker.client import WorkerTransportError
from nexa.worker.docker_client import DockerCli, DockerCommandBackend
from nexa.worker.errors import ExecutorError
from nexa.worker.executor import DockerExecutor
from nexa.worker.journal import ExecutionJournal, JournalWriteError
from nexa.worker.state import PendingOperationStore
from tests.worker.test_docker_config import start

INSTALLATION_ID = "018f0d60-7b6a-7a29-9d82-1aa39c4f3001"
NEVER_STARTED = "0001-01-01T00:00:00Z"


class LabelDocker(DockerCommandBackend):
    """A Docker daemon that stores containers and answers exact label filters."""

    def __init__(self) -> None:
        self.containers: dict[str, dict] = {}
        self.calls: list[str] = []
        self.unavailable = False
        self.fail_start = False
        self.after_ps = None

    def add(self, labels: dict[str, str], *, running: bool = False) -> str:
        container_id = f"{len(self.containers) + 1:064x}"
        self.containers[container_id] = {
            "labels": dict(labels),
            "status": "running" if running else "created",
            "started_at": "2026-09-28T00:00:00Z" if running else NEVER_STARTED,
        }
        return container_id

    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        verb = argv[1]
        self.calls.append(verb)
        if self.unavailable:
            raise RuntimeError("Docker daemon is unavailable")
        if verb == "create":
            labels = dict(
                argv[index + 1].split("=", 1)
                for index, value in enumerate(argv)
                if value == "--label"
            )
            return 0, (self.add(labels) + "\n").encode(), b""
        if verb == "ps":
            wanted = dict(
                argv[index + 1].removeprefix("label=").split("=", 1)
                for index, value in enumerate(argv)
                if value == "--filter"
            )
            found = [
                container_id
                for container_id, container in self.containers.items()
                if all(container["labels"].get(key) == value for key, value in wanted.items())
            ]
            if self.after_ps is not None:
                hook, self.after_ps = self.after_ps, None
                hook()
            return 0, "".join(f"{value}\n" for value in found).encode(), b""
        container_id = argv[-1]
        container = self.containers.get(container_id)
        if container is None:
            return 1, b"", f"Error: No such object: {container_id}".encode()
        if verb == "inspect":
            return 0, json.dumps([_inspection(container_id, container)]).encode(), b""
        if verb == "start":
            if self.fail_start:
                return 1, b"", b"start failed"
            container.update(status="running", started_at="2026-09-28T00:00:01Z")
            return 0, b"", b""
        if verb == "stop":
            container["status"] = "exited"
            return 0, b"", b""
        if verb == "rm":
            del self.containers[container_id]
            return 0, b"", b""
        return 0, b"", b""


def _inspection(container_id: str, container: dict) -> dict:
    return {
        "Id": container_id,
        "Image": "sha256:" + "a" * 64,
        "Config": {"Labels": container["labels"], "Image": "registry.invalid/cpu"},
        "State": {
            "Running": container["status"] == "running",
            "Status": container["status"],
            "StartedAt": container["started_at"],
            "ExitCode": 0,
            "OOMKilled": False,
        },
        "HostConfig": {"ReadonlyRootfs": True, "NetworkMode": "none"},
    }


class CleanupPeer:
    def __init__(self, page: dict) -> None:
        self.page = page
        self.cleanup_calls: list[tuple[str, str, dict]] = []

    def reconciliation(self, *_args, **_kwargs) -> dict:
        return self.page

    def cleanup(self, attempt_id: str, callback_id: str, body: dict) -> dict:
        self.cleanup_calls.append((attempt_id, callback_id, body))
        return {
            "callback_id": callback_id,
            "verified": True,
            "allocation_state": "RELEASED",
            "server_time": "2026-09-28T00:00:00Z",
        }


def _labels(request) -> dict[str, str]:
    authority = request.context.authority
    return {
        "nexa.managed": "true",
        "nexa.installation_id": INSTALLATION_ID,
        "nexa.attempt_id": authority.attempt_id,
        "nexa.allocation_id": authority.allocation_id,
        "nexa.startup_nonce": request.startup_nonce,
    }


def _revoked_claimed_item(request) -> dict:
    authority = request.context.authority
    resources = request.context.resources
    return {
        "authority": asdict(authority),
        "authority_state": "REVOKED",
        "lease_expires_at": "2026-09-28T00:00:00Z",
        "desired_state": "RUNNING",
        "allocation": {
            "allocation_id": authority.allocation_id,
            "tenant_id": str(new_uuid7()),
            "job_id": str(new_uuid7()),
            "attempt_id": authority.attempt_id,
            "worker_id": authority.worker_id,
            "resources": asdict(resources),
            "gpu_uuids": [],
            "state": "QUARANTINED",
            "held_at": "2026-09-28T00:00:00Z",
            "quarantined_at": "2026-09-28T00:00:00Z",
            "released_at": None,
        },
        "startup_nonce": request.startup_nonce,
        "claim_state": "CLAIMED",
        "expected_container": None,
    }


def _dead_worker(tmp_path, window: str, *, claim_acknowledged: bool = True):
    """Leave journal, agent state and Docker exactly as a worker killed in ``window`` would."""
    request = start()
    attempt_id = request.context.authority.attempt_id
    docker = LabelDocker()
    journal = ExecutionJournal(tmp_path / "journal")
    state = PendingOperationStore(tmp_path / "state.json", boot_id="boot")
    old = DockerExecutor(
        journal, docker, image_ref="registry.invalid/cpu", installation_id=INSTALLATION_ID
    )
    claim_callback = str(new_uuid7())
    state.begin(
        claim_callback,
        operation="claim",
        payload={
            "attempt_id": attempt_id,
            "body": {"authority": asdict(request.context.authority)},
        },
    )
    state.first_send(claim_callback)
    if claim_acknowledged:
        state.acknowledge(claim_callback, {"callback_id": claim_callback, "accepted": True})
    if window != "no-journal":
        prepared = old.prepare(request)
        record = journal.load(attempt_id)
        if window in {"create-in-flight", "create-landed"}:
            journal.begin_create(attempt_id, expected_sequence=record.operation_sequence)
            if window == "create-landed":
                docker.add(_labels(request))
        elif window == "created":
            docker.fail_start = True
            with pytest.raises(ExecutorError):
                old.start(prepared)
            docker.fail_start = False
        elif window == "started":
            old.start(prepared)
        elif window in {"cleanup-before-rm", "cleanup-after-rm"}:
            identity = old.start(prepared)
            record = journal.load(attempt_id)
            docker.containers[identity.container_id]["status"] = "exited"
            journal.begin_cleanup(
                attempt_id,
                expected_sequence=record.operation_sequence,
                inspection_checksum="sha256:" + "c" * 64,
                stopped_at="2026-09-28T00:00:02Z",
                exit_code=0,
            )
            if window == "cleanup-after-rm":
                del docker.containers[identity.container_id]
    return request, docker, journal, state


def _new_incarnation(tmp_path, request, docker, journal, state):
    client = CleanupPeer({"items": [_revoked_claimed_item(request)], "page": {"next_cursor": None}})
    agent = WorkerAgent(
        worker_id=request.context.authority.worker_id,
        incarnation_id=str(new_uuid7()),
        installation_id=INSTALLATION_ID,
        client=client,
        state=state,
        journal=journal,
        docker=DockerCli(docker),
        executor=DockerExecutor(
            journal, docker, image_ref="registry.invalid/cpu", installation_id=INSTALLATION_ID
        ),
        provider=object(),
    )
    return agent, client


@pytest.mark.parametrize(
    "window",
    [
        "no-journal",
        "prepared",
        "create-in-flight",
        "create-landed",
        "created",
        "started",
        "cleanup-before-rm",
        "cleanup-after-rm",
    ],
)
@pytest.mark.parametrize("claim_acknowledged", [True, False])
def test_new_incarnation_proves_no_container_and_releases_a_fenced_claim(
    tmp_path, window, claim_acknowledged
) -> None:
    request, docker, journal, state = _dead_worker(
        tmp_path, window, claim_acknowledged=claim_acknowledged
    )
    authority = request.context.authority
    agent, client = _new_incarnation(tmp_path, request, docker, journal, state)

    result = agent.reconcile_once()

    assert result.complete is True, result
    assert docker.containers == {}
    record = journal.load(authority.attempt_id)
    assert record.state == "TOMBSTONED"
    (cleanup,) = client.cleanup_calls
    body = cleanup[2]
    assert body["worker_incarnation_id"] == authority.worker_incarnation_id
    assert (body["attempt_id"], body["allocation_id"], body["job_fence"]) == (
        authority.attempt_id,
        authority.allocation_id,
        authority.job_fence,
    )
    proof = body["proof"]
    assert proof["proof_type"] == "NO_CONTAINER"
    assert proof["startup_nonce"] == request.startup_nonce
    assert proof["executor_operation_sequence"] == record.operation_sequence
    assert proof["tombstone_sequence"] == record.tombstone_sequence
    assert proof["tombstone_sequence"] >= proof["executor_operation_sequence"] >= 1
    assert state.operations == {}

    # The tombstone rejects any delayed create or start of the dead process's request.
    with pytest.raises(ExecutorError):
        agent.executor.start(replace(agent.executor.prepare(request)))
    assert docker.containers == {}


def test_a_replayed_page_rebuilds_the_same_proof_from_the_tombstone(tmp_path) -> None:
    request, docker, journal, state = _dead_worker(tmp_path, "create-in-flight")
    agent, client = _new_incarnation(tmp_path, request, docker, journal, state)

    assert agent.reconcile_once().complete is True
    assert agent.reconcile_once().complete is True

    # The second page still lists the row until the server stops reporting it; the
    # local tombstone replays the same sequences and the proof checksum is unchanged.
    first, second = (call[2]["proof"] for call in client.cleanup_calls)
    for key in ("executor_operation_sequence", "tombstone_sequence", "inspection_checksum"):
        assert first[key] == second[key]


@pytest.mark.parametrize("window", ["no-journal", "prepared", "create-in-flight", "created"])
def test_docker_unavailable_keeps_the_fenced_claim_unresolved(tmp_path, window) -> None:
    request, docker, journal, state = _dead_worker(tmp_path, window)
    agent, client = _new_incarnation(tmp_path, request, docker, journal, state)
    before = journal.load(request.context.authority.attempt_id) if window != "no-journal" else None
    docker.unavailable = True

    with pytest.raises(RuntimeError):
        agent.reconcile_once()

    docker.unavailable = False
    assert client.cleanup_calls == []
    if before is None:
        assert not journal.exists(request.context.authority.attempt_id)
    else:
        assert journal.load(request.context.authority.attempt_id) == before


def test_foreign_container_for_the_startup_identity_is_never_proven_absent(tmp_path) -> None:
    request, docker, journal, state = _dead_worker(tmp_path, "prepared")
    # A container with the startup labels cannot exist before CREATE_IN_FLIGHT.
    docker.add(_labels(request))
    agent, client = _new_incarnation(tmp_path, request, docker, journal, state)

    result = agent.reconcile_once()

    assert result.complete is False
    assert request.context.authority.attempt_id in result.unresolved_attempts
    assert client.cleanup_calls == []
    assert journal.load(request.context.authority.attempt_id).state == "PREPARED"
    assert len(docker.containers) == 1


def test_live_fenced_claim_waits_for_the_server_to_revoke_it(tmp_path) -> None:
    request, docker, journal, state = _dead_worker(tmp_path, "create-in-flight")
    agent, client = _new_incarnation(tmp_path, request, docker, journal, state)
    client.page["items"][0]["authority_state"] = "LIVE"

    result = agent.reconcile_once()

    assert result.complete is False
    assert client.cleanup_calls == []
    assert journal.load(request.context.authority.attempt_id).state == "CREATE_IN_FLIGHT"


def test_late_create_after_the_tombstone_is_removed_without_being_started(tmp_path) -> None:
    request, docker, journal, state = _dead_worker(tmp_path, "create-in-flight")
    agent, client = _new_incarnation(tmp_path, request, docker, journal, state)
    assert agent.reconcile_once().complete is True

    # The dead process's create reaches the daemon only after the proof was sent.
    late = docker.add(_labels(request))
    client.page = {"items": [], "page": {"next_cursor": None}}

    result = agent.reconcile_once()

    assert result.complete is True, result
    assert late not in docker.containers
    assert "start" not in docker.calls
    assert len(client.cleanup_calls) == 1


def test_late_create_that_was_started_fails_closed(tmp_path) -> None:
    request, docker, journal, state = _dead_worker(tmp_path, "create-in-flight")
    agent, client = _new_incarnation(tmp_path, request, docker, journal, state)
    assert agent.reconcile_once().complete is True
    late = docker.add(_labels(request), running=True)
    client.page = {"items": [], "page": {"next_cursor": None}}

    result = agent.reconcile_once()

    assert result.complete is False
    assert late in docker.containers


def test_create_landing_during_the_absence_scan_is_caught_by_the_second_scan(tmp_path) -> None:
    request, docker, journal, state = _dead_worker(tmp_path, "create-in-flight")
    agent, client = _new_incarnation(tmp_path, request, docker, journal, state)
    labels = _labels(request)
    scans = 0

    def land_on_the_attempt_scan() -> None:
        nonlocal scans
        scans += 1
        if scans == 2:
            docker.add(labels)
        else:
            docker.after_ps = land_on_the_attempt_scan

    # Scan 1 is the installation-wide discovery; scan 2 is the executor's first
    # exact-label scan, after which the delayed create lands.
    docker.after_ps = land_on_the_attempt_scan

    result = agent.reconcile_once()

    # The journal is tombstoned before the final scan, so nothing can start the
    # container that landed, and no proof is sent while Docker still has it.
    assert result.complete is False
    assert client.cleanup_calls == []
    assert journal.load(request.context.authority.attempt_id).state == "TOMBSTONED"
    assert len(docker.containers) == 1
    assert agent.reconcile_once().complete is True
    assert "start" not in docker.calls
    assert docker.containers == {}
    assert client.cleanup_calls[0][2]["proof"]["proof_type"] == "NO_CONTAINER"


def test_a_lost_cleanup_response_is_resumed_with_the_same_durable_callback(tmp_path) -> None:
    request, docker, journal, state = _dead_worker(tmp_path, "created")
    agent, client = _new_incarnation(tmp_path, request, docker, journal, state)
    answer = client.cleanup
    lost = True

    def cleanup(attempt_id, callback_id, body):
        nonlocal lost
        client.cleanup_calls.append((attempt_id, callback_id, body))
        if lost:
            lost = False
            raise WorkerTransportError("response lost")
        client.cleanup_calls.pop()
        return answer(attempt_id, callback_id, body)

    client.cleanup = cleanup
    with pytest.raises(WorkerTransportError):
        agent.reconcile_once()

    assert agent.reconcile_once().complete is True
    first, second = client.cleanup_calls
    assert first[1] == second[1]
    assert first[2] == second[2]
    assert state.operations == {}


def test_a_journal_for_another_startup_identity_is_never_tombstoned(tmp_path) -> None:
    request, docker, journal, state = _dead_worker(tmp_path, "create-in-flight")
    agent, client = _new_incarnation(tmp_path, request, docker, journal, state)
    client.page["items"][0]["startup_nonce"] = str(new_uuid7())

    result = agent.reconcile_once()

    assert result.complete is False
    assert client.cleanup_calls == []
    assert journal.load(request.context.authority.attempt_id).state == "CREATE_IN_FLIGHT"


def test_reconciliation_tombstone_claim_state_is_bounded(tmp_path) -> None:
    request = start()
    authority = request.context.authority
    journal = ExecutionJournal(tmp_path / "journal")
    arguments = {
        "attempt_id": authority.attempt_id,
        "allocation_id": authority.allocation_id,
        "startup_nonce": request.startup_nonce,
        "authority": authority,
        "resources": request.context.resources,
        "inspection_checksum": "sha256:" + "0" * 64,
        "reason": "REVOKED_CLAIMED_RECONCILIATION",
    }
    with pytest.raises(JournalWriteError):
        journal.create_reconciliation_tombstone(**arguments, claim_state="STARTED")
    record = journal.create_reconciliation_tombstone(**arguments, claim_state="CLAIMED")
    assert record.execution_binding == {"source": "reconciliation", "claim_state": "CLAIMED"}
    assert (record.state, record.operation_sequence, record.tombstone_sequence) == (
        "TOMBSTONED",
        1,
        1,
    )


def test_only_an_unbound_create_in_flight_can_be_abandoned(tmp_path) -> None:
    request, docker, journal, _ = _dead_worker(tmp_path, "prepared")
    attempt_id = request.context.authority.attempt_id
    prepared = journal.load(attempt_id)
    with pytest.raises(JournalWriteError):
        journal.tombstone_abandoned_create(
            attempt_id,
            expected_sequence=prepared.operation_sequence,
            reason="REVOKED_CLAIMED_RECONCILIATION",
            inspection_checksum="sha256:" + "0" * 64,
        )
    in_flight = journal.begin_create(attempt_id, expected_sequence=prepared.operation_sequence)
    with pytest.raises(JournalWriteError):
        journal.tombstone_abandoned_create(
            attempt_id,
            expected_sequence=prepared.operation_sequence,
            reason="REVOKED_CLAIMED_RECONCILIATION",
            inspection_checksum="sha256:" + "0" * 64,
        )
    tombstone = journal.tombstone_abandoned_create(
        attempt_id,
        expected_sequence=in_flight.operation_sequence,
        reason="REVOKED_CLAIMED_RECONCILIATION",
        inspection_checksum="sha256:" + "0" * 64,
    )
    assert tombstone.state == "TOMBSTONED"
    assert tombstone.operation_sequence == tombstone.tombstone_sequence
    assert tombstone.operation_sequence == in_flight.operation_sequence + 1
    with pytest.raises(JournalWriteError):
        journal.begin_create(attempt_id, expected_sequence=tombstone.operation_sequence)
