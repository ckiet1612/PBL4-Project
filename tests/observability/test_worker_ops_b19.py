"""B19 worker metrics and ops readiness: counted after acknowledgement, bounded sampling."""

import asyncio
import json
import socket
import urllib.error
import urllib.request

import pytest

from nexa.observability import metrics_worker as m
from nexa.worker.agent import WorkerAgent
from nexa.worker.client import WorkerTransportError
from nexa.worker.docker_client import DockerCli
from nexa.worker.ops import WorkerOps
from tests.worker.test_rem_b15_r39_resolution_lock import GatedPeer, _running_attempt

CONTAINER_A = "a" * 64
CONTAINER_B = "b" * 64


def _value(metric, *labels):
    target = metric.labels(*labels) if labels else metric
    return target._value.get()


class StatsBackend:
    def __init__(self, code=0, stdout=b"", error=None):
        self.code, self.stdout, self.error = code, stdout, error
        self.calls = []

    def run(self, argv, timeout_seconds):
        self.calls.append((argv, timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.code, self.stdout, b""


def test_docker_stats_parses_known_rows_and_skips_the_rest():
    rows = [
        {"ID": CONTAINER_A, "MemPerc": "50.00%", "CPUPerc": "150.5%"},
        {"ID": "c" * 64, "MemPerc": "1%", "CPUPerc": "1%"},  # not requested
        {"ID": CONTAINER_B, "MemPerc": "--", "CPUPerc": "0%"},  # unparsable
    ]
    stdout = b"\n".join(json.dumps(row).encode() for row in rows) + b"\nnot json\n"
    backend = StatsBackend(stdout=stdout)
    samples = DockerCli(backend).stats((CONTAINER_A, CONTAINER_B), timeout_seconds=5)
    assert samples == {CONTAINER_A: (50.0, 150.5)}
    argv, timeout = backend.calls[0]
    assert argv[:4] == ("docker", "stats", "--no-stream", "--no-trunc") and timeout == 5
    assert DockerCli(StatsBackend()).stats((), timeout_seconds=5) == {}
    with pytest.raises(ValueError):
        DockerCli(StatsBackend()).stats(("../x",), timeout_seconds=5)
    with pytest.raises(RuntimeError):
        DockerCli(StatsBackend(code=1)).stats((CONTAINER_A,), timeout_seconds=5)


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        (StatsBackend(), True),
        (StatsBackend(code=1), False),
        (StatsBackend(error=TimeoutError("slow")), False),
        (StatsBackend(error=OSError("no docker")), False),
    ],
)
def test_docker_ping(backend, expected):
    assert DockerCli(backend).ping(timeout_seconds=3) is expected


class Clock:
    def __init__(self):
        self.ns = 1_000_000_000_000

    def __call__(self):
        return self.ns


def _agent() -> WorkerAgent:
    agent = WorkerAgent.for_test(None, worker_id="w", incarnation_id="i")
    agent.monotonic_ns = Clock()
    agent._loop_beat_ns = agent.monotonic_ns()
    return agent


def test_readiness_needs_reconciliation_and_a_heartbeat_accepted_within_30_seconds():
    agent = _agent()
    assert agent.readiness_checks() == {"reconciled": "fail", "heartbeat": "fail"}
    agent._reconcile_complete = True
    agent._heartbeat_accepted_ns = agent.monotonic_ns()
    assert agent.readiness_checks() == {"reconciled": "ok", "heartbeat": "ok"}
    agent.monotonic_ns.ns += 30 * 10**9
    assert agent.readiness_checks()["heartbeat"] == "ok"
    agent.monotonic_ns.ns += 1
    assert agent.readiness_checks()["heartbeat"] == "fail"
    agent._heartbeat_accepted_ns = agent.monotonic_ns()
    agent._readiness_blocked = True
    assert agent.readiness_checks() == {"reconciled": "fail", "heartbeat": "ok"}


def test_liveness_goes_stale_after_60_seconds_without_a_heartbeat_iteration():
    agent = _agent()
    assert agent.live()
    agent.monotonic_ns.ns += 60 * 10**9 - 1
    assert agent.live()
    agent.monotonic_ns.ns += 1
    assert not agent.live()


def test_loop_failures_are_counted_by_closed_operation_name():
    agent = _agent()
    agent.operation_timeout_seconds = 1.0
    before = _value(m.LOOP_FAILURES, "renew")
    calls = []

    def renew_once():
        calls.append(1)
        agent._stop.set()
        raise WorkerTransportError("api down")

    asyncio.run(agent._loop(renew_once, 0.01))
    assert calls and _value(m.LOOP_FAILURES, "renew") == before + 1
    m.loop_failed("not_a_loop")  # unknown names fall back to a closed label
    assert set(
        sample.labels["operation"]
        for metric in m.LOOP_FAILURES.collect()
        for sample in metric.samples
    ) <= set(m.LOOP_OPERATIONS)


def test_stats_sampler_failure_is_counted_and_never_blocks_readiness():
    agent = _agent()
    agent._reconcile_complete = True
    agent._server_ready = True
    agent._heartbeat_accepted_ns = agent.monotonic_ns()

    def broken():
        agent._stop.set()
        raise RuntimeError("docker stats failed")

    agent.sample_containers = broken
    before = _value(m.LOOP_FAILURES, "stats")
    asyncio.run(agent._stats_loop())
    assert _value(m.LOOP_FAILURES, "stats") == before + 1
    assert agent.readiness_checks() == {"reconciled": "ok", "heartbeat": "ok"}
    assert agent.reported_ready() and not agent._readiness_blocked


def test_sampler_reports_largest_ratios_relative_to_granted_cpu(tmp_path):
    peer = GatedPeer("none")
    request, identity, journal, _state, agent = _running_attempt(tmp_path, peer)
    cpu_millis = journal.load(request.context.authority.attempt_id).resources.cpu_millis
    stdout = json.dumps(
        {"ID": identity.container_id, "MemPerc": "40%", "CPUPerc": f"{cpu_millis / 10}%"}
    ).encode()
    backend = StatsBackend(stdout=stdout)
    agent.docker = DockerCli(backend)
    agent.sample_containers()
    assert _value(m.MEMORY_RATIO) == pytest.approx(0.4)
    assert _value(m.CPU_RATIO) == pytest.approx(1.0)
    agent._adopted.clear()
    agent.sample_containers()
    assert _value(m.MEMORY_RATIO) == 0 and _value(m.CPU_RATIO) == 0


def test_failure_outcome_counts_once_after_the_acknowledgement(tmp_path):
    peer = GatedPeer("none")
    peer.fail_transport_once = True
    request, _identity, _journal, state, agent = _running_attempt(tmp_path, peer)
    attempt_id = request.context.authority.attempt_id
    oom, executions = _value(m.OOM), _value(m.EXECUTIONS, "OOM")

    with pytest.raises(WorkerTransportError):
        agent._execution_failed(attempt_id, failure_class="OOM", reason_code="OOM_KILLED")
    assert _value(m.EXECUTIONS, "OOM") == executions  # response lost: not acknowledged
    [callback_id] = state.operations
    assert agent._send_resolution(callback_id) is True  # replay finishes it
    assert agent._execution_failed(attempt_id, failure_class="OOM", reason_code="OOM_KILLED")
    assert _value(m.EXECUTIONS, "OOM") == executions + 1
    assert _value(m.OOM) == oom + 1


def test_completion_counts_only_an_accepted_acknowledgement(tmp_path):
    from nexa.worker.journal import ExecutionJournal
    from nexa.worker.models import ResourceVector
    from nexa.worker.result_flow import ResultFlow
    from tests.worker.test_journal import ALLOC, ATTEMPT, AUTHORITY, BINDING, NONCE

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

    class Client:
        accepted = False

        def complete(self, attempt, callback, body):
            return {"accepted": self.accepted}

    client = Client()
    flow = ResultFlow(journal, client, None, None)
    before = _value(m.EXECUTIONS, "SUCCEEDED")
    with pytest.raises(ValueError):
        flow._complete(ATTEMPT, AUTHORITY, "callback", {})
    assert _value(m.EXECUTIONS, "SUCCEEDED") == before
    client.accepted = True
    flow._complete(ATTEMPT, AUTHORITY, "callback", {})
    assert _value(m.EXECUTIONS, "SUCCEEDED") == before + 1


def _get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def test_worker_ops_listener_reports_docker_and_ready_gauge():
    agent = _agent()
    agent._reconcile_complete = True
    agent._heartbeat_accepted_ns = agent.monotonic_ns()
    agent._server_ready = True
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    ops = WorkerOps(("127.0.0.1", port), agent, DockerCli(StatsBackend()))
    ops.start()
    try:
        code, body = _get(port, "/readyz")
        assert code == 200
        assert json.loads(body) == {
            "status": "ready",
            "checks": {"reconciled": "ok", "heartbeat": "ok", "docker": "ok"},
        }
        code, metrics = _get(port, "/metrics")
        assert code == 200
        assert "nexa_worker_ready 1.0" in metrics
        assert 'nexa_ready{check="docker"} 1.0' in metrics
        assert "process_" not in metrics and "python_" not in metrics
        agent._server_ready = False
        assert "nexa_worker_ready 0.0" in _get(port, "/metrics")[1]
        assert _get(port, "/livez")[0] == 200
        assert _get(port, "/v1/workers")[0] == 404
    finally:
        ops.stop()

    down = WorkerOps(("127.0.0.1", port), agent, DockerCli(StatsBackend(error=OSError())))
    report = down._probe()
    assert report.checks["docker"] == "fail" and not report.ready
    assert _value(m.READY, "docker") == 0


def test_worker_main_rejects_an_invalid_ops_bind_before_starting(monkeypatch):
    from nexa.worker import main as worker_main

    monkeypatch.setattr("sys.argv", ["nexa-worker"])
    monkeypatch.setenv("NEXA_OPS_BIND", "no-port")
    monkeypatch.setattr(worker_main, "run", lambda *_a, **_k: pytest.fail("run must not start"))
    with pytest.raises(SystemExit, match="NEXA_OPS_BIND"):
        worker_main.main()


def test_worker_metrics_scrape_alone_tracks_readiness():
    # B19-RV02: only /metrics is scraped; Docker down shows 0, then 1 after recovery.
    import time

    agent = _agent()
    agent._reconcile_complete = True
    agent._heartbeat_accepted_ns = agent.monotonic_ns()
    backend = StatsBackend()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    ops = WorkerOps(("127.0.0.1", port), agent, DockerCli(backend))
    ops.start()

    def poll(expected: float) -> None:
        deadline = time.monotonic() + 10
        while f'nexa_ready{{check="docker"}} {expected}' not in _get(port, "/metrics")[1]:
            assert time.monotonic() < deadline, f"docker readiness never became {expected}"
            time.sleep(0.2)

    try:
        poll(1.0)
        backend.error = OSError("docker down")
        poll(0.0)
        backend.error = None
        poll(1.0)
    finally:
        ops.stop()
