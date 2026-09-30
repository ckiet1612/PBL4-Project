"""Remediation B15-R11/R14/R10: a runner that dies at startup is classified by Docker.

A start authority deadline the runner never accepts is either a runner that is still
starting or one whose container already exited. Only a Docker-proven exit of the bound
container (inspect state/exit/identity), never elapsed time, may end the attempt early:
a killed runner is retryable INFRASTRUCTURE/RUNNER_UNAVAILABLE, reported at once rather
than after the 30-second startup budget as a non-retryable STARTUP_TIMEOUT. A runner
still running keeps the replay, so a genuine startup timeout stays TIMEOUT.
"""

import hashlib
import json
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from nexa.worker import execution, protocol
from nexa.worker.agent import WorkerAgent
from nexa.worker.docker_client import DockerCli
from nexa.worker.executor import DockerExecutor
from nexa.worker.journal import ExecutionJournal
from nexa.worker.protocol import FrameDecoder, encode_frame
from nexa.worker.runner_control import RunnerControlError
from nexa.worker.state import PendingOperationStore
from tests.worker.test_docker_config import start
from tests.worker.test_executor import FakeDocker

CONTENT = b'{"initial_value":17}'
BUDGET_NS = 30_000_000_000


class ExitingDocker(FakeDocker):
    """FakeDocker whose started container exits once ``exit_code`` is set."""

    def __init__(self):
        super().__init__()
        self.exit_code = None
        self.oom_killed = False

    def run(self, argv, timeout_seconds):
        code, stdout, stderr = super().run(argv, timeout_seconds)
        if argv[1] == "inspect" and code == 0 and self.exit_code is not None:
            (payload,) = json.loads(stdout)
            payload["State"] = {
                "Running": False,
                "Status": "exited",
                "ExitCode": self.exit_code,
                "OOMKilled": self.oom_killed,
            }
            stdout = json.dumps([payload]).encode()
        return code, stdout, stderr


class DeadRelay:
    """The control relay of a container that is gone: docker exec has exited."""

    def __init__(self, *, fail_on, frames=()):
        self.fail_on = fail_on
        self.frames = list(frames)
        self.acks = []

    def settimeout(self, timeout):
        pass

    def sendall(self, raw):
        if self.fail_on == "send":
            raise RuntimeError("Docker control relay exited with code 1: not running")
        self.fail_on = "recv"
        for frame in FrameDecoder().feed(raw):
            if "ack_sequence" in frame:
                self.acks.append(frame)

    def recv(self, maximum):
        if self.frames:
            return encode_frame(self.frames.pop(0))
        raise RuntimeError("Docker control relay closed unexpectedly")


class SilentRunner:
    """A running runner that never answers the deadline control."""

    def settimeout(self, timeout):
        pass

    def sendall(self, raw):
        pass

    def recv(self, maximum):
        raise TimeoutError("timed out waiting for the trusted runner")


def _harness(tmp_path, channel, *, exit_on_start=None, oom_killed=False):
    requested = start()
    authority = requested.context.authority
    artifact = {
        "artifact_id": requested.context.job_id,
        "tenant_id": requested.context.tenant_id,
        "checksum": "sha256:" + hashlib.sha256(CONTENT).hexdigest(),
        "size_bytes": len(CONTENT),
        "media_type": "application/vnd.nexa.cpu-iterative-input+json",
    }
    context = {
        "authority": asdict(authority),
        "job_id": requested.context.job_id,
        "logical_session_id": requested.context.logical_session_id,
        "spec": {
            "template_id": "cpu-iterative",
            "template_version": 1,
            "input_artifact_id": artifact["artifact_id"],
            "parameters": {"iterations": 2, "seed": 1, "modulus": 100},
            "runtime_limit_seconds": 30,
        },
        "execution_intent": "RUN",
        "restore_checkpoint": None,
        "template_snapshot": {"capability_requirement": {"architectures": ["linux/amd64"]}},
        "adapter_id": "cpu.iterative",
        "adapter_version": "1.0.0",
        "image_digest": requested.context.image_digest,
        "startup_nonce": requested.startup_nonce,
        "allocation": {"resources": asdict(requested.context.resources)},
        "input_artifacts": [artifact],
    }
    backend = ExitingDocker()

    class Client:
        def __init__(self):
            self.failures = []
            self.cleanups = []

        def claim(self, attempt, callback, body):
            return {"accepted": True, "callback_id": callback, "execution_context": context}

        def download_execution(self, auth, descriptor, path):
            path.write_bytes(CONTENT)

        def start(self, attempt, callback, body):
            # The container dies after the API accepted the start and before the
            # runner accepts its authority deadline.
            if exit_on_start is not None:
                backend.exit_code = exit_on_start
                backend.oom_killed = oom_killed
            return {
                "accepted": True,
                "callback_id": callback,
                "lease_duration_seconds": 45,
                "safety_margin_seconds": 5,
            }

        def fail(self, attempt, callback, body):
            self.failures.append(body)
            return {"callback_id": callback, "accepted": True}

        def cleanup(self, attempt, callback, body):
            self.cleanups.append(body)
            return {"callback_id": callback, "verified": True, "allocation_state": "RELEASED"}

    client = Client()
    journal = ExecutionJournal(tmp_path / "journal")
    executor = DockerExecutor(
        journal, backend, image_ref="registry.invalid/cpu", staging_root=tmp_path / "staging"
    )
    offset = [0]
    agent = WorkerAgent(
        worker_id=authority.worker_id,
        incarnation_id=authority.worker_incarnation_id,
        installation_id="test",
        client=client,
        journal=journal,
        state=PendingOperationStore(tmp_path / "pending.json", boot_id="test"),
        docker=DockerCli(backend),
        executor=executor,
        provider=SimpleNamespace(discover=lambda: SimpleNamespace(architecture="linux/amd64")),
        channel_factory=lambda _: channel,
        monotonic_ns=lambda: time.monotonic_ns() + offset[0],
    )
    return SimpleNamespace(
        agent=agent,
        client=client,
        backend=backend,
        journal=journal,
        authority=authority,
        offer={"authority": asdict(authority)},
        offset=offset,
    )


def _single_failure(harness):
    (failure,) = harness.client.failures
    observation = failure["observation"]
    assert observation["observation_type"] == "CONTAINER"
    assert observation["container"]["container_id"] == harness.backend.container_id
    return failure, observation


def _settled(harness):
    """The exact container is cleaned up and no startup callback stays pending."""
    (cleanup,) = harness.client.cleanups
    assert cleanup["proof"]["proof_type"] == "CONTAINER_STOPPED"
    assert cleanup["proof"]["container"]["container_id"] == harness.backend.container_id
    assert harness.agent.state.operations == {}
    assert harness.authority.attempt_id not in harness.agent._adopted


@pytest.mark.parametrize("fail_on", ["send", "recv"])
def test_container_killed_before_deadline_is_runner_unavailable_at_once(tmp_path, fail_on):
    harness = _harness(tmp_path, DeadRelay(fail_on=fail_on), exit_on_start=137)

    # No replay and no elapsed startup budget: Docker already proves the exit.
    harness.agent._dispatch_offer(harness.offer)

    failure, observation = _single_failure(harness)
    assert (failure["failure_class"], failure["reason_code"]) == (
        "INFRASTRUCTURE",
        "RUNNER_UNAVAILABLE",
    )
    assert observation["exit_code"] == 137
    assert observation["oom_killed"] is False
    assert observation["runtime_limit_reached"] is False
    _settled(harness)
    exited = harness.journal.load(harness.authority.attempt_id).runner_state["container_exit"]
    assert exited["exit_code"] == 137


def test_container_oom_killed_before_deadline_is_container_oom(tmp_path):
    harness = _harness(tmp_path, DeadRelay(fail_on="recv"), exit_on_start=137, oom_killed=True)

    harness.agent._dispatch_offer(harness.offer)

    failure, observation = _single_failure(harness)
    assert (failure["failure_class"], failure["reason_code"]) == ("OOM", "CONTAINER_OOM")
    assert observation["oom_killed"] is True
    _settled(harness)


def test_kept_startup_limit_frame_names_a_startup_timeout(tmp_path):
    # The runner hit its own 30-second startup limit, handed this connection its
    # STOPPED frame and exited 0 once the worker kept it (B15-R10).
    stopped = {
        "schema_version": 1,
        "message_sequence": 1,
        "type": "STOPPED",
        "payload": {"reason": "STARTUP_LIMIT", "exit_code": -1, "stopped_monotonic_ns": 9},
    }
    relay = DeadRelay(fail_on="recv", frames=[stopped])
    harness = _harness(tmp_path, relay, exit_on_start=0)

    harness.agent._dispatch_offer(harness.offer)

    assert [ack["ack_sequence"] for ack in relay.acks] == [1]
    failure, observation = _single_failure(harness)
    assert (failure["failure_class"], failure["reason_code"]) == ("TIMEOUT", "STARTUP_TIMEOUT")
    # STARTUP_TIMEOUT is distinct from a runner runtime-limit observation.
    assert observation["runtime_limit_reached"] is False
    _settled(harness)


@pytest.mark.parametrize(
    ("reason", "expected", "runtime_limit_reached"),
    [
        ("STARTUP_LIMIT", ("TIMEOUT", "STARTUP_TIMEOUT"), False),
        ("LEASE_DEADLINE", ("INFRASTRUCTURE", "RUNNER_UNAVAILABLE"), False),
        ("FAILURE", ("INTERNAL", "WORKLOAD_EXIT_NONZERO"), False),
    ],
)
def test_unacknowledged_stop_exit_status_names_the_stop_reason(
    tmp_path, reason, expected, runtime_limit_reached
):
    # No worker connection kept the STOPPED frame; the runner's exit status carries
    # its stop reason instead of an exit 0 without a frame (B15-R14).
    code = protocol.STOP_EXIT_CODES[reason]
    harness = _harness(tmp_path, DeadRelay(fail_on="send"), exit_on_start=code)

    harness.agent._dispatch_offer(harness.offer)

    failure, observation = _single_failure(harness)
    assert (failure["failure_class"], failure["reason_code"]) == expected
    assert observation["exit_code"] == code
    assert observation["runtime_limit_reached"] is runtime_limit_reached
    _settled(harness)


def test_running_runner_that_never_accepts_still_times_out(tmp_path):
    harness = _harness(tmp_path, SilentRunner())

    # A runner that is still starting is not proven dead: replay, report nothing.
    with pytest.raises(RunnerControlError):
        harness.agent._dispatch_offer(harness.offer)
    assert harness.client.failures == []
    assert harness.backend.stopped == []

    harness.offset[0] = BUDGET_NS
    with pytest.raises(TimeoutError):
        harness.agent._dispatch_offer(harness.offer)
    failure, observation = _single_failure(harness)
    assert (failure["failure_class"], failure["reason_code"]) == ("TIMEOUT", "STARTUP_TIMEOUT")
    assert observation["runtime_limit_reached"] is False
    assert observation["exit_code"] is None


def test_container_killed_after_the_budget_is_still_runner_unavailable(tmp_path):
    # A replay that finds the budget spent does not blame a runner Docker shows
    # already exited on a startup timeout.
    harness = _harness(tmp_path, SilentRunner())
    with pytest.raises(RunnerControlError):
        harness.agent._dispatch_offer(harness.offer)
    harness.backend.exit_code = 137
    harness.offset[0] = BUDGET_NS

    harness.agent._dispatch_offer(harness.offer)

    failure, observation = _single_failure(harness)
    assert (failure["failure_class"], failure["reason_code"]) == (
        "INFRASTRUCTURE",
        "RUNNER_UNAVAILABLE",
    )
    assert observation["exit_code"] == 137
    _settled(harness)


@pytest.mark.parametrize(
    ("status", "oom_killed", "expected"),
    [
        (0, False, ("INTERNAL", "RUNNER_PROTOCOL_ERROR")),
        (124, False, ("INFRASTRUCTURE", "RUNNER_UNAVAILABLE")),
        (137, False, ("INFRASTRUCTURE", "RUNNER_UNAVAILABLE")),
        (137, True, ("OOM", "CONTAINER_OOM")),
        ("RUNTIME_LIMIT", False, ("TIMEOUT", "RUNTIME_LIMIT_REACHED")),
        ("STARTUP_LIMIT", False, ("TIMEOUT", "STARTUP_TIMEOUT")),
        ("LEASE_DEADLINE", False, ("INFRASTRUCTURE", "RUNNER_UNAVAILABLE")),
        ("FAILURE", False, ("INTERNAL", "WORKLOAD_EXIT_NONZERO")),
        ("CANCEL", False, ("INTERNAL", "WORKLOAD_EXIT_NONZERO")),
    ],
)
def test_container_exit_status_classification(status, oom_killed, expected):
    # A stop reason names the runner's exit status for an unacknowledged stop.
    code = status if isinstance(status, int) else protocol.STOP_EXIT_CODES[status]
    exited = {"exit_code": code, "oom_killed": oom_killed, "observed_at": "t"}
    assert execution.container_exit_failure(exited) == expected


def test_failure_exit_after_a_rejected_checkpoint_control_is_a_checkpoint_protocol_error():
    exited = {
        "exit_code": protocol.STOP_EXIT_CODES["FAILURE"],
        "oom_killed": False,
        "observed_at": "t",
    }
    assert execution.container_exit_failure(exited, {"checkpoint_rejected": True}) == (
        "INTERNAL",
        "CHECKPOINT_PROTOCOL_ERROR",
    )
