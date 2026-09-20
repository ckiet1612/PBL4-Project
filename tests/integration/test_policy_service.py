from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import insert, update

from nexa.application.admin_service import AdminService
from nexa.application.errors import ApplicationError
from nexa.application.policy_service import PolicyService
from nexa.infrastructure.persistence.schema import (
    admission_counters,
    allocations,
    tenant_policies,
)
from nexa.infrastructure.persistence.transactions import run_transaction

from ._factories import seed_authority, seed_job, seed_tenant_graph, seed_worker
from .identity_support import bootstrap_admin, make_identity_service

pytestmark = pytest.mark.postgres


class AllowProofs:
    def freeze_ready(self, session) -> bool:
        return True

    def restore_verified(self, session) -> bool:
        return True

    def readiness_verified(self, session) -> bool:
        return True


def _context(migrated_postgres_engine, tmp_path, *, proofs=None):
    identity = make_identity_service(migrated_postgres_engine, tmp_path)
    bootstrap_admin(identity)
    login = identity.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.1",
    )
    principal = identity.resolve_browser_session(login.cookie).principal
    admin = AdminService(identity.session_factory, identity.settings, identity)
    policy = PolicyService(
        identity.session_factory,
        identity.settings,
        identity,
        proof_provider=proofs,
    )
    return identity, admin, policy, principal


def test_tenant_policy_round_trips_decimal_and_replays_before_etag(
    migrated_postgres_engine, tmp_path
) -> None:
    _, admin, policy, principal = _context(migrated_postgres_engine, tmp_path)
    tenant = admin.create_tenant(
        principal,
        slug="policy-team",
        display_name="Policy Team",
        idempotency_key="create-tenant-0101",
        request_hash="sha256:" + "a" * 64,
    )
    tenant_id = UUID(tenant["tenant_id"])
    seeded = policy.get_tenant_policy(principal, tenant_id)
    assert seeded["version"] == 1
    assert seeded["weight"] == 1.0
    assert seeded["resource_limit"] == {
        "cpu_millis": 0,
        "memory_bytes": 0,
        "gpu_count": 0,
    }

    changes = {
        "weight": Decimal("0.10000000000000002"),
        "resource_limit": {
            "cpu_millis": 4000,
            "memory_bytes": 8_589_934_592,
            "gpu_count": 1,
        },
        "submit_rate_per_second": Decimal("2.5"),
    }
    updated = policy.update_tenant_policy(
        principal,
        tenant_id=tenant_id,
        expected_version=1,
        changes=changes,
        idempotency_key="update-policy-0001",
        request_hash="sha256:" + "b" * 64,
    )
    replay = policy.update_tenant_policy(
        principal,
        tenant_id=tenant_id,
        expected_version=1,
        changes=changes,
        idempotency_key="update-policy-0001",
        request_hash="sha256:" + "b" * 64,
    )
    assert replay == updated
    assert updated["version"] == 2
    assert updated["weight"] == 0.10000000000000002
    assert policy.get_tenant_policy(principal, tenant_id) == updated


def test_tenant_policy_rejects_limits_below_committed_counter(
    migrated_postgres_engine, tmp_path
) -> None:
    identity, admin, policy, principal = _context(migrated_postgres_engine, tmp_path)
    tenant = admin.create_tenant(
        principal,
        slug="quota-team",
        display_name="Quota Team",
        idempotency_key="create-tenant-0102",
        request_hash="sha256:" + "c" * 64,
    )
    tenant_id = UUID(tenant["tenant_id"])
    run_transaction(
        identity.session_factory,
        lambda session: session.execute(
            insert(admission_counters).values(
                scope_type="TENANT",
                scope_id=str(tenant_id),
                outstanding=5,
                active_attempts=2,
            )
        ),
    )
    run_transaction(
        identity.session_factory,
        lambda session: session.execute(
            insert(admission_counters).values(
                scope_type="USER",
                scope_id=f"{tenant_id}:{principal.user_id}",
                outstanding=3,
                active_attempts=1,
            )
        ),
    )

    with pytest.raises(ApplicationError) as below_counter:
        policy.update_tenant_policy(
            principal,
            tenant_id=tenant_id,
            expected_version=1,
            changes={"outstanding_limit": 4},
            idempotency_key="update-policy-0002",
            request_hash="sha256:" + "d" * 64,
        )
    assert below_counter.value.status == 409

    with pytest.raises(ApplicationError) as below_user_counter:
        policy.update_tenant_policy(
            principal,
            tenant_id=tenant_id,
            expected_version=1,
            changes={"user_outstanding_limit": 2},
            idempotency_key="update-policy-0003",
            request_hash="sha256:" + "d" * 64,
        )
    assert below_user_counter.value.status == 409


def test_global_mode_transitions_are_ordered_and_fail_closed_without_proof(
    migrated_postgres_engine, tmp_path
) -> None:
    identity, _, policy, principal = _context(migrated_postgres_engine, tmp_path)
    with pytest.raises(ApplicationError) as skipped:
        policy.update_global_policy(
            principal,
            expected_version=1,
            global_outstanding_limit=None,
            operational_mode="WRITE_FROZEN",
            idempotency_key="global-policy-0001",
            request_hash="sha256:" + "e" * 64,
        )
    assert skipped.value.status == 409

    admission_off = policy.update_global_policy(
        principal,
        expected_version=1,
        global_outstanding_limit=None,
        operational_mode="ADMISSION_OFF",
        idempotency_key="global-policy-0002",
        request_hash="sha256:" + "f" * 64,
    )
    assert admission_off["operational_mode"] == "ADMISSION_OFF"
    with pytest.raises(ApplicationError) as no_proof:
        policy.update_global_policy(
            principal,
            expected_version=2,
            global_outstanding_limit=None,
            operational_mode="WRITE_FROZEN",
            idempotency_key="global-policy-0003",
            request_hash="sha256:" + "1" * 64,
        )
    assert no_proof.value.status == 409

    proved = PolicyService(
        identity.session_factory,
        identity.settings,
        identity,
        proof_provider=AllowProofs(),
    )
    frozen = proved.update_global_policy(
        principal,
        expected_version=2,
        global_outstanding_limit=None,
        operational_mode="WRITE_FROZEN",
        idempotency_key="global-policy-0004",
        request_hash="sha256:" + "2" * 64,
    )
    assert frozen["version"] == 3
    assert frozen["operational_mode"] == "WRITE_FROZEN"

    with pytest.raises(ApplicationError) as mixed_restore:
        proved.update_global_policy(
            principal,
            expected_version=3,
            global_outstanding_limit=99_999,
            operational_mode="ADMISSION_OFF",
            idempotency_key="global-policy-0005",
            request_hash="sha256:" + "4" * 64,
        )
    assert mixed_restore.value.status == 409


def test_tenant_policy_counts_all_unreleased_allocations(
    migrated_postgres_engine, tmp_path
) -> None:
    identity, _, policy, principal = _context(migrated_postgres_engine, tmp_path)
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="policy-held")
        connection.execute(
            insert(tenant_policies).values(
                tenant_id=graph["tenant_id"],
                version=1,
                weight=Decimal("1"),
                cpu_limit_millis=4000,
                memory_limit_bytes=8_589_934_592,
                gpu_limit=1,
                outstanding_limit=2000,
                user_outstanding_limit=2000,
                tenant_active_limit=2,
                user_active_limit=1,
                tenant_rate_per_second=Decimal("5"),
                tenant_rate_burst=Decimal("20"),
                user_rate_per_second=Decimal("2"),
                user_rate_burst=Decimal("10"),
                is_current=True,
            )
        )
        worker = seed_worker(connection, label="policy-held")
        job = seed_job(connection, graph)
        authority = seed_authority(connection, graph, job, worker)
        connection.execute(
            update(allocations)
            .where(allocations.c.allocation_id == authority["allocation_id"])
            .values(state="QUARANTINED", quarantined_at=datetime.now(UTC))
        )

    with pytest.raises(ApplicationError) as below_held:
        policy.update_tenant_policy(
            principal,
            tenant_id=graph["tenant_id"],
            expected_version=1,
            changes={
                "resource_limit": {
                    "cpu_millis": 999,
                    "memory_bytes": 8_589_934_592,
                    "gpu_count": 1,
                }
            },
            idempotency_key="update-policy-held1",
            request_hash="sha256:" + "3" * 64,
        )
    assert below_held.value.status == 409
