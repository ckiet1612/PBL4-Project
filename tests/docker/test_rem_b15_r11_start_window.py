"""Remediation B15-R11 on real Docker: the runner dies after /start, before its deadline.

Reuses the B15 stack (production API, coordinator thread, worker container, CPU workload
image). The start callback locks the Job row; the test holds that row, freezes the worker
while /start waits, lets /start commit, acts on the exact runner container and only then
thaws the worker, so the start ACK is read before the runner accepts any deadline.

* Killed runner: Docker proves the exit, so the attempt fails at once as retryable
  INFRASTRUCTURE/RUNNER_UNAVAILABLE and a new attempt produces the uninterrupted result.
  Before the fix the worker replayed until the 30-second budget and reported a
  non-retryable TIMEOUT/STARTUP_TIMEOUT.
* Paused runner (negative): the container still runs, so nothing proves an exit; the
  replay continues and a genuine STARTUP_TIMEOUT stays TIMEOUT without a retry.

Evidence rows contain identifiers, states, timestamps and checksums only.
"""

import subprocess
import threading
import time
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from nexa.infrastructure.persistence import schema as s
from tests.api.test_http_contract import _client
from tests.docker.test_b14_checkpoint_restore import _event_pairs, _ordered, _template, _wait
from tests.docker.test_b15_control_recovery import (
    _B15Harness,
    _docker_images,
    _start_stack,
    _write_evidence,
)
from tests.docker.test_rem_b11_h01_claim_window import _blocked_on_job_row

pytestmark = [pytest.mark.docker, pytest.mark.postgres]


def _waiting_on_lock(engine, pid):
    with engine.connect() as observer:
        return (
            observer.execute(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            ).scalar_one_or_none()
            == "Lock"
        )


def _docker(*argv):
    subprocess.run(["docker", *argv], check=True, capture_output=True, timeout=15)


def _container_state(container):
    output = subprocess.check_output(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Running}} {{.State.Paused}} {{.State.ExitCode}} {{.State.OOMKilled}}",
            container,
        ],
        text=True,
        timeout=10,
    ).split()
    return {
        "running": output[0] == "true",
        "paused": output[1] == "true",
        "exit_code": int(output[2]),
        "oom_killed": output[3] == "true",
    }


class _JobRowHolder:
    """One test connection that holds, or queues for, the Job row lock until released."""

    def __init__(self, engine, job):
        self.engine = engine
        self.row = select(s.jobs.c.job_id).where(s.jobs.c.job_id == UUID(job)).with_for_update()
        self.pid = None
        self.connected, self.locked, self.done = (threading.Event() for _ in range(3))
        self.errors = []
        self.thread = threading.Thread(target=self._hold, name="rem-r11-job-row", daemon=True)
        self.thread.start()
        assert self.connected.wait(timeout=10) and not self.errors, self.errors

    def _hold(self):
        try:
            with self.engine.connect() as connection:
                transaction = connection.begin()
                self.pid = connection.execute(select(func.pg_backend_pid())).scalar_one()
                self.connected.set()
                connection.execute(self.row)
                self.locked.set()
                self.done.wait(timeout=120)
                transaction.rollback()
        except BaseException as exc:  # pragma: no cover - asserted by the caller
            self.errors.append(exc)
            self.connected.set()
            self.locked.set()

    def release(self):
        self.done.set()
        self.thread.join(timeout=30)
        assert not self.thread.is_alive() and not self.errors, self.errors


def _start_committed_with_worker_frozen(harness, engine, key):
    """Submit a Job and return (job, attempt, container) with /start committed, worker frozen.

    Every worker request that locks the Job row (claim, input download, /start) first
    waits on a test holder. Before the holder is released a successor queues behind
    that request, so the worker cannot pass two requests unobserved. The request
    waiting once the workload container exists is /start: the worker is frozen, the
    holder released, and /start commits while its acknowledgment stays unread.
    """
    _docker("pause", harness.worker)
    job = harness.submit(key)
    try:
        (created,) = _wait(lambda: harness.attempts(job), "attempt dispatched", timeout=30)
    except AssertionError as exc:
        worker = harness.worker_row()
        with engine.connect() as connection:
            now = connection.execute(select(func.now())).scalar_one()
            reservations = connection.execute(
                select(s.reservations.c.job_id, s.reservations.c.invalidated_at)
            ).all()
        raise AssertionError(
            f"{exc}; job_state={harness.job(job)['state']}; worker="
            f"{worker['health']}/{worker['admin_state']}; heartbeat_age="
            f"{now - worker['last_heartbeat_at']}; reservations={reservations}"
        ) from exc
    assert created["state"] == "CREATED"
    holders = [_JobRowHolder(engine, job)]
    try:
        assert holders[-1].locked.wait(timeout=10)
        _docker("unpause", harness.worker)
        passed = 0
        while True:
            _wait(
                lambda: _blocked_on_job_row(engine, holders[-1].pid),
                f"worker request {passed + 1} waiting on the Job row",
                timeout=30,
                pause=0.01,
            )
            (attempt,) = harness.attempts(job)
            if attempt["state"] == "CLAIMED" and harness.containers(attempt["attempt_id"]):
                break
            assert passed < 5, "the worker kept locking the Job row before /start"
            successor = _JobRowHolder(engine, job)
            holders.append(successor)
            _wait(
                lambda pid=successor.pid: _waiting_on_lock(engine, pid),
                "successor queued behind the worker request",
                timeout=10,
                pause=0.01,
            )
            holders[-2].release()
            assert successor.locked.wait(timeout=30) and not successor.errors, successor.errors
            passed += 1
        # Claim and input download passed; /start waits and nothing started the runtime.
        assert passed >= 2 and attempt["started_at"] is None, (passed, attempt)
        _docker("pause", harness.worker)
    finally:
        for holder in holders:
            holder.release()

    def started():
        (attempt,) = harness.attempts(job)
        return attempt if attempt["started_at"] is not None else None

    attempt = _wait(started, "/start committed while the worker is frozen", timeout=30)
    (container,) = harness.containers(attempt["attempt_id"])
    return job, attempt, container


def _failed(harness, job):
    def reported():
        (attempt, *_) = harness.attempts(job)
        return attempt if attempt["failure_reason"] is not None else None

    return reported


def test_rem_b15_r11_runner_killed_after_start_before_deadline_on_docker(
    migrated_postgres_engine, tmp_path
):
    image, worker_image, architecture = _docker_images()
    engine = migrated_postgres_engine
    _template(engine, image, architecture)
    evidence = {"image": image, "architecture": architecture, "worker_image": worker_image}
    with _client(engine, tmp_path) as client:
        harness = _B15Harness(engine, tmp_path, client, image, worker_image)
        try:
            evidence["epoch"] = _start_stack(harness, engine)
            baseline = harness.submit("rem-b15-r11-baseline")
            harness.wait_succeeded(baseline)
            r0 = harness.result(baseline)
            harness.settle(baseline, "SUCCEEDED", 1)

            # Positive: the runner container is SIGKILLed inside the window.
            job, first, container = _start_committed_with_worker_frozen(
                harness, engine, "rem-b15-r11-killed"
            )
            _docker("kill", "--signal", "KILL", container)
            killed = _wait(
                lambda: (lambda st: None if st["running"] else st)(_container_state(container)),
                "runner container exited",
                timeout=15,
                pause=0.05,
            )
            assert (killed["exit_code"], killed["oom_killed"]) == (137, False), killed
            _docker("unpause", harness.worker)
            thawed = time.monotonic()
            reported = _wait(_failed(harness, job), "attempt failure reported", timeout=60)
            reported_after = round(time.monotonic() - thawed, 3)
            assert (reported["failure_class"], reported["failure_reason"]) == (
                "INFRASTRUCTURE",
                "RUNNER_UNAVAILABLE",
            ), reported
            # Reported from the Docker-proven exit, not after the 30-second budget.
            assert reported_after < 20, reported_after
            harness.wait_succeeded(job)
            r1 = harness.result(job)
            timeline = harness.settle(job, "SUCCEEDED", 2)
            attempts = timeline["attempts"]
            assert [
                (row["state"], row["failure_class"], row["failure_reason"]) for row in attempts
            ] == [
                ("FAILED", "INFRASTRUCTURE", "RUNNER_UNAVAILABLE"),
                ("SUCCEEDED", None, None),
            ], attempts
            assert attempts[1]["job_fence"] > attempts[0]["job_fence"]
            assert timeline["job"]["retry_count"] == 1
            assert _ordered(
                _event_pairs(timeline),
                [
                    ("ATTEMPT_STARTED", "attempt_started"),
                    ("ATTEMPT_FAILED", "RUNNER_UNAVAILABLE"),
                    ("ALLOCATION_RELEASED", "VERIFIED_CLEANUP"),
                    ("RETRY_READY", "BACKOFF_ELAPSED"),
                ],
            ), _event_pairs(timeline)
            assert r1["checksum"] == r0["checksum"]
            harness.assert_counters()
            evidence["killed_runner"] = {
                "attempt_id": str(first["attempt_id"]),
                "container_id": container[:12],
                "container_exit": killed,
                "reported_after_thaw_seconds": reported_after,
                "checksum": r1["checksum"],
                "timeline": timeline,
            }

            # Negative: a paused runner still runs; only the budget ends it.
            job, first, container = _start_committed_with_worker_frozen(
                harness, engine, "rem-b15-r11-paused"
            )
            _docker("pause", container)
            paused = _container_state(container)
            assert (paused["running"], paused["paused"]) == (True, True), paused
            _docker("unpause", harness.worker)
            thawed = time.monotonic()
            reported = _wait(_failed(harness, job), "startup timeout reported", timeout=90)
            reported_after = round(time.monotonic() - thawed, 3)
            assert (reported["failure_class"], reported["failure_reason"]) == (
                "TIMEOUT",
                "STARTUP_TIMEOUT",
            ), reported
            # Thaw the runner unless the worker's cleanup already stopped it; settle()
            # below proves the exact container was stopped and verified either way.
            if _container_state(container)["paused"]:
                subprocess.run(["docker", "unpause", container], capture_output=True, timeout=15)
            timeline = harness.settle(job, "FAILED", 1)
            (attempt,) = timeline["attempts"]
            assert (attempt["state"], attempt["failure_class"], attempt["failure_reason"]) == (
                "FAILED",
                "TIMEOUT",
                "STARTUP_TIMEOUT",
            ), attempt
            assert timeline["job"]["retry_count"] == 0
            assert ("RETRY_READY", "BACKOFF_ELAPSED") not in _event_pairs(timeline)
            harness.assert_counters()
            evidence["paused_runner"] = {
                "attempt_id": str(first["attempt_id"]),
                "container_id": container[:12],
                "reported_after_thaw_seconds": reported_after,
                "timeline": timeline,
            }
        finally:
            harness.close()
            _write_evidence(evidence, "-rem-b15-r11")
