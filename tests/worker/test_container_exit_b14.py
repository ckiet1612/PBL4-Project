"""B14: a workload container that dies under a live worker fails its attempt closed."""

import json

import pytest

from nexa.worker.docker_client import DockerCli
from tests.worker.test_docker_config import start
from tests.worker.test_executor import FakeDocker


class ExitedDocker(FakeDocker):
    """FakeDocker whose started container can die with a given Docker state."""

    def __init__(self) -> None:
        super().__init__()
        self.exited: tuple[int, bool] | None = None

    def run(self, argv, timeout_seconds):
        code, stdout, stderr = super().run(argv, timeout_seconds)
        if argv[1] == "inspect" and code == 0 and self.exited is not None:
            payload = json.loads(stdout)
            exit_code, oom_killed = self.exited
            payload[0]["State"] = {
                "Running": False,
                "Status": "exited",
                "ExitCode": exit_code,
                "OOMKilled": oom_killed,
            }
            stdout = json.dumps(payload).encode()
        return code, stdout, stderr


def _dead_channel(_container_id):
    raise RuntimeError("Docker control relay exited with code 1: container is not running")


def _agent(tmp_path):
    from nexa.worker.agent import WorkerAgent
    from nexa.worker.executor import DockerExecutor
    from nexa.worker.journal import ExecutionJournal
    from nexa.worker.state import PendingOperationStore

    request = start()
    journal = ExecutionJournal(tmp_path / "journal")
    backend = ExitedDocker()
    executor = DockerExecutor(
        journal, backend, image_ref="registry.invalid/cpu", staging_root=tmp_path / "staging"
    )
    identity = executor.start(executor.prepare(request))
    authority = request.context.authority
    observed = []

    class Client:
        def renew(self, attempt, callback, body):
            observed.append(("renew", callback))
            return {"lease_expires_at": "2026-09-26T00:00:45.000Z", "safety_margin_seconds": 10}

        def fail(self, attempt, callback, body):
            observed.append(("failure", callback, body))
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
    agent.channel_factory = _dead_channel
    return agent, backend, identity, authority, observed


@pytest.mark.parametrize(
    ("exit_code", "oom_killed", "failure_class", "reason_code"),
    [
        (137, False, "INFRASTRUCTURE", "RUNNER_UNAVAILABLE"),
        (137, True, "OOM", "CONTAINER_OOM"),
        # The runner exits 0 only after a terminal frame the worker never saw.
        (0, False, "INTERNAL", "RUNNER_PROTOCOL_ERROR"),
    ],
)
def test_dead_container_fails_attempt_then_cleans_exact_identity(
    tmp_path, exit_code, oom_killed, failure_class, reason_code
):
    agent, backend, identity, authority, observed = _agent(tmp_path)
    backend.exited = (exit_code, oom_killed)

    agent._ipc_once()

    exited = agent.journal.load(authority.attempt_id).runner_state["container_exit"]
    assert exited["exit_code"] == exit_code
    assert exited["oom_killed"] is oom_killed
    assert observed == []

    agent._result_once()

    assert [item[0] for item in observed] == ["failure", "cleanup"]
    body = observed[0][2]
    assert (body["failure_class"], body["reason_code"]) == (failure_class, reason_code)
    assert body["observation"]["observation_type"] == "CONTAINER"
    assert body["observation"]["container"]["container_id"] == identity.container_id
    assert body["observation"]["exit_code"] == exit_code
    assert body["observation"]["oom_killed"] is oom_killed
    assert body["observation"]["runtime_limit_reached"] is False
    assert observed[1] == ("cleanup", identity.container_id)
    assert backend.stopped == []
    assert backend.removed == [identity.container_id]
    assert authority.attempt_id not in agent._adopted
    assert agent.state.operations == {}


def test_running_container_channel_failure_is_not_a_container_exit(tmp_path):
    agent, backend, _identity, authority, observed = _agent(tmp_path)

    with pytest.raises(RuntimeError, match="control relay"):
        agent._ipc_once()

    assert "container_exit" not in (agent.journal.load(authority.attempt_id).runner_state or {})
    assert observed == []
    assert backend.removed == []
    assert authority.attempt_id in agent._adopted


def test_result_loop_detects_dead_container_behind_pending_result_frame(tmp_path):
    agent, backend, identity, authority, observed = _agent(tmp_path)
    backend.exited = (137, False)

    def reserve_result(attempt, callback, body):
        observed.append(("reserve", callback))
        return {"callback_id": callback, "attempt_id": attempt, "result_id": "result"}

    agent.client.reserve_result = reserve_result
    # IPC skips an attempt with a pending frame; the result loop's control
    # exchange is then the first to hit the dead relay.
    envelope = {
        "schema_version": 1,
        "message_sequence": 1,
        "type": "RESULT_PREPARE",
        "payload": {"completion_token": "token"},
    }
    agent.journal.update_runner_state(
        authority.attempt_id, lambda local: {**local, "pending_execution_message": envelope}
    )

    agent._result_once()
    assert agent.journal.load(authority.attempt_id).runner_state["container_exit"] is not None
    assert [item[0] for item in observed] == ["reserve"]

    agent._result_once()

    assert [item[0] for item in observed] == ["reserve", "failure", "cleanup"]
    assert observed[1][2]["reason_code"] == "RUNNER_UNAVAILABLE"
    assert observed[2] == ("cleanup", identity.container_id)
    assert authority.attempt_id not in agent._adopted


def test_acknowledged_renew_with_undelivered_deadline_does_not_block_after_cleanup(tmp_path):
    agent, backend, identity, authority, observed = _agent(tmp_path)
    backend.exited = (137, False)

    # The server extends the lease, then the deadline control hits the dead relay.
    with pytest.raises(RuntimeError, match="control relay"):
        agent.renew_once()
    assert [record["operation"] for record in agent.state.operations.values()] == ["renew"]
    assert agent._blocking_pending_attempts() == [authority.attempt_id]

    agent._ipc_once()
    agent._result_once()

    assert [item[0] for item in observed] == ["renew", "failure", "cleanup"]
    assert backend.removed == [identity.container_id]
    assert agent.state.operations == {}
    assert agent._blocking_pending_attempts() == []


def test_renew_is_kept_until_failure_is_acknowledged_and_cleanup_verified(tmp_path):
    from nexa.worker.client import WorkerApiError

    agent, backend, identity, authority, observed = _agent(tmp_path)
    backend.exited = (137, False)
    with pytest.raises(RuntimeError):
        agent.renew_once()
    agent._ipc_once()
    cleanup = agent.client.cleanup

    def unavailable(attempt, callback, body):
        raise WorkerApiError(503, "dependency_unavailable")

    agent.client.cleanup = unavailable
    agent._result_once()
    # Failure is acknowledged but the allocation is not yet proven released.
    assert sorted(record["operation"] for record in agent.state.operations.values()) == [
        "cleanup",
        "renew",
    ]

    agent.client.cleanup = cleanup
    for callback_id in list(agent.state.operations):
        if agent.state.operations[callback_id]["operation"] == "cleanup":
            assert agent._send_resolution(callback_id)
    assert agent.state.operations == {}
    assert backend.removed == [identity.container_id]


def test_lost_completion_ack_replays_after_the_container_stopped(tmp_path):
    from nexa.infrastructure.persistence.ids import new_uuid7
    from nexa.worker.journal import ExecutionJournal
    from nexa.worker.models import ResourceVector
    from nexa.worker.result_flow import ResultFlow
    from nexa.workloads.trusted_runner import RunnerSupervisor
    from tests.worker.test_journal import ALLOC, ATTEMPT, AUTHORITY, BINDING, NONCE
    from tests.workloads.test_runner import COMPLETION, RESULT, result_provenance

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
    completions = []
    stopped = [False]

    class Client:
        def reserve_result(self, attempt, callback, body):
            return {"callback_id": callback, "result_id": RESULT, "attempt_id": attempt}

        def upload_artifact(self, authority, descriptor, key, content):
            return {**descriptor, "artifact_id": str(new_uuid7())}

        def complete(self, attempt, callback, body):
            completions.append((callback, body))
            if len(completions) == 1:
                raise TimeoutError("completion committed but its response was lost")
            return {"accepted": True}

    sequence = [0]

    def control(attempt, key, type_, payload):
        sequence[0] += 1
        envelope = {
            "schema_version": 1,
            "control_sequence": sequence[0],
            "type": type_,
            "payload": payload,
        }
        assert runner.apply_control(envelope) == "ACCEPTED"

    def read_output(attempt, descriptor):
        if stopped[0]:
            raise RuntimeError("Docker exec failed: container is not running")
        return (tmp_path / descriptor["staging_name"]).read_bytes()

    flow = ResultFlow(journal, Client(), read_output, control)
    flow.process(ATTEMPT, runner.pending_messages[0])
    flow.process(ATTEMPT, runner.pending_messages[1])
    ready = runner.pending_messages[2]
    with pytest.raises(TimeoutError):
        flow.process(ATTEMPT, ready)

    # The runner watchdog stops the workload; its output can no longer be read.
    stopped[0] = True
    flow.process(ATTEMPT, ready)

    assert len(completions) == 2
    assert completions[1] == completions[0]
    assert journal.load(ATTEMPT).runner_state["result_flow"]["completed"] is True
