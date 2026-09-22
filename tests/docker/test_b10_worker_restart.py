import hashlib
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select, update

from nexa.api.app import create_app
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema import (
    attempt_authority_grants,
    attempt_leases,
    attempts,
    callback_receipts,
    container_identities,
    worker_credentials,
    worker_incarnations,
    workers,
)
from nexa.worker.credentials import CredentialStore
from nexa.worker.docker_client import SubprocessDockerBackend
from nexa.worker.executor import DockerExecutor
from nexa.worker.journal import ExecutionJournal
from nexa.worker.models import (
    AllocationIdentity,
    Authority,
    CpuWorkloadSpec,
    ExecutionContext,
    InputMount,
    ResourceVector,
    StartExecution,
)
from nexa.worker.probes import DockerProbeBackend
from tests.integration._factories import seed_authority, seed_job, seed_tenant_graph
from tests.integration.identity_support import (
    FINGERPRINT,
    make_identity_service,
)

pytestmark = [pytest.mark.docker, pytest.mark.postgres]


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_until(predicate, *, timeout: float, detail: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {detail}; last observation={last!r}")


def _start_api(
    engine,
    tmp_path: Path,
    *,
    installation_id: UUID,
    worker_id: UUID,
) -> tuple[uvicorn.Server, threading.Thread, str]:
    identity = make_identity_service(
        engine,
        tmp_path,
        installation_id=installation_id,
        worker_id=worker_id,
    )
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(identity.settings, engine=engine),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, name="b10-api", daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"

    def ready() -> bool:
        try:
            return httpx.get(f"{base_url}/openapi.json", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    _wait_until(ready, timeout=10, detail="B10 API startup")
    return server, thread, base_url


def _wait_for_runner_socket(container_id: str) -> bool:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "--user",
            "1000:1000",
            container_id,
            "test",
            "-S",
            "/run/nexa/control.sock",
        ],
        capture_output=True,
    )
    return result.returncode == 0


def _worker_observation(
    engine,
    attempt_id: UUID,
    *,
    worker_id: UUID,
    minimum_sequence: int,
):
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(
                    workers.c.health,
                    workers.c.current_incarnation_id,
                    worker_incarnations.c.sequence,
                    attempts.c.worker_incarnation_id.label("attempt_incarnation_id"),
                    attempt_leases.c.current_worker_incarnation_id.label("lease_incarnation_id"),
                    attempt_leases.c.expires_at,
                )
                .join(
                    worker_incarnations,
                    worker_incarnations.c.worker_incarnation_id == workers.c.current_incarnation_id,
                )
                .join(attempts, attempts.c.worker_id == workers.c.worker_id)
                .join(attempt_leases, attempt_leases.c.attempt_id == attempts.c.attempt_id)
                .where(workers.c.worker_id == worker_id, attempts.c.attempt_id == attempt_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        active_grants = connection.execute(
            select(func.count())
            .select_from(attempt_authority_grants)
            .where(
                attempt_authority_grants.c.attempt_id == attempt_id,
                attempt_authority_grants.c.ended_at.is_(None),
            )
        ).scalar_one()
        renewals = connection.execute(
            select(func.count())
            .select_from(callback_receipts)
            .where(callback_receipts.c.operation_id == "workerRenewAttempt")
        ).scalar_one()
    current = row["current_incarnation_id"]
    if (
        row["health"] == "READY"
        and int(row["sequence"]) >= minimum_sequence
        and row["attempt_incarnation_id"] == current
        and row["lease_incarnation_id"] == current
        and active_grants == 1
        and renewals >= 1
    ):
        return dict(row)
    return None


def _spawn_worker(
    base_url: str,
    state_root: Path,
    bootstrap_secret: Path,
    image: str,
    *,
    installation_id: UUID,
    worker_id: UUID,
):
    environment = os.environ.copy()
    environment.update(
        {
            "NEXA_WORKER_API_URL": base_url,
            "NEXA_INSTALLATION_ID": str(installation_id),
            "NEXA_LOCAL_WORKER_ID": str(worker_id),
            "NEXA_LOCAL_WORKER_FINGERPRINT": FINGERPRINT,
            "NEXA_WORKER_STATE_ROOT": str(state_root),
            "NEXA_BOOTSTRAP_SECRET_FILE": str(bootstrap_secret),
            "NEXA_CPU_IMAGE_REF": image,
            "PYTHONPATH": str(Path.cwd() / "src"),
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    return subprocess.Popen(
        [sys.executable, "-m", "nexa.worker.main"],
        cwd=Path.cwd(),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _assert_process_alive(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        return
    stdout, stderr = process.communicate(timeout=1)
    raise AssertionError(
        f"worker exited early with {process.returncode}: stdout={stdout!r} stderr={stderr!r}"
    )


def test_worker_kill_restart_adopts_and_renews_the_live_runner(
    migrated_postgres_engine, tmp_path: Path
) -> None:
    if os.environ.get("NEXA_B10_RUNTIME_EVIDENCE") != "1":
        pytest.skip("opt-in B10 worker restart evidence")
    if sys.platform != "linux":
        pytest.skip("worker and runner must share a Linux monotonic clock domain")
    image = os.environ.get("NEXA_B09_IMAGE_REF", "")
    if "@sha256:" not in image:
        pytest.fail("NEXA_B09_IMAGE_REF must be an exact digest reference")

    installation_id = new_uuid7()
    worker_id = new_uuid7()
    server, server_thread, base_url = _start_api(
        migrated_postgres_engine,
        tmp_path,
        installation_id=installation_id,
        worker_id=worker_id,
    )
    state_root = tmp_path / "worker-state"
    state_root.mkdir()
    bootstrap_secret = tmp_path / "bootstrap-secret"
    journal = ExecutionJournal(state_root / "journal")
    executor = None
    identity = None
    first_worker = None
    second_worker = None
    try:
        with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as client:
            bootstrap = client.post(
                "/v1/internal/worker-bootstrap",
                headers={
                    "X-Nexa-Bootstrap-Secret": "b" * 32,
                    "Idempotency-Key": "b10-runtime-bootstrap-0001",
                },
                json={
                    "installation_id": str(installation_id),
                    "credential_public_fingerprint": FINGERPRINT,
                },
            )
            assert bootstrap.status_code == 201, bootstrap.text
            credential = bootstrap.json()["credential"]
            CredentialStore(state_root / "credential.json").save(
                worker_id=str(worker_id),
                installation_id=str(installation_id),
                credential=credential,
            )
            prior = client.post(
                f"/v1/workers/{worker_id}/incarnations",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Idempotency-Key": "b10-runtime-prior-incarnation-0001",
                },
                json={"process_start_nonce": "018f0d60-7b6a-7a40-9d82-1aa39c4f30b7"},
            )
            assert prior.status_code == 201, prior.text

        with migrated_postgres_engine.begin() as connection:
            graph = seed_tenant_graph(connection, label="b10-runtime")
            job = seed_job(connection, graph, state="RUNNING")
            seeded = seed_authority(
                connection,
                graph,
                job,
                {
                    "worker_id": worker_id,
                    "incarnation_id": UUID(prior.json()["worker_incarnation_id"]),
                },
            )
            connection.execute(
                update(attempts)
                .where(attempts.c.attempt_id == seeded["attempt_id"])
                .values(state="RUNNING", started_at=datetime.now(UTC))
            )
            startup_nonce = connection.execute(
                select(attempts.c.startup_nonce).where(
                    attempts.c.attempt_id == seeded["attempt_id"]
                )
            ).scalar_one()
            credential_hash = connection.execute(
                select(worker_credentials.c.credential_hash).where(
                    worker_credentials.c.worker_id == worker_id,
                    worker_credentials.c.revoked_at.is_(None),
                )
            ).scalar_one()

        image_ref, image_digest = image.rsplit("@", 1)
        architecture = DockerProbeBackend(image_ref=image).snapshot().architecture
        source_root = tmp_path / "staging"
        source_root.mkdir()
        source = source_root / "input.json"
        source.write_bytes(b'{"initial_value":17}')
        input_checksum = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
        resources = ResourceVector(1000, 1024**3, 0)
        authority = Authority(
            worker_id=str(worker_id),
            worker_incarnation_id=prior.json()["worker_incarnation_id"],
            attempt_id=str(seeded["attempt_id"]),
            allocation_id=str(seeded["allocation_id"]),
            lease_id=str(seeded["lease_id"]),
            job_fence=1,
        )
        request = StartExecution(
            context=ExecutionContext(
                authority=authority,
                tenant_id=str(graph["tenant_id"]),
                job_id=str(job["job_id"]),
                logical_session_id=str(job["session_id"]),
                template_id="cpu-iterative",
                template_version=1,
                image_digest=image_digest,
                architecture=architecture,
                adapter_id="cpu.iterative",
                adapter_version="1.0.0",
                input_checksum=input_checksum,
                startup_nonce=str(startup_nonce),
                resources=resources,
            ),
            allocation=AllocationIdentity(
                allocation_id=str(seeded["allocation_id"]),
                attempt_id=str(seeded["attempt_id"]),
                resources=resources,
            ),
            startup_nonce=str(startup_nonce),
            operation_sequence=1,
            scratch_bytes=64 * 1024**2,
            log_bytes=1024**2,
            runtime_limit_seconds=300,
            input_mounts=(
                InputMount(
                    artifact_id=str(graph["artifact_id"]),
                    source_path=str(source),
                    target_path="/input/input.json",
                    content_checksum=input_checksum,
                    size_bytes=source.stat().st_size,
                ),
            ),
            cpu_workload=CpuWorkloadSpec(
                iterations=1_000_000_000,
                seed=7,
                modulus=1_000_000_007,
                spec_checksum="sha256:" + "f" * 64,
            ),
        )
        executor = DockerExecutor(
            journal,
            SubprocessDockerBackend(),
            image_ref=image_ref,
            staging_root=source_root,
            installation_id=str(installation_id),
        )
        identity = executor.start(executor.prepare(request))
        _wait_until(
            lambda: _wait_for_runner_socket(identity.container_id),
            timeout=10,
            detail="trusted runner control socket",
        )
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                container_identities.insert().values(
                    tenant_id=graph["tenant_id"],
                    job_id=job["job_id"],
                    attempt_id=seeded["attempt_id"],
                    allocation_id=seeded["allocation_id"],
                    startup_nonce=startup_nonce,
                    executor_create_sequence=1,
                    container_id=identity.container_id,
                    runtime_identity_digest=identity.runtime_identity_digest,
                    created_at=datetime.now(UTC),
                )
            )

        first_worker = _spawn_worker(
            base_url,
            state_root,
            bootstrap_secret,
            image,
            installation_id=installation_id,
            worker_id=worker_id,
        )
        first = _wait_until(
            lambda: (
                _assert_process_alive(first_worker)
                or _worker_observation(
                    migrated_postgres_engine,
                    seeded["attempt_id"],
                    worker_id=worker_id,
                    minimum_sequence=2,
                )
            ),
            timeout=25,
            detail="first worker adoption, renewal and READY",
        )
        first_pid = first_worker.pid
        first_worker.kill()
        assert first_worker.wait(timeout=5) < 0
        running_after_kill = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", identity.container_id],
            check=True,
            capture_output=True,
            text=True,
        )
        assert running_after_kill.stdout.strip() == "true"

        second_worker = _spawn_worker(
            base_url,
            state_root,
            bootstrap_secret,
            image,
            installation_id=installation_id,
            worker_id=worker_id,
        )
        second = _wait_until(
            lambda: (
                _assert_process_alive(second_worker)
                or _worker_observation(
                    migrated_postgres_engine,
                    seeded["attempt_id"],
                    worker_id=worker_id,
                    minimum_sequence=int(first["sequence"]) + 1,
                )
            ),
            timeout=25,
            detail="restarted worker adoption, renewal and READY",
        )
        assert second_worker.pid != first_pid
        assert second["current_incarnation_id"] != first["current_incarnation_id"]
        assert second["expires_at"] > first["expires_at"]

        record = journal.load(str(seeded["attempt_id"]))
        assert record.authority.worker_incarnation_id == str(second["current_incarnation_id"])
        assert record.runner_state is not None
        assert record.runner_state["last_control_sequence"] >= 2
        assert record.runner_state["authority_deadline_monotonic_ns"] > time.monotonic_ns()

        with migrated_postgres_engine.connect() as connection:
            credentials = (
                connection.execute(
                    select(worker_credentials.c.credential_hash).where(
                        worker_credentials.c.worker_id == worker_id,
                        worker_credentials.c.revoked_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            grant_count = connection.execute(
                select(func.count())
                .select_from(attempt_authority_grants)
                .where(attempt_authority_grants.c.attempt_id == seeded["attempt_id"])
            ).scalar_one()
        assert credentials == [credential_hash]
        assert grant_count == 3
    finally:
        for process in (second_worker, first_worker):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        if executor is not None and identity is not None:
            executor.cleanup(identity)
        server.should_exit = True
        server_thread.join(timeout=10)
        assert not server_thread.is_alive()
