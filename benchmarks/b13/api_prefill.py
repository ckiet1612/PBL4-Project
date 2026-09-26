"""Prefill a guarded PostgreSQL queue through the production HTTP API.

The CPU template is an explicit DB-only benchmark fixture because its admin
endpoint is not implemented yet. Tenant, policy, artifact and job writes use API.
"""

import argparse
import hashlib
import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select

from benchmarks.b13.queue_microbench import _guard_url, _provenance
from nexa.api.app import create_app
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.schema_guard import (
    SchemaCompatibility,
    inspect_schema_compatibility,
)
from tests.integration.identity_support import make_identity_service
from tests.integration.test_jobs_b08 import (
    _running_api_process,
    _seed_cpu_inputs,
    _submit_body,
)

_INPUT = b'{"iterations":100,"seed":7,"modulus":101}'


def _accepted(response, expected):
    if response.status_code != expected:
        raise RuntimeError(
            f"{response.request.method} {response.request.url.path}: "
            f"HTTP {response.status_code} {response.text[:400]}"
        )
    return response.json()


def _setup(
    engine,
    state_dir: Path,
    tenants: int,
    jobs_per_tenant: int,
    login_rate_per_minute: int,
    workers: int,
):
    tenant_artifacts = []
    identity = make_identity_service(engine, state_dir)
    settings = replace(identity.settings, login_rate_per_minute=login_rate_per_minute)
    with TestClient(
        create_app(settings, engine=engine),
        base_url="https://nexa.test",
        client=("127.0.0.1", 50000),
    ) as client:
        bootstrap = client.post(
            "/v1/internal/admin-bootstrap",
            headers={
                "X-Nexa-Bootstrap-Secret": "b" * 32,
                "Idempotency-Key": "b13-api-prefill-bootstrap",
            },
            json={
                "username": "b13-admin@example.test",
                "display_name": "B13 Benchmark Admin",
                "password": "correct-horse-battery-staple",
            },
        )
        _accepted(bootstrap, 201)
        login = _accepted(
            client.post(
                "/v1/auth/login",
                headers={"Origin": "https://nexa.test"},
                json={
                    "username": "b13-admin@example.test",
                    "password": "correct-horse-battery-staple",
                },
            ),
            200,
        )
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": login["csrf_token"]}
        submitters = []
        for index in range(workers):
            user = _accepted(
                client.post(
                    "/v1/admin/users",
                    headers={**write, "Idempotency-Key": f"b13-prefill-user-{index:04d}"},
                    json={
                        "username": f"b13-submit-{index:04d}@example.test",
                        "display_name": f"B13 Submit {index:04d}",
                        "password": "correct-horse-battery-staple",
                        "system_roles": [],
                    },
                ),
                201,
            )
            submitters.append(user["user_id"])
        admin_write_token = _accepted(
            client.post(
                "/v1/tokens",
                headers={**write, "Idempotency-Key": f"b13-admin-cli-{time.time_ns()}"},
                json={
                    "name": "B13 prefill admin",
                    "scopes": ["admin:read", "admin:write"],
                    "expires_in_seconds": 86400,
                },
            ),
            201,
        )["token"]
        write = {"Authorization": f"Bearer {admin_write_token}"}
        client.cookies.clear()
        global_policy = _accepted(client.get("/v1/admin/policy", headers=write), 200)
        benchmark_cap = max(100_000, tenants * jobs_per_tenant + 1_000)
        if global_policy["global_outstanding_limit"] < benchmark_cap:
            global_policy = _accepted(
                client.patch(
                    "/v1/admin/policy",
                    headers={
                        **write,
                        "Idempotency-Key": f"b13-prefill-global-{benchmark_cap}",
                        "If-Match": f'"v{global_policy["version"]}"',
                    },
                    json={"global_outstanding_limit": benchmark_cap},
                ),
                200,
            )
        template_exists = False
        with engine.connect() as connection:
            template_exists = (
                connection.execute(
                    select(s.templates.c.template_id).where(
                        s.templates.c.template_id == "cpu-iterative"
                    )
                ).first()
                is not None
            )

        for index in range(tenants):
            tenant = _accepted(
                client.post(
                    "/v1/admin/tenants",
                    headers={**write, "Idempotency-Key": f"b13-prefill-tenant-{index:04d}"},
                    json={
                        "slug": f"b13-prefill-{index:04d}",
                        "display_name": f"B13 Prefill {index:04d}",
                    },
                ),
                201,
            )
            tenant_id = tenant["tenant_id"]
            if not template_exists:
                _seed_cpu_inputs(engine, tenant_id)
                template_exists = True
            membership_set = _accepted(
                client.get(f"/v1/admin/tenants/{tenant_id}/memberships", headers=write),
                200,
            )
            _accepted(
                client.post(
                    f"/v1/admin/tenants/{tenant_id}/memberships",
                    headers={
                        **write,
                        "Idempotency-Key": f"b13-prefill-submitter-{index:04d}",
                        "If-Match": f'"v{membership_set["membership_set_version"]}"',
                    },
                    json={"user_id": submitters[index % workers], "role": "MEMBER"},
                ),
                200,
            )
            _accepted(
                client.patch(
                    f"/v1/admin/tenants/{tenant_id}/policy",
                    headers={
                        **write,
                        "Idempotency-Key": f"b13-prefill-policy-{index:04d}",
                        "If-Match": '"v1"',
                    },
                    json={
                        "outstanding_limit": max(2000, jobs_per_tenant),
                        "user_outstanding_limit": max(2000, jobs_per_tenant),
                        "resource_limit": {
                            "cpu_millis": 100000,
                            "memory_bytes": 16 * 1024**3,
                            "gpu_count": 0,
                        },
                        "submit_rate_per_second": "1000",
                        "submit_burst": max(1000, jobs_per_tenant),
                        "user_submit_rate_per_second": "1000",
                        "user_submit_burst": max(1000, jobs_per_tenant),
                    },
                ),
                200,
            )
            submitter_login = _accepted(
                client.post(
                    "/v1/auth/login",
                    headers={"Origin": "https://nexa.test"},
                    json={
                        "username": f"b13-submit-{index % workers:04d}@example.test",
                        "password": "correct-horse-battery-staple",
                    },
                ),
                200,
            )
            artifact_write = {
                "Origin": "https://nexa.test",
                "X-CSRF-Token": submitter_login["csrf_token"],
            }
            digest = "sha256:" + hashlib.sha256(_INPUT).hexdigest()
            artifact = _accepted(
                client.post(
                    "/v1/artifacts",
                    headers={
                        **artifact_write,
                        "X-Nexa-Tenant-Id": tenant_id,
                        "Idempotency-Key": f"b13-prefill-artifact-{index:04d}",
                        "X-Artifact-Checksum": digest,
                        "X-Artifact-Size": str(len(_INPUT)),
                        "X-Artifact-Kind": "INPUT",
                        "X-Artifact-Media-Type": "application/vnd.nexa.cpu-iterative-input+json",
                        "Content-Type": "application/octet-stream",
                    },
                    content=_INPUT,
                ),
                201,
            )
            client.cookies.clear()
            tenant_artifacts.append((tenant_id, artifact["artifact_id"]))

        worker_tokens = []
        for index in range(workers):
            submitter_login = _accepted(
                client.post(
                    "/v1/auth/login",
                    headers={"Origin": "https://nexa.test"},
                    json={
                        "username": f"b13-submit-{index:04d}@example.test",
                        "password": "correct-horse-battery-staple",
                    },
                ),
                200,
            )
            worker_tokens.append(
                _accepted(
                    client.post(
                        "/v1/tokens",
                        headers={
                            "Origin": "https://nexa.test",
                            "X-CSRF-Token": submitter_login["csrf_token"],
                            "Idempotency-Key": f"b13-prefill-cli-{time.time_ns()}",
                        },
                        json={
                            "name": f"B13 submit {index}",
                            "scopes": ["jobs:write", "jobs:read"],
                            "expires_in_seconds": 86400,
                        },
                    ),
                    201,
                )["token"]
            )
    return tenant_artifacts, worker_tokens, global_policy


def _recover_journal(engine, journal: Path):
    recorded = {}
    if journal.exists():
        for line in journal.read_text().splitlines():
            row = json.loads(line)
            recorded[row["job_id"]] = row
    with engine.connect() as connection:
        committed = connection.execute(
            select(
                s.idempotency_records.c.idempotency_key,
                s.idempotency_records.c.resource_id,
                s.jobs.c.tenant_id,
                s.logical_sessions.c.session_id,
            )
            .join(s.jobs, s.jobs.c.job_id == s.idempotency_records.c.resource_id)
            .join(s.logical_sessions, s.logical_sessions.c.job_id == s.jobs.c.job_id)
            .where(
                s.idempotency_records.c.operation_id == "submitJob",
                s.idempotency_records.c.idempotency_key.like("b13-job-%"),
                s.idempotency_records.c.state == "COMPLETED",
                s.idempotency_records.c.response_status == 202,
            )
        ).mappings()
        with journal.open("a") as output:
            for item in committed:
                job_id = str(item["resource_id"])
                if job_id in recorded:
                    row = recorded[job_id]
                    if (
                        row["idempotency_key"] != item["idempotency_key"]
                        or row["tenant_id"] != str(item["tenant_id"])
                        or row["session_id"] != str(item["session_id"])
                    ):
                        raise RuntimeError("Accepted-ID journal disagrees with committed rows")
                    continue
                row = {
                    "idempotency_key": item["idempotency_key"],
                    "tenant_id": str(item["tenant_id"]),
                    "job_id": job_id,
                    "session_id": str(item["session_id"]),
                    "latency_ms": None,
                    "status": 202,
                    "source": "durable_idempotency_recovery",
                }
                output.write(json.dumps(row) + "\n")
                recorded[job_id] = row
            output.flush()
    primary = {}
    for row in recorded.values():
        primary.setdefault(row["idempotency_key"], row)
    return primary, recorded


def _reconcile(engine, accepted_ids: list[UUID]):
    found = 0
    with engine.connect() as connection:
        for start in range(0, len(accepted_ids), 500):
            batch = accepted_ids[start : start + 500]
            rows = connection.execute(
                select(s.jobs.c.job_id, s.logical_sessions.c.session_id, s.job_specs.c.job_id)
                .join(s.logical_sessions, s.jobs.c.job_id == s.logical_sessions.c.job_id)
                .join(s.job_specs, s.jobs.c.job_id == s.job_specs.c.job_id)
                .where(s.jobs.c.job_id.in_(batch))
            ).all()
            found += len(rows)
        counters = {
            row["scope_type"]: row["outstanding"]
            for row in connection.execute(
                select(s.admission_counters).where(s.admission_counters.c.scope_type == "GLOBAL")
            ).mappings()
        }
        total = connection.execute(select(func.count()).select_from(s.jobs)).scalar_one()
    return {
        "accepted_ids": len(accepted_ids),
        "traceable_job_session_spec": found,
        "db_job_count": total,
        "global_outstanding": counters.get("GLOBAL"),
    }


def _submit(api, tenant_id, key, body, token):
    started = time.perf_counter()
    retries = 0
    while True:
        response = api.post(
            "/v1/jobs",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Nexa-Tenant-Id": tenant_id,
                "Idempotency-Key": key,
            },
            json=body,
        )
        if (
            response.status_code != 503
            or response.json().get("code") != "dependency_unavailable"
            or retries >= 5
        ):
            return key, response, (time.perf_counter() - started) * 1000, retries
        time.sleep(min(0.1 * 2**retries, 1.0))
        retries += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenants", type=int, default=100)
    parser.add_argument("--jobs-per-tenant", type=int, default=1000)
    parser.add_argument("--login-rate-per-minute", type=int, default=120)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--submit-concurrency", type=int, default=None)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.submit_concurrency is None:
        args.submit_concurrency = args.workers
    if (
        min(args.tenants, args.jobs_per_tenant, args.workers, args.submit_concurrency) < 1
        or not 1 <= args.login_rate_per_minute <= 120
    ):
        parser.error("counts must be positive and login rate must be between 1 and 120")
    url = _guard_url(os.environ.get("NEXA_TEST_DATABASE_URL", ""))
    engine = create_engine(url, pool_pre_ping=True)
    if inspect_schema_compatibility(engine) is not SchemaCompatibility.CURRENT:
        raise RuntimeError("Migrate the isolated API benchmark database first")
    started_at = datetime.now(UTC).isoformat()
    starting_provenance = _provenance()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    journal = args.output.with_suffix(".accepted.jsonl")
    previously_accepted, accepted_by_job = _recover_journal(engine, journal)
    setup, worker_tokens, global_policy = _setup(
        engine,
        args.state_dir,
        args.tenants,
        args.jobs_per_tenant,
        args.login_rate_per_minute,
        args.workers,
    )
    durations = [
        row["latency_ms"] for row in accepted_by_job.values() if row["latency_ms"] is not None
    ]
    newly_accepted = 0
    retry_count = 0
    errors = []
    with (
        _running_api_process(engine, args.state_dir) as (base_url, _process),
        httpx.Client(base_url=base_url, timeout=30) as api,
        journal.open("a") as output,
        ThreadPoolExecutor(max_workers=args.submit_concurrency) as pool,
    ):

        def pending_submissions():
            bodies = [_submit_body(artifact_id) for _, artifact_id in setup]
            for job_index in range(args.jobs_per_tenant):
                for tenant_index, (tenant_id, _) in enumerate(setup):
                    key = f"b13-job-{tenant_index:04d}-{job_index:07d}"
                    if key not in previously_accepted:
                        yield (
                            tenant_id,
                            key,
                            bodies[tenant_index],
                            worker_tokens[tenant_index % args.workers],
                        )

        tenant_by_key = {
            f"b13-job-{index:04d}": tenant_id for index, (tenant_id, _) in enumerate(setup)
        }
        pending = pending_submissions()
        while batch := list(islice(pending, 256)):
            for key, response, elapsed, retries in pool.map(
                lambda item: _submit(api, *item), batch
            ):
                retry_count += retries
                if response.status_code != 202:
                    errors.append(
                        {"key": key, "status": response.status_code, "body": response.text[:400]}
                    )
                    break
                payload = response.json()
                row = {
                    "idempotency_key": key,
                    "tenant_id": tenant_by_key[key[:12]],
                    "job_id": payload["job_id"],
                    "session_id": payload["session_id"],
                    "latency_ms": elapsed,
                    "status": response.status_code,
                }
                output.write(json.dumps(row) + "\n")
                output.flush()
                previously_accepted[key] = row
                accepted_by_job[row["job_id"]] = row
                durations.append(elapsed)
                newly_accepted += 1
                if len(previously_accepted) % 1000 == 0:
                    print(f"accepted {len(previously_accepted)} jobs", flush=True)
            if errors:
                break
    accepted_ids = [UUID(job_id) for job_id in accepted_by_job]
    restarted_reads = 0
    cross_tenant_denied = 0
    by_tenant = {}
    for row in previously_accepted.values():
        by_tenant.setdefault(row["tenant_id"], []).append(row)
    tenant_tokens = {
        tenant_id: worker_tokens[index % args.workers] for index, (tenant_id, _) in enumerate(setup)
    }
    with (
        _running_api_process(engine, args.state_dir) as (base_url, _process),
        httpx.Client(base_url=base_url, timeout=30) as restarted_api,
    ):
        for tenant_id, rows in by_tenant.items():
            for row in (rows[0], rows[-1]):
                fetched = restarted_api.get(
                    f"/v1/jobs/{row['job_id']}",
                    headers={
                        "Authorization": f"Bearer {tenant_tokens[tenant_id]}",
                        "X-Nexa-Tenant-Id": tenant_id,
                    },
                )
                if fetched.status_code != 200 or fetched.json().get("job_id") != row["job_id"]:
                    raise RuntimeError("Accepted job was not readable after API process restart")
                restarted_reads += 1
            other_tenant = next((item for item in by_tenant if item != tenant_id), None)
            if other_tenant is not None:
                denied = restarted_api.get(
                    f"/v1/jobs/{rows[0]['job_id']}",
                    headers={
                        "Authorization": f"Bearer {tenant_tokens[other_tenant]}",
                        "X-Nexa-Tenant-Id": other_tenant,
                    },
                )
                if denied.status_code != 404:
                    raise RuntimeError("Cross-tenant job read was not rejected")
                cross_tenant_denied += 1
    ending_provenance = _provenance()
    result = {
        "evidence_layer": "P production HTTP API and PostgreSQL; CPU queue only",
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(UTC).isoformat(),
        "fixture": {
            "tenants": args.tenants,
            "jobs_per_tenant": args.jobs_per_tenant,
            "login_rate_per_minute": args.login_rate_per_minute,
            "submit_clients": args.workers,
            "submit_concurrency": args.submit_concurrency,
            "global_policy_version": global_policy["version"],
            "global_outstanding_limit": global_policy["global_outstanding_limit"],
            "template_setup": (
                "direct DB static template; tenants/policy/artifacts/jobs through API"
            ),
            "accepted_id_journal": str(journal),
        },
        "provenance_start": starting_provenance,
        "provenance_end": ending_provenance,
        "provenance_changed_during_run": starting_provenance != ending_provenance,
        "accepted_count": len(accepted_ids),
        "distinct_idempotency_keys": len(previously_accepted),
        "cross_principal_duplicate_keys": len(accepted_ids) - len(previously_accepted),
        "measurement": {
            "run_count": len(durations),
            "newly_accepted_this_run": newly_accepted,
            "dependency_unavailable_retries_this_run": retry_count,
            "median_ms": statistics.median(durations) if durations else None,
            "range_ms": [min(durations), max(durations)] if durations else None,
            "errors": errors,
        },
        "reconciliation": _reconcile(engine, accepted_ids),
        "restart_authorization": {
            "sampled_reads": restarted_reads,
            "cross_tenant_denied": cross_tenant_denied,
        },
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "accepted_count": len(accepted_ids),
                "reconciliation": result["reconciliation"],
                "errors": errors[:3],
            }
        )
    )


if __name__ == "__main__":
    main()
