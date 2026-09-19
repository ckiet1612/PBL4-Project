from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema import (
    allocation_gpu_claims,
    allocations,
    artifacts,
    attempt_authority_grants,
    attempt_leases,
    attempts,
    gpu_devices,
    job_specs,
    jobs,
    logical_sessions,
    membership_sets,
    memberships,
    template_versions,
    templates,
    tenants,
    users,
    worker_incarnations,
    worker_inventories,
    workers,
)

CHECKSUM = "sha256:" + "a" * 64
IMAGE_DIGEST = "sha256:" + "b" * 64


def seed_tenant_graph(connection: Connection, *, label: str) -> dict[str, Any]:
    tenant_id = new_uuid7()
    user_id = new_uuid7()
    artifact_id = new_uuid7()
    template_id = f"cpu.iterative.{label}"
    connection.execute(
        tenants.insert(),
        {
            "tenant_id": tenant_id,
            "slug": f"tenant-{label}",
            "display_name": f"Tenant {label}",
            "enabled": True,
            "version": 1,
        },
    )
    connection.execute(membership_sets.insert(), {"tenant_id": tenant_id, "version": 1})
    connection.execute(
        users.insert(),
        {
            "user_id": user_id,
            "username": f"user-{label}",
            "display_name": f"User {label}",
            "password_hash": "$argon2id$" + "x" * 40,
            "enabled": True,
            "version": 1,
        },
    )
    connection.execute(
        memberships.insert(),
        {"tenant_id": tenant_id, "user_id": user_id, "role": "TENANT_ADMIN"},
    )
    connection.execute(
        templates.insert(),
        {
            "template_id": template_id,
            "current_version": 1,
            "display_name": f"CPU {label}",
            "description": "test template",
            "enabled": True,
        },
    )
    connection.execute(
        template_versions.insert(),
        {
            "template_id": template_id,
            "version": 1,
            "parameter_schema": {},
            "resource_bounds": {},
            "capability_requirements": {},
            "adapter_id": "cpu.iterative",
            "adapter_version": "1.0.0",
            "image_digest": IMAGE_DIGEST,
            "checkpointable": True,
            "restart_safe": True,
        },
    )
    connection.execute(
        artifacts.insert(),
        {
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "kind": "INPUT",
            "media_type": "application/octet-stream",
            "size_bytes": 4,
            "checksum": CHECKSUM,
            "blob_key": f"blob/{label}/input",
            "state": "COMMITTED",
            "version": 1,
        },
    )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "artifact_id": artifact_id,
        "template_id": template_id,
    }


def seed_job(
    connection: Connection,
    graph: dict[str, Any],
    *,
    job_id: UUID | None = None,
    state: str = "QUEUED",
    desired_state: str = "RUNNING",
    retry_of_job_id: UUID | None = None,
    artifact_id: UUID | None = None,
) -> dict[str, UUID]:
    job_id = job_id or new_uuid7()
    session_id = new_uuid7()
    connection.execute(
        jobs.insert(),
        {
            "job_id": job_id,
            "tenant_id": graph["tenant_id"],
            "submitter_user_id": graph["user_id"],
            "state": state,
            "desired_state": desired_state,
            "version": 1,
            "job_fence": 0,
            "event_sequence": 0,
            "checkpoint_sequence": 0,
            "retry_count": 0,
            "max_retries": 2,
            "retry_of_job_id": retry_of_job_id,
            "base_priority": 1,
            "ready_sequence": int(job_id.int & 0x7FFFFFFF),
        },
    )
    connection.execute(
        job_specs.insert(),
        {
            "job_id": job_id,
            "tenant_id": graph["tenant_id"],
            "canonical_spec": {"seed": 7},
            "spec_checksum": CHECKSUM,
            "template_id": graph["template_id"],
            "template_version": 1,
            "input_artifact_id": artifact_id or graph["artifact_id"],
            "cpu_millis": 1000,
            "memory_bytes": 1_073_741_824,
            "gpu_count": 0,
            "runtime_limit_seconds": 300,
            "checkpoint_interval_seconds": 30,
        },
    )
    connection.execute(
        logical_sessions.insert(),
        {"session_id": session_id, "tenant_id": graph["tenant_id"], "job_id": job_id},
    )
    return {"job_id": job_id, "session_id": session_id}


def seed_worker(connection: Connection, *, label: str, gpu_versions: int = 1) -> dict[str, Any]:
    worker_id = new_uuid7()
    incarnation_id = new_uuid7()
    connection.execute(
        workers.insert(),
        {
            "worker_id": worker_id,
            "admin_state": "ENABLED",
            "health": "READY",
            "version": 1,
        },
    )
    connection.execute(
        worker_incarnations.insert(),
        {
            "worker_incarnation_id": incarnation_id,
            "worker_id": worker_id,
            "sequence": 1,
            "process_start_nonce": new_uuid7(),
            "process_started_at": datetime.now(UTC),
            "reconcile_completed_at": datetime.now(UTC),
            "ready_at": datetime.now(UTC),
        },
    )
    connection.execute(
        workers.update()
        .where(workers.c.worker_id == worker_id)
        .values(current_incarnation_id=incarnation_id, current_inventory_version=gpu_versions)
    )
    gpu_uuid = f"GPU-{label}"
    for version in range(1, gpu_versions + 1):
        inventory_id = new_uuid7()
        connection.execute(
            worker_inventories.insert(),
            {
                "inventory_id": inventory_id,
                "worker_id": worker_id,
                "worker_incarnation_id": incarnation_id,
                "inventory_version": version,
                "architecture": "linux/arm64",
                "host_cpu_millis": 8_000,
                "host_memory_bytes": 16 * 1024**3,
                "allocatable_cpu_millis": 6_000,
                "allocatable_memory_bytes": 12 * 1024**3,
                "allocatable_gpu_count": 1,
                "runtime_capabilities": {"cgroups": 2},
                "workload_capabilities": {"gpu": True},
                "checksum": CHECKSUM,
                "observed_at": datetime.now(UTC),
            },
        )
        connection.execute(
            gpu_devices.insert(),
            {
                "worker_id": worker_id,
                "inventory_version": version,
                "inventory_id": inventory_id,
                "gpu_uuid": gpu_uuid,
                "model": "Test GPU",
                "memory_bytes": 8 * 1024**3,
                "compute_capability": "8.0",
                "driver_version": "1.0",
                "api_version": "1.0",
                "healthy": True,
            },
        )
    return {
        "worker_id": worker_id,
        "incarnation_id": incarnation_id,
        "gpu_uuid": gpu_uuid,
        "inventory_version": gpu_versions,
    }


def seed_authority(
    connection: Connection,
    graph: dict[str, Any],
    job: dict[str, UUID],
    worker: dict[str, Any],
    *,
    gpu_count: int = 0,
    inventory_version: int | None = None,
    attempt_number: int = 1,
    job_fence: int = 1,
) -> dict[str, UUID]:
    attempt_id = new_uuid7()
    allocation_id = new_uuid7()
    lease_id = new_uuid7()
    grant_id = new_uuid7()
    connection.execute(
        jobs.update().where(jobs.c.job_id == job["job_id"]).values(job_fence=job_fence)
    )
    connection.execute(
        attempts.insert(),
        {
            "attempt_id": attempt_id,
            "tenant_id": graph["tenant_id"],
            "job_id": job["job_id"],
            "attempt_number": attempt_number,
            "state": "CREATED",
            "execution_intent": "RUN",
            "worker_id": worker["worker_id"],
            "worker_incarnation_id": worker["incarnation_id"],
            "job_fence": job_fence,
            "startup_nonce": new_uuid7(),
            "progress_sequence": 0,
        },
    )
    connection.execute(
        allocations.insert(),
        {
            "allocation_id": allocation_id,
            "tenant_id": graph["tenant_id"],
            "job_id": job["job_id"],
            "attempt_id": attempt_id,
            "worker_id": worker["worker_id"],
            "cpu_millis": 1000,
            "memory_bytes": 1_073_741_824,
            "gpu_count": gpu_count,
            "state": "HELD",
        },
    )
    if gpu_count:
        connection.execute(
            allocation_gpu_claims.insert(),
            {
                "allocation_id": allocation_id,
                "worker_id": worker["worker_id"],
                "inventory_version": inventory_version or worker["inventory_version"],
                "gpu_uuid": worker["gpu_uuid"],
            },
        )
    connection.execute(
        attempt_leases.insert(),
        {
            "lease_id": lease_id,
            "tenant_id": graph["tenant_id"],
            "job_id": job["job_id"],
            "attempt_id": attempt_id,
            "allocation_id": allocation_id,
            "worker_id": worker["worker_id"],
            "current_worker_incarnation_id": worker["incarnation_id"],
            "job_fence": job_fence,
            "expires_at": datetime.now(UTC) + timedelta(seconds=45),
        },
    )
    connection.execute(
        attempt_authority_grants.insert(),
        {
            "grant_id": grant_id,
            "tenant_id": graph["tenant_id"],
            "job_id": job["job_id"],
            "attempt_id": attempt_id,
            "allocation_id": allocation_id,
            "lease_id": lease_id,
            "worker_id": worker["worker_id"],
            "worker_incarnation_id": worker["incarnation_id"],
            "job_fence": job_fence,
            "callback_id": new_uuid7(),
        },
    )
    return {
        "attempt_id": attempt_id,
        "allocation_id": allocation_id,
        "lease_id": lease_id,
        "grant_id": grant_id,
    }
