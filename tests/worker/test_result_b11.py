import hashlib
import io
import tarfile

import pytest

from nexa.worker.docker_client import DockerCli


def test_container_output_rejects_tar_links_and_mismatched_bytes():
    cli = DockerCli()
    assert hasattr(cli, "read_output"), "missing bounded exact-container output reader"


def test_descriptor_key_changes_with_descriptor_and_survives_incarnation():
    from nexa.worker import result_flow

    descriptor = {
        "staging_name": "result.json",
        "logical_name": "result.json",
        "kind": "RESULT_FILE",
        "media_type": "application/json",
        "size_bytes": 2,
        "checksum": "sha256:" + "a" * 64,
    }
    key = result_flow.descriptor_key("attempt", "callback", 3, descriptor)
    assert len(key) == 64
    assert key == result_flow.descriptor_key("attempt", "callback", 3, dict(descriptor))
    assert key != result_flow.descriptor_key("attempt", "callback", 4, descriptor)


@pytest.mark.parametrize("kind", ["file", "symlink", "extra", "traversal", "changed"])
def test_closed_output_tar_is_verified(kind):
    body = b"{}"
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        entry = tarfile.TarInfo("../result.json" if kind == "traversal" else "result.json")
        entry.size = len(body)
        if kind == "symlink":
            entry.type = tarfile.SYMTYPE
            entry.linkname = "/etc/passwd"
        archive.addfile(entry, io.BytesIO(body))
        if kind == "extra":
            archive.addfile(tarfile.TarInfo("unexpected"))

    class Backend:
        def run(self, argv, timeout_seconds):
            assert argv == (
                "docker",
                "exec",
                "--user",
                "1000:1000",
                "a" * 64,
                "tar",
                "-C",
                "/output",
                "-cf",
                "-",
                "--",
                "result.json",
            )
            return 0, output.getvalue(), b""

    cli = DockerCli(Backend())
    assert hasattr(cli, "read_output"), "missing bounded output reader"
    descriptor = {
        "staging_name": "result.json",
        "size_bytes": 2,
        "checksum": "sha256:" + hashlib.sha256(body if kind != "changed" else b"aa").hexdigest(),
    }
    if kind == "file":
        assert cli.read_output("a" * 64, descriptor) == body
    else:
        with pytest.raises(ValueError):
            cli.read_output("a" * 64, descriptor)


def test_real_runner_result_handshake_replays_upload_loss(tmp_path):
    import json

    from nexa.infrastructure.persistence.ids import new_uuid7
    from nexa.worker import result_flow
    from nexa.worker.journal import ExecutionJournal
    from nexa.worker.models import ResourceVector
    from nexa.workloads.trusted_runner import RunnerSupervisor
    from tests.worker.test_journal import ALLOC, ATTEMPT, AUTHORITY, BINDING, NONCE
    from tests.workloads.test_runner import COMPLETION, RESULT, result_provenance

    assert hasattr(result_flow, "ResultFlow"), "missing durable result handshake"
    journal = ExecutionJournal(tmp_path / "journal")
    journal.prepare(
        attempt_id=ATTEMPT,
        allocation_id=ALLOC,
        startup_nonce=NONCE,
        authority=AUTHORITY,
        resources=ResourceVector(1000, 128 * 1024**2, 0),
        image_digest="sha256:" + "a" * 64,
        execution_binding=BINDING,
    )
    runner = RunnerSupervisor(
        startup_limit_seconds=30, runtime_limit_seconds=300, stop_grace_seconds=5
    )
    runner.accept_authority_deadline(runner.clock() + 100)
    (tmp_path / "result.json").write_bytes(b'{"value":1}')
    runner.stage_result(
        tmp_path / "result.json",
        completion_token=COMPLETION,
        logical_name="result.json",
        media_type="application/json",
        provenance=result_provenance(),
    )
    receipts = {}
    completed = []

    class Client:
        lost = False

        def reserve_result(self, attempt, callback, body):
            return {"callback_id": callback, "result_id": RESULT, "attempt_id": attempt}

        def upload_artifact(self, authority, descriptor, key, content):
            if key not in receipts:
                receipts[key] = {**descriptor, "artifact_id": str(new_uuid7())}
            if not self.lost:
                self.lost = True
                raise TimeoutError("response loss after upload commit")
            return receipts[key]

        def complete(self, attempt, callback, body):
            completed.append(body)
            return {"accepted": True}

    control_sequence = [0]

    def control(attempt, key, type_, payload):
        control_sequence[0] += 1
        assert (
            runner.apply_control(
                {
                    "schema_version": 1,
                    "control_sequence": control_sequence[0],
                    "type": type_,
                    "payload": payload,
                }
            )
            == "ACCEPTED"
        )

    flow = result_flow.ResultFlow(
        journal,
        Client(),
        lambda attempt, desc: (tmp_path / desc["staging_name"]).read_bytes(),
        control,
    )
    first = runner.pending_messages[0]
    flow.process(ATTEMPT, first)
    batch = runner.pending_messages[1]
    with pytest.raises(TimeoutError):
        flow.process(ATTEMPT, batch)
    flow.process(ATTEMPT, batch)
    ready = runner.pending_messages[2]
    flow.process(ATTEMPT, ready)
    assert len(receipts) == 2
    assert len(completed) == 1
    manifest = json.loads((tmp_path / "result-manifest.json").read_bytes())
    assert completed[0]["manifest"] == manifest
    assert manifest["files"][0]["artifact_id"] in {r["artifact_id"] for r in receipts.values()}
    assert journal.load(ATTEMPT).runner_state["result_flow"]["completed"] is True


def test_terminal_result_discards_pending_renewal_and_renews_other_attempt(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from nexa.worker.agent import WorkerAgent
    from nexa.worker.state import PendingOperationStore

    class Journal:
        def lock(self, _attempt):
            return nullcontext()

        def load(self, attempt):
            return SimpleNamespace(
                runner_state={
                    "result_flow": {
                        "completed": attempt == "finished",
                        "completion_ack": {"accepted": True, "job_state": "SUCCEEDED"},
                    }
                }
            )

    state = PendingOperationStore(tmp_path / "pending", boot_id="boot")
    state.begin("renew-finished", operation="renew", payload={"attempt_id": "finished"})
    agent = WorkerAgent.for_test(object(), worker_id="worker", incarnation_id="incarnation")
    agent.journal = Journal()
    agent.state = state
    agent._adopted = {"finished": object(), "running": object()}
    renewed = []
    agent._renew_attempt = lambda attempt, authority: renewed.append(attempt)

    agent.renew_once()

    assert renewed == ["running"]
    assert state.operations == {}


def test_one_renewal_conflict_does_not_starve_another_attempt():
    from contextlib import nullcontext
    from types import SimpleNamespace

    from nexa.worker.agent import WorkerAgent
    from nexa.worker.client import WorkerApiError

    class Journal:
        def lock(self, _attempt):
            return nullcontext()

        def load(self, _attempt):
            return SimpleNamespace(runner_state={})

    agent = WorkerAgent.for_test(object(), worker_id="worker", incarnation_id="incarnation")
    agent.journal = Journal()
    agent._adopted = {"conflicted": object(), "healthy": object()}
    renewed = []

    def renew(attempt, _authority):
        renewed.append(attempt)
        if attempt == "conflicted":
            raise WorkerApiError(409, "state_conflict")

    agent._renew_attempt = renew
    with pytest.raises(WorkerApiError):
        agent.renew_once()
    assert renewed == ["conflicted", "healthy"]


def test_ipc_connection_serializes_with_attempt_controls():
    import threading
    from contextlib import contextmanager
    from types import SimpleNamespace

    from nexa.worker.agent import WorkerAgent

    held = threading.Lock()
    opened = threading.Event()

    class Journal:
        @contextmanager
        def lock(self, _attempt):
            with held:
                yield

        def load(self, _attempt):
            return SimpleNamespace(
                container=SimpleNamespace(container_id="a" * 64), runner_state={}
            )

    class Channel:
        def __init__(self, _container):
            opened.set()

        def settimeout(self, _value):
            pass

        def recv(self, _maximum):
            raise TimeoutError()

    agent = WorkerAgent.for_test(object(), worker_id="worker", incarnation_id="incarnation")
    agent.journal = Journal()
    agent._adopted["attempt"] = object()
    agent.channel_factory = Channel
    held.acquire()
    worker = threading.Thread(target=agent._ipc_once)
    worker.start()
    try:
        assert not opened.wait(0.1), "IPC opened while an attempt control held its lock"
    finally:
        held.release()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert opened.is_set()


def test_ipc_waits_for_persisted_result_message_before_reopening_channel():
    from contextlib import nullcontext
    from types import SimpleNamespace

    from nexa.worker.agent import WorkerAgent

    class Journal:
        def lock(self, _attempt):
            return nullcontext()

        def load(self, _attempt):
            return SimpleNamespace(
                container=SimpleNamespace(container_id="a" * 64),
                runner_state={"pending_execution_message": {"type": "RESULT_FILE_BATCH"}},
            )

    agent = WorkerAgent.for_test(object(), worker_id="worker", incarnation_id="incarnation")
    agent.journal = Journal()
    agent._adopted["attempt"] = object()
    opened = []
    agent.channel_factory = lambda container: opened.append(container)

    agent._ipc_once()

    assert opened == []


def test_result_message_is_not_acked_before_durable_handshake(tmp_path):
    from nexa.worker.agent import WorkerAgent
    from nexa.worker.journal import ExecutionJournal
    from nexa.worker.models import ResourceVector
    from tests.worker.test_journal import ALLOC, ATTEMPT, AUTHORITY, BINDING, NONCE

    journal = ExecutionJournal(tmp_path)
    journal.prepare(
        attempt_id=ATTEMPT,
        allocation_id=ALLOC,
        startup_nonce=NONCE,
        authority=AUTHORITY,
        resources=ResourceVector(1000, 128 * 1024**2, 0),
        image_digest="sha256:" + "a" * 64,
        execution_binding=BINDING,
    )
    agent = WorkerAgent.for_test(
        None, worker_id=AUTHORITY.worker_id, incarnation_id=AUTHORITY.worker_incarnation_id
    )
    agent.journal = journal
    envelope = {
        "schema_version": 1,
        "message_sequence": 1,
        "type": "RESULT_PREPARE",
        "payload": {"completion_token": NONCE},
    }
    assert agent._record_runner_message(ATTEMPT, envelope) == "OUT_OF_ORDER"
    assert journal.load(ATTEMPT).runner_state["pending_execution_message"] == envelope
    assert agent._record_runner_message(ATTEMPT, envelope) == "OUT_OF_ORDER"


def test_production_ipc_result_loop_completes_and_cleans_exact_container(tmp_path):
    import json

    from nexa.infrastructure.persistence.ids import new_uuid7
    from nexa.worker.agent import WorkerAgent
    from nexa.worker.docker_client import DockerCli
    from nexa.worker.executor import DockerExecutor
    from nexa.worker.journal import ExecutionJournal
    from nexa.worker.protocol import FrameDecoder, encode_frame
    from nexa.worker.state import PendingOperationStore
    from nexa.workloads.trusted_runner import RunnerSupervisor
    from tests.worker.test_docker_config import start
    from tests.worker.test_executor import FakeDocker
    from tests.workloads.test_runner import COMPLETION, RESULT, result_provenance

    class Backend(FakeDocker):
        def run(self, argv, timeout_seconds):
            if argv[1] == "exec" and argv[5] == "tar":
                assert argv[:5] == ("docker", "exec", "--user", "1000:1000", identity.container_id)
                assert argv[5:11] == ("tar", "-C", "/output", "-cf", "-", "--")
                source = tmp_path / argv[11]
                output = io.BytesIO()
                with tarfile.open(fileobj=output, mode="w") as archive:
                    archive.add(source, arcname=source.name)
                return 0, output.getvalue(), b""
            result = super().run(argv, timeout_seconds)
            if argv[1] == "inspect" and result[0] == 0:
                payload = json.loads(result[1])
                payload[0]["Config"]["Labels"]["nexa.installation_id"] = "test"
                return 0, json.dumps(payload).encode(), b""
            return result

    journal = ExecutionJournal(tmp_path / "journal")
    backend = Backend()
    executor = DockerExecutor(
        journal,
        backend,
        image_ref="registry.invalid/cpu",
        staging_root=tmp_path / "staging",
        installation_id="test",
    )
    identity = executor.start(executor.prepare(start()))
    runner = RunnerSupervisor(
        startup_limit_seconds=30, runtime_limit_seconds=300, stop_grace_seconds=5
    )
    runner.accept_authority_deadline(runner.clock() + 100)
    (tmp_path / "result.json").write_bytes(b'{"value":1}')
    runner.stage_result(
        tmp_path / "result.json",
        completion_token=COMPLETION,
        logical_name="result.json",
        media_type="application/json",
        provenance=result_provenance(),
    )

    class Channel:
        def __init__(self):
            self.acks = []

        def settimeout(self, value):
            pass

        def sendall(self, raw):
            for frame in FrameDecoder().feed(raw):
                if "ack_sequence" in frame:
                    runner.acknowledge_message(frame)
                else:
                    code = runner.apply_control(frame)
                    self.acks.append(
                        {
                            "schema_version": 1,
                            "ack_sequence": frame["control_sequence"],
                            "accepted": code in {"ACCEPTED", "DUPLICATE"},
                            "code": code,
                        }
                    )

        def recv(self, maximum):
            frames = [*self.acks, *runner.pending_messages]
            self.acks = []
            if not frames:
                raise TimeoutError()
            return b"".join(encode_frame(frame) for frame in frames)

    recognized = []
    cleanups = []

    class Client:
        def reserve_result(self, attempt, callback, body):
            return {"callback_id": callback, "result_id": RESULT, "attempt_id": attempt}

        def upload_artifact(self, authority, descriptor, key, content):
            return {**descriptor, "artifact_id": str(new_uuid7())}

        def complete(self, attempt, callback, body):
            recognized.append(body)
            return {"accepted": True}

        def cleanup(self, attempt, callback, body):
            cleanups.append(body)
            return {"verified": True, "allocation_state": "RELEASED"}

    auth = start().context.authority
    agent = WorkerAgent(
        worker_id=auth.worker_id,
        incarnation_id=auth.worker_incarnation_id,
        installation_id="test",
        client=Client(),
        journal=journal,
        state=PendingOperationStore(tmp_path / "pending", boot_id="test"),
        docker=DockerCli(backend),
        executor=executor,
        provider=None,
        channel_factory=lambda _: Channel(),
    )
    agent._adopted[auth.attempt_id] = auth
    for _ in range(5):
        agent._ipc_once()
        agent._result_once()
    assert len(recognized) == 1
    assert recognized[0]["manifest"] == json.loads((tmp_path / "result-manifest.json").read_bytes())
    assert len(cleanups) == 1
    assert cleanups[0]["proof"]["container"]["container_id"] == identity.container_id
    assert backend.removed == [identity.container_id]
    assert auth.attempt_id not in agent._adopted


@pytest.mark.parametrize("lost_failure_ack", [False, True])
def test_invalid_result_frame_linearizes_failure_before_cleanup(tmp_path, lost_failure_ack):
    from nexa.worker.agent import WorkerAgent
    from nexa.worker.executor import DockerExecutor
    from nexa.worker.journal import ExecutionJournal
    from nexa.worker.state import PendingOperationStore
    from tests.worker.test_docker_config import start
    from tests.worker.test_executor import FakeDocker

    request = start()
    journal = ExecutionJournal(tmp_path / "journal")
    backend = FakeDocker()
    executor = DockerExecutor(
        journal, backend, image_ref="registry.invalid/cpu", staging_root=tmp_path / "staging"
    )
    identity = executor.start(executor.prepare(request))
    authority = request.context.authority
    observed = []

    class Client:
        def fail(self, attempt, callback, body):
            observed.append(("failure", callback, body))
            if lost_failure_ack and len([item for item in observed if item[0] == "failure"]) == 1:
                raise RuntimeError("response lost after failure commit")
            return {"accepted": True}

        def cleanup(self, attempt, callback, body):
            observed.append(("cleanup", body["proof"]["container"]["container_id"]))
            return {"verified": True, "allocation_state": "RELEASED"}

    agent = WorkerAgent(
        worker_id=authority.worker_id,
        incarnation_id=authority.worker_incarnation_id,
        installation_id="test",
        client=Client(),
        state=PendingOperationStore(tmp_path / "pending", boot_id="test"),
        journal=journal,
        docker=DockerCli(backend),
        executor=executor,
        provider=None,
    )
    agent._adopted[authority.attempt_id] = authority
    # A valid result message without a committed reservation is a protocol violation.
    envelope = {
        "schema_version": 1,
        "message_sequence": 1,
        "type": "RESULT_FILE_BATCH",
        "payload": {
            "completion_token": request.startup_nonce,
            "reservation_callback_id": request.startup_nonce,
            "result_id": request.context.job_id,
            "batch_index": 0,
            "batch_count": 1,
            "artifacts": [],
        },
    }
    assert agent._record_runner_message(authority.attempt_id, envelope) == "OUT_OF_ORDER"
    if lost_failure_ack:
        with pytest.raises(RuntimeError, match="response lost"):
            agent._result_once()
        agent._result_once()
        assert observed[0] == observed[1]
    else:
        agent._result_once()
    assert observed[-2][0] == "failure"
    assert observed[-2][2]["reason_code"] == "INVALID_RESULT"
    assert observed[-1] == ("cleanup", identity.container_id)
    assert backend.removed == [identity.container_id]
