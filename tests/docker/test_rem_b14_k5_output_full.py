"""B14-K5 on real Docker: a full `/output` fails the checkpoint copy as INTERNAL, not a retry.

The runner stages a checkpoint copy on the attempt's bounded `/output` tmpfs, shared
with the workload. The test freezes only the CPU workload process (SIGSTOP, so no
snapshot write is in flight), fills the rest of `/output` as the workload UID, and
lets the first periodic checkpoint find the device full. The copy fails ENOSPC in
the runner: the attempt must end `INTERNAL/CHECKPOINT_STORAGE_FAILED`, the Job
`FAILED` without spending a retry, the open reservation `ABANDONED`, never
`INFRASTRUCTURE/RUNNER_UNAVAILABLE` (workloads-checkpoints.md, `INTERNAL` row).

Evidence rows contain identifiers, states, reasons and byte counts only.
"""

import json
import os
import subprocess
import time
from uuid import UUID

import pytest
from sqlalchemy import select

from nexa.infrastructure.persistence import schema as s
from tests.api.test_http_contract import _client
from tests.docker.test_b11_vertical import _check, _worker_ready
from tests.docker.test_b14_checkpoint_restore import (
    ITERATIONS,
    TERMINAL,
    _event_pairs,
    _Harness,
    _template,
    _wait,
)

pytestmark = [pytest.mark.docker, pytest.mark.postgres]

# The longest interval the template allows: the freeze and fill finish well before
# the worker's first periodic checkpoint request.
INTERVAL_SECONDS = 60

_STATE_PRESENT = "import os,sys; sys.exit(0 if os.path.exists('/output/state.json') else 1)"

_FREEZE_AND_FILL = """
import errno, json, os, signal, time


def argv(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read().split(b"\\0")
    except OSError:
        return []


# The CPU workload is the supervisor's child; only the supervisor also names its module.
(pid,) = [
    int(entry)
    for entry in os.listdir("/proc")
    if entry.isdigit()
    and b"--state-output" in argv(entry)
    and b"nexa.workloads.workload_supervisor" not in argv(entry)
]
os.kill(pid, signal.SIGSTOP)
deadline = time.monotonic() + 5
while open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0] != "T":
    if time.monotonic() > deadline:
        raise SystemExit("workload did not stop")
    time.sleep(0.01)
descriptor = os.open("/output/.rem-k5-fill", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
size, written = 1 << 20, 0
while size:
    try:
        written += os.write(descriptor, bytes(size))
    except OSError as exc:
        if exc.errno != errno.ENOSPC:
            raise
        size //= 2
os.fsync(descriptor)
os.close(descriptor)
print(json.dumps({
    "filled_bytes": written,
    "free_blocks": os.statvfs("/output").f_bfree,
    "state_present": os.path.exists("/output/state.json"),
}))
"""


def _exec(container, script):
    return subprocess.run(
        ["docker", "exec", "--user", "1001:1000", container, "python", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _wait_for_first_snapshot(container, timeout=30, pause=0.2):
    """Wait for the workload's first state snapshot; on timeout, say why it is absent."""
    until = time.monotonic() + timeout
    last = None
    while time.monotonic() < until:
        last = _exec(container, _STATE_PRESENT)
        if last.returncode == 0:
            return
        time.sleep(pause)
    inspect = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}} exit={{.State.ExitCode}}", container],
        capture_output=True,
        text=True,
        timeout=15,
    )
    top = subprocess.run(
        ["docker", "top", container, "-eo", "pid,uid,args"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    raise AssertionError(
        "first workload snapshot: "
        f"exec_rc={last.returncode if last else None} "
        f"exec_stderr={(last.stderr[-500:] if last else '')!r} "
        f"container={(inspect.stdout or inspect.stderr).strip()!r} "
        f"processes={(top.stdout or top.stderr)[-1500:]!r}"
    )


def _submit(harness, key):
    spec = {
        "template_id": "cpu-iterative",
        "template_version": 1,
        "input_artifact_id": harness.input_artifact_id,
        "resources": {"cpu_millis": 1000, "memory_bytes": 512 * 1024**2, "gpu_count": 0},
        "priority": 1,
        "runtime_limit_seconds": 300,
        "checkpoint_interval_seconds": INTERVAL_SECONDS,
        "parameters": {"iterations": ITERATIONS, "seed": 7, "modulus": 2_147_483_647},
    }
    accepted = _check(
        harness.client.post(
            "/v1/jobs", headers={**harness.write, "Idempotency-Key": key}, json={"spec": spec}
        ),
        202,
    )
    harness.submitted[accepted["job_id"]] = key
    return accepted["job_id"]


def test_rem_b14_k5_full_output_fails_the_checkpoint_as_internal_without_a_retry(
    migrated_postgres_engine, tmp_path
):
    if os.environ.get("NEXA_RUN_DOCKER") != "1":
        pytest.skip("opt-in B14-K5 Docker evidence")
    image = os.environ["NEXA_B09_IMAGE_REF"]
    worker_image = os.environ["NEXA_B11_WORKER_IMAGE"]
    assert "@sha256:" in image
    architecture = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image],
        text=True,
        timeout=10,
    ).strip()
    engine = migrated_postgres_engine
    _template(engine, image, architecture)
    evidence = {"image": image, "architecture": architecture, "worker_image": worker_image}
    with _client(engine, tmp_path) as client:
        harness = _Harness(engine, tmp_path, client, image, worker_image)
        try:
            harness.bootstrap()
            harness.start_api()
            harness.start_worker()
            try:
                _wait(lambda: _worker_ready(engine), "worker READY", timeout=40)
            except AssertionError as exc:
                raise AssertionError(f"{exc}; worker_log={harness.worker_logs()}") from exc
            harness.coordinator.start()

            job = _submit(harness, "rem-k5-output-full")
            attempt, container = harness.running_attempt(job, 1)
            _wait_for_first_snapshot(container)
            probe = _exec(container, _FREEZE_AND_FILL)
            assert probe.returncode == 0, probe.stderr[-2000:]
            filled = json.loads(probe.stdout)
            filled_at = time.monotonic()
            assert filled["free_blocks"] == 0 and filled["state_present"], filled
            # The fill precedes the first checkpoint request: nothing reserved yet.
            assert harness.reserved(attempt["attempt_id"]) is None
            assert harness.checkpoints(job) == []
            evidence["fill"] = filled

            _wait(
                lambda: harness.job(job)["state"] in TERMINAL,
                f"job {job} terminal",
                timeout=INTERVAL_SECONDS + 90,
            )
            evidence["seconds_fill_to_terminal"] = round(time.monotonic() - filled_at, 1)

            def released():
                with engine.connect() as connection:
                    states = list(
                        connection.execute(
                            select(s.allocations.c.state).where(s.allocations.c.job_id == UUID(job))
                        ).scalars()
                    )
                return states == ["RELEASED"]

            _wait(released, f"allocation release of {job}", timeout=30)
            timeline = harness.timeline(job)
            evidence["timeline"] = timeline

            assert timeline["job"]["state"] == "FAILED"
            assert timeline["job"]["desired_state"] == "RUNNING"
            assert timeline["job"]["retry_count"] == 0
            assert [
                (row["state"], row["failure_class"], row["failure_reason"])
                for row in timeline["attempts"]
            ] == [("FAILED", "INTERNAL", "CHECKPOINT_STORAGE_FAILED")]
            # The copy failed before any file was announced: the cycle is abandoned.
            assert [(row["sequence"], row["state"]) for row in timeline["reservations"]] == [
                (1, "ABANDONED")
            ]
            assert timeline["checkpoints"] == []
            assert timeline["results"] == []
            assert timeline["retries"] == []
            pairs = _event_pairs(timeline)
            assert ("ATTEMPT_FAILED", "CHECKPOINT_STORAGE_FAILED") in pairs
            assert all(reason != "RUNNER_UNAVAILABLE" for _event, reason in pairs)
            assert [row["sequence"] for row in timeline["events"]] == list(
                range(1, timeline["job"]["event_sequence"] + 1)
            )
            assert {row["state"] for row in timeline["allocations"]} == {"RELEASED"}
            assert all(row["stopped"] and row["verified"] for row in timeline["containers"])
            assert harness.containers(attempt["attempt_id"]) == [], "leaked workload container"
            with engine.connect() as connection:
                counters = set(
                    connection.execute(
                        select(
                            s.admission_counters.c.outstanding,
                            s.admission_counters.c.active_attempts,
                        )
                    ).all()
                )
            assert counters == {(0, 0)}
            print("REM B14-K5 docker: full /output -> INTERNAL/CHECKPOINT_STORAGE_FAILED")
        finally:
            output = os.environ.get("NEXA_REM_K5_EVIDENCE_OUT")
            if output:
                with open(output, "w", encoding="utf-8") as handle:
                    json.dump(evidence, handle, indent=1, sort_keys=True, default=str)
            harness.close()
