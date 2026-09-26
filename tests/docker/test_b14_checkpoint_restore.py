"""Opt-in B14 CPU checkpoint crash-resume, corruption fallback and adoption on real Docker.

Runs the production API (in-process uvicorn), the coordinator loop (in-process
thread, same ``runtime.run`` as ``nexa.coordinator.main``), the local worker as
a container and CPU workload containers from the rebuilt image. Faults are
injected only by this test: SIGKILL of a workload or worker container and
rewriting a committed checkpoint blob on the test storage root.

Evidence rows contain identifiers, states, sequences and checksums only; no
credential, input, cursor, accumulator or manifest content is recorded.
"""

import hashlib
import json
import os
import socket
import subprocess
import threading
import time
from decimal import Decimal
from uuid import UUID

import pytest
import uvicorn
from sqlalchemy import func, select, update

from nexa.api.app import create_app
from nexa.coordinator.runtime import run as run_coordinator
from nexa.coordinator.service import CoordinatorService
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.worker.credentials import CredentialStore
from tests.api.test_http_contract import _client
from tests.docker.test_b11_vertical import (
    _check,
    _check_output,
    _runner_diagnostics,
    _worker_ready,
    _worker_ready_with_new_incarnation,
)
from tests.integration.test_worker_api_b10 import (
    FINGERPRINT,
    INSTALLATION_ID,
    WORKER_ID,
    _bootstrap_worker,
)

pytestmark = [pytest.mark.docker, pytest.mark.postgres]

ITERATIONS = int(os.environ.get("NEXA_B14_ITERATIONS", "250000000"))
INTERVAL_SECONDS = 5
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


def _wait(predicate, detail, timeout=120, pause=0.2):
    until = time.monotonic() + timeout
    last = None
    while time.monotonic() < until:
        last = predicate()
        if last:
            return last
        time.sleep(pause)
    raise AssertionError(f"{detail}: last={last!r}")


def _template(engine, image, architecture):
    digest = image.rsplit("@", 1)[1]
    with engine.begin() as connection:
        connection.execute(
            s.templates.insert().values(
                template_id="cpu-iterative",
                current_version=1,
                display_name="CPU iterative",
                description="B14 checkpoint slice",
                enabled=True,
            )
        )
        connection.execute(
            s.template_versions.insert().values(
                template_id="cpu-iterative",
                version=1,
                parameter_schema=[
                    {
                        "name": name,
                        "type": "INTEGER",
                        "required": True,
                        "minimum": minimum,
                        "maximum": maximum,
                    }
                    for name, minimum, maximum in (
                        ("iterations", 1, 1_000_000_000),
                        ("seed", 0, 2_147_483_647),
                        ("modulus", 2, 2_147_483_647),
                    )
                ],
                resource_bounds={
                    "resources": {"cpu_millis": 6000, "memory_bytes": 2 * 1024**3, "gpu_count": 0},
                    "runtime_limit_seconds": 300,
                    "checkpoint_interval_seconds": 60,
                },
                capability_requirements={
                    "architectures": [architecture],
                    "adapter_id": "cpu.iterative",
                    "adapter_version": "1.0.0",
                    "image_digest": digest,
                    "device": "CPU",
                    "framework": "NEXA_CPU",
                    "framework_version": "1.0.0",
                    "cuda_runtime_min": None,
                    "driver_min": None,
                    "compute_capability_min": None,
                },
                adapter_id="cpu.iterative",
                adapter_version="1.0.0",
                image_digest=digest,
                checkpointable=True,
                restart_safe=True,
            )
        )


class _Coordinator:
    """The coordinator loop; pausing keeps one holder so the epoch history is explicit."""

    def __init__(self, engine):
        self.service = CoordinatorService(create_session_factory(engine))
        self.stop = None
        self.thread = None

    def start(self):
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=run_coordinator, args=(self.service, self.stop), daemon=True
        )
        self.thread.start()

    def pause(self):
        if self.thread is None:
            return
        self.stop.set()
        self.thread.join(timeout=15)
        assert not self.thread.is_alive(), "coordinator loop did not stop"
        self.thread = None


class _Harness:
    def __init__(self, engine, tmp_path, client, image, worker_image):
        self.engine = engine
        self.tmp_path = tmp_path
        self.client = client
        self.image = image
        self.worker_image = worker_image
        self.root = (tmp_path / "worker-shared").resolve()
        self.worker = None
        self.server = None
        self.thread = None
        self.coordinator = _Coordinator(engine)
        self.tenant_id = None
        self.write = None
        self.input_artifact_id = None
        self.submitted = {}

    # -- setup -----------------------------------------------------------------
    def bootstrap(self):
        client = self.client
        self.root.mkdir(mode=0o700)
        credential = _bootstrap_worker(client)
        CredentialStore(self.root / "credential.json").save(
            worker_id=WORKER_ID, installation_id=INSTALLATION_ID, credential=credential
        )
        (self.root / "bootstrap-secret").write_bytes(b"b" * 32)
        login = _check(
            client.post(
                "/v1/internal/admin-bootstrap",
                headers={
                    "X-Nexa-Bootstrap-Secret": "b" * 32,
                    "Idempotency-Key": "b14-docker-admin-bootstrap",
                },
                json={
                    "username": "b14-docker@example.test",
                    "display_name": "B14 Docker",
                    "password": "correct-horse-battery-staple",
                },
            ),
            201,
        )
        csrf = self._login()
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": csrf}
        tenant = _check(
            client.post(
                "/v1/admin/tenants",
                headers={**write, "Idempotency-Key": "b14-docker-tenant"},
                json={"slug": "b14-docker", "display_name": "B14 Docker"},
            ),
            201,
        )
        self.tenant_id = tenant["tenant_id"]
        _check(
            client.post(
                f"/v1/admin/tenants/{self.tenant_id}/memberships",
                headers={
                    **write,
                    "If-Match": '"v1"',
                    "Idempotency-Key": "b14-docker-membership",
                },
                json={"user_id": login["user_id"], "role": "MEMBER"},
            ),
            200,
        )
        with self.engine.begin() as connection:
            connection.execute(
                update(s.tenant_policies)
                .where(s.tenant_policies.c.tenant_id == UUID(self.tenant_id))
                .values(cpu_limit_millis=6000, memory_limit_bytes=12 * 1024**3)
            )
        csrf = self._login()
        self.write = {
            "Origin": "https://nexa.test",
            "X-CSRF-Token": csrf,
            "X-Nexa-Tenant-Id": self.tenant_id,
        }
        content = json.dumps({"initial_value": 5}, separators=(",", ":")).encode()
        artifact = _check(
            client.post(
                "/v1/artifacts",
                content=content,
                headers={
                    **self.write,
                    "Idempotency-Key": "b14-docker-input",
                    "X-Artifact-Checksum": "sha256:" + hashlib.sha256(content).hexdigest(),
                    "X-Artifact-Size": str(len(content)),
                    "X-Artifact-Kind": "INPUT",
                    "X-Artifact-Media-Type": "application/vnd.nexa.cpu-iterative-input+json",
                    "Content-Type": "application/octet-stream",
                },
            ),
            201,
        )
        self.input_artifact_id = artifact["artifact_id"]

    def _login(self):
        return _check(
            self.client.post(
                "/v1/auth/login",
                headers={"Origin": "https://nexa.test"},
                json={
                    "username": "b14-docker@example.test",
                    "password": "correct-horse-battery-staple",
                },
            ),
            200,
        )["csrf_token"]

    def start_api(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.server = uvicorn.Server(
            uvicorn.Config(
                create_app(self.client.app.state.services.settings, engine=self.engine),
                host="0.0.0.0",
                port=port,
                log_level="warning",
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        _wait(lambda: self.server.started, "API readiness", timeout=10)
        self.worker_command = [
            "docker",
            "run",
            "--detach",
            "--network",
            "bridge",
            "--add-host",
            "host.docker.internal:host-gateway",
            "--mount",
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--mount",
            f"type=bind,src={self.root},dst={self.root}",
            "--env",
            f"NEXA_WORKER_API_URL=http://host.docker.internal:{port}",
            "--env",
            f"NEXA_WORKER_STATE_ROOT={self.root}",
            "--env",
            f"NEXA_INSTALLATION_ID={INSTALLATION_ID}",
            "--env",
            f"NEXA_LOCAL_WORKER_ID={WORKER_ID}",
            "--env",
            f"NEXA_LOCAL_WORKER_FINGERPRINT={FINGERPRINT}",
            "--env",
            f"NEXA_BOOTSTRAP_SECRET_FILE={self.root / 'bootstrap-secret'}",
            "--env",
            f"NEXA_CPU_IMAGE_REF={self.image}",
            self.worker_image,
            "nexa-worker",
        ]

    def start_worker(self):
        self.worker = subprocess.check_output(self.worker_command, text=True).strip()

    def worker_logs(self):
        if self.worker is None:
            return ""
        logs = subprocess.run(
            ["docker", "logs", "--tail", "60", self.worker],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return logs.stderr[-3000:]

    def close(self):
        self.coordinator.pause()
        if self.worker is not None:
            subprocess.run(
                ["docker", "rm", "--force", self.worker],
                check=False,
                capture_output=True,
                timeout=15,
            )
        # Remove only workload containers of attempts created by this test.
        with self.engine.connect() as connection:
            attempt_ids = list(connection.execute(select(s.attempts.c.attempt_id)).scalars())
        for attempt_id in attempt_ids:
            leaked = self.containers(attempt_id)
            if leaked:
                subprocess.run(
                    ["docker", "rm", "--force", *leaked],
                    check=False,
                    capture_output=True,
                    timeout=15,
                )
        if self.server is not None:
            self.server.should_exit = True
            self.thread.join(timeout=8)

    # -- operations ------------------------------------------------------------
    def submit(self, key):
        spec = {
            "template_id": "cpu-iterative",
            "template_version": 1,
            "input_artifact_id": self.input_artifact_id,
            "resources": {"cpu_millis": 1000, "memory_bytes": 512 * 1024**2, "gpu_count": 0},
            "priority": 1,
            "runtime_limit_seconds": 300,
            "checkpoint_interval_seconds": INTERVAL_SECONDS,
            "parameters": {"iterations": ITERATIONS, "seed": 7, "modulus": 2_147_483_647},
        }
        accepted = _check(
            self.client.post(
                "/v1/jobs",
                headers={**self.write, "Idempotency-Key": key},
                json={"spec": spec},
            ),
            202,
        )
        self.submitted[accepted["job_id"]] = key
        return accepted["job_id"]

    def job(self, job_id):
        with self.engine.connect() as connection:
            return (
                connection.execute(select(s.jobs).where(s.jobs.c.job_id == UUID(job_id)))
                .mappings()
                .one()
            )

    def attempts(self, job_id):
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    select(s.attempts)
                    .where(s.attempts.c.job_id == UUID(job_id))
                    .order_by(s.attempts.c.attempt_number)
                ).mappings()
            )

    def checkpoints(self, job_id, attempt_id=None):
        query = select(s.checkpoints.c.checkpoint_id, s.checkpoints.c.sequence).where(
            s.checkpoints.c.job_id == UUID(job_id)
        )
        if attempt_id is not None:
            query = query.where(s.checkpoints.c.attempt_id == attempt_id)
        with self.engine.connect() as connection:
            return list(connection.execute(query.order_by(s.checkpoints.c.sequence)).all())

    def reserved(self, attempt_id):
        with self.engine.connect() as connection:
            return connection.execute(
                select(s.checkpoint_reservations.c.checkpoint_id).where(
                    s.checkpoint_reservations.c.attempt_id == attempt_id,
                    s.checkpoint_reservations.c.state == "RESERVED",
                )
            ).scalar_one_or_none()

    def containers(self, attempt_id):
        return subprocess.check_output(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label=nexa.installation_id={INSTALLATION_ID}",
                "--filter",
                f"label=nexa.attempt_id={attempt_id}",
            ],
            text=True,
            timeout=10,
        ).split()

    def running_attempt(self, job_id, number):
        """Wait until attempt ``number`` runs in exactly one workload container."""

        def ready():
            rows = self.attempts(job_id)
            if len(rows) < number or rows[number - 1]["state"] != "RUNNING":
                return None
            containers = self.containers(rows[number - 1]["attempt_id"])
            return (rows[number - 1], containers[0]) if len(containers) == 1 else None

        return _wait(ready, f"attempt {number} of {job_id} running", timeout=120, pause=0.05)

    def kill_workload(self, container_id):
        subprocess.run(
            ["docker", "kill", "--signal", "KILL", container_id],
            check=True,
            capture_output=True,
            timeout=15,
        )

    def wait_committed(self, job_id, attempt_id, count, pause=0.02):
        return _wait(
            lambda: (rows := self.checkpoints(job_id, attempt_id)) and len(rows) >= count and rows,
            f"{count} committed checkpoints for {attempt_id}",
            timeout=150,
            pause=pause,
        )

    def wait_retry_released(self, job_id, attempts):
        """The killed attempt is failed, its allocation released only after cleanup proof."""

        def released():
            job = self.job(job_id)
            rows = self.attempts(job_id)
            if job["state"] in TERMINAL - {"SUCCEEDED"}:
                raise AssertionError(f"job {job_id} ended {job['state']}")
            with self.engine.connect() as connection:
                states = list(
                    connection.execute(
                        select(s.allocations.c.state).where(
                            s.allocations.c.attempt_id == rows[attempts - 1]["attempt_id"]
                        )
                    ).scalars()
                )
            return (
                job["retry_count"] >= attempts
                and states == ["RELEASED"]
                and rows[attempts - 1]["failure_class"] is not None
                and job
            )

        return _wait(released, f"retry of {job_id} after attempt {attempts}", timeout=60)

    def wait_succeeded(self, job_id, timeout=300):
        def done():
            job = self.job(job_id)
            if job["state"] in TERMINAL - {"SUCCEEDED"}:
                raise AssertionError(f"job {job_id} ended {job['state']}")
            return job["state"] == "SUCCEEDED" and job

        try:
            return _wait(done, f"job {job_id} SUCCEEDED", timeout=timeout, pause=0.5)
        except AssertionError as exc:
            raise AssertionError(
                f"{exc}; timeline={json.dumps(self.timeline(job_id), default=str)[-6000:]}; "
                f"runner={_runner_diagnostics(self.engine, self.root)}; "
                f"worker_log={self.worker_logs()}"
            ) from exc

    def result(self, job_id):
        result = _check(
            self.client.get(
                f"/v1/jobs/{job_id}/result", headers={"X-Nexa-Tenant-Id": self.tenant_id}
            ),
            200,
        )
        with self.engine.connect() as connection:
            output_id = connection.execute(
                select(s.artifact_references.c.artifact_id).where(
                    s.artifact_references.c.owner_type == "RESULT",
                    s.artifact_references.c.owner_id == UUID(result["result_id"]),
                    s.artifact_references.c.purpose == "RESULT_FILE",
                )
            ).scalar_one()
            checksum = connection.execute(
                select(s.artifacts.c.checksum).where(s.artifacts.c.artifact_id == output_id)
            ).scalar_one()
        data = _check_output(self.client, self.tenant_id, output_id)
        assert "sha256:" + hashlib.sha256(data).hexdigest() == checksum
        return {"result_id": result["result_id"], "bytes": data, "checksum": checksum}

    def corrupt_state_blob(self, checkpoint_id):
        """Fault injection on the test storage root only: rewrite a committed state blob."""
        with self.engine.connect() as connection:
            artifact_id = connection.execute(
                select(s.artifact_references.c.artifact_id).where(
                    s.artifact_references.c.owner_type == "CHECKPOINT",
                    s.artifact_references.c.owner_id == checkpoint_id,
                    s.artifact_references.c.purpose == "CHECKPOINT_FILE",
                )
            ).scalar_one()
            key = connection.execute(
                select(s.artifacts.c.blob_key).where(s.artifacts.c.artifact_id == artifact_id)
            ).scalar_one()
        path = self.client.app.state.services.artifact.store._path_for_key(key)
        original = path.read_bytes()
        os.chmod(path, 0o600)
        path.write_bytes(bytes([original[0] ^ 0x01]) + original[1:])

    def restores(self, job_id):
        """attempt_id -> (checkpoint_id, sequence) the committed claim froze, or None."""
        restore = s.attempts.c.execution_context["restore_checkpoint"]["record"]
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    s.attempts.c.attempt_id,
                    restore["checkpoint_id"].astext,
                    restore["sequence"].astext,
                )
                .where(
                    s.attempts.c.job_id == UUID(job_id),
                    s.attempts.c.execution_context.is_not(None),
                )
                .order_by(s.attempts.c.attempt_number)
            ).all()
        return {
            str(attempt): (UUID(checkpoint), int(sequence)) if checkpoint else None
            for attempt, checkpoint, sequence in rows
        }

    def events(self, job_id):
        with self.engine.connect() as connection:
            return [
                (row.sequence, row.event_type, row.reason)
                for row in connection.execute(
                    select(s.events.c.sequence, s.events.c.event_type, s.events.c.reason)
                    .where(s.events.c.job_id == UUID(job_id))
                    .order_by(s.events.c.sequence)
                )
            ]

    def timeline(self, job_id):
        """Separate identity fields per the recovery/fencing evidence oracle."""
        key = UUID(job_id)
        with self.engine.connect() as connection:
            job = connection.execute(
                select(
                    s.jobs.c.state,
                    s.jobs.c.desired_state,
                    s.jobs.c.job_fence,
                    s.jobs.c.event_sequence,
                    s.jobs.c.checkpoint_sequence,
                    s.jobs.c.retry_count,
                    s.jobs.c.max_retries,
                ).where(s.jobs.c.job_id == key)
            ).one()
            epoch = connection.execute(select(s.coordinator_leadership.c.epoch)).scalar_one()
            attempts = [
                {
                    "attempt_number": row.attempt_number,
                    "attempt_id": str(row.attempt_id),
                    "state": row.state,
                    "worker_incarnation_id": str(row.worker_incarnation_id),
                    "job_fence": row.job_fence,
                    "failure_class": row.failure_class,
                    "failure_reason": row.failure_reason,
                }
                for row in connection.execute(
                    select(s.attempts)
                    .where(s.attempts.c.job_id == key)
                    .order_by(s.attempts.c.attempt_number)
                )
            ]
            leases = [
                {
                    "lease_id": str(row.lease_id),
                    "attempt_id": str(row.attempt_id),
                    "job_fence": row.job_fence,
                    "current_worker_incarnation_id": str(row.current_worker_incarnation_id),
                    "revoked": row.revoked_at is not None,
                    "revoke_reason": row.revoke_reason,
                }
                for row in connection.execute(
                    select(s.attempt_leases)
                    .where(s.attempt_leases.c.job_id == key)
                    .order_by(s.attempt_leases.c.issued_at)
                )
            ]
            grants = [
                {
                    "attempt_id": str(row.attempt_id),
                    "worker_incarnation_id": str(row.worker_incarnation_id),
                    "job_fence": row.job_fence,
                    "ended": row.ended_at is not None,
                }
                for row in connection.execute(
                    select(s.attempt_authority_grants)
                    .where(s.attempt_authority_grants.c.job_id == key)
                    .order_by(s.attempt_authority_grants.c.granted_at)
                )
            ]
            allocations = [
                {
                    "allocation_id": str(row.allocation_id),
                    "attempt_id": str(row.attempt_id),
                    "state": row.state,
                    "quarantined": row.quarantined_at is not None,
                    "release_reason": row.release_reason,
                }
                for row in connection.execute(
                    select(s.allocations)
                    .where(s.allocations.c.job_id == key)
                    .order_by(s.allocations.c.held_at)
                )
            ]
            containers = [
                {
                    "attempt_id": str(row.attempt_id),
                    "container_id": row.container_id[:12],
                    "stopped": row.stopped_at is not None,
                    "verified": row.verified_at is not None,
                }
                for row in connection.execute(
                    select(s.container_identities)
                    .where(s.container_identities.c.job_id == key)
                    .order_by(s.container_identities.c.created_at)
                )
            ]
            corrupt = dict(
                connection.execute(
                    select(
                        s.checkpoint_corruptions.c.checkpoint_id,
                        s.checkpoint_corruptions.c.reason_code,
                    )
                    .join(
                        s.checkpoints,
                        s.checkpoints.c.checkpoint_id == s.checkpoint_corruptions.c.checkpoint_id,
                    )
                    .where(s.checkpoints.c.job_id == key)
                ).all()
            )
            checkpoints = [
                {
                    "sequence": row.sequence,
                    "checkpoint_id": str(row.checkpoint_id),
                    "attempt_id": str(row.attempt_id),
                    "corrupt": corrupt.get(row.checkpoint_id),
                }
                for row in connection.execute(
                    select(s.checkpoints)
                    .where(s.checkpoints.c.job_id == key)
                    .order_by(s.checkpoints.c.sequence)
                )
            ]
            reservations = [
                {"sequence": row.sequence, "attempt_id": str(row.attempt_id), "state": row.state}
                for row in connection.execute(
                    select(s.checkpoint_reservations)
                    .where(s.checkpoint_reservations.c.job_id == key)
                    .order_by(s.checkpoint_reservations.c.sequence)
                )
            ]
            retries = [
                {"retry_number": row.retry_number, "reason": row.reason, "closed": bool(row.closed)}
                for row in connection.execute(
                    select(
                        s.retry_schedules.c.retry_number,
                        s.retry_schedules.c.reason,
                        s.retry_schedules.c.closed_at.is_not(None).label("closed"),
                    )
                    .where(s.retry_schedules.c.job_id == key)
                    .order_by(s.retry_schedules.c.retry_number)
                )
            ]
            results = [
                {"result_id": str(row.result_id), "attempt_id": str(row.attempt_id)}
                for row in connection.execute(select(s.results).where(s.results.c.job_id == key))
            ]
            events = [
                {
                    "sequence": row.sequence,
                    "event_type": row.event_type,
                    "reason": row.reason,
                    "at": row.created_at.isoformat(timespec="milliseconds"),
                }
                for row in connection.execute(
                    select(s.events).where(s.events.c.job_id == key).order_by(s.events.c.sequence)
                )
            ]
            idempotency = connection.execute(
                select(func.count())
                .select_from(s.idempotency_records)
                .where(s.idempotency_records.c.idempotency_key == self.submitted.get(job_id))
            ).scalar_one()
        return {
            "job_id": job_id,
            "submit_idempotency_key": self.submitted.get(job_id),
            "submit_idempotency_records": idempotency,
            "coordinator_epoch": epoch,
            "job": dict(job._mapping),
            "attempts": attempts,
            "leases": leases,
            "grants": grants,
            "allocations": allocations,
            "containers": containers,
            "checkpoints": checkpoints,
            "reservations": reservations,
            "restores": {
                attempt: (
                    None
                    if value is None
                    else {"checkpoint_id": str(value[0]), "sequence": value[1]}
                )
                for attempt, value in self.restores(job_id).items()
            },
            "retries": retries,
            "results": results,
            "events": events,
        }

    def reconcile(self, job_id, expected_attempts):
        """Accepted IDs, release-after-proof, counters, events, reservations, containers."""

        def released():
            with self.engine.connect() as connection:
                states = list(
                    connection.execute(
                        select(s.allocations.c.state).where(s.allocations.c.job_id == UUID(job_id))
                    ).scalars()
                )
            return states == ["RELEASED"] * expected_attempts

        # The allocation is released only after cleanup proof, which commits after SUCCEEDED.
        _wait(released, f"allocation release of {job_id}", timeout=30)
        timeline = self.timeline(job_id)
        assert timeline["job"]["state"] == "SUCCEEDED"
        assert timeline["job"]["desired_state"] == "RUNNING"
        assert timeline["submit_idempotency_records"] == 1
        assert len(timeline["attempts"]) == expected_attempts
        assert len(timeline["results"]) == 1
        assert timeline["results"][0]["attempt_id"] == timeline["attempts"][-1]["attempt_id"]
        assert [row["sequence"] for row in timeline["events"]] == list(
            range(1, timeline["job"]["event_sequence"] + 1)
        )
        assert len(timeline["allocations"]) == expected_attempts
        assert {row["state"] for row in timeline["allocations"]} == {"RELEASED"}
        assert all(row["stopped"] and row["verified"] for row in timeline["containers"])
        assert all(row["revoked"] for row in timeline["leases"])
        assert all(row["ended"] for row in timeline["grants"])
        assert [row["state"] for row in timeline["reservations"]].count("RESERVED") == 0
        assert timeline["job"]["checkpoint_sequence"] == max(
            (row["sequence"] for row in timeline["reservations"]), default=0
        )
        released = sum(event["event_type"] == "ALLOCATION_RELEASED" for event in timeline["events"])
        assert released == expected_attempts
        for attempt in timeline["attempts"]:
            assert self.containers(attempt["attempt_id"]) == [], "leaked workload container"
        with self.engine.connect() as connection:
            counters = set(
                connection.execute(
                    select(
                        s.admission_counters.c.outstanding, s.admission_counters.c.active_attempts
                    )
                ).all()
            )
            # Exact decimal text column: summed as Decimal, not in SQL.
            charged = list(
                connection.execute(
                    select(s.allocation_ledger_segments.c.charged_amount)
                    .join(
                        s.allocations,
                        s.allocations.c.allocation_id
                        == s.allocation_ledger_segments.c.allocation_id,
                    )
                    .where(s.allocations.c.job_id == UUID(job_id))
                ).scalars()
            )
        assert counters == {(0, 0)}
        timeline["ledger_segments"] = {
            "count": len(charged),
            "charged": str(sum((Decimal(str(value)) for value in charged), Decimal(0))),
        }
        return timeline

    def fairness(self):
        with self.engine.connect() as connection:
            score = connection.execute(
                select(s.fairness_ledgers.c.virtual_score).where(
                    s.fairness_ledgers.c.tenant_id == UUID(self.tenant_id)
                )
            ).scalar_one_or_none()
        return None if score is None else Decimal(str(score))


def _event_pairs(timeline):
    return [(row["event_type"], row["reason"]) for row in timeline["events"]]


def _ordered(pairs, expected):
    """``expected`` occurs in ``pairs`` as an ordered subsequence."""
    position = 0
    for pair in pairs:
        if position < len(expected) and pair == expected[position]:
            position += 1
    return position == len(expected)


def test_b14_cpu_checkpoint_crash_resume_corruption_and_adoption(
    migrated_postgres_engine, tmp_path
):
    if os.environ.get("NEXA_RUN_DOCKER") != "1":
        pytest.skip("opt-in B14 Docker checkpoint evidence")
    image = os.environ["NEXA_B09_IMAGE_REF"]
    worker_image = os.environ["NEXA_B11_WORKER_IMAGE"]
    assert "@sha256:" in image
    labels = json.loads(
        subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", image],
            text=True,
            timeout=10,
        )
    )
    assert labels.get("io.nexa.runner.checkpoint") == "cpu-state-v1"
    architecture = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image],
        text=True,
        timeout=10,
    ).strip()
    engine = migrated_postgres_engine
    _template(engine, image, architecture)
    evidence = {
        "image": image,
        "architecture": architecture,
        "worker_image": worker_image,
        "iterations": ITERATIONS,
        "checkpoint_interval_seconds": INTERVAL_SECONDS,
    }
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
            scores = []

            # 1. Baseline R0: uninterrupted run with periodic checkpoints.
            started = time.monotonic()
            baseline = harness.submit("b14-docker-baseline")
            harness.wait_succeeded(baseline)
            r0 = harness.result(baseline)
            timeline = harness.reconcile(baseline, 1)
            assert len(timeline["checkpoints"]) >= 3, (
                "raise NEXA_B14_ITERATIONS: fewer than 3 checkpoints in the baseline"
            )
            assert list(timeline["restores"].values()) == [None]
            evidence["baseline"] = {
                "seconds": round(time.monotonic() - started, 1),
                "checksum": r0["checksum"],
                "timeline": timeline,
            }
            scores.append(harness.fairness())

            # 2. Crash-resume: SIGKILL the workload after its 2nd checkpoint.
            job = harness.submit("b14-docker-crash-resume")
            first, container = harness.running_attempt(job, 1)
            committed = harness.wait_committed(job, first["attempt_id"], 2)
            harness.kill_workload(container)
            harness.wait_retry_released(job, 1)
            harness.wait_succeeded(job)
            r1 = harness.result(job)
            timeline = harness.reconcile(job, 2)
            newest = max(harness.checkpoints(job, first["attempt_id"]), key=lambda row: row[1])
            assert newest[1] >= committed[-1][1]
            attempts = timeline["attempts"]
            assert (attempts[0]["failure_class"], attempts[0]["failure_reason"]) == (
                "INFRASTRUCTURE",
                "RUNNER_UNAVAILABLE",
            )
            assert timeline["job"]["retry_count"] == 1
            assert timeline["restores"][attempts[1]["attempt_id"]] == {
                "checkpoint_id": str(newest[0]),
                "sequence": newest[1],
            }
            assert _ordered(
                _event_pairs(timeline),
                [
                    ("CHECKPOINT_COMMITTED", "CHECKPOINT_COMMITTED"),
                    ("CHECKPOINT_COMMITTED", "CHECKPOINT_COMMITTED"),
                    ("ATTEMPT_FAILED", "RUNNER_UNAVAILABLE"),
                    ("ALLOCATION_RELEASED", "VERIFIED_CLEANUP"),
                    ("RETRY_READY", "BACKOFF_ELAPSED"),
                    ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED"),
                ],
            ), _event_pairs(timeline)
            assert r1["bytes"] == r0["bytes"] and r1["checksum"] == r0["checksum"]
            evidence["crash_resume"] = {"checksum": r1["checksum"], "timeline": timeline}
            scores.append(harness.fairness())

            # 3. Corrupt newest: coordinator paused so the blob changes before any claim.
            job = harness.submit("b14-docker-corrupt-newest")
            first, container = harness.running_attempt(job, 1)
            harness.wait_committed(job, first["attempt_id"], 2)
            harness.coordinator.pause()
            harness.kill_workload(container)
            harness.wait_retry_released(job, 1)
            committed = harness.checkpoints(job)
            newest, previous = committed[-1], committed[-2]
            harness.corrupt_state_blob(newest[0])
            harness.coordinator.start()
            harness.wait_succeeded(job)
            r2 = harness.result(job)
            timeline = harness.reconcile(job, 2)
            second = timeline["attempts"][1]["attempt_id"]
            assert timeline["restores"][second] == {
                "checkpoint_id": str(previous[0]),
                "sequence": previous[1],
            }
            corrupt = {row["checkpoint_id"]: row["corrupt"] for row in timeline["checkpoints"]}
            assert corrupt[str(newest[0])] == "CHECKPOINT_CHECKSUM_MISMATCH"
            assert [value for value in corrupt.values() if value] == [
                "CHECKPOINT_CHECKSUM_MISMATCH"
            ]
            assert _ordered(
                _event_pairs(timeline),
                [
                    ("ATTEMPT_FAILED", "RUNNER_UNAVAILABLE"),
                    ("CHECKPOINT_CORRUPT", "CHECKPOINT_CHECKSUM_MISMATCH"),
                    ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED"),
                ],
            ), _event_pairs(timeline)
            assert r2["bytes"] == r0["bytes"] and r2["checksum"] == r0["checksum"]
            evidence["corrupt_newest"] = {"checksum": r2["checksum"], "timeline": timeline}
            scores.append(harness.fairness())

            # 4. No valid checkpoint left (restart_safe): fall back to the input.
            job = harness.submit("b14-docker-fallback-input")
            first, container = harness.running_attempt(job, 1)
            harness.wait_committed(job, first["attempt_id"], 2)
            harness.coordinator.pause()
            harness.kill_workload(container)
            harness.wait_retry_released(job, 1)
            committed = harness.checkpoints(job)
            for checkpoint_id, _sequence in committed:
                harness.corrupt_state_blob(checkpoint_id)
            harness.coordinator.start()
            harness.wait_succeeded(job)
            r3 = harness.result(job)
            timeline = harness.reconcile(job, 2)
            second = timeline["attempts"][1]["attempt_id"]
            assert timeline["restores"][second] is None
            corrupt = [row["corrupt"] for row in timeline["checkpoints"][: len(committed)]]
            assert corrupt == ["CHECKPOINT_CHECKSUM_MISMATCH"] * len(committed)
            pairs = _event_pairs(timeline)
            assert _ordered(
                pairs,
                [("CHECKPOINT_CORRUPT", "CHECKPOINT_CHECKSUM_MISMATCH")] * len(committed)
                + [("CHECKPOINT_FALLBACK_TO_INPUT", "CHECKPOINT_FALLBACK_TO_INPUT")],
            ), pairs
            assert ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED") not in pairs
            assert r3["bytes"] == r0["bytes"] and r3["checksum"] == r0["checksum"]
            evidence["fallback_input"] = {"checksum": r3["checksum"], "timeline": timeline}
            scores.append(harness.fairness())

            # 5a. Two kills in one job (the whole 2-retry budget): before the first
            # checkpoint, then between two checkpoints of the restarted attempt.
            job = harness.submit("b14-docker-multi-kill")
            first, container = harness.running_attempt(job, 1)
            time.sleep(1.0)
            assert harness.checkpoints(job, first["attempt_id"]) == []
            assert harness.reserved(first["attempt_id"]) is None
            harness.kill_workload(container)
            harness.wait_retry_released(job, 1)
            assert harness.checkpoints(job, first["attempt_id"]) == []
            second, container = harness.running_attempt(job, 2)
            harness.wait_committed(job, second["attempt_id"], 1)
            # Mid-interval: the next cycle is ~INTERVAL_SECONDS away.
            time.sleep(INTERVAL_SECONDS / 2)
            between = harness.checkpoints(job, second["attempt_id"])
            assert harness.reserved(second["attempt_id"]) is None
            harness.kill_workload(container)
            harness.wait_retry_released(job, 2)
            harness.wait_succeeded(job)
            r4 = harness.result(job)
            timeline = harness.reconcile(job, 3)
            attempts = timeline["attempts"]
            newest = max(harness.checkpoints(job, second["attempt_id"]), key=lambda row: row[1])
            assert newest[1] == between[-1][1]
            assert timeline["restores"][attempts[1]["attempt_id"]] is None
            assert timeline["restores"][attempts[2]["attempt_id"]] == {
                "checkpoint_id": str(newest[0]),
                "sequence": newest[1],
            }
            assert timeline["job"]["retry_count"] == 2 == timeline["job"]["max_retries"]
            assert [row["failure_reason"] for row in attempts[:2]] == ["RUNNER_UNAVAILABLE"] * 2
            pairs = _event_pairs(timeline)
            assert _ordered(
                pairs,
                [
                    ("ATTEMPT_FAILED", "RUNNER_UNAVAILABLE"),
                    ("CHECKPOINT_FALLBACK_TO_INPUT", "CHECKPOINT_FALLBACK_TO_INPUT"),
                    ("CHECKPOINT_COMMITTED", "CHECKPOINT_COMMITTED"),
                    ("ATTEMPT_FAILED", "RUNNER_UNAVAILABLE"),
                    ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED"),
                ],
            ), pairs
            assert r4["bytes"] == r0["bytes"] and r4["checksum"] == r0["checksum"]
            evidence["kill_before_first_and_between"] = {
                "checksum": r4["checksum"],
                "timeline": timeline,
            }
            scores.append(harness.fairness())

            # 5b. Kill immediately after a checkpoint publish commits.
            job = harness.submit("b14-docker-kill-after-publish")
            first, container = harness.running_attempt(job, 1)
            published = harness.wait_committed(job, first["attempt_id"], 1, pause=0.01)
            harness.kill_workload(container)
            harness.wait_retry_released(job, 1)
            harness.wait_succeeded(job)
            r5 = harness.result(job)
            timeline = harness.reconcile(job, 2)
            newest = max(harness.checkpoints(job, first["attempt_id"]), key=lambda row: row[1])
            assert newest[1] == published[-1][1]
            assert timeline["restores"][timeline["attempts"][1]["attempt_id"]] == {
                "checkpoint_id": str(newest[0]),
                "sequence": newest[1],
            }
            assert r5["bytes"] == r0["bytes"] and r5["checksum"] == r0["checksum"]
            evidence["kill_after_publish"] = {"checksum": r5["checksum"], "timeline": timeline}
            scores.append(harness.fairness())

            # 6. Worker process crash while a checkpoint reservation is open; the
            # workload container keeps running and the next incarnation adopts it.
            job = harness.submit("b14-docker-worker-restart")
            first, container = harness.running_attempt(job, 1)
            harness.wait_committed(job, first["attempt_id"], 1)
            open_cycle = _wait(
                lambda: harness.reserved(first["attempt_id"]),
                "open checkpoint reservation",
                timeout=30,
                pause=0.01,
            )
            with engine.connect() as connection:
                prior_incarnation = connection.execute(
                    select(s.workers.c.current_incarnation_id).where(
                        s.workers.c.worker_id == UUID(WORKER_ID)
                    )
                ).scalar_one()
            subprocess.run(
                ["docker", "kill", "--signal", "KILL", harness.worker],
                check=True,
                capture_output=True,
                timeout=15,
            )
            subprocess.run(
                ["docker", "rm", harness.worker], check=True, capture_output=True, timeout=15
            )
            harness.worker = None
            running = subprocess.check_output(
                ["docker", "inspect", "--format", "{{.State.Running}}", container],
                text=True,
                timeout=10,
            ).strip()
            assert running == "true", "workload container must outlive the worker process"
            harness.start_worker()
            _wait(
                lambda: _worker_ready_with_new_incarnation(engine, prior_incarnation),
                "worker READY with a new incarnation",
                timeout=60,
            )
            harness.wait_succeeded(job)
            r6 = harness.result(job)
            timeline = harness.reconcile(job, 1)
            assert timeline["job"]["retry_count"] == 0
            assert [row["container_id"] for row in timeline["containers"]] == [container[:12]]
            incarnations = {row["worker_incarnation_id"] for row in timeline["grants"]}
            assert len(incarnations) == 2 and str(prior_incarnation) in incarnations
            assert timeline["leases"][0]["current_worker_incarnation_id"] != str(prior_incarnation)
            with engine.connect() as connection:
                open_state = connection.execute(
                    select(s.checkpoint_reservations.c.state).where(
                        s.checkpoint_reservations.c.checkpoint_id == open_cycle
                    )
                ).scalar_one()
            assert open_state in {"COMMITTED", "ABANDONED"}
            assert r6["bytes"] == r0["bytes"] and r6["checksum"] == r0["checksum"]
            evidence["worker_restart_adoption"] = {
                "checksum": r6["checksum"],
                "open_reservation_final_state": open_state,
                "timeline": timeline,
            }
            scores.append(harness.fairness())

            # Ledger never resets or double-counts backwards across restarts.
            assert all(value is not None for value in scores)
            assert scores == sorted(scores)
            evidence["fairness_scores"] = [str(value) for value in scores]
            print("B14 docker: baseline, crash-resume, corruption, fallback, kills, adoption")
        finally:
            output = os.environ.get("NEXA_B14_EVIDENCE_OUT")
            if output:
                with open(output, "w", encoding="utf-8") as handle:
                    json.dump(evidence, handle, indent=1, sort_keys=True, default=str)
            harness.close()
