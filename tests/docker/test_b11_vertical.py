"""Opt-in two-tenant production API/coordinator/Linux-worker/Docker CPU slice."""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from decimal import Decimal, localcontext
from uuid import UUID

import pytest
import uvicorn
from sqlalchemy import select, update

from nexa.api.app import create_app
from nexa.infrastructure.persistence import schema as s
from nexa.worker.credentials import CredentialStore
from nexa.workloads.cpu_iterative import CpuIterativeAdapter
from tests.api.test_http_contract import _client
from tests.integration.test_cli_b12_vertical import _success
from tests.integration.test_jobs_b08 import _running_api_process
from tests.integration.test_worker_api_b10 import (
    FINGERPRINT,
    INSTALLATION_ID,
    WORKER_ID,
    _bootstrap_worker,
)

pytestmark = [pytest.mark.docker, pytest.mark.postgres]


def _wait_for(predicate, detail, timeout=80):
    until = time.monotonic() + timeout
    last = None
    while time.monotonic() < until:
        last = predicate()
        if last:
            return last
        time.sleep(0.3)
    raise AssertionError(f"{detail}: last={last!r}")


def _check(response, status):
    assert response.status_code == status, response.text
    return response.json()


class DropFirstCallbackBody:
    """Lose one success body after the API transaction has committed."""

    def __init__(self, app, path_suffix, *, pause=None):
        self.app = app
        self.path_suffix = path_suffix
        self.pause = pause
        self.arrived = threading.Event()
        self.armed = False
        self.dropped = False
        self.callback_id = None
        self.lock = threading.Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].endswith(self.path_suffix):
            return await self.app(scope, receive, send)
        chosen = False

        async def intercept(message):
            nonlocal chosen
            if message["type"] == "http.response.start" and message["status"] == 200:
                with self.lock:
                    if not self.armed:
                        self.armed = True
                        chosen = True
                        self.callback_id = next(
                            value.decode("ascii")
                            for key, value in scope["headers"]
                            if key == b"x-callback-id"
                        )
            if chosen and message["type"] == "http.response.body":
                self.arrived.set()
                if self.pause is not None:
                    await asyncio.to_thread(self.pause.wait, 20)
                if not message.get("more_body", False):
                    self.dropped = True
                return
            await send(message)

        return await self.app(scope, receive, intercept)


def _template(engine, image):
    digest = image.rsplit("@", 1)[1]
    with engine.begin() as connection:
        connection.execute(
            s.templates.insert().values(
                template_id="cpu-iterative",
                current_version=1,
                display_name="CPU iterative",
                description="B11 production slice",
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
                        ("iterations", 1, 1_000_000),
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
                    "architectures": ["linux/arm64"],
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
                checkpointable=False,
                restart_safe=True,
            )
        )


@pytest.mark.parametrize("restart_before_cleanup", [False, True])
def test_two_tenant_api_to_docker_result_and_release(
    migrated_postgres_engine, tmp_path, restart_before_cleanup
):
    if os.environ.get("NEXA_B11_RUNTIME_EVIDENCE") != "1":
        pytest.skip("opt-in B11 Linux Docker vertical slice")
    image = os.environ["NEXA_B09_IMAGE_REF"]
    worker_image = os.environ["NEXA_B11_WORKER_IMAGE"]
    assert "@sha256:" in image
    probe = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "alpine:3.22", "/bin/true"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert probe.returncode == 0, probe.stderr
    engine = migrated_postgres_engine
    _template(engine, image)
    root = (tmp_path / "worker-shared").resolve()
    root.mkdir(mode=0o700)
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        CredentialStore(root / "credential.json").save(
            worker_id=WORKER_ID,
            installation_id=INSTALLATION_ID,
            credential=credential,
        )
        (root / "bootstrap-secret").write_bytes(b"b" * 32)
        login = _check(
            client.post(
                "/v1/internal/admin-bootstrap",
                headers={
                    "X-Nexa-Bootstrap-Secret": "b" * 32,
                    "Idempotency-Key": "b11-vertical-admin-bootstrap",
                },
                json={
                    "username": "b11-vertical@example.test",
                    "display_name": "B11 Vert",
                    "password": "correct-horse-battery-staple",
                },
            ),
            201,
        )
        admin = UUID(login["user_id"])
        session = _check(
            client.post(
                "/v1/auth/login",
                headers={"Origin": "https://nexa.test"},
                json={
                    "username": "b11-vertical@example.test",
                    "password": "correct-horse-battery-staple",
                },
            ),
            200,
        )
        csrf = session["csrf_token"]
        tenants = []
        jobs = []
        cli_token = None
        job_count = 1 if restart_before_cleanup else 2
        for index in range(job_count):
            write = {"Origin": "https://nexa.test", "X-CSRF-Token": csrf}
            tenant = _check(
                client.post(
                    "/v1/admin/tenants",
                    headers={**write, "Idempotency-Key": f"b11-vertical-tenant-{index}"},
                    json={"slug": f"b11-vertical-{index}", "display_name": f"Vertical {index}"},
                ),
                201,
            )
            tenant_id = tenant["tenant_id"]
            _check(
                client.post(
                    f"/v1/admin/tenants/{tenant_id}/memberships",
                    headers={
                        **write,
                        "If-Match": '"v1"',
                        "Idempotency-Key": f"b11-vertical-membership-{index}",
                    },
                    json={"user_id": str(admin), "role": "MEMBER"},
                ),
                200,
            )
            with engine.begin() as connection:
                connection.execute(
                    update(s.tenant_policies)
                    .where(s.tenant_policies.c.tenant_id == UUID(tenant_id))
                    .values(cpu_limit_millis=6000, memory_limit_bytes=12 * 1024**3)
                )
            session = _check(
                client.post(
                    "/v1/auth/login",
                    headers={"Origin": "https://nexa.test"},
                    json={
                        "username": "b11-vertical@example.test",
                        "password": "correct-horse-battery-staple",
                    },
                ),
                200,
            )
            csrf = session["csrf_token"]
            content = json.dumps({"initial_value": index + 5}, separators=(",", ":")).encode()
            checksum = "sha256:" + hashlib.sha256(content).hexdigest()
            if index == 0 and not restart_before_cleanup:
                cli_token = _check(
                    client.post(
                        "/v1/tokens",
                        headers={
                            "Origin": "https://nexa.test",
                            "X-CSRF-Token": csrf,
                            "Idempotency-Key": "b12-docker-cli-token-0001",
                        },
                        json={
                            "name": "b12-docker-vertical",
                            "scopes": [
                                "jobs:read",
                                "jobs:write",
                                "artifacts:read",
                                "artifacts:write",
                            ],
                            "expires_in_seconds": 600,
                        },
                    ),
                    201,
                )["token"]
                source = tmp_path / "b12-docker-input.json"
                source.write_bytes(content)
                with _running_api_process(engine, tmp_path) as (cli_url, _process):
                    artifact = _success(
                        cli_url,
                        cli_token,
                        tmp_path,
                        "artifact",
                        "upload",
                        str(source),
                        "--kind",
                        "INPUT",
                        "--media-type",
                        "application/vnd.nexa.cpu-iterative-input+json",
                        "--tenant",
                        tenant_id,
                        "--idempotency-key",
                        "b12-docker-cli-upload-0001",
                    )
                assert artifact["checksum"] == checksum
            else:
                artifact = _check(
                    client.post(
                        "/v1/artifacts",
                        content=content,
                        headers={
                            **write,
                            "X-CSRF-Token": csrf,
                            "X-Nexa-Tenant-Id": tenant_id,
                            "Idempotency-Key": f"b11-vertical-input-{index}",
                            "X-Artifact-Checksum": checksum,
                            "X-Artifact-Size": str(len(content)),
                            "X-Artifact-Kind": "INPUT",
                            "X-Artifact-Media-Type": (
                                "application/vnd.nexa.cpu-iterative-input+json"
                            ),
                            "Content-Type": "application/octet-stream",
                        },
                    ),
                    201,
                )
            body = {
                "spec": {
                    "template_id": "cpu-iterative",
                    "template_version": 1,
                    "input_artifact_id": artifact["artifact_id"],
                    "resources": {
                        "cpu_millis": 1000,
                        "memory_bytes": 512 * 1024**2,
                        "gpu_count": 0,
                    },
                    "priority": 1,
                    "runtime_limit_seconds": 120,
                    "checkpoint_interval_seconds": 30,
                    "parameters": {"iterations": 100, "seed": 7, "modulus": 101},
                }
            }
            if index == 0 and not restart_before_cleanup:
                spec_file = tmp_path / "b12-docker-job-spec.json"
                spec_file.write_text(json.dumps(body["spec"]), encoding="utf-8")
                with _running_api_process(engine, tmp_path) as (cli_url, _process):
                    accepted = _success(
                        cli_url,
                        cli_token,
                        tmp_path,
                        "job",
                        "submit",
                        "--spec-file",
                        str(spec_file),
                        "--tenant",
                        tenant_id,
                        "--idempotency-key",
                        "b12-docker-cli-submit-0001",
                    )
                    assert (
                        _success(
                            cli_url,
                            cli_token,
                            tmp_path,
                            "job",
                            "get",
                            accepted["job_id"],
                            "--tenant",
                            tenant_id,
                        )["state"]
                        == "QUEUED"
                    )
                    assert (
                        _success(
                            cli_url,
                            cli_token,
                            tmp_path,
                            "job",
                            "events",
                            accepted["job_id"],
                            "--tenant",
                            tenant_id,
                        )["items"][0]["sequence"]
                        == 1
                    )
            else:
                accepted = _check(
                    client.post(
                        "/v1/jobs",
                        headers={
                            **write,
                            "X-CSRF-Token": csrf,
                            "X-Nexa-Tenant-Id": tenant_id,
                            "Idempotency-Key": f"b11-vertical-submit-{index}",
                        },
                        json=body,
                    ),
                    202,
                )
            tenants.append(tenant_id)
            jobs.append((accepted["job_id"], tenant_id, index + 5))
        if not restart_before_cleanup:
            cli_token = _check(
                client.post(
                    "/v1/tokens",
                    headers={
                        "Origin": "https://nexa.test",
                        "X-CSRF-Token": csrf,
                        "Idempotency-Key": "b12-docker-cli-read-token-0001",
                    },
                    json={
                        "name": "b12-docker-result-read",
                        "scopes": ["jobs:read", "artifacts:read"],
                        "expires_in_seconds": 600,
                    },
                ),
                201,
            )["token"]
        # Same FastAPI factory and PostgreSQL database; separate serving process
        # boundary is exercised by worker HTTP and coordinator subprocess.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        completion_resume = threading.Event() if restart_before_cleanup else None
        completion_fault = DropFirstCallbackBody(
            create_app(client.app.state.services.settings, engine=engine),
            "/complete",
            pause=completion_resume,
        )
        claim_fault = DropFirstCallbackBody(completion_fault, "/claim")
        server = uvicorn.Server(
            uvicorn.Config(
                claim_fault,
                host="0.0.0.0",
                port=port,
                log_level="warning",
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        _wait_for(lambda: server.started, "API readiness", timeout=10)
        worker_command = [
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
            f"type=bind,src={root},dst={root}",
            "--env",
            f"NEXA_WORKER_API_URL=http://host.docker.internal:{port}",
            "--env",
            f"NEXA_WORKER_STATE_ROOT={root}",
            "--env",
            f"NEXA_INSTALLATION_ID={INSTALLATION_ID}",
            "--env",
            f"NEXA_LOCAL_WORKER_ID={WORKER_ID}",
            "--env",
            f"NEXA_LOCAL_WORKER_FINGERPRINT={FINGERPRINT}",
            "--env",
            f"NEXA_BOOTSTRAP_SECRET_FILE={root / 'bootstrap-secret'}",
            "--env",
            f"NEXA_CPU_IMAGE_REF={image}",
            worker_image,
            "nexa-worker",
        ]
        coordinator = None
        worker = None
        try:
            worker = subprocess.check_output(worker_command, text=True).strip()
            try:
                _wait_for(lambda: _worker_ready(engine), "worker READY", timeout=25)
            except AssertionError as exc:
                logs = subprocess.run(
                    ["docker", "logs", "--tail", "80", worker],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                with engine.connect() as connection:
                    states = list(
                        connection.execute(select(s.workers.c.health, s.workers.c.admin_state))
                    )
                    incarnation = [
                        (row.reconciliation_drained, row.reconciliation_snapshot)
                        for row in connection.execute(
                            select(
                                s.worker_incarnations.c.reconciliation_drained,
                                s.worker_incarnations.c.reconciliation_snapshot,
                            )
                        )
                    ]
                    inventory = [
                        (
                            row.host_cpu_millis,
                            row.host_memory_bytes,
                            row.allocatable_cpu_millis,
                            row.allocatable_memory_bytes,
                            row.runtime_capabilities,
                            row.workload_capabilities,
                        )
                        for row in connection.execute(
                            select(
                                s.worker_inventories.c.host_cpu_millis,
                                s.worker_inventories.c.host_memory_bytes,
                                s.worker_inventories.c.allocatable_cpu_millis,
                                s.worker_inventories.c.allocatable_memory_bytes,
                                s.worker_inventories.c.runtime_capabilities,
                                s.worker_inventories.c.workload_capabilities,
                            )
                        )
                    ]
                raise AssertionError(
                    f"{exc}; states={states}; incarnation={incarnation}; "
                    f"inventory={inventory}; worker_log={logs.stderr[-3000:]}"
                ) from exc
            coordinator = subprocess.Popen(
                [sys.executable, "-m", "nexa.coordinator.main"],
                env={
                    **os.environ,
                    "NEXA_DATABASE_URL": os.environ["NEXA_TEST_DATABASE_URL"],
                    "PYTHONPATH": "src",
                },
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for job_id, tenant_id, initial in jobs:

                def recognized(job_id=job_id, tenant_id=tenant_id):
                    response = client.get(
                        f"/v1/jobs/{job_id}/result",
                        headers={"X-Nexa-Tenant-Id": tenant_id},
                    )
                    if response.status_code != 200:
                        return None
                    return response.json()

                try:
                    result = _wait_for(recognized, f"recognized result {job_id}", timeout=35)
                except AssertionError as exc:
                    with engine.connect() as connection:
                        snapshot = {
                            "jobs": [
                                (str(row.job_id), row.state, row.waiting_reason)
                                for row in connection.execute(
                                    select(s.jobs.c.job_id, s.jobs.c.state, s.jobs.c.waiting_reason)
                                )
                            ],
                            "attempts": [
                                (str(row.attempt_id), row.state)
                                for row in connection.execute(
                                    select(s.attempts.c.attempt_id, s.attempts.c.state)
                                )
                            ],
                            "allocations": [
                                (str(row.allocation_id), row.state)
                                for row in connection.execute(
                                    select(s.allocations.c.allocation_id, s.allocations.c.state)
                                )
                            ],
                            "worker": [
                                (row.health, row.admin_state)
                                for row in connection.execute(
                                    select(s.workers.c.health, s.workers.c.admin_state)
                                )
                            ],
                        }
                    logs = subprocess.run(
                        ["docker", "logs", "--tail", "80", worker],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    loop_warnings = [
                        line for line in logs.stderr.splitlines() if "worker_loop_" in line
                    ]
                    raise AssertionError(
                        f"{exc}; coordinator={coordinator.poll()}; state={snapshot}; "
                        f"runners={_runner_diagnostics(engine, root)}; "
                        f"loop_warnings={loop_warnings}; worker_log={logs.stderr[-5000:]}"
                    ) from exc
                if restart_before_cleanup:
                    assert completion_fault.arrived.wait(5), "completion commit was not intercepted"
                    with engine.connect() as connection:
                        held = connection.execute(select(s.allocations.c.state)).scalar_one()
                        prior_incarnation = connection.execute(
                            select(s.workers.c.current_incarnation_id)
                        ).scalar_one()
                    assert held == "HELD"
                    subprocess.run(
                        ["docker", "rm", "--force", worker],
                        check=True,
                        capture_output=True,
                        timeout=12,
                    )
                    worker = None
                    completion_resume.set()
                    worker = subprocess.check_output(worker_command, text=True).strip()
                    _wait_for(
                        lambda prior=prior_incarnation: _worker_ready_with_new_incarnation(
                            engine, prior
                        ),
                        "worker READY after committed completion before cleanup",
                        timeout=30,
                    )
                    with engine.connect() as connection:
                        attempt_id = str(
                            connection.execute(select(s.attempts.c.attempt_id)).scalar_one()
                        )
                    from nexa.worker.journal import ExecutionJournal

                    flow = (ExecutionJournal(root / "journal").load(attempt_id).runner_state or {})[
                        "result_flow"
                    ]
                    assert flow["completed"] is True
                    assert flow["completion_ack"]["callback_id"] == completion_fault.callback_id
                with engine.connect() as connection:
                    output_id = connection.execute(
                        select(s.artifact_references.c.artifact_id).where(
                            s.artifact_references.c.owner_type == "RESULT",
                            s.artifact_references.c.owner_id == UUID(result["result_id"]),
                            s.artifact_references.c.purpose == "RESULT_FILE",
                        )
                    ).scalar_one()
                    spec_checksum = connection.execute(
                        select(s.job_specs.c.spec_checksum).where(
                            s.job_specs.c.job_id == UUID(job_id)
                        )
                    ).scalar_one()
                data = _check_output(client, tenant_id, output_id)
                expected = CpuIterativeAdapter().run(
                    input_bytes=json.dumps(
                        {"initial_value": initial}, separators=(",", ":")
                    ).encode(),
                    iterations=100,
                    seed=7,
                    modulus=101,
                    spec_checksum=spec_checksum,
                )
                assert data == expected.result_bytes
                if job_id == jobs[0][0] and not restart_before_cleanup:
                    assert cli_token is not None
                    cli_url = f"http://127.0.0.1:{port}"
                    cli_status = _success(
                        cli_url, cli_token, tmp_path, "job", "get", job_id, "--tenant", tenant_id
                    )
                    assert cli_status["state"] == "SUCCEEDED"
                    cli_events = _success(
                        cli_url,
                        cli_token,
                        tmp_path,
                        "job",
                        "events",
                        job_id,
                        "--tenant",
                        tenant_id,
                    )
                    assert len(cli_events["items"]) > 1
                    cli_result = _success(
                        cli_url,
                        cli_token,
                        tmp_path,
                        "job",
                        "result",
                        job_id,
                        "--tenant",
                        tenant_id,
                    )
                    assert cli_result["manifest_artifact_id"] == result["manifest_artifact_id"]
                    manifest_id = UUID(result["manifest_artifact_id"])
                    with engine.connect() as connection:
                        manifest_checksum = connection.execute(
                            select(s.artifacts.c.checksum).where(
                                s.artifacts.c.artifact_id == manifest_id
                            )
                        ).scalar_one()
                        output_checksum = connection.execute(
                            select(s.artifacts.c.checksum).where(
                                s.artifacts.c.artifact_id == output_id
                            )
                        ).scalar_one()
                    manifest_bytes = _check_output(client, tenant_id, manifest_id)
                    assert json.loads(manifest_bytes)["provenance"]["job_id"] == job_id
                    manifest_path = tmp_path / "b12-docker-result-manifest.json"
                    cli_manifest = _success(
                        cli_url,
                        cli_token,
                        tmp_path,
                        "job",
                        "result-download",
                        job_id,
                        "--output-file",
                        str(manifest_path),
                        "--checksum",
                        manifest_checksum,
                        "--tenant",
                        tenant_id,
                    )
                    assert manifest_path.read_bytes() == manifest_bytes
                    assert cli_manifest["sha256"] == manifest_checksum
                    output_path = tmp_path / "b12-docker-result.json"
                    cli_output = _success(
                        cli_url,
                        cli_token,
                        tmp_path,
                        "artifact",
                        "download",
                        str(output_id),
                        "--output-file",
                        str(output_path),
                        "--checksum",
                        output_checksum,
                        "--tenant",
                        tenant_id,
                    )
                    assert output_path.read_bytes() == expected.result_bytes
                    assert cli_output["sha256"] == output_checksum
            _wait_for(
                lambda: _released(engine, job_count), "allocation/counter release", timeout=30
            )
            assert claim_fault.dropped and claim_fault.callback_id is not None
            assert completion_fault.dropped and completion_fault.callback_id is not None
            with engine.connect() as connection:
                claim_receipts = (
                    connection.execute(
                        select(s.callback_receipts.c.callback_id).where(
                            s.callback_receipts.c.operation_id == "workerClaimAttempt"
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(claim_receipts) == job_count
                assert UUID(claim_fault.callback_id) in claim_receipts
                completion_receipts = (
                    connection.execute(
                        select(s.callback_receipts.c.callback_id).where(
                            s.callback_receipts.c.operation_id == "workerCompleteAttempt"
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(completion_receipts) == job_count
                assert UUID(completion_fault.callback_id) in completion_receipts
                assert len(connection.execute(select(s.results)).all()) == job_count
                allocations = {
                    row.allocation_id: row
                    for row in connection.execute(select(s.allocations)).mappings()
                }
                segments = list(connection.execute(select(s.allocation_ledger_segments)).mappings())
                ledgers = {
                    row.tenant_id: row.virtual_score
                    for row in connection.execute(select(s.fairness_ledgers)).mappings()
                }
                assert {row.allocation_id for row in segments} == set(allocations)
                for allocation_id, allocation in allocations.items():
                    history = sorted(
                        (row for row in segments if row.allocation_id == allocation_id),
                        key=lambda row: (row.started_at, row.segment_id),
                    )
                    assert history[0].started_at == allocation.held_at
                    assert history[-1].ended_at == allocation.released_at
                    assert all(row.ended_at is not None for row in history)
                    assert all(
                        previous.ended_at == following.started_at
                        for previous, following in zip(history, history[1:], strict=False)
                    )
                with localcontext() as decimal_context:
                    decimal_context.prec = 100
                    charge_totals = {
                        tenant_id: sum(
                            row.charged_amount for row in segments if row.tenant_id == tenant_id
                        )
                        for tenant_id in ledgers
                    }
                assert all(
                    abs(score - charge_totals[tenant_id]) <= Decimal("1e-40")
                    for tenant_id, score in ledgers.items()
                ), (ledgers, charge_totals)
                assert all(ledgers[allocation.tenant_id] > 0 for allocation in allocations.values())
                assert all(
                    row == 0
                    for row in connection.execute(
                        select(s.admission_counters.c.active_attempts)
                    ).scalars()
                )
                assert all(
                    row == 0
                    for row in connection.execute(
                        select(s.admission_counters.c.outstanding)
                    ).scalars()
                )
            with engine.connect() as connection:
                prior_incarnation = connection.execute(
                    select(s.workers.c.current_incarnation_id)
                ).scalar_one()
                prior_results = tuple(connection.execute(select(s.results.c.result_id)).scalars())
                prior_scores = tuple(
                    connection.execute(
                        select(
                            s.fairness_ledgers.c.tenant_id, s.fairness_ledgers.c.virtual_score
                        ).order_by(s.fairness_ledgers.c.tenant_id)
                    )
                )
                prior_segments = tuple(
                    connection.execute(
                        select(
                            s.allocation_ledger_segments.c.segment_id,
                            s.allocation_ledger_segments.c.charged_amount,
                        ).order_by(s.allocation_ledger_segments.c.segment_id)
                    )
                )
            subprocess.run(
                ["docker", "stop", "--time", "3", worker],
                check=True,
                capture_output=True,
                timeout=12,
            )
            subprocess.run(["docker", "rm", worker], check=True, capture_output=True, timeout=8)
            worker = None
            coordinator.terminate()
            coordinator.wait(timeout=8)
            coordinator = None
            worker = subprocess.check_output(worker_command, text=True).strip()
            _wait_for(
                lambda: _worker_ready_with_new_incarnation(engine, prior_incarnation),
                "worker READY after restart",
                timeout=30,
            )
            coordinator = subprocess.Popen(
                [sys.executable, "-m", "nexa.coordinator.main"],
                env={
                    **os.environ,
                    "NEXA_DATABASE_URL": os.environ["NEXA_TEST_DATABASE_URL"],
                    "PYTHONPATH": "src",
                },
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for job_id, tenant_id, _initial in jobs:
                after = _check(
                    client.get(
                        f"/v1/jobs/{job_id}/result", headers={"X-Nexa-Tenant-Id": tenant_id}
                    ),
                    200,
                )
                assert UUID(after["result_id"]) in prior_results
            with engine.connect() as connection:
                assert (
                    tuple(connection.execute(select(s.results.c.result_id)).scalars())
                    == prior_results
                )
                assert (
                    tuple(
                        connection.execute(
                            select(
                                s.fairness_ledgers.c.tenant_id, s.fairness_ledgers.c.virtual_score
                            ).order_by(s.fairness_ledgers.c.tenant_id)
                        )
                    )
                    == prior_scores
                )
                assert (
                    tuple(
                        connection.execute(
                            select(
                                s.allocation_ledger_segments.c.segment_id,
                                s.allocation_ledger_segments.c.charged_amount,
                            ).order_by(s.allocation_ledger_segments.c.segment_id)
                        )
                    )
                    == prior_segments
                )
                assert set(
                    connection.execute(
                        select(
                            s.admission_counters.c.outstanding,
                            s.admission_counters.c.active_attempts,
                        )
                    )
                ) == {(0, 0)}
            print("B11 vertical: response loss, restart, results and accounting")
        finally:
            if completion_resume is not None:
                completion_resume.set()
            if coordinator is not None:
                coordinator.terminate()
                coordinator.wait(timeout=8)
            if worker is not None:
                subprocess.run(
                    ["docker", "rm", "--force", worker],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
            # The fixture drops its database after this block. Remove only
            # containers created for attempt IDs in this test, including a
            # runner still alive after an assertion failure.
            with engine.connect() as connection:
                attempt_ids = list(connection.execute(select(s.attempts.c.attempt_id)).scalars())
            for attempt_id in attempt_ids:
                containers = subprocess.check_output(
                    [
                        "docker",
                        "ps",
                        "--all",
                        "--quiet",
                        "--filter",
                        f"label=nexa.installation_id={INSTALLATION_ID}",
                        "--filter",
                        f"label=nexa.attempt_id={attempt_id}",
                    ],
                    text=True,
                    timeout=5,
                ).splitlines()
                if containers:
                    subprocess.run(
                        ["docker", "rm", "--force", *containers],
                        check=True,
                        capture_output=True,
                        timeout=10,
                    )
            server.should_exit = True
            thread.join(timeout=8)


def _worker_ready(engine):
    with engine.connect() as connection:
        return (
            connection.execute(
                select(s.workers.c.health).where(s.workers.c.worker_id == UUID(WORKER_ID))
            ).scalar_one_or_none()
            == "READY"
        )


def _worker_ready_with_new_incarnation(engine, old_incarnation):
    with engine.connect() as connection:
        row = connection.execute(
            select(s.workers.c.health, s.workers.c.current_incarnation_id).where(
                s.workers.c.worker_id == UUID(WORKER_ID)
            )
        ).one()
        return row.health == "READY" and row.current_incarnation_id != old_incarnation


def _released(engine, count=2):
    with engine.connect() as connection:
        states = list(connection.execute(select(s.allocations.c.state)).scalars())
        return len(states) == count and states == ["RELEASED"] * count


def _check_output(client, tenant_id, artifact_id):
    response = client.get(
        f"/v1/artifacts/{artifact_id}/content",
        headers={"X-Nexa-Tenant-Id": tenant_id},
    )
    assert response.status_code == 200, response.text
    return response.content


def _runner_diagnostics(engine, root):
    with engine.connect() as connection:
        attempt_ids = list(connection.execute(select(s.attempts.c.attempt_id)).scalars())
    details = {}
    pending_path = root / "agent-state.json"
    if pending_path.exists():
        operations = json.loads(pending_path.read_text()).get("operations", {})
        details["pending_operations"] = [
            (
                record["operation"],
                record["payload"].get("attempt_id"),
                record["acknowledgment"] is not None,
            )
            for record in operations.values()
        ]
    for attempt_id in attempt_ids:
        ids = subprocess.check_output(
            ["docker", "ps", "--all", "--quiet", "--filter", f"label=nexa.attempt_id={attempt_id}"],
            text=True,
            timeout=5,
        ).splitlines()
        path = root / "journal" / "records" / f"{attempt_id}.json"
        journal = json.loads(path.read_text()) if path.exists() else {}
        details[str(attempt_id)] = {
            "journal_state": journal.get("state"),
            "runner_state_keys": list((journal.get("runner_state") or {}).keys()),
            "containers": [
                {
                    "id": container,
                    "state": subprocess.check_output(
                        ["docker", "inspect", "--format", "{{json .State}}", container],
                        text=True,
                        timeout=5,
                    ).strip(),
                    "runner": subprocess.run(
                        [
                            "docker",
                            "exec",
                            "--user",
                            "1000:1000",
                            container,
                            "python",
                            "-c",
                            "import json,pathlib; p=pathlib.Path('/run/nexa/runner-state.json'); "
                            "d=json.loads(p.read_text()); "
                            "print({k:d.get(k) for k in "
                            "('state','runtime_started_at','authority_deadline','stop_reason')}, "
                            "[x.get('type') for x in d.get('pending_messages',[])], "
                            "[x.name for x in pathlib.Path('/run/nexa').iterdir()])",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    ).stdout[-1000:],
                    "processes": subprocess.run(
                        ["docker", "top", container],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    ).stdout[-1000:],
                    "log": subprocess.run(
                        ["docker", "logs", "--tail", "20", container],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    ).stderr[-1200:],
                }
                for container in ids
            ],
        }
    return details
