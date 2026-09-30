"""Opt-in B15 cancel, pause/resume, recovery, worker admin and full restart on real Docker.

Reuses the B14 stack: the production API (in-process uvicorn), the coordinator
loop (in-process thread), the local worker as a container and CPU workload
containers. Faults are injected only by this test: SIGKILL of a workload or
worker container, cutting the worker off the control plane (bridge disconnect; in
loopback mode a 127.0.0.1 relay that resets its connections) and
stopping every control-plane process while PostgreSQL and storage stay up.

Evidence rows contain identifiers, states, sequences, timestamps and checksums
only; no credential, input, checkpoint content or free-text reason is recorded.
"""

import json
import math
import os
import subprocess
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select, text, update

from nexa.infrastructure.persistence import schema as s
from tests.api.test_http_contract import _client
from tests.docker.test_b11_vertical import _worker_ready, _worker_ready_with_new_incarnation
from tests.docker.test_b14_checkpoint_restore import (
    INTERVAL_SECONDS,
    ITERATIONS,
    TERMINAL,
    _Coordinator,
    _event_pairs,
    _Harness,
    _ordered,
    _template,
    _wait,
)
from tests.integration.test_worker_api_b10 import WORKER_ID

pytestmark = [pytest.mark.docker, pytest.mark.postgres]


def _timeout_limit_seconds(baseline):
    """C8 runtime limit derived from the uninterrupted C0 run on this host.

    C8 must stop short of the result (limit < compute T), and C7a inherits the same
    limit, so it must finish from C8's newest checkpoint: that checkpoint is at most one
    checkpoint gap G behind the limit, hence 2 * limit >= T + G (+1 s restore). A fixed
    28 s sat below that bound once throughput dropped ~15% (run 18). The midpoint of
    the two bounds leaves the same margin on both sides.
    """
    at = {}
    committed = []
    for event in baseline["events"]:
        moment = datetime.fromisoformat(event["at"])
        if event["event_type"] == "CHECKPOINT_COMMITTED":
            committed.append(moment)
        else:
            at.setdefault(event["event_type"], moment)
    started = at["ATTEMPT_STARTED"]
    compute = (at["RESULT_RECOGNIZED"] - started).total_seconds()
    marks = [started, *committed]
    gap = max((b - a).total_seconds() for a, b in zip(marks, marks[1:], strict=False))
    limit = math.ceil((3 * compute + gap + 1) / 4)
    assert gap + 1 < limit < compute, (compute, gap, limit)
    return limit, {"compute_seconds": round(compute, 3), "checkpoint_gap_seconds": round(gap, 3)}


LEASE_SECONDS = 45


def _not_restart_safe(engine):
    """Fixture the one CPU template as not restart-safe before any Job references it.

    The contract fixes `template_id` to `cpu-iterative` and a referenced template
    version is immutable, so this slice needs its own database.
    """
    with engine.begin() as connection:
        connection.execute(
            update(s.template_versions)
            .where(s.template_versions.c.template_id == "cpu-iterative")
            .values(restart_safe=False)
        )


def _docker_images():
    if os.environ.get("NEXA_RUN_DOCKER") != "1":
        pytest.skip("opt-in B15 Docker control/recovery evidence")
    image = os.environ["NEXA_B09_IMAGE_REF"]
    worker_image = os.environ["NEXA_B11_WORKER_IMAGE"]
    assert "@sha256:" in image
    architecture = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image],
        text=True,
        timeout=10,
    ).strip()
    return image, worker_image, architecture


def _start_stack(harness, engine):
    """Start API, worker and coordinator; return the first coordinator epoch."""
    harness.bootstrap()
    harness.start_api()
    harness.start_worker()
    try:
        _wait(lambda: _worker_ready(engine), "worker READY", timeout=40)
    except AssertionError as exc:
        raise AssertionError(f"{exc}; worker_log={harness.worker_logs()}") from exc
    harness.coordinator.start()
    return _wait(harness.epoch, "coordinator leadership", timeout=30)


def _write_evidence(evidence, suffix=""):
    output = os.environ.get("NEXA_B15_EVIDENCE_OUT")
    if output:
        root, extension = os.path.splitext(output)
        with open(f"{root}{suffix}{extension}", "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, indent=1, sort_keys=True, default=str)


def _docker_time(value):
    # Docker reports RFC 3339 with nanoseconds; microseconds are enough here.
    head, _, rest = value.partition(".")
    fraction = rest.rstrip("Z")[:6].ljust(6, "0")
    return datetime.fromisoformat(f"{head}.{fraction}+00:00")


class _ContainerWatch:
    """Poll one container so its stop time is known even if the worker removes it."""

    def __init__(self, container):
        self.container = container
        self.state = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            inspected = subprocess.run(
                ["docker", "inspect", "--format", "{{json .State}}", self.container],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if inspected.returncode != 0:
                return
            state = json.loads(inspected.stdout)
            self.state = state
            if not state["Running"]:
                return
            time.sleep(0.2)

    def finished_at(self):
        if self.state is None or self.state["Running"]:
            return None
        return _docker_time(self.state["FinishedAt"])

    def close(self):
        self.stop.set()
        self.thread.join(timeout=15)


# Prints only the runner's STOPPED instant and one (monotonic, wall) clock pair
# read inside the container; the state file itself never leaves it.
_RUNNER_STOP_PROBE = (
    "import json,time\n"
    "state=json.load(open('/run/nexa/runner-state.json'))\n"
    "stops=[m['payload'] for m in state['pending_messages'] if m['type']=='STOPPED']\n"
    "mono,wall=time.monotonic_ns(),time.time_ns()\n"
    "print(json.dumps({'stops':[[p['reason'],p['stopped_monotonic_ns']] for p in stops],"
    "'mono':mono,'wall':wall}))"
)


def _runner_stop_time(container, watch):
    """When the runner stopped compute, as (instant, source).

    A runner that stopped keeps serving its STOPPED frame until a worker ACKs
    it or its 3 s linger ends (B15-R16), so its container outlives the stop; its own
    `stopped_monotonic_ns`, mapped to the container's wall clock, is exact. A
    runner that already exited leaves the container `FinishedAt`, an upper bound.
    """
    probe = subprocess.run(
        ["docker", "exec", "--user", "1000:1000", container, "python", "-c", _RUNNER_STOP_PROBE],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if probe.returncode == 0:
        observed = json.loads(probe.stdout)
        if observed["stops"]:
            ((reason, stopped_ns),) = observed["stops"]
            wall_ns = observed["wall"] - (observed["mono"] - stopped_ns)
            instant = datetime.fromtimestamp(wall_ns / 1_000_000_000, tz=UTC)
            return instant, f"runner STOPPED{{{reason}}}"
    finished = watch.finished_at()
    return (finished, "container FinishedAt") if finished is not None else None


class _B15Harness(_Harness):
    # -- operations ------------------------------------------------------------
    def submit(self, key, *, runtime_limit_seconds=300):
        spec = {
            "template_id": "cpu-iterative",
            "template_version": 1,
            "input_artifact_id": self.input_artifact_id,
            "resources": {"cpu_millis": 1000, "memory_bytes": 512 * 1024**2, "gpu_count": 0},
            "priority": 1,
            "runtime_limit_seconds": runtime_limit_seconds,
            "checkpoint_interval_seconds": INTERVAL_SECONDS,
            "parameters": {"iterations": ITERATIONS, "seed": 7, "modulus": 2_147_483_647},
        }
        response = self.client.post(
            "/v1/jobs", headers={**self.write, "Idempotency-Key": key}, json={"spec": spec}
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        self.submitted[job_id] = key
        return job_id

    def etag(self, job_id):
        response = self.client.get(
            f"/v1/jobs/{job_id}", headers={"X-Nexa-Tenant-Id": self.tenant_id}
        )
        assert response.status_code == 200, response.text
        return response.headers["ETag"]

    def control(self, job_id, action, key, *, status=202, etag=None, checkpoint_id=None):
        body = {"reason": "b15 docker scenario"}
        if action == "retry":
            body["checkpoint_id"] = checkpoint_id
        response = self.client.post(
            f"/v1/jobs/{job_id}/{action}",
            headers={
                **self.write,
                "If-Match": etag or self.etag(job_id),
                "Idempotency-Key": key,
            },
            json=body,
        )
        assert response.status_code == status, response.text
        payload = response.json()
        if action == "retry" and status == 202:
            # The retry's own key owns the new job's submit idempotency record.
            self.submitted[payload["job_id"]] = key
        return payload, response.headers.get("ETag")

    def worker_admin(self, action, key, *, status):
        current = self.client.get(f"/v1/admin/workers/{WORKER_ID}")
        assert current.status_code == 200, current.text
        response = self.client.post(
            f"/v1/admin/workers/{WORKER_ID}/{action}",
            headers={
                "Origin": "https://nexa.test",
                "X-CSRF-Token": self.write["X-CSRF-Token"],
                "If-Match": current.headers["ETag"],
                "Idempotency-Key": key,
            },
            json={"reason": "b15 docker scenario"},
        )
        assert response.status_code == status, response.text
        return response.json()

    def worker_row(self):
        with self.engine.connect() as connection:
            query = select(s.workers).where(s.workers.c.worker_id == UUID(WORKER_ID))
            return connection.execute(query).mappings().one()

    def wait_state(self, job_id, states, timeout=120):
        states = {states} if isinstance(states, str) else set(states)

        def reached():
            job = self.job(job_id)
            if job["state"] in states:
                return job
            if job["state"] in TERMINAL:
                raise AssertionError(f"job {job_id} ended {job['state']}, wanted {states}")
            return None

        try:
            return _wait(reached, f"job {job_id} in {states}", timeout=timeout, pause=0.1)
        except AssertionError as exc:
            raise AssertionError(
                f"{exc}; timeline={json.dumps(self.timeline(job_id), default=str)[-6000:]}; "
                f"worker_log={self.worker_logs()}"
            ) from exc

    def allocation_states(self, job_id):
        with self.engine.connect() as connection:
            return [
                (str(row.attempt_id), row.state)
                for row in connection.execute(
                    select(s.allocations.c.attempt_id, s.allocations.c.state)
                    .where(s.allocations.c.job_id == UUID(job_id))
                    .order_by(s.allocations.c.held_at)
                )
            ]

    def charge(self, attempt_id):
        """(open segments, charged total) of the attempt's allocation, summed as Decimal."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    s.allocation_ledger_segments.c.ended_at,
                    s.allocation_ledger_segments.c.charged_amount,
                )
                .join(
                    s.allocations,
                    s.allocations.c.allocation_id == s.allocation_ledger_segments.c.allocation_id,
                )
                .where(s.allocations.c.attempt_id == attempt_id)
            ).all()
        return (
            sum(row.ended_at is None for row in rows),
            sum((Decimal(str(row.charged_amount)) for row in rows), Decimal(0)),
        )

    def lease(self, attempt_id):
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    select(s.attempt_leases)
                    .where(s.attempt_leases.c.attempt_id == attempt_id)
                    .order_by(s.attempt_leases.c.issued_at.desc())
                    .limit(1)
                )
                .mappings()
                .one()
            )

    def counters(self):
        with self.engine.connect() as connection:
            rows = set(
                connection.execute(
                    select(
                        s.admission_counters.c.outstanding, s.admission_counters.c.active_attempts
                    )
                ).all()
            )
            outstanding = connection.execute(
                select(func.count()).select_from(s.jobs).where(s.jobs.c.state.not_in(TERMINAL))
            ).scalar_one()
            active = connection.execute(
                select(func.count())
                .select_from(s.allocations)
                .where(s.allocations.c.state != "RELEASED")
            ).scalar_one()
        return rows, (outstanding, active)

    def assert_counters(self):
        rows, expected = self.counters()
        # One tenant and one submitter: GLOBAL, TENANT and USER scopes hold the same jobs.
        assert rows == {expected}, (rows, expected)
        return expected

    def epoch(self):
        with self.engine.connect() as connection:
            # The row appears with the first acquire of the coordinator thread.
            return connection.execute(select(s.coordinator_leadership.c.epoch)).scalar_one_or_none()

    def save_worker_log(self):
        """Append the full worker log to NEXA_B15_WORKER_LOG_OUT (diagnosis only, not evidence)."""
        out = os.environ.get("NEXA_B15_WORKER_LOG_OUT")
        if not out or self.worker is None:
            return
        logs = subprocess.run(
            ["docker", "logs", "--timestamps", self.worker],
            capture_output=True,
            text=True,
            timeout=30,
        )
        with open(out, "a") as handle:
            handle.write(f"== worker {self.worker}\n{logs.stdout}{logs.stderr}")

    def close(self):
        self.save_worker_log()
        super().close()

    def kill_worker(self):
        prior = self.worker_row()["current_incarnation_id"]
        self.save_worker_log()
        subprocess.run(
            ["docker", "kill", "--signal", "KILL", self.worker],
            check=True,
            capture_output=True,
            timeout=15,
        )
        subprocess.run(["docker", "rm", self.worker], check=True, capture_output=True, timeout=15)
        self.worker = None
        return prior

    def restart_worker(self, prior):
        self.start_worker()
        try:
            _wait(
                lambda: _worker_ready_with_new_incarnation(self.engine, prior),
                "worker READY with a new incarnation",
                timeout=90,
            )
        except AssertionError as exc:
            raise AssertionError(f"{exc}; worker_log={self.worker_logs()}") from exc

    def crash_after_pause_commit(self, job_id, attempt_id, container, committed):
        """Kill the workload after the pause checkpoint commits, before the worker learns it.

        The pause publish locks the Attempt's RESERVED checkpoint row; this test holds
        that row first, so the publish blocks. With the publish provably waiting, the
        worker container is frozen, the lock released so the publish commits, the
        workload killed, and only then is the worker thawed to read the ACK. No
        REQUEST_STOP{PAUSE} can reach the workload in between.
        """
        with self.engine.connect() as holder:
            transaction = holder.begin()
            try:
                pid = holder.execute(select(func.pg_backend_pid())).scalar_one()

                def reserved():
                    return holder.execute(
                        select(s.checkpoint_reservations.c.checkpoint_id)
                        .where(
                            s.checkpoint_reservations.c.attempt_id == attempt_id,
                            s.checkpoint_reservations.c.state == "RESERVED",
                        )
                        .with_for_update()
                    ).scalar_one_or_none()

                checkpoint_id = _wait(reserved, "pause checkpoint reserved", timeout=60, pause=0.01)
                assert self.job(job_id)["state"] == "PAUSING"

                def publish_waiting():
                    with self.engine.connect() as observer:
                        return observer.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity"
                                " WHERE :pid = ANY(pg_blocking_pids(pid))"
                            ),
                            {"pid": pid},
                        ).scalar_one()

                _wait(publish_waiting, "pause publish blocked", timeout=60, pause=0.01)
                subprocess.run(
                    ["docker", "pause", self.worker], check=True, capture_output=True, timeout=15
                )
            except BaseException:
                transaction.rollback()
                raise
            try:
                transaction.commit()
                rows = self.wait_committed(job_id, attempt_id, committed + 1, pause=0.01)
                assert rows[-1][0] == checkpoint_id
                self.kill_workload(container)

                def stopped():
                    state = json.loads(
                        subprocess.check_output(
                            ["docker", "inspect", "--format", "{{json .State}}", container],
                            text=True,
                            timeout=10,
                        )
                    )
                    return None if state["Running"] else state

                # Read while the worker is frozen: it removes the container after cleanup.
                state = _wait(stopped, "workload stopped", timeout=30)
            finally:
                subprocess.run(
                    ["docker", "unpause", self.worker], check=True, capture_output=True, timeout=15
                )
        return checkpoint_id, state["ExitCode"], _docker_time(state["FinishedAt"])

    def started_at(self, container):
        return _docker_time(
            subprocess.check_output(
                ["docker", "inspect", "--format", "{{.State.StartedAt}}", container],
                text=True,
                timeout=10,
            ).strip()
        )

    def computing_before_checkpoint(self, job_id, attempt_id, container):
        """Wait until the runner has launched the workload, then require no checkpoint yet.

        ATTEMPT_STARTED precedes the runner's authority deadline. The worker's supervisor
        exec is already present then; only after the deadline is accepted does the runner
        send START and the supervisor spawn the workload as its own process.
        """

        def launched():
            lines = subprocess.check_output(
                ["docker", "top", container], text=True, timeout=10
            ).splitlines()[1:]
            return any(
                "nexa.workloads.cpu_entrypoint" in line and "workload_supervisor" not in line
                for line in lines
            )

        _wait(launched, f"workload launched in {container}", timeout=30, pause=0.05)
        assert self.checkpoints(job_id, attempt_id) == [], "first checkpoint already committed"

    # -- evidence --------------------------------------------------------------
    def timeline(self, job_id):
        timeline = super().timeline(job_id)
        key = UUID(job_id)
        with self.engine.connect() as connection:
            extra = connection.execute(
                select(
                    s.jobs.c.version,
                    s.jobs.c.recovery_intent,
                    s.jobs.c.retry_of_job_id,
                    s.jobs.c.terminal_at.is_not(None).label("terminal"),
                ).where(s.jobs.c.job_id == key)
            ).one()
            timeline["lease_times"] = [
                {
                    "attempt_id": str(row.attempt_id),
                    "issued_at": row.issued_at.isoformat(timespec="milliseconds"),
                    "expires_at": row.expires_at.isoformat(timespec="milliseconds"),
                    "revoked_at": (
                        None
                        if row.revoked_at is None
                        else row.revoked_at.isoformat(timespec="milliseconds")
                    ),
                }
                for row in connection.execute(
                    select(s.attempt_leases)
                    .where(s.attempt_leases.c.job_id == key)
                    .order_by(s.attempt_leases.c.issued_at)
                )
            ]
            timeline["attempt_intents"] = [
                {"attempt_number": row.attempt_number, "execution_intent": row.execution_intent}
                for row in connection.execute(
                    select(s.attempts.c.attempt_number, s.attempts.c.execution_intent)
                    .where(s.attempts.c.job_id == key)
                    .order_by(s.attempts.c.attempt_number)
                )
            ]
        timeline["job"].update(
            version=extra.version,
            recovery_intent=extra.recovery_intent,
            retry_of_job_id=None if extra.retry_of_job_id is None else str(extra.retry_of_job_id),
            terminal=extra.terminal,
        )
        return timeline

    def settle(self, job_id, state, attempts, *, allocations=None):
        """Accepted ID, release-after-proof, counters, ledger, events, reservations, containers."""
        allocations = attempts if allocations is None else allocations

        def released():
            states = [row[1] for row in self.allocation_states(job_id)]
            return states == ["RELEASED"] * allocations

        _wait(released, f"allocation release of {job_id}", timeout=45)
        timeline = self.timeline(job_id)
        assert timeline["job"]["state"] == state, timeline["job"]
        assert timeline["submit_idempotency_records"] == 1
        assert len(timeline["attempts"]) == attempts
        if state == "SUCCEEDED":
            assert len(timeline["results"]) == 1
            assert timeline["results"][0]["attempt_id"] == timeline["attempts"][-1]["attempt_id"]
        else:
            assert timeline["results"] == []
        assert timeline["job"]["terminal"] == (state in TERMINAL)
        assert [row["sequence"] for row in timeline["events"]] == list(
            range(1, timeline["job"]["event_sequence"] + 1)
        )
        assert len(timeline["allocations"]) == allocations
        assert all(row["stopped"] and row["verified"] for row in timeline["containers"])
        assert all(row["revoked"] for row in timeline["leases"])
        assert all(row["ended"] for row in timeline["grants"])
        assert [row["state"] for row in timeline["reservations"]].count("RESERVED") == 0
        assert timeline["job"]["checkpoint_sequence"] == max(
            (row["sequence"] for row in timeline["reservations"]), default=0
        )
        released_events = sum(
            event["event_type"] == "ALLOCATION_RELEASED" for event in timeline["events"]
        )
        assert released_events == allocations
        for attempt in timeline["attempts"]:
            assert self.containers(attempt["attempt_id"]) == [], "leaked workload container"
            assert attempt["state"] in {"SUCCEEDED", "FAILED", "LOST", "CANCELLED"}, attempt
        timeline["counters"] = self.assert_counters()
        with self.engine.connect() as connection:
            segments = connection.execute(
                select(
                    s.allocation_ledger_segments.c.ended_at,
                    s.allocation_ledger_segments.c.charged_amount,
                )
                .join(
                    s.allocations,
                    s.allocations.c.allocation_id == s.allocation_ledger_segments.c.allocation_id,
                )
                .where(s.allocations.c.job_id == UUID(job_id))
            ).all()
        assert all(row.ended_at is not None for row in segments), "open ledger segment"
        timeline["ledger_segments"] = {
            "count": len(segments),
            "charged": str(sum((Decimal(str(row.charged_amount)) for row in segments), Decimal(0))),
        }
        return timeline


def test_b15_control_recovery_on_docker(migrated_postgres_engine, tmp_path):
    image, worker_image, architecture = _docker_images()
    engine = migrated_postgres_engine
    _template(engine, image, architecture)
    evidence = {
        "image": image,
        "architecture": architecture,
        "worker_image": worker_image,
        "iterations": ITERATIONS,
        "checkpoint_interval_seconds": INTERVAL_SECONDS,
        "not_run": {
            "C8_oom": "the cpu-iterative fixture has no memory-exhaustion mode",
            "C8_log_flood": "the cpu-iterative fixture has no log-volume mode",
        },
    }
    scenario = None
    with _client(engine, tmp_path) as client:
        harness = _B15Harness(engine, tmp_path, client, image, worker_image)
        try:
            scores = []
            evidence["epochs"] = [_start_stack(harness, engine)]

            # C0. Baseline R0: uninterrupted run.
            scenario = "C0"
            started = time.monotonic()
            baseline = harness.submit("b15-docker-c0-baseline")
            harness.wait_succeeded(baseline)
            r0 = harness.result(baseline)
            timeline = harness.settle(baseline, "SUCCEEDED", 1)
            assert timeline["job"]["retry_count"] == 0
            evidence["C0_baseline"] = {
                "seconds": round(time.monotonic() - started, 1),
                "checksum": r0["checksum"],
                "timeline": timeline,
            }
            scores.append(harness.fairness())

            # C1. Pause while RUNNING -> PAUSED (checkpoint, proof, release) -> resume.
            scenario = "C1"
            job = harness.submit("b15-docker-c1-pause-resume")
            first, _container = harness.running_attempt(job, 1)
            harness.wait_committed(job, first["attempt_id"], 1)
            accepted, _etag = harness.control(job, "pause", "b15-docker-c1-pause")
            assert (accepted["state"], accepted["desired_state"]) == ("PAUSING", "PAUSED")
            harness.wait_state(job, "PAUSED", timeout=60)
            paused = harness.timeline(job)
            assert harness.allocation_states(job) == [(str(first["attempt_id"]), "RELEASED")]
            assert (paused["attempts"][0]["state"], paused["attempts"][0]["failure_reason"]) == (
                "CANCELLED",
                "PAUSE",
            )
            assert paused["job"]["retry_count"] == 0
            assert harness.containers(first["attempt_id"]) == []
            # PAUSED keeps admission outstanding; no attempt is active.
            assert harness.assert_counters() == (1, 0)
            assert _ordered(
                _event_pairs(paused),
                [("PAUSE_REQUESTED", "USER_PAUSE"), ("ALLOCATION_RELEASED", "VERIFIED_CLEANUP")],
            ), _event_pairs(paused)
            harness.control(job, "pause", "b15-docker-c1-pause-again", status=409)
            newest = harness.checkpoints(job)[-1]
            resumed, _etag = harness.control(job, "resume", "b15-docker-c1-resume")
            assert resumed["state"] == "QUEUED"
            harness.wait_succeeded(job)
            r1 = harness.result(job)
            timeline = harness.settle(job, "SUCCEEDED", 2)
            assert timeline["job"]["retry_count"] == 0
            assert timeline["restores"][timeline["attempts"][1]["attempt_id"]] == {
                "checkpoint_id": str(newest[0]),
                "sequence": newest[1],
            }
            assert _ordered(
                _event_pairs(timeline),
                [
                    ("PAUSE_REQUESTED", "USER_PAUSE"),
                    ("ALLOCATION_RELEASED", "VERIFIED_CLEANUP"),
                    ("JOB_RESUMED", "USER_RESUME"),
                    ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED"),
                ],
            ), _event_pairs(timeline)
            assert r1["bytes"] == r0["bytes"] and r1["checksum"] == r0["checksum"]
            evidence["C1_pause_resume"] = {
                "checksum": r1["checksum"],
                "paused": paused,
                "timeline": timeline,
            }
            scores.append(harness.fairness())

            # C2. Cancel while RUNNING -> CANCELLING -> CANCELLED; replay is stored.
            scenario = "C2"
            job = harness.submit("b15-docker-c2-cancel")
            first, _container = harness.running_attempt(job, 1)
            etag = harness.etag(job)
            accepted, accepted_etag = harness.control(
                job, "cancel", "b15-docker-c2-cancel-key", etag=etag
            )
            assert (accepted["state"], accepted["desired_state"]) == ("CANCELLING", "CANCELLED")
            replay, replay_etag = harness.control(
                job, "cancel", "b15-docker-c2-cancel-key", etag=etag
            )
            assert (replay, replay_etag) == (accepted, accepted_etag)
            harness.wait_state(job, "CANCELLED", timeout=60)
            harness.control(job, "cancel", "b15-docker-c2-cancel-again", status=409)
            timeline = harness.settle(job, "CANCELLED", 1)
            assert timeline["job"]["desired_state"] == "CANCELLED"
            assert timeline["attempts"][0]["state"] == "CANCELLED"
            assert _ordered(
                _event_pairs(timeline),
                [
                    ("CANCEL_REQUESTED", "USER_CANCEL"),
                    ("ALLOCATION_RELEASED", "VERIFIED_CLEANUP"),
                ],
            ), _event_pairs(timeline)
            evidence["C2_cancel_running"] = {"replayed": True, "timeline": timeline}
            scores.append(harness.fairness())

            # C4. Pause-crash after a checkpoint commits while PAUSING: PAUSED, no retry.
            # Deterministic (B15 round 2): the workload dies after the pause checkpoint
            # commits and before any REQUEST_STOP{PAUSE} reaches it.
            scenario = "C4"
            job = harness.submit("b15-docker-c4-pause-crash-after")
            first, container = harness.running_attempt(job, 1)
            before = harness.wait_committed(job, first["attempt_id"], 1)
            harness.control(job, "pause", "b15-docker-c4-pause")
            pause_checkpoint, exit_code, finished_at = harness.crash_after_pause_commit(
                job, first["attempt_id"], container, len(before)
            )
            # SIGKILL, not the runner's graceful pause stop, ended the workload.
            assert exit_code == 137, exit_code
            harness.wait_state(job, "PAUSED", timeout=90)
            paused = harness.timeline(job)
            attempt = paused["attempts"][0]
            assert (attempt["state"], attempt["failure_class"], attempt["failure_reason"]) == (
                "CANCELLED",
                None,
                "PAUSE",
            ), attempt
            assert paused["job"]["retry_count"] == 0
            assert paused["retries"] == []
            assert harness.assert_counters() == (1, 0)
            newest = harness.checkpoints(job)[-1]
            assert newest[0] == pause_checkpoint
            harness.control(job, "resume", "b15-docker-c4-resume")
            harness.wait_succeeded(job)
            r4 = harness.result(job)
            timeline = harness.settle(job, "SUCCEEDED", 2)
            assert timeline["job"]["retry_count"] == 0
            assert timeline["restores"][timeline["attempts"][1]["attempt_id"]] == {
                "checkpoint_id": str(newest[0]),
                "sequence": newest[1],
            }
            assert r4["bytes"] == r0["bytes"] and r4["checksum"] == r0["checksum"]
            evidence["C4_pause_crash_after_checkpoint"] = {
                "crash_path": "workload SIGKILL after pause publish commit, worker frozen",
                "workload_exit_code": exit_code,
                "workload_finished_at": finished_at,
                "checksum": r4["checksum"],
                "paused": paused,
                "timeline": timeline,
            }
            scores.append(harness.fairness())

            # C5a. Pause-crash before the first checkpoint, restart_safe:
            # CHECKPOINT_FOR_PAUSE consumes exactly one retry and ends PAUSED.
            scenario = "C5a"
            job = harness.submit("b15-docker-c5a-pause-crash-before")
            first, container = harness.running_attempt(job, 1)
            harness.computing_before_checkpoint(job, first["attempt_id"], container)
            harness.control(job, "pause", "b15-docker-c5a-pause")
            harness.kill_workload(container)
            harness.wait_state(job, "PAUSED", timeout=120)
            paused = harness.timeline(job)
            assert harness.checkpoints(job, first["attempt_id"]) == []
            assert paused["job"]["retry_count"] == 1
            assert [row["reason"] for row in paused["retries"]] == ["INFRASTRUCTURE"]
            assert paused["job"]["recovery_intent"] is None
            assert [row["execution_intent"] for row in paused["attempt_intents"]][1] == (
                "CHECKPOINT_FOR_PAUSE"
            )
            first_row, second_row = paused["attempts"]
            assert (first_row["failure_class"], first_row["failure_reason"]) == (
                "INFRASTRUCTURE",
                "RUNNER_UNAVAILABLE",
            )
            assert (second_row["state"], second_row["failure_reason"]) == ("CANCELLED", "PAUSE")
            assert harness.checkpoints(job, UUID(second_row["attempt_id"])) != []
            assert harness.assert_counters() == (1, 0)
            cancelled, _etag = harness.control(job, "cancel", "b15-docker-c5a-cancel")
            assert cancelled["state"] == "CANCELLED"
            timeline = harness.settle(job, "CANCELLED", 2)
            assert timeline["job"]["retry_count"] == 1
            evidence["C5a_pause_crash_before_checkpoint_restart_safe"] = {
                "paused": paused,
                "timeline": timeline,
            }
            scores.append(harness.fairness())

            # C8. Runtime limit -> FAILED TIMEOUT without an automatic retry.
            scenario = "C8"
            limit, derived = _timeout_limit_seconds(evidence["C0_baseline"]["timeline"])
            timed_out = harness.submit("b15-docker-c8-timeout", runtime_limit_seconds=limit)
            harness.wait_state(timed_out, "FAILED", timeout=limit + 90)
            timeline = harness.settle(timed_out, "FAILED", 1)
            assert (
                timeline["attempts"][0]["failure_class"],
                timeline["attempts"][0]["failure_reason"],
            ) == ("TIMEOUT", "RUNTIME_LIMIT_REACHED")
            assert timeline["job"]["retry_count"] == 0
            assert timeline["retries"] == []
            assert len(timeline["checkpoints"]) >= 1
            evidence["C8_timeout"] = {
                "runtime_limit_seconds": limit,
                "derived_from_C0": derived,
                "timeline": timeline,
            }
            scores.append(harness.fairness())

            # C7a. Manual retry of the timed-out job from its newest checkpoint.
            scenario = "C7a"
            source_version = harness.job(timed_out)["version"]
            newest = harness.checkpoints(timed_out)[-1]
            retried, _etag = harness.control(
                timed_out, "retry", "b15-docker-c7a-retry", checkpoint_id=str(newest[0])
            )
            job = retried["job_id"]
            harness.wait_succeeded(job)
            r7a = harness.result(job)
            timeline = harness.settle(job, "SUCCEEDED", 1)
            assert timeline["job"]["retry_of_job_id"] == timed_out
            assert list(timeline["restores"].values()) == [
                {"checkpoint_id": str(newest[0]), "sequence": newest[1]}
            ]
            source = harness.job(timed_out)
            assert (source["state"], source["version"]) == ("FAILED", source_version)
            # The source spec carries the C8 runtime limit, so only its
            # spec_checksum differs from R0; the computation must not.
            with harness.engine.connect() as connection:
                spec_checksums = {
                    str(row.job_id): row.spec_checksum
                    for row in connection.execute(
                        select(s.job_specs.c.job_id, s.job_specs.c.spec_checksum).where(
                            s.job_specs.c.job_id.in_([UUID(timed_out), UUID(job)])
                        )
                    )
                }
            assert spec_checksums[job] == spec_checksums[timed_out]
            expected = {**json.loads(r0["bytes"]), "spec_checksum": spec_checksums[job]}
            assert json.loads(r7a["bytes"]) == expected
            assert r7a["bytes"] == json.dumps(expected, separators=(",", ":")).encode()
            evidence["C7a_manual_retry_with_checkpoint"] = {
                "source_job_id": timed_out,
                "checkpoint": {"checkpoint_id": str(newest[0]), "sequence": newest[1]},
                "checksum": r7a["checksum"],
                "timeline": timeline,
            }
            scores.append(harness.fairness())

            # C3. SIGKILL the worker agent and keep it dead past the lease: the runner
            # stops on its own deadline, the reaper fences, the next incarnation proves.
            scenario = "C3"
            job = harness.submit("b15-docker-c3-worker-dead")
            first, container = harness.running_attempt(job, 1)
            harness.wait_committed(job, first["attempt_id"], 1)
            watch = _ContainerWatch(container)
            prior = harness.kill_worker()
            killed_at = time.monotonic()
            reaped = harness.wait_state(job, "RECOVERING", timeout=LEASE_SECONDS + 45)
            lease = harness.lease(first["attempt_id"])
            assert lease["revoke_reason"] == "LEASE_EXPIRED"
            attempt = harness.attempts(job)[0]
            assert (attempt["state"], attempt["failure_class"], attempt["failure_reason"]) == (
                "LOST",
                "INFRASTRUCTURE",
                "LEASE_EXPIRED",
            )
            assert harness.allocation_states(job) == [(str(first["attempt_id"]), "QUARANTINED")]
            open_before, charged_before = harness.charge(first["attempt_id"])
            time.sleep(3)
            open_after, charged_after = harness.charge(first["attempt_id"])
            assert open_before == open_after == 1 and charged_after > charged_before
            _wait(lambda: _runner_stop_time(container, watch), "runner self-stop", timeout=30)
            finished, finished_source = _runner_stop_time(container, watch)
            watch.close()
            assert finished < lease["expires_at"], (finished, lease["expires_at"])
            reaped_timeline = harness.timeline(job)
            harness.restart_worker(prior)
            harness.wait_succeeded(job)
            r3 = harness.result(job)
            timeline = harness.settle(job, "SUCCEEDED", 2)
            newest = harness.checkpoints(job, first["attempt_id"])[-1]
            assert timeline["job"]["retry_count"] == 1
            assert timeline["attempts"][0]["state"] == "LOST"
            assert timeline["restores"][timeline["attempts"][1]["attempt_id"]] == {
                "checkpoint_id": str(newest[0]),
                "sequence": newest[1],
            }
            assert reaped["job_fence"] == first["job_fence"] + 1
            assert _ordered(
                _event_pairs(timeline),
                [
                    ("ATTEMPT_LOST", "LEASE_EXPIRED"),
                    ("ALLOCATION_RELEASED", "VERIFIED_CLEANUP"),
                    ("RETRY_READY", "BACKOFF_ELAPSED"),
                    ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED"),
                ],
            ), _event_pairs(timeline)
            assert r3["bytes"] == r0["bytes"] and r3["checksum"] == r0["checksum"]
            evidence["C3_worker_dead_past_lease"] = {
                "worker_dead_seconds_until_reaped": round(time.monotonic() - killed_at, 1),
                "runner_finished_at": finished.isoformat(timespec="milliseconds"),
                "runner_finished_source": finished_source,
                "lease_expires_at": lease["expires_at"].isoformat(timespec="milliseconds"),
                "runner_stop_margin_seconds": round(
                    (lease["expires_at"] - finished).total_seconds(), 3
                ),
                "quarantined_charge": {
                    "before": str(charged_before),
                    "after_3s": str(charged_after),
                },
                "checksum": r3["checksum"],
                "reaped": reaped_timeline,
                "timeline": timeline,
            }
            scores.append(harness.fairness())

            # C9. Network loss of the worker during renewals: the runner stops before its
            # deadline, the reaper fences, and the old and new computes never overlap.
            scenario = "C9"
            job = harness.submit("b15-docker-c9-network-loss")
            first, container = harness.running_attempt(job, 1)
            harness.wait_committed(job, first["attempt_id"], 1)
            incarnation = harness.worker_row()["current_incarnation_id"]
            watch = _ContainerWatch(container)
            harness.cut_worker_network()
            try:
                reaped = harness.wait_state(job, "RECOVERING", timeout=LEASE_SECONDS + 45)
                lease = harness.lease(first["attempt_id"])
                _wait(lambda: _runner_stop_time(container, watch), "runner self-stop", timeout=30)
                finished, finished_source = _runner_stop_time(container, watch)
            finally:
                harness.restore_worker_network()
            watch.close()
            assert lease["revoke_reason"] == "LEASE_EXPIRED"
            assert finished < lease["expires_at"], (finished, lease["expires_at"])
            second, container = harness.running_attempt(job, 2)
            second_started = harness.started_at(container)
            assert second_started > finished, (second_started, finished)
            harness.wait_succeeded(job)
            r9 = harness.result(job)
            timeline = harness.settle(job, "SUCCEEDED", 2)
            assert timeline["job"]["retry_count"] == 1
            assert harness.worker_row()["current_incarnation_id"] == incarnation
            assert r9["bytes"] == r0["bytes"] and r9["checksum"] == r0["checksum"]
            evidence["C9_network_loss"] = {
                "old_runner_finished_at": finished.isoformat(timespec="milliseconds"),
                "old_runner_finished_source": finished_source,
                "old_lease_expires_at": lease["expires_at"].isoformat(timespec="milliseconds"),
                "runner_stop_margin_seconds": round(
                    (lease["expires_at"] - finished).total_seconds(), 3
                ),
                "new_runner_started_at": second_started.isoformat(timespec="milliseconds"),
                "compute_gap_seconds": round((second_started - finished).total_seconds(), 3),
                "same_worker_incarnation": True,
                "reaped_fence": reaped["job_fence"],
                "checksum": r9["checksum"],
                "timeline": timeline,
            }
            scores.append(harness.fairness())

            # C6. Drain keeps running work and blocks new work; disable fences with
            # quarantine and proof; enable only after reconciliation.
            scenario = "C6"
            running = harness.submit("b15-docker-c6-running")
            first, container = harness.running_attempt(running, 1)
            harness.wait_committed(running, first["attempt_id"], 1)
            drained = harness.worker_admin("drain", "b15-docker-c6-drain", status=202)
            assert drained["admin_state"] == "DRAINING"
            queued = harness.submit("b15-docker-c6-queued")
            time.sleep(INTERVAL_SECONDS + 2)
            assert harness.job(queued)["state"] == "QUEUED" and harness.attempts(queued) == []
            # Drain keeps running work, including its periodic checkpoint cycles.
            assert harness.attempts(running)[0]["state"] in {"RUNNING", "CHECKPOINTING"}
            disabled = harness.worker_admin("disable", "b15-docker-c6-disable", status=202)
            assert (disabled["admin_state"], disabled["health"]) == ("DISABLED", "STARTING")
            fenced = harness.job(running)
            assert fenced["state"] == "RECOVERING"
            assert fenced["job_fence"] == first["job_fence"] + 1
            attempt = harness.attempts(running)[0]
            assert (attempt["state"], attempt["failure_reason"]) == ("STOPPING", "WORKER_DISABLED")
            assert harness.lease(first["attempt_id"])["revoke_reason"] == "WORKER_DISABLED"
            assert harness.allocation_states(running) == [(str(first["attempt_id"]), "QUARANTINED")]
            fenced_timeline = harness.timeline(running)
            harness.wait_state(running, {"RETRY_WAIT", "QUEUED"}, timeout=60)
            _wait(
                lambda: (
                    harness.allocation_states(running) == [(str(first["attempt_id"]), "RELEASED")]
                ),
                "release after the disable proof",
                timeout=30,
            )
            time.sleep(INTERVAL_SECONDS)
            assert len(harness.attempts(running)) == 1 and harness.attempts(queued) == []
            enable_tries = 0

            def enable():
                nonlocal enable_tries
                enable_tries += 1
                response = harness.client.post(
                    f"/v1/admin/workers/{WORKER_ID}/enable",
                    headers={
                        "Origin": "https://nexa.test",
                        "X-CSRF-Token": harness.write["X-CSRF-Token"],
                        "If-Match": harness.client.get(f"/v1/admin/workers/{WORKER_ID}").headers[
                            "ETag"
                        ],
                        "Idempotency-Key": f"b15-docker-c6-enable-{enable_tries:03d}",
                    },
                    json={"reason": "b15 docker scenario"},
                )
                assert response.status_code in {200, 409}, response.text
                return response.status_code == 200 and response.json()

            enabled = _wait(enable, "worker enable", timeout=90, pause=1)
            assert enabled["admin_state"] == "ENABLED"
            _wait(lambda: _worker_ready(engine), "worker READY after enable", timeout=60)
            harness.wait_succeeded(running)
            harness.wait_succeeded(queued)
            r6a = harness.result(running)
            r6b = harness.result(queued)
            timeline = harness.settle(running, "SUCCEEDED", 2)
            queued_timeline = harness.settle(queued, "SUCCEEDED", 1)
            assert timeline["job"]["retry_count"] == 1
            assert (
                timeline["attempts"][0]["failure_class"],
                timeline["attempts"][0]["failure_reason"],
            ) == ("INFRASTRUCTURE", "WORKER_DISABLED")
            newest = harness.checkpoints(running, first["attempt_id"])[-1]
            assert timeline["restores"][timeline["attempts"][1]["attempt_id"]] == {
                "checkpoint_id": str(newest[0]),
                "sequence": newest[1],
            }
            for result in (r6a, r6b):
                assert result["bytes"] == r0["bytes"] and result["checksum"] == r0["checksum"]
            evidence["C6_drain_disable_enable"] = {
                "enable_attempts": enable_tries,
                "checksums": [r6a["checksum"], r6b["checksum"]],
                "fenced": fenced_timeline,
                "timeline": timeline,
                "queued_timeline": queued_timeline,
            }
            scores.append(harness.fairness())

            # C10. Stop API, coordinator and worker together (PostgreSQL and storage
            # stay), then start them again: every accepted job keeps its trace.
            scenario = "C10"
            restart_jobs = [
                harness.submit("b15-docker-c10-first"),
                harness.submit("b15-docker-c10-second"),
            ]
            first, _container = harness.running_attempt(restart_jobs[0], 1)
            harness.wait_committed(restart_jobs[0], first["attempt_id"], 1)
            epoch_before = harness.epoch()
            prior = harness.kill_worker()
            harness.coordinator.pause()
            harness.server.should_exit = True
            harness.thread.join(timeout=15)
            assert not harness.thread.is_alive(), "API server did not stop"
            before = {job_id: harness.job(job_id)["state"] for job_id in restart_jobs}
            time.sleep(5)
            harness.coordinator = _Coordinator(engine)
            harness.start_api()
            harness.coordinator.start()
            harness.restart_worker(prior)
            _wait(
                lambda: (harness.epoch() or 0) > epoch_before, "new coordinator epoch", timeout=30
            )
            restart_results = []
            restart_timelines = []
            for job_id in restart_jobs:
                harness.wait_succeeded(job_id)
                result = harness.result(job_id)
                restart_results.append(result["checksum"])
                assert result["bytes"] == r0["bytes"] and result["checksum"] == r0["checksum"]
                attempts = len(harness.attempts(job_id))
                restart_timelines.append(harness.settle(job_id, "SUCCEEDED", attempts))
            evidence["C10_full_restart"] = {
                "epoch_before": epoch_before,
                "epoch_after": harness.epoch(),
                "states_at_stop": list(before.values()),
                "retry_counts": [row["job"]["retry_count"] for row in restart_timelines],
                "checksums": restart_results,
                "timelines": restart_timelines,
            }
            scores.append(harness.fairness())

            # Every accepted job keeps its trace; counters and ledger are consistent.
            scenario = "final"
            with engine.connect() as connection:
                jobs = {
                    str(row.job_id): row.state
                    for row in connection.execute(select(s.jobs.c.job_id, s.jobs.c.state))
                }
                sessions = set(
                    str(value)
                    for value in connection.execute(select(s.logical_sessions.c.job_id)).scalars()
                )
                attempt_ids = list(connection.execute(select(s.attempts.c.attempt_id)).scalars())
            assert set(harness.submitted) == set(jobs) == sessions
            assert set(jobs.values()) <= TERMINAL
            assert harness.assert_counters() == (0, 0)
            for attempt_id in attempt_ids:
                assert harness.containers(attempt_id) == [], "leaked workload container"
            assert all(value is not None for value in scores)
            assert scores == sorted(scores)
            evidence["accepted_jobs"] = {"count": len(jobs), "states": sorted(jobs.values())}
            evidence["fairness_scores"] = [str(value) for value in scores]
            evidence["epochs"].append(harness.epoch())
            scenario = None
            print("B15 docker: C0-C10 control, recovery, admin and restart")
        finally:
            evidence["failed_scenario"] = scenario
            _write_evidence(evidence)
            harness.close()


def test_b15_pause_crash_not_restart_safe_on_docker(migrated_postgres_engine, tmp_path):
    """C5b and C7b need a not restart-safe template, hence their own database."""
    image, worker_image, architecture = _docker_images()
    engine = migrated_postgres_engine
    _template(engine, image, architecture)
    _not_restart_safe(engine)
    evidence = {
        "image": image,
        "architecture": architecture,
        "worker_image": worker_image,
        "iterations": ITERATIONS,
        "checkpoint_interval_seconds": INTERVAL_SECONDS,
        "restart_safe": False,
    }
    scenario = None
    with _client(engine, tmp_path) as client:
        harness = _B15Harness(engine, tmp_path, client, image, worker_image)
        try:
            evidence["epochs"] = [_start_stack(harness, engine)]

            # R0 of this database: an uninterrupted run of the same spec.
            scenario = "C5b-R0"
            baseline = harness.submit("b15-docker-c5b-baseline")
            harness.wait_succeeded(baseline)
            r0 = harness.result(baseline)
            timeline = harness.settle(baseline, "SUCCEEDED", 1)
            evidence["C5b_baseline"] = {"checksum": r0["checksum"], "timeline": timeline}

            # C5b. The same crash on a non-restart_safe template ends FAILED.
            scenario = "C5b"
            nrs_failed = harness.submit("b15-docker-c5b-pause-crash-nrs")
            first, container = harness.running_attempt(nrs_failed, 1)
            harness.computing_before_checkpoint(nrs_failed, first["attempt_id"], container)
            harness.control(nrs_failed, "pause", "b15-docker-c5b-pause")
            harness.kill_workload(container)
            harness.wait_state(nrs_failed, "FAILED", timeout=60)
            assert harness.checkpoints(nrs_failed) == []
            timeline = harness.settle(nrs_failed, "FAILED", 1)
            assert timeline["job"]["retry_count"] == 0
            assert timeline["retries"] == []
            assert (
                timeline["attempts"][0]["failure_class"],
                timeline["attempts"][0]["failure_reason"],
            ) == ("INFRASTRUCTURE", "RUNNER_UNAVAILABLE")
            evidence["C5b_pause_crash_before_checkpoint_not_restart_safe"] = {"timeline": timeline}

            # C7b. Manual retry of the FAILED job without a checkpoint: from the input.
            scenario = "C7b"
            source_version = harness.job(nrs_failed)["version"]
            retried, _etag = harness.control(nrs_failed, "retry", "b15-docker-c7b-retry")
            job = retried["job_id"]
            assert retried["state"] == "QUEUED"
            harness.wait_succeeded(job)
            r7b = harness.result(job)
            timeline = harness.settle(job, "SUCCEEDED", 1)
            assert timeline["job"]["retry_of_job_id"] == nrs_failed
            assert list(timeline["restores"].values()) == [None]
            source = harness.job(nrs_failed)
            assert (source["state"], source["version"]) == ("FAILED", source_version)
            assert r7b["bytes"] == r0["bytes"] and r7b["checksum"] == r0["checksum"]
            evidence["C7b_manual_retry_without_checkpoint"] = {
                "source_job_id": nrs_failed,
                "checksum": r7b["checksum"],
                "timeline": timeline,
            }

            scenario = "final"
            assert harness.assert_counters() == (0, 0)
            scenario = None
            print("B15 docker: C5b-C7b not restart-safe pause-crash and manual retry")
        finally:
            evidence["failed_scenario"] = scenario
            _write_evidence(evidence, "-not-restart-safe")
            harness.close()
