from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema import (
    job_specs,
    jobs,
    logical_sessions,
    membership_sets,
    policy_versions,
    reservations,
    sweep_children,
    sweep_parents,
    tenants,
)

from ._factories import CHECKSUM, seed_job, seed_tenant_graph

pytestmark = pytest.mark.postgres


def test_tenant_commit_requires_membership_set_even_when_empty(migrated_postgres_engine) -> None:
    with (
        pytest.raises(IntegrityError, match="membership set"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            tenants.insert(),
            {
                "tenant_id": new_uuid7(),
                "slug": "missing-set",
                "display_name": "Missing set",
                "enabled": True,
                "version": 1,
            },
        )


def test_empty_membership_set_cannot_be_deleted_while_tenant_exists(
    migrated_postgres_engine,
) -> None:
    tenant_id = new_uuid7()
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            tenants.insert(),
            {
                "tenant_id": tenant_id,
                "slug": "empty-membership-set",
                "display_name": "Empty membership set",
                "enabled": True,
                "version": 1,
            },
        )
        connection.execute(membership_sets.insert(), {"tenant_id": tenant_id, "version": 1})

    with (
        pytest.raises(IntegrityError, match="membership set"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(membership_sets.delete().where(membership_sets.c.tenant_id == tenant_id))


def test_normalized_slug_is_enforced_on_insert_and_update(migrated_postgres_engine) -> None:
    with (
        pytest.raises(IntegrityError, match="slug_(normalized|format)"),
        migrated_postgres_engine.begin() as connection,
    ):
        tenant_id = new_uuid7()
        connection.execute(
            tenants.insert(),
            {
                "tenant_id": tenant_id,
                "slug": "Mixed-Case",
                "display_name": "Mixed",
                "enabled": True,
                "version": 1,
            },
        )
        connection.execute(membership_sets.insert(), {"tenant_id": tenant_id, "version": 1})

    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="normalized")
    with (
        pytest.raises(IntegrityError, match="slug_(normalized|format)"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            tenants.update()
            .where(tenants.c.tenant_id == graph["tenant_id"])
            .values(slug="Changed-Case")
        )


def test_job_commit_requires_one_spec_and_one_logical_session(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="complete")

    with (
        pytest.raises(IntegrityError, match="exactly one spec and logical session"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            jobs.insert(),
            {
                "job_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "submitter_user_id": graph["user_id"],
                "state": "QUEUED",
                "desired_state": "RUNNING",
                "version": 1,
                "ready_sequence": 1,
            },
        )


def test_cross_tenant_artifact_reference_is_rejected_during_job_insert(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        tenant_a = seed_tenant_graph(connection, label="cross-a")
        tenant_b = seed_tenant_graph(connection, label="cross-b")

    job_id = new_uuid7()
    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        connection.execute(
            jobs.insert(),
            {
                "job_id": job_id,
                "tenant_id": tenant_a["tenant_id"],
                "submitter_user_id": tenant_a["user_id"],
                "state": "QUEUED",
                "desired_state": "RUNNING",
                "version": 1,
                "ready_sequence": 2,
            },
        )
        connection.execute(
            job_specs.insert(),
            {
                "job_id": job_id,
                "tenant_id": tenant_a["tenant_id"],
                "canonical_spec": {},
                "spec_checksum": CHECKSUM,
                "template_id": tenant_a["template_id"],
                "template_version": 1,
                "input_artifact_id": tenant_b["artifact_id"],
                "cpu_millis": 1000,
                "memory_bytes": 1024,
                "gpu_count": 0,
                "runtime_limit_seconds": 60,
            },
        )


def test_job_spec_and_terminal_authority_fields_are_immutable(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="immutable")
        job = seed_job(connection, graph, state="FAILED", desired_state="RUNNING")

    with (
        pytest.raises(IntegrityError, match="job_specs rows are immutable"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            job_specs.update()
            .where(job_specs.c.job_id == job["job_id"])
            .values(canonical_spec={"changed": True})
        )

    with (
        pytest.raises(IntegrityError, match="terminal job authority and state are immutable"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            jobs.update().where(jobs.c.job_id == job["job_id"]).values(state="QUEUED")
        )


def test_job_fence_cannot_decrease(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="fence-monotonic")
        job = seed_job(connection, graph)
        connection.execute(jobs.update().where(jobs.c.job_id == job["job_id"]).values(job_fence=2))

    with (
        pytest.raises(IntegrityError, match="job fence cannot decrease"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(jobs.update().where(jobs.c.job_id == job["job_id"]).values(job_fence=1))

    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            jobs.update().where(jobs.c.job_id == job["job_id"]).values(event_sequence=1)
        )


def test_deleting_logical_session_cannot_leave_accepted_job_incomplete(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="session-delete")
        job = seed_job(connection, graph)

    with (
        pytest.raises(IntegrityError, match="exactly one spec and logical session"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            logical_sessions.delete().where(logical_sessions.c.session_id == job["session_id"])
        )


def test_manual_retry_source_must_belong_to_same_tenant(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        tenant_a = seed_tenant_graph(connection, label="retry-a")
        tenant_b = seed_tenant_graph(connection, label="retry-b")
        source = seed_job(connection, tenant_b, state="FAILED")

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        seed_job(
            connection,
            tenant_a,
            retry_of_job_id=source["job_id"],
            state="QUEUED",
        )


def test_only_one_local_reservation_can_be_active(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="local-reservation")
        first_job = seed_job(connection, graph)
        second_job = seed_job(connection, graph)
        connection.execute(
            policy_versions.insert(),
            {
                "policy_version": 1,
                "global_outstanding_limit": 100_000,
                "operational_mode": "NORMAL",
                "is_current": True,
            },
        )
        connection.execute(
            reservations.insert(),
            {
                "reservation_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": first_job["job_id"],
                "eligibility_since": datetime.now(UTC),
                "policy_version": 1,
            },
        )

    with (
        pytest.raises(IntegrityError, match="uq_reservations_one_active"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            reservations.insert(),
            {
                "reservation_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": second_job["job_id"],
                "eligibility_since": datetime.now(UTC),
                "policy_version": 1,
            },
        )


def test_sweep_parent_ownership_is_enforced_on_insert_and_update(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        tenant_a = seed_tenant_graph(connection, label="sweep-owner-a")
        tenant_b = seed_tenant_graph(connection, label="sweep-owner-b")
        job_a = seed_job(connection, tenant_a)
        job_b = seed_job(connection, tenant_b)
        sweep_id = new_uuid7()
        connection.execute(
            sweep_parents.insert(),
            {
                "sweep_id": sweep_id,
                "tenant_id": tenant_a["tenant_id"],
                "submitter_user_id": tenant_a["user_id"],
                "base_spec_checksum": CHECKSUM,
                "idempotency_context": str(tenant_a["tenant_id"]),
                "child_count": 2,
            },
        )

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        connection.execute(
            sweep_children.insert(),
            {
                "sweep_id": sweep_id,
                "tenant_id": tenant_b["tenant_id"],
                "child_index": 0,
                "parameter_hash": "sha256:" + "c" * 64,
                "job_id": job_b["job_id"],
            },
        )

    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            sweep_children.insert(),
            {
                "sweep_id": sweep_id,
                "tenant_id": tenant_a["tenant_id"],
                "child_index": 1,
                "parameter_hash": "sha256:" + "d" * 64,
                "job_id": job_a["job_id"],
            },
        )

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        connection.execute(
            sweep_children.update()
            .where(
                sweep_children.c.sweep_id == sweep_id,
                sweep_children.c.child_index == 1,
            )
            .values(tenant_id=tenant_b["tenant_id"], job_id=job_b["job_id"])
        )


def test_job_event_counter_can_advance_without_mutating_terminal_state(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="terminal-event")
        job = seed_job(connection, graph, state="SUCCEEDED", desired_state="RUNNING")

    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            jobs.update()
            .where(jobs.c.job_id == job["job_id"])
            .values(event_sequence=1, updated_at=datetime.now(UTC))
        )
        value = connection.execute(
            text("SELECT event_sequence FROM jobs WHERE job_id = :job_id"),
            {"job_id": job["job_id"]},
        ).scalar_one()
    assert value == 1
