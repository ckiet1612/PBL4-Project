from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy import event, insert, select, update

from nexa.application.admin_service import AdminService
from nexa.application.errors import ApplicationError
from nexa.application.identity_service import IdentityService
from nexa.application.policy_service import PolicyService
from nexa.infrastructure.persistence.schema import (
    admission_counters,
    auth_control,
    policy_versions,
    tenant_policies,
)
from nexa.infrastructure.persistence.transactions import run_transaction

from .identity_support import bootstrap_admin, make_identity_service

pytestmark = pytest.mark.postgres


class AllowProofs:
    def freeze_ready(self, session) -> bool:
        return True

    def restore_verified(self, session) -> bool:
        return True

    def readiness_verified(self, session) -> bool:
        return True


def test_tenant_policy_waits_for_counter_commit_before_checking_limit(
    migrated_postgres_engine, tmp_path
) -> None:
    identity = make_identity_service(migrated_postgres_engine, tmp_path)
    bootstrap_admin(identity)
    login = identity.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.1",
    )
    principal = identity.resolve_browser_session(login.cookie).principal
    admin = AdminService(identity.session_factory, identity.settings, identity)
    policy = PolicyService(identity.session_factory, identity.settings, identity)
    tenant = admin.create_tenant(
        principal,
        slug="policy-counter-race",
        display_name="Policy Counter Race",
        idempotency_key="create-tenant-race2",
        request_hash="sha256:" + "a" * 64,
    )
    tenant_id = UUID(tenant["tenant_id"])
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            insert(admission_counters).values(
                scope_type="TENANT",
                scope_id=str(tenant_id),
                outstanding=1,
                active_attempts=0,
            )
        )

    counter_updated = Event()
    allow_counter_commit = Event()
    policy_lock_attempted = Event()

    def observe_policy_lock(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(statement.lower().split())
        if "from tenant_policies" in normalized and "for update" in normalized:
            policy_lock_attempted.set()

    event.listen(migrated_postgres_engine, "before_cursor_execute", observe_policy_lock)

    def commit_counter() -> None:
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                select(tenant_policies.c.version)
                .where(
                    tenant_policies.c.tenant_id == tenant_id,
                    tenant_policies.c.is_current.is_(True),
                )
                .with_for_update(read=True)
            ).scalar_one()
            connection.execute(
                update(admission_counters)
                .where(
                    admission_counters.c.scope_type == "TENANT",
                    admission_counters.c.scope_id == str(tenant_id),
                )
                .values(outstanding=5)
            )
            counter_updated.set()
            assert allow_counter_commit.wait(timeout=5)

    def lower_limit() -> tuple[int, str]:
        try:
            policy.update_tenant_policy(
                principal,
                tenant_id=tenant_id,
                expected_version=1,
                changes={"outstanding_limit": 4},
                idempotency_key="policy-counter-race1",
                request_hash="sha256:" + "b" * 64,
            )
        except ApplicationError as exc:
            return exc.status, exc.code
        return 200, "ok"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            counter_future = pool.submit(commit_counter)
            assert counter_updated.wait(timeout=5)
            policy_future = pool.submit(lower_limit)
            assert policy_lock_attempted.wait(timeout=5)
            assert not policy_future.done()
            allow_counter_commit.set()
            counter_future.result(timeout=5)
            assert policy_future.result(timeout=5) == (409, "state_conflict")
    finally:
        allow_counter_commit.set()
        event.remove(migrated_postgres_engine, "before_cursor_execute", observe_policy_lock)

    current = policy.get_tenant_policy(principal, tenant_id)
    assert current["version"] == 1
    assert current["outstanding_limit"] == 2000


def test_freeze_transition_serializes_after_deployment_identity_initialization(
    migrated_postgres_engine, tmp_path
) -> None:
    identity = make_identity_service(migrated_postgres_engine, tmp_path)
    bootstrap_admin(identity)
    login = identity.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.1",
    )
    principal = identity.resolve_browser_session(login.cookie).principal
    policy = PolicyService(
        identity.session_factory,
        identity.settings,
        identity,
        proof_provider=AllowProofs(),
    )

    def prepare_uninitialized_admission_off(session) -> None:
        session.execute(
            update(auth_control)
            .where(auth_control.c.singleton_key == "auth")
            .values(
                installation_id=None,
                local_worker_id=None,
                worker_credential_fingerprint=None,
                worker_window_opened_at=None,
                worker_window_expires_at=None,
            )
        )
        session.execute(
            update(policy_versions)
            .where(policy_versions.c.is_current.is_(True))
            .values(operational_mode="ADMISSION_OFF")
        )

    run_transaction(identity.session_factory, prepare_uninitialized_admission_off)
    restarted = IdentityService(identity.session_factory, identity.settings)
    initialize_write_attempted = Event()
    allow_initialize_write = Event()
    freeze_lock_attempted = Event()

    def observe_lock_order(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("update auth_control") and "installation_id" in normalized:
            initialize_write_attempted.set()
            assert allow_initialize_write.wait(timeout=5)
        if "from policy_versions" in normalized and normalized.endswith("for update"):
            freeze_lock_attempted.set()

    event.listen(migrated_postgres_engine, "before_cursor_execute", observe_lock_order)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            initialize_future = pool.submit(restarted.initialize)
            assert initialize_write_attempted.wait(timeout=5)
            freeze_future = pool.submit(
                policy.update_global_policy,
                principal,
                expected_version=1,
                global_outstanding_limit=None,
                operational_mode="WRITE_FROZEN",
                idempotency_key="freeze-init-race01",
                request_hash="sha256:" + "c" * 64,
            )
            assert freeze_lock_attempted.wait(timeout=5)
            assert not freeze_future.done()
            allow_initialize_write.set()
            initialize_future.result(timeout=5)
            frozen = freeze_future.result(timeout=5)
    finally:
        allow_initialize_write.set()
        event.remove(migrated_postgres_engine, "before_cursor_execute", observe_lock_order)

    assert frozen["operational_mode"] == "WRITE_FROZEN"
    durable = run_transaction(
        identity.session_factory,
        lambda session: session.execute(
            select(
                auth_control.c.installation_id,
                policy_versions.c.operational_mode,
            ).select_from(
                auth_control.join(policy_versions, policy_versions.c.is_current.is_(True))
            )
        ).one(),
    )
    assert durable == (identity.settings.installation_id, "WRITE_FROZEN")
