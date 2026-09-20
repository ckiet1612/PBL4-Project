from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from threading import Barrier

import httpx
import pytest
from sqlalchemy import func, select, text

from nexa.application.errors import ApplicationError
from nexa.application.job_service import JobService
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema import (
    admission_counters,
    allocations,
    artifact_references,
    artifacts,
    attempt_leases,
    attempts,
    audit_records,
    events,
    idempotency_records,
    job_specs,
    jobs,
    logical_sessions,
    rate_buckets,
    template_versions,
    templates,
    tenant_policies,
)
from tests.api.test_http_contract import _client

pytestmark = pytest.mark.postgres


def _seed_cpu_inputs(engine, tenant_id):
    artifact_id = new_uuid7()
    checksum = f"sha256:{sha256(b'input').hexdigest()}"
    with engine.begin() as connection:
        connection.execute(
            templates.insert().values(
                template_id="cpu-iterative",
                current_version=1,
                display_name="CPU iterative",
                description="B08 test template",
                enabled=True,
            )
        )
        connection.execute(
            template_versions.insert().values(
                template_id="cpu-iterative",
                version=1,
                parameter_schema=[
                    {
                        "name": "iterations",
                        "type": "INTEGER",
                        "required": True,
                        "minimum": 1,
                        "maximum": 1_000_000_000,
                    },
                    {
                        "name": "seed",
                        "type": "INTEGER",
                        "required": True,
                        "minimum": 0,
                        "maximum": 2_147_483_647,
                    },
                    {
                        "name": "modulus",
                        "type": "INTEGER",
                        "required": True,
                        "minimum": 2,
                        "maximum": 2_147_483_647,
                    },
                ],
                resource_bounds={
                    "resources": {
                        "cpu_millis": 100_000_000,
                        "memory_bytes": 9_223_372_036_854_775_807,
                        "gpu_count": 0,
                    },
                    "runtime_limit_seconds": 300,
                    "checkpoint_interval_seconds": 60,
                },
                capability_requirements={
                    "architectures": ["linux/amd64"],
                    "adapter_id": "cpu.iterative",
                    "adapter_version": "1.0.0",
                    "image_digest": "sha256:" + "b" * 64,
                    "device": "CPU",
                    "framework": "NEXA_CPU",
                    "framework_version": "1.0.0",
                    "cuda_runtime_min": None,
                    "driver_min": None,
                    "compute_capability_min": None,
                },
                adapter_id="cpu.iterative",
                adapter_version="1.0.0",
                image_digest="sha256:" + "b" * 64,
                checkpointable=True,
                restart_safe=True,
            )
        )
        connection.execute(
            artifacts.insert().values(
                artifact_id=artifact_id,
                tenant_id=tenant_id,
                kind="INPUT",
                media_type="application/vnd.nexa.cpu-iterative-input+json",
                size_bytes=5,
                checksum=checksum,
                blob_key=f"tenant/{tenant_id}/blob/{artifact_id}",
                state="COMMITTED",
                version=1,
            )
        )
        connection.execute(
            text(
                "UPDATE tenant_policies SET cpu_limit_millis = 100000000, "
                "memory_limit_bytes = 9223372036854775807, gpu_limit = 0 "
                "WHERE tenant_id = :tenant_id AND is_current"
            ),
            {"tenant_id": tenant_id},
        )
    return artifact_id


def _seed_managed_template_inputs(
    engine,
    tenant_id,
    *,
    template_id: str,
    input_media_type: str,
    model_media_type: str | None = None,
):
    input_artifact_id = new_uuid7()
    model_artifact_id = new_uuid7() if model_media_type is not None else None
    if template_id == "pytorch-cifar10-cnn":
        adapter_id = "pytorch.cifar10"
        parameter_schema = [
            {"name": "epochs", "type": "INTEGER", "required": True, "minimum": 1},
            {"name": "batch_size", "type": "INTEGER", "required": True, "minimum": 1},
            {"name": "learning_rate", "type": "NUMBER", "required": True, "minimum": 0},
            {"name": "seed", "type": "INTEGER", "required": True, "minimum": 0},
            {"name": "subset_size", "type": "INTEGER", "required": True, "minimum": 100},
        ]
    else:
        adapter_id = "batch.inference"
        parameter_schema = [
            {"name": "chunk_size", "type": "INTEGER", "required": True, "minimum": 1},
            {"name": "batch_size", "type": "INTEGER", "required": True, "minimum": 1},
            {
                "name": "output_format",
                "type": "ENUM",
                "required": True,
                "enum_values": ["JSONL", "PARQUET"],
            },
        ]
    image_digest = "sha256:" + "c" * 64
    with engine.begin() as connection:
        connection.execute(
            templates.insert().values(
                template_id=template_id,
                current_version=1,
                display_name=template_id,
                description="B08 media compatibility test template",
                enabled=True,
            )
        )
        connection.execute(
            template_versions.insert().values(
                template_id=template_id,
                version=1,
                parameter_schema=parameter_schema,
                resource_bounds={
                    "resources": {
                        "cpu_millis": 100_000_000,
                        "memory_bytes": 9_223_372_036_854_775_807,
                        "gpu_count": 0,
                    },
                    "runtime_limit_seconds": 300,
                    "checkpoint_interval_seconds": 60,
                },
                capability_requirements={
                    "architectures": ["linux/amd64"],
                    "adapter_id": adapter_id,
                    "adapter_version": "1.0.0",
                    "image_digest": image_digest,
                    "device": "CPU",
                    "framework": "PYTORCH",
                    "framework_version": "2.0.0",
                    "cuda_runtime_min": None,
                    "driver_min": None,
                    "compute_capability_min": None,
                },
                adapter_id=adapter_id,
                adapter_version="1.0.0",
                image_digest=image_digest,
                checkpointable=True,
                restart_safe=True,
            )
        )
        connection.execute(
            artifacts.insert().values(
                artifact_id=input_artifact_id,
                tenant_id=tenant_id,
                kind="DATASET",
                media_type=input_media_type,
                size_bytes=5,
                checksum=f"sha256:{sha256(b'dataset').hexdigest()}",
                blob_key=f"tenant/{tenant_id}/blob/{input_artifact_id}",
                state="COMMITTED",
                version=1,
            )
        )
        if model_artifact_id is not None:
            connection.execute(
                artifacts.insert().values(
                    artifact_id=model_artifact_id,
                    tenant_id=tenant_id,
                    kind="MODEL",
                    media_type=model_media_type,
                    size_bytes=5,
                    checksum=f"sha256:{sha256(b'model').hexdigest()}",
                    blob_key=f"tenant/{tenant_id}/blob/{model_artifact_id}",
                    state="COMMITTED",
                    version=1,
                )
            )
        connection.execute(
            text(
                "UPDATE tenant_policies SET cpu_limit_millis = 100000000, "
                "memory_limit_bytes = 9223372036854775807, gpu_limit = 0 "
                "WHERE tenant_id = :tenant_id AND is_current"
            ),
            {"tenant_id": tenant_id},
        )
    return input_artifact_id, model_artifact_id


def _bootstrap_member(client):
    bootstrap = client.post(
        "/v1/internal/admin-bootstrap",
        headers={
            "X-Nexa-Bootstrap-Secret": "b" * 32,
            "Idempotency-Key": "b08-bootstrap-admin",
        },
        json={
            "username": "b08-admin@example.test",
            "display_name": "B08 Admin",
            "password": "correct-horse-battery-staple",
        },
    )
    assert bootstrap.status_code == 201, bootstrap.text
    admin_id = bootstrap.json()["user_id"]
    login = client.post(
        "/v1/auth/login",
        headers={"Origin": "https://nexa.test"},
        json={
            "username": "b08-admin@example.test",
            "password": "correct-horse-battery-staple",
        },
    )
    assert login.status_code == 200, login.text
    assert "nexa_session=" in login.headers.get("set-cookie", "")
    assert "nexa_session" in client.cookies
    return admin_id, login.json()["csrf_token"]


def _submit_body(artifact_id):
    return {
        "spec": {
            "template_id": "cpu-iterative",
            "template_version": 1,
            "input_artifact_id": str(artifact_id),
            "resources": {"cpu_millis": 1000, "memory_bytes": 67_108_864, "gpu_count": 0},
            "priority": 1,
            "runtime_limit_seconds": 120,
            "checkpoint_interval_seconds": 30,
            "parameters": {"iterations": 100, "seed": 7, "modulus": 101},
        }
    }


def _managed_submit_body(template_id, input_artifact_id, model_artifact_id=None):
    spec = {
        "template_id": template_id,
        "template_version": 1,
        "input_artifact_id": str(input_artifact_id),
        "resources": {"cpu_millis": 1000, "memory_bytes": 67_108_864, "gpu_count": 0},
        "priority": 1,
        "runtime_limit_seconds": 120,
        "checkpoint_interval_seconds": 30,
    }
    if template_id == "pytorch-cifar10-cnn":
        spec["parameters"] = {
            "epochs": 1,
            "batch_size": 16,
            "learning_rate": 0.1,
            "seed": 7,
            "subset_size": 100,
        }
    else:
        spec["model_artifact_id"] = str(model_artifact_id)
        spec["parameters"] = {
            "chunk_size": 100,
            "batch_size": 16,
            "output_format": "JSONL",
        }
    return {"spec": spec}


@contextmanager
def _running_api_process(engine, tmp_path: Path, *, app_target: str = "nexa.api.main:app"):
    repository = Path(__file__).resolve().parents[2]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("NEXA_")}
    environment.update(
        {
            "PYTHONPATH": str(repository / "src"),
            "NEXA_ENVIRONMENT": "test",
            "NEXA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
            "NEXA_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            "NEXA_PUBLIC_ORIGIN": "https://nexa.test",
            "NEXA_SERVER_SECRET_FILE": str(tmp_path / "server-secret"),
            "NEXA_BOOTSTRAP_SECRET_FILE": str(tmp_path / "bootstrap-secret"),
            "NEXA_INSTALLATION_ID": "018f05c4-a922-7d0d-9f55-f9084a72d0f1",
            "NEXA_LOCAL_WORKER_ID": "018f05c4-a922-7d0d-9f55-f9084a72d0f2",
            "NEXA_LOCAL_WORKER_FINGERPRINT": "sha256:" + "a" * 64,
            "NEXA_MAINTENANCE_CIDRS": "127.0.0.0/8",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            app_target,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        cwd=repository,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                output = process.stdout.read() if process.stdout is not None else ""
                raise AssertionError(
                    f"API process exited with {return_code} before readiness: {output}"
                )
            try:
                if httpx.get(f"{base_url}/openapi.json", timeout=0.2).status_code == 200:
                    break
            except httpx.RequestError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("API process did not become ready within 10 seconds")
        yield base_url, process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_rate_bucket_reconciles_tightened_policy_before_consumption(migrated_postgres_engine):
    initial = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            rate_buckets.insert().values(
                scope_type="TENANT",
                scope_id="rate-policy-test",
                tokens=Decimal("0"),
                capacity=Decimal("20"),
                refill_rate=Decimal("5"),
                last_refill_at=initial,
                updated_at=initial,
            )
        )
        JobService._rate_lock(
            connection,
            scope_type="TENANT",
            scope_id="rate-policy-test",
            capacity=Decimal("2"),
            refill_rate=Decimal("1"),
            now=initial + timedelta(seconds=1),
        )
        row = (
            connection.execute(
                select(rate_buckets).where(
                    rate_buckets.c.scope_type == "TENANT",
                    rate_buckets.c.scope_id == "rate-policy-test",
                )
            )
            .mappings()
            .one()
        )
        assert row["capacity"] == Decimal("2")
        assert row["refill_rate"] == Decimal("1")
        assert row["tokens"] == Decimal("0")


def test_submit_is_durable_replayable_and_queryable(migrated_postgres_engine, tmp_path: Path):
    with _client(migrated_postgres_engine, tmp_path) as client:
        admin_id, csrf = _bootstrap_member(client)
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": csrf}
        tenant = client.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": "b08-create-tenant"},
            json={"slug": "b08-team", "display_name": "B08 Team"},
        )
        assert tenant.status_code == 201, tenant.text
        tenant_id = tenant.json()["tenant_id"]
        membership = client.post(
            f"/v1/admin/tenants/{tenant_id}/memberships",
            headers={**write, "Idempotency-Key": "b08-add-membership", "If-Match": '"v1"'},
            json={"user_id": admin_id, "role": "MEMBER"},
        )
        assert membership.status_code == 200, membership.text
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b08-admin@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": login.json()["csrf_token"]}
        artifact_id = _seed_cpu_inputs(migrated_postgres_engine, tenant_id)
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                admission_counters.insert().values(
                    scope_type="USER",
                    scope_id=tenant_id,
                    outstanding=0,
                    active_attempts=0,
                )
            )
        auth_session = client.get("/v1/auth/session")
        assert auth_session.status_code == 200, auth_session.text
        headers = {
            **write,
            "X-Nexa-Tenant-Id": tenant_id,
            "Idempotency-Key": "b08-submit-key-0001",
        }
        client.app.state.services.jobs.settings = replace(
            client.app.state.services.jobs.settings,
            idempotency_terminal_retention_days=45,
        )
        body = _submit_body(artifact_id)
        accepted = client.post("/v1/jobs", headers=headers, json=body)
        assert accepted.status_code == 202, accepted.text
        assert accepted.headers["location"].endswith(accepted.json()["job_id"])
        assert accepted.headers["etag"] == '"v1"'
        job = accepted.json()
        assert job["state"] == "QUEUED"
        assert job["desired_state"] == "RUNNING"
        assert job["version"] == 1
        assert job["job_fence"] == 0
        assert job["event_sequence"] == 1
        assert job["waiting_reason"] == "waiting_for_worker"

        replay = client.post("/v1/jobs", headers=headers, json=body)
        assert replay.status_code == 202
        assert replay.json() == job
        assert replay.headers["location"] == accepted.headers["location"]
        assert replay.headers["etag"] == accepted.headers["etag"]

        other_tenant = client.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": "b08-other-tenant"},
            json={"slug": "b08-other", "display_name": "B08 Other"},
        )
        assert other_tenant.status_code == 201, other_tenant.text
        other_tenant_id = other_tenant.json()["tenant_id"]
        other_membership = client.post(
            f"/v1/admin/tenants/{other_tenant_id}/memberships",
            headers={**write, "Idempotency-Key": "b08-other-member", "If-Match": '"v1"'},
            json={"user_id": admin_id, "role": "MEMBER"},
        )
        assert other_membership.status_code == 200, other_membership.text
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b08-admin@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200, login.text
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": login.json()["csrf_token"]}
        headers = {
            **write,
            "X-Nexa-Tenant-Id": tenant_id,
            "Idempotency-Key": "b08-submit-key-0001",
        }
        cross_tenant = client.get(
            f"/v1/jobs/{job['job_id']}",
            headers={"X-Nexa-Tenant-Id": other_tenant_id},
        )
        assert cross_tenant.status_code == 404

        read_token = client.post(
            "/v1/tokens",
            headers={**write, "Idempotency-Key": "b08-read-token-01"},
            json={
                "name": "b08-read",
                "scopes": ["jobs:read"],
                "expires_in_seconds": 300,
            },
        )
        write_token = client.post(
            "/v1/tokens",
            headers={**write, "Idempotency-Key": "b08-write-token-1"},
            json={
                "name": "b08-write",
                "scopes": ["jobs:write"],
                "expires_in_seconds": 300,
            },
        )
        assert read_token.status_code == write_token.status_code == 201
        with _client(migrated_postgres_engine, tmp_path) as cli_client:
            cli_client.cookies.clear()
            cli_read = {"Authorization": f"Bearer {read_token.json()['token']}"}
            cli_write = {"Authorization": f"Bearer {write_token.json()['token']}"}
            assert (
                cli_client.get(
                    f"/v1/jobs/{job['job_id']}",
                    headers={**cli_read, "X-Nexa-Tenant-Id": tenant_id},
                ).status_code
                == 200
            )
            denied_submit = cli_client.post(
                "/v1/jobs",
                headers={
                    **cli_read,
                    "X-Nexa-Tenant-Id": tenant_id,
                    "Idempotency-Key": "b08-read-submit-1",
                },
                json=body,
            )
            assert denied_submit.status_code == 403
            assert denied_submit.json()["code"] == "permission_denied"
            denied_read = cli_client.get(
                f"/v1/jobs/{job['job_id']}",
                headers={**cli_write, "X-Nexa-Tenant-Id": tenant_id},
            )
            assert denied_read.status_code == 403
            assert denied_read.json()["code"] == "permission_denied"

        missing_csrf = client.post(
            "/v1/jobs",
            headers={
                "X-Nexa-Tenant-Id": tenant_id,
                "Idempotency-Key": "b08-missing-csrf",
            },
            json=body,
        )
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["code"] == "invalid_csrf"

        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                text("UPDATE policy_versions SET global_outstanding_limit = 1 WHERE is_current")
            )
        queue_full = client.post(
            "/v1/jobs",
            headers={**headers, "Idempotency-Key": "b08-queue-full-key"},
            json=body,
        )
        assert queue_full.status_code == 503
        assert queue_full.json()["code"] == "queue_full"
        assert queue_full.headers["retry-after"] == "1"

        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE policy_versions SET operational_mode = 'ADMISSION_OFF' WHERE is_current"
                )
            )
        replay_while_closed = client.post("/v1/jobs", headers=headers, json=body)
        assert replay_while_closed.status_code == 202
        assert replay_while_closed.json() == job
        closed_new_submit = client.post(
            "/v1/jobs",
            headers={**headers, "Idempotency-Key": "b08-admission-closed"},
            json=body,
        )
        assert closed_new_submit.status_code == 409
        assert closed_new_submit.json()["code"] == "state_conflict"

        fetched = client.get(f"/v1/jobs/{job['job_id']}", headers={"X-Nexa-Tenant-Id": tenant_id})
        assert fetched.status_code == 200
        session = client.get(
            f"/v1/sessions/{job['session_id']}", headers={"X-Nexa-Tenant-Id": tenant_id}
        )
        assert session.status_code == 200
        assert session.json()["derived_state"] == "QUEUED"
        events_response = client.get(
            f"/v1/jobs/{job['job_id']}/events",
            headers={"X-Nexa-Tenant-Id": tenant_id},
        )
        assert events_response.status_code == 200
        assert [item["sequence"] for item in events_response.json()["items"]] == [1]
        events_after = client.get(
            f"/v1/jobs/{job['job_id']}/events?after_sequence=1",
            headers={"X-Nexa-Tenant-Id": tenant_id},
        )
        assert events_after.status_code == 200
        assert events_after.json()["items"] == []

        with migrated_postgres_engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(jobs)).scalar_one() == 1
            assert connection.execute(select(func.count()).select_from(job_specs)).scalar_one() == 1
            assert (
                connection.execute(select(func.count()).select_from(logical_sessions)).scalar_one()
                == 1
            )
            assert connection.execute(select(func.count()).select_from(events)).scalar_one() == 1
            assert connection.execute(select(func.count()).select_from(attempts)).scalar_one() == 0
            assert (
                connection.execute(select(func.count()).select_from(allocations)).scalar_one() == 0
            )
            assert (
                connection.execute(select(func.count()).select_from(attempt_leases)).scalar_one()
                == 0
            )
            assert connection.execute(select(jobs.c.ready_sequence)).scalar_one() == 1
            references = connection.execute(select(artifact_references)).mappings().all()
            assert {
                (str(row["artifact_id"]), row["owner_type"], str(row["owner_id"]), row["purpose"])
                for row in references
            } == {(str(artifact_id), "JOB_SPEC", job["job_id"], "INPUT")}
            counter_rows = connection.execute(select(admission_counters)).mappings().all()
            counter_values = {
                (row["scope_type"], row["scope_id"]): int(row["outstanding"])
                for row in counter_rows
            }
            assert counter_values == {
                ("GLOBAL", "global"): 1,
                ("TENANT", tenant_id): 1,
                ("USER", tenant_id): 0,
                ("USER", f"{tenant_id}:{job['user_id']}"): 1,
            }
            assert (
                connection.execute(select(func.count()).select_from(rate_buckets)).scalar_one() == 2
            )
            rate_rows = connection.execute(select(rate_buckets)).mappings().all()
            assert {(row["scope_type"], row["scope_id"]) for row in rate_rows} == {
                ("TENANT", tenant_id),
                ("USER", f"{tenant_id}:{job['user_id']}"),
            }
            assert {
                row.version for row in connection.execute(select(rate_buckets.c.version)).all()
            } == {2}
            submit_idempotency = (
                connection.execute(
                    select(idempotency_records).where(
                        idempotency_records.c.operation_id == "submitJob",
                        idempotency_records.c.idempotency_key == "b08-submit-key-0001",
                    )
                )
                .mappings()
                .one()
            )
            assert submit_idempotency["state"] == "COMPLETED"
            assert str(submit_idempotency["resource_id"]) == job["job_id"]
            assert submit_idempotency["expires_at"] >= datetime.now(UTC) + timedelta(days=44)
            assert submit_idempotency["expires_at"] < datetime.now(UTC) + timedelta(days=46)
            audit = (
                connection.execute(
                    select(audit_records).where(
                        audit_records.c.action == "job.submit",
                        audit_records.c.target_id == job["job_id"],
                    )
                )
                .mappings()
                .one()
            )
            assert audit["after_version"] == 1

        with pytest.raises(ApplicationError) as gc_rejected:
            client.app.state.services.artifact.issue_gc_token(
                blob_key=f"tenant/{tenant_id}/blob/{artifact_id}",
                checksum=f"sha256:{sha256(b'input').hexdigest()}",
                size_bytes=5,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        assert gc_rejected.value.code == "state_conflict"

    with _client(migrated_postgres_engine, tmp_path) as restarted:
        login = restarted.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b08-admin@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200
        replay = restarted.post(
            "/v1/jobs",
            headers={
                "Origin": "https://nexa.test",
                "X-CSRF-Token": login.json()["csrf_token"],
                "X-Nexa-Tenant-Id": tenant_id,
                "Idempotency-Key": "b08-submit-key-0001",
            },
            json=body,
        )
        assert replay.status_code == 202
        assert replay.json()["job_id"] == job["job_id"]
        listed = restarted.get("/v1/jobs", headers={"X-Nexa-Tenant-Id": tenant_id})
        assert listed.status_code == 200
        assert [item["job_id"] for item in listed.json()["items"]] == [job["job_id"]]


def test_same_key_different_payload_is_conflict_without_second_job(
    migrated_postgres_engine, tmp_path: Path
):
    with _client(migrated_postgres_engine, tmp_path) as client:
        admin_id, csrf = _bootstrap_member(client)
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": csrf}
        tenant = client.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": "b08-conflict-tenant"},
            json={"slug": "b08-conflict", "display_name": "B08 Conflict"},
        )
        tenant_id = tenant.json()["tenant_id"]
        client.post(
            f"/v1/admin/tenants/{tenant_id}/memberships",
            headers={**write, "Idempotency-Key": "b08-conflict-member", "If-Match": '"v1"'},
            json={"user_id": admin_id, "role": "MEMBER"},
        )
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b08-admin@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": login.json()["csrf_token"]}
        artifact_id = _seed_cpu_inputs(migrated_postgres_engine, tenant_id)
        auth_session = client.get("/v1/auth/session")
        assert auth_session.status_code == 200, auth_session.text
        headers = {
            **write,
            "X-Nexa-Tenant-Id": tenant_id,
            "Idempotency-Key": "b08-conflict-submit",
        }
        first = client.post("/v1/jobs", headers=headers, json=_submit_body(artifact_id))
        assert first.status_code == 202
        changed = _submit_body(artifact_id)
        changed["spec"]["parameters"]["iterations"] = 101
        conflict = client.post("/v1/jobs", headers=headers, json=changed)
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "idempotency_conflict"
        with migrated_postgres_engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(jobs)).scalar_one() == 1
            assert (
                connection.execute(
                    select(func.count()).select_from(idempotency_records)
                ).scalar_one()
                >= 1
            )


def test_concurrent_same_key_commits_one_job_and_replays_one_snapshot(
    migrated_postgres_engine, tmp_path: Path
):
    with _client(migrated_postgres_engine, tmp_path) as client_a:
        admin_id, csrf = _bootstrap_member(client_a)
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": csrf}
        tenant = client_a.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": "b08-race-tenant-1"},
            json={"slug": "b08-race", "display_name": "B08 Race"},
        )
        tenant_id = tenant.json()["tenant_id"]
        member = client_a.post(
            f"/v1/admin/tenants/{tenant_id}/memberships",
            headers={**write, "Idempotency-Key": "b08-race-member-1", "If-Match": '"v1"'},
            json={"user_id": admin_id, "role": "MEMBER"},
        )
        assert member.status_code == 200
        login_a = client_a.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b08-admin@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login_a.status_code == 200
        csrf_a = login_a.json()["csrf_token"]
        artifact_id = _seed_cpu_inputs(migrated_postgres_engine, tenant_id)
        body = _submit_body(artifact_id)
        barrier = Barrier(2)

        with _client(migrated_postgres_engine, tmp_path) as client_b:
            login_b = client_b.post(
                "/v1/auth/login",
                headers={"Origin": "https://nexa.test"},
                json={
                    "username": "b08-admin@example.test",
                    "password": "correct-horse-battery-staple",
                },
            )
            assert login_b.status_code == 200

            def send(client, csrf_token):
                barrier.wait(timeout=5)
                return client.post(
                    "/v1/jobs",
                    headers={
                        "Origin": "https://nexa.test",
                        "X-CSRF-Token": csrf_token,
                        "X-Nexa-Tenant-Id": tenant_id,
                        "Idempotency-Key": "b08-race-submit-key",
                    },
                    json=body,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(send, client_a, csrf_a),
                    pool.submit(send, client_b, login_b.json()["csrf_token"]),
                ]
                responses = [future.result(timeout=10) for future in futures]
            assert [response.status_code for response in responses] == [202, 202]
            assert responses[0].json() == responses[1].json()

        with migrated_postgres_engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(jobs)).scalar_one() == 1
            assert connection.execute(select(func.count()).select_from(events)).scalar_one() == 1
            assert connection.execute(select(func.count()).select_from(job_specs)).scalar_one() == 1


@pytest.mark.parametrize(
    ("race_kind", "rejected_status", "rejected_code"),
    [
        ("queue", 503, "queue_full"),
        ("rate", 429, "rate_limited"),
    ],
)
def test_concurrent_different_keys_respect_queue_and_rate_limits(
    migrated_postgres_engine,
    tmp_path: Path,
    race_kind: str,
    rejected_status: int,
    rejected_code: str,
):
    with _client(migrated_postgres_engine, tmp_path) as client_a:
        admin_id, csrf = _bootstrap_member(client_a)
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": csrf}
        tenant = client_a.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": f"b08-{race_kind}-tenant-01"},
            json={"slug": f"b08-{race_kind}", "display_name": f"B08 {race_kind}"},
        )
        assert tenant.status_code == 201, tenant.text
        tenant_id = tenant.json()["tenant_id"]
        membership = client_a.post(
            f"/v1/admin/tenants/{tenant_id}/memberships",
            headers={
                **write,
                "Idempotency-Key": f"b08-{race_kind}-member-01",
                "If-Match": '"v1"',
            },
            json={"user_id": admin_id, "role": "MEMBER"},
        )
        assert membership.status_code == 200, membership.text
        login_a = client_a.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b08-admin@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login_a.status_code == 200, login_a.text
        artifact_id = _seed_cpu_inputs(migrated_postgres_engine, tenant_id)
        with migrated_postgres_engine.begin() as connection:
            if race_kind == "queue":
                connection.execute(
                    text("UPDATE policy_versions SET global_outstanding_limit = 1 WHERE is_current")
                )
            else:
                connection.execute(
                    tenant_policies.update()
                    .where(
                        tenant_policies.c.tenant_id == tenant_id,
                        tenant_policies.c.is_current.is_(True),
                    )
                    .values(
                        tenant_rate_burst=Decimal("1"),
                        tenant_rate_per_second=Decimal("0.000001"),
                        user_rate_burst=Decimal("1"),
                        user_rate_per_second=Decimal("0.000001"),
                    )
                )
        barrier = Barrier(2)
        body = _submit_body(artifact_id)

        with _client(migrated_postgres_engine, tmp_path) as client_b:
            login_b = client_b.post(
                "/v1/auth/login",
                headers={"Origin": "https://nexa.test"},
                json={
                    "username": "b08-admin@example.test",
                    "password": "correct-horse-battery-staple",
                },
            )
            assert login_b.status_code == 200, login_b.text

            def send(client, csrf_token, suffix):
                barrier.wait(timeout=5)
                return client.post(
                    "/v1/jobs",
                    headers={
                        "Origin": "https://nexa.test",
                        "X-CSRF-Token": csrf_token,
                        "X-Nexa-Tenant-Id": tenant_id,
                        "Idempotency-Key": f"b08-{race_kind}-submit-{suffix}",
                    },
                    json=body,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(send, client_a, login_a.json()["csrf_token"], "a"),
                    pool.submit(send, client_b, login_b.json()["csrf_token"], "b"),
                ]
                responses = [future.result(timeout=10) for future in futures]

        assert sorted(response.status_code for response in responses) == [202, rejected_status]
        rejected = next(response for response in responses if response.status_code != 202)
        assert rejected.json()["code"] == rejected_code
        if race_kind == "queue":
            assert rejected.headers["retry-after"] == "1"
        else:
            assert int(rejected.headers["retry-after"]) >= 1
        with migrated_postgres_engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(jobs)).scalar_one() == 1
            assert connection.execute(select(func.count()).select_from(job_specs)).scalar_one() == 1
            assert connection.execute(select(func.count()).select_from(events)).scalar_one() == 1
            counters = {
                (row.scope_type, row.scope_id): row.outstanding
                for row in connection.execute(
                    select(
                        admission_counters.c.scope_type,
                        admission_counters.c.scope_id,
                        admission_counters.c.outstanding,
                    )
                )
            }
            assert counters[("GLOBAL", "global")] == 1
            assert counters[("TENANT", tenant_id)] == 1
            assert counters[("USER", f"{tenant_id}:{admin_id}")] == 1


def test_job_list_keyset_is_stable_across_insert_and_rejects_cursor_reuse(
    migrated_postgres_engine, tmp_path: Path
):
    with _client(migrated_postgres_engine, tmp_path) as client:
        admin_id, csrf = _bootstrap_member(client)
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": csrf}
        tenant = client.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": "b08-cursor-tenant-01"},
            json={"slug": "b08-cursor", "display_name": "B08 Cursor"},
        )
        assert tenant.status_code == 201, tenant.text
        tenant_id = tenant.json()["tenant_id"]
        membership = client.post(
            f"/v1/admin/tenants/{tenant_id}/memberships",
            headers={**write, "Idempotency-Key": "b08-cursor-member-01", "If-Match": '"v1"'},
            json={"user_id": admin_id, "role": "MEMBER"},
        )
        assert membership.status_code == 200, membership.text
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b08-admin@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200, login.text
        submit_headers = {
            "Origin": "https://nexa.test",
            "X-CSRF-Token": login.json()["csrf_token"],
            "X-Nexa-Tenant-Id": tenant_id,
        }
        artifact_id = _seed_cpu_inputs(migrated_postgres_engine, tenant_id)
        accepted_ids = []
        for suffix in ("a", "b", "c"):
            response = client.post(
                "/v1/jobs",
                headers={
                    **submit_headers,
                    "Idempotency-Key": f"b08-cursor-submit-{suffix}",
                },
                json=_submit_body(artifact_id),
            )
            assert response.status_code == 202, response.text
            accepted_ids.append(response.json()["job_id"])

        shared_timestamp = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                jobs.update().values(created_at=shared_timestamp, updated_at=shared_timestamp)
            )
        first = client.get(
            "/v1/jobs?page_size=2",
            headers={"X-Nexa-Tenant-Id": tenant_id},
        )
        assert first.status_code == 200, first.text
        expected = sorted(accepted_ids, reverse=True)
        assert [item["job_id"] for item in first.json()["items"]] == expected[:2]
        cursor = first.json()["page"]["next_cursor"]
        assert cursor is not None

        inserted = client.post(
            "/v1/jobs",
            headers={**submit_headers, "Idempotency-Key": "b08-cursor-submit-new"},
            json=_submit_body(artifact_id),
        )
        assert inserted.status_code == 202, inserted.text
        second = client.get(
            "/v1/jobs?page_size=2",
            headers={"X-Nexa-Tenant-Id": tenant_id},
            params={"cursor": cursor},
        )
        assert second.status_code == 200, second.text
        assert [item["job_id"] for item in second.json()["items"]] == expected[2:]

        replacement = "A" if cursor[-1] != "A" else "B"
        tampered = client.get(
            "/v1/jobs",
            headers={"X-Nexa-Tenant-Id": tenant_id},
            params={"cursor": cursor[:-1] + replacement},
        )
        rebound = client.get(
            "/v1/jobs",
            headers={"X-Nexa-Tenant-Id": tenant_id},
            params={"cursor": cursor, "state": "QUEUED"},
        )
        assert tampered.status_code == 400
        assert tampered.json()["code"] == "invalid_cursor"
        assert rebound.status_code == 400
        assert rebound.json()["code"] == "invalid_cursor"


def test_submit_rejects_incompatible_cpu_input_without_partial_state(
    migrated_postgres_engine, tmp_path: Path
):
    with _client(migrated_postgres_engine, tmp_path) as client:
        admin_id, csrf = _bootstrap_member(client)
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": csrf}
        tenant = client.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": "b08-artifact-tenant"},
            json={"slug": "b08-artifact", "display_name": "B08 Artifact"},
        )
        tenant_id = tenant.json()["tenant_id"]
        membership = client.post(
            f"/v1/admin/tenants/{tenant_id}/memberships",
            headers={**write, "Idempotency-Key": "b08-artifact-member", "If-Match": '"v1"'},
            json={"user_id": admin_id, "role": "MEMBER"},
        )
        assert membership.status_code == 200
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b08-admin@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200
        artifact_id = _seed_cpu_inputs(migrated_postgres_engine, tenant_id)
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                artifacts.update()
                .where(artifacts.c.artifact_id == artifact_id)
                .values(kind="MODEL", media_type="application/json")
            )

        response = client.post(
            "/v1/jobs",
            headers={
                "Origin": "https://nexa.test",
                "X-CSRF-Token": login.json()["csrf_token"],
                "X-Nexa-Tenant-Id": tenant_id,
                "Idempotency-Key": "b08-artifact-submit",
            },
            json=_submit_body(artifact_id),
        )

        assert response.status_code == 422
        assert response.json()["code"] == "infeasible_request"
        with migrated_postgres_engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(jobs)).scalar_one() == 0
            assert connection.execute(select(func.count()).select_from(job_specs)).scalar_one() == 0
            assert (
                connection.execute(select(func.count()).select_from(logical_sessions)).scalar_one()
                == 0
            )
            assert connection.execute(select(func.count()).select_from(events)).scalar_one() == 0
            assert (
                connection.execute(
                    select(func.coalesce(func.sum(admission_counters.c.outstanding), 0))
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(select(func.count()).select_from(rate_buckets)).scalar_one() == 0
            )


@pytest.mark.parametrize(
    "resource_bounds",
    [
        None,
        [],
        {},
        {
            "resources": {
                "cpu_millis": 100_000_000,
                "memory_bytes": 9_223_372_036_854_775_807,
            },
            "runtime_limit_seconds": 300,
            "checkpoint_interval_seconds": 60,
        },
        {
            "resources": {
                "cpu_millis": "unbounded",
                "memory_bytes": 9_223_372_036_854_775_807,
                "gpu_count": 0,
            },
            "runtime_limit_seconds": 300,
            "checkpoint_interval_seconds": 60,
        },
        {
            "resources": {
                "cpu_millis": 100_000_000,
                "memory_bytes": 9_223_372_036_854_775_807,
                "gpu_count": 0,
            },
            "runtime_limit_seconds": {"maximum": "unbounded"},
            "checkpoint_interval_seconds": 60,
        },
        {
            "resources": {
                "cpu_millis": 100_000_000,
                "memory_bytes": 9_223_372_036_854_775_807,
                "gpu_count": 0,
            },
            "runtime_limit_seconds": 300,
        },
    ],
    ids=[
        "json-null",
        "list",
        "empty-object",
        "missing-resource",
        "invalid-resource-type",
        "invalid-runtime-type",
        "missing-checkpoint-bound",
    ],
)
def test_submit_rejects_malformed_template_resource_bounds_without_partial_state(
    migrated_postgres_engine, tmp_path: Path, resource_bounds
):
    with _client(migrated_postgres_engine, tmp_path) as client:
        admin_id, csrf = _bootstrap_member(client)
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": csrf}
        tenant = client.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": "b08-bounds-tenant"},
            json={"slug": "b08-bounds", "display_name": "B08 Bounds"},
        )
        tenant_id = tenant.json()["tenant_id"]
        membership = client.post(
            f"/v1/admin/tenants/{tenant_id}/memberships",
            headers={**write, "Idempotency-Key": "b08-bounds-member", "If-Match": '"v1"'},
            json={"user_id": admin_id, "role": "MEMBER"},
        )
        assert membership.status_code == 200
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b08-admin@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200
        artifact_id = _seed_cpu_inputs(migrated_postgres_engine, tenant_id)
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                template_versions.update()
                .where(
                    template_versions.c.template_id == "cpu-iterative",
                    template_versions.c.version == 1,
                )
                .values(resource_bounds=resource_bounds)
            )

        response = client.post(
            "/v1/jobs",
            headers={
                "Origin": "https://nexa.test",
                "X-CSRF-Token": login.json()["csrf_token"],
                "X-Nexa-Tenant-Id": tenant_id,
                "Idempotency-Key": "b08-bounds-submit",
            },
            json=_submit_body(artifact_id),
        )

        assert response.status_code == 422
        assert response.json()["code"] == "infeasible_request"
        with migrated_postgres_engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(jobs)).scalar_one() == 0
            assert connection.execute(select(func.count()).select_from(job_specs)).scalar_one() == 0
            assert (
                connection.execute(select(func.count()).select_from(logical_sessions)).scalar_one()
                == 0
            )
            assert connection.execute(select(func.count()).select_from(events)).scalar_one() == 0
            assert (
                connection.execute(
                    select(func.coalesce(func.sum(admission_counters.c.outstanding), 0))
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(select(func.count()).select_from(rate_buckets)).scalar_one() == 0
            )


@pytest.mark.parametrize(
    ("template_id", "input_media_type", "model_media_type"),
    [
        ("pytorch-cifar10-cnn", "application/json", None),
        ("batch-inference", "application/json", "application/octet-stream"),
        ("batch-inference", "application/vnd.apache.arrow.file", "application/json"),
    ],
    ids=["training-dataset", "inference-dataset", "inference-model"],
)
def test_submit_rejects_template_incompatible_media_types_without_partial_state(
    migrated_postgres_engine,
    tmp_path: Path,
    template_id: str,
    input_media_type: str,
    model_media_type: str | None,
):
    with _client(migrated_postgres_engine, tmp_path) as client:
        admin_id, csrf = _bootstrap_member(client)
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": csrf}
        tenant = client.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": "b08-media-tenant"},
            json={"slug": "b08-media", "display_name": "B08 Media"},
        )
        tenant_id = tenant.json()["tenant_id"]
        membership = client.post(
            f"/v1/admin/tenants/{tenant_id}/memberships",
            headers={**write, "Idempotency-Key": "b08-media-member", "If-Match": '"v1"'},
            json={"user_id": admin_id, "role": "MEMBER"},
        )
        assert membership.status_code == 200
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b08-admin@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200
        input_artifact_id, model_artifact_id = _seed_managed_template_inputs(
            migrated_postgres_engine,
            tenant_id,
            template_id=template_id,
            input_media_type=input_media_type,
            model_media_type=model_media_type,
        )

        response = client.post(
            "/v1/jobs",
            headers={
                "Origin": "https://nexa.test",
                "X-CSRF-Token": login.json()["csrf_token"],
                "X-Nexa-Tenant-Id": tenant_id,
                "Idempotency-Key": "b08-media-submit",
            },
            json=_managed_submit_body(template_id, input_artifact_id, model_artifact_id),
        )

        assert response.status_code == 422
        assert response.json()["code"] == "infeasible_request"
        with migrated_postgres_engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(jobs)).scalar_one() == 0
            assert connection.execute(select(func.count()).select_from(job_specs)).scalar_one() == 0
            assert (
                connection.execute(select(func.count()).select_from(logical_sessions)).scalar_one()
                == 0
            )
            assert connection.execute(select(func.count()).select_from(events)).scalar_one() == 0
            assert (
                connection.execute(
                    select(func.count()).select_from(artifact_references)
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    select(func.coalesce(func.sum(admission_counters.c.outstanding), 0))
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(select(func.count()).select_from(rate_buckets)).scalar_one() == 0
            )
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(audit_records)
                    .where(audit_records.c.action == "job.submit")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(idempotency_records)
                    .where(idempotency_records.c.operation_id == "submitJob")
                ).scalar_one()
                == 0
            )


def test_database_fault_before_initial_event_rolls_back_every_submit_effect(
    migrated_postgres_engine, tmp_path: Path
):
    with _client(migrated_postgres_engine, tmp_path) as client:
        admin_id, csrf = _bootstrap_member(client)
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": csrf}
        tenant = client.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": "b08-fault-tenant-01"},
            json={"slug": "b08-fault", "display_name": "B08 Fault"},
        )
        assert tenant.status_code == 201, tenant.text
        tenant_id = tenant.json()["tenant_id"]
        membership = client.post(
            f"/v1/admin/tenants/{tenant_id}/memberships",
            headers={**write, "Idempotency-Key": "b08-fault-member-01", "If-Match": '"v1"'},
            json={"user_id": admin_id, "role": "MEMBER"},
        )
        assert membership.status_code == 200, membership.text
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b08-admin@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200, login.text
        artifact_id = _seed_cpu_inputs(migrated_postgres_engine, tenant_id)
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE FUNCTION nexa_b08_fail_event() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$ BEGIN RAISE EXCEPTION 'b08 injected event failure'; END $$"
                )
            )
            connection.execute(
                text(
                    "CREATE TRIGGER tr_b08_fail_event BEFORE INSERT ON events "
                    "FOR EACH ROW EXECUTE FUNCTION nexa_b08_fail_event()"
                )
            )

        response = client.post(
            "/v1/jobs",
            headers={
                "Origin": "https://nexa.test",
                "X-CSRF-Token": login.json()["csrf_token"],
                "X-Nexa-Tenant-Id": tenant_id,
                "Idempotency-Key": "b08-fault-submit-01",
            },
            json=_submit_body(artifact_id),
        )

        assert response.status_code == 503
        assert response.json()["code"] == "dependency_unavailable"
        assert "b08 injected" not in response.text
        with migrated_postgres_engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(jobs)).scalar_one() == 0
            assert connection.execute(select(func.count()).select_from(job_specs)).scalar_one() == 0
            assert (
                connection.execute(select(func.count()).select_from(logical_sessions)).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    select(func.count()).select_from(artifact_references)
                ).scalar_one()
                == 0
            )
            assert connection.execute(select(func.count()).select_from(events)).scalar_one() == 0
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(audit_records)
                    .where(audit_records.c.action == "job.submit")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(idempotency_records)
                    .where(idempotency_records.c.operation_id == "submitJob")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    select(func.coalesce(func.sum(admission_counters.c.outstanding), 0))
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(select(func.count()).select_from(rate_buckets)).scalar_one() == 0
            )


def test_submit_replays_after_post_commit_api_crash_before_response(
    migrated_postgres_engine, tmp_path: Path
):
    with _client(migrated_postgres_engine, tmp_path) as client:
        admin_id, csrf = _bootstrap_member(client)
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": csrf}
        tenant = client.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": "b08-process-tenant"},
            json={"slug": "b08-process", "display_name": "B08 Process Restart"},
        )
        assert tenant.status_code == 201, tenant.text
        tenant_id = tenant.json()["tenant_id"]
        membership = client.post(
            f"/v1/admin/tenants/{tenant_id}/memberships",
            headers={**write, "Idempotency-Key": "b08-process-member", "If-Match": '"v1"'},
            json={"user_id": admin_id, "role": "MEMBER"},
        )
        assert membership.status_code == 200, membership.text
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b08-admin@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200, login.text
        csrf = login.json()["csrf_token"]
        session_cookie = next(
            cookie.value for cookie in client.cookies.jar if cookie.name == "nexa_session"
        )
        artifact_id = _seed_cpu_inputs(migrated_postgres_engine, tenant_id)

    headers = {
        "Cookie": f"nexa_session={session_cookie}",
        "Host": "nexa.test",
        "Origin": "https://nexa.test",
        "X-CSRF-Token": csrf,
        "X-Nexa-Tenant-Id": tenant_id,
        "Idempotency-Key": "b08-process-submit",
    }
    body = _submit_body(artifact_id)
    with (
        _running_api_process(
            migrated_postgres_engine,
            tmp_path,
            app_target="tests.integration.response_loss_app:app",
        ) as (base_url, process),
        httpx.Client(base_url=base_url, timeout=5) as remote,
    ):
        with pytest.raises(httpx.TransportError):
            remote.post("/v1/jobs", headers=headers, json=body)
        assert process.wait(timeout=5) == 91

    with migrated_postgres_engine.connect() as connection:
        stored = (
            connection.execute(
                select(idempotency_records).where(
                    idempotency_records.c.operation_id == "submitJob",
                    idempotency_records.c.idempotency_key == "b08-process-submit",
                )
            )
            .mappings()
            .one()
        )
        assert stored["state"] == "COMPLETED"
        assert stored["response_status"] == 202
        accepted_body = dict(stored["response_body"])
        accepted_headers = dict(stored["response_headers"])
        assert str(stored["resource_id"]) == accepted_body["job_id"]
        assert connection.execute(select(func.count()).select_from(jobs)).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(job_specs)).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(events)).scalar_one() == 1

    with (
        _running_api_process(migrated_postgres_engine, tmp_path) as (base_url, _process),
        httpx.Client(base_url=base_url, timeout=5) as restarted,
    ):
        fetched = restarted.get(
            f"/v1/jobs/{accepted_body['job_id']}",
            headers={
                "Cookie": f"nexa_session={session_cookie}",
                "X-Nexa-Tenant-Id": tenant_id,
            },
        )
        replay = restarted.post("/v1/jobs", headers=headers, json=body)

    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["job_id"] == accepted_body["job_id"]
    assert replay.status_code == 202, replay.text
    assert replay.json() == accepted_body
    assert replay.headers["location"] == accepted_headers["Location"]
    assert replay.headers["etag"] == accepted_headers["ETag"]
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(jobs)).scalar_one() == 1
