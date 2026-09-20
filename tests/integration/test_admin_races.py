from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import UUID

import pytest
from sqlalchemy import event, func, select

from nexa.application.admin_service import AdminService
from nexa.application.errors import ApplicationError
from nexa.application.policy_service import PolicyService
from nexa.domain.identity import TokenScope
from nexa.infrastructure.persistence.schema import (
    browser_sessions,
    cli_tokens,
    system_role_grants,
    tenant_policies,
    users,
    worker_credentials,
)
from nexa.infrastructure.persistence.transactions import run_transaction

from .identity_support import (
    FINGERPRINT,
    INSTALLATION_ID,
    bootstrap_admin,
    make_identity_service,
)

pytestmark = pytest.mark.postgres


def _outcome(call):
    try:
        return "ok", call()
    except ApplicationError as exc:
        return str(exc.status), exc.code


def test_two_different_first_admin_requests_create_exactly_one_admin(
    migrated_postgres_engine, tmp_path
) -> None:
    identity = make_identity_service(migrated_postgres_engine, tmp_path)
    barrier = Barrier(2, timeout=5)

    def bootstrap(index: int):
        barrier.wait()
        return identity.bootstrap_admin(
            bootstrap_secret=b"b" * 32,
            source_allowed=True,
            username=f"admin-{index}@example.test",
            display_name=f"Admin {index}",
            password="correct-horse-battery-staple",
            idempotency_key=f"bootstrap-race-{index:04d}",
            request_hash="sha256:" + str(index) * 64,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda index: _outcome(lambda: bootstrap(index)), (1, 2)))

    assert sorted(status for status, _ in outcomes) == ["409", "ok"]
    count = run_transaction(
        identity.session_factory,
        lambda session: session.execute(
            select(func.count())
            .select_from(users)
            .join(system_role_grants, system_role_grants.c.user_id == users.c.user_id)
            .where(
                users.c.enabled.is_(True),
                system_role_grants.c.revoked_at.is_(None),
            )
        ).scalar_one(),
    )
    assert count == 1


def test_membership_cas_and_policy_current_pointer_have_one_winner(
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
        slug="race-team",
        display_name="Race Team",
        idempotency_key="create-tenant-race1",
        request_hash="sha256:" + "a" * 64,
    )
    user = admin.create_user(
        principal,
        username="race-user@example.test",
        display_name="Race User",
        password="race-user-password",
        system_roles=set(),
        idempotency_key="create-user-race01",
        request_hash="sha256:" + "b" * 64,
    )
    tenant_id = UUID(tenant["tenant_id"])
    user_id = UUID(user["user_id"])

    membership_barrier = Barrier(2, timeout=5)

    def membership(role: str, suffix: str):
        membership_barrier.wait()
        return admin.upsert_membership(
            principal,
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            expected_version=1,
            idempotency_key=f"membership-race-{suffix}",
            request_hash="sha256:" + suffix * 64,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_outcome, lambda: membership("MEMBER", "c")),
            pool.submit(_outcome, lambda: membership("TENANT_ADMIN", "d")),
        ]
        membership_outcomes = [future.result(timeout=10) for future in futures]
    assert sorted(status for status, _ in membership_outcomes) == ["412", "ok"]

    policy_barrier = Barrier(2, timeout=5)

    def update_policy(limit: int, suffix: str):
        policy_barrier.wait()
        return policy.update_tenant_policy(
            principal,
            tenant_id=tenant_id,
            expected_version=1,
            changes={"outstanding_limit": limit},
            idempotency_key=f"policy-race-{suffix:0>8}",
            request_hash="sha256:" + suffix * 64,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_outcome, lambda: update_policy(100, "e")),
            pool.submit(_outcome, lambda: update_policy(200, "f")),
        ]
        policy_outcomes = [future.result(timeout=10) for future in futures]
    assert sorted(status for status, _ in policy_outcomes) == ["412", "ok"]
    current_count = run_transaction(
        identity.session_factory,
        lambda session: session.execute(
            select(func.count())
            .select_from(tenant_policies)
            .where(
                tenant_policies.c.tenant_id == tenant_id,
                tenant_policies.c.is_current.is_(True),
            )
        ).scalar_one(),
    )
    assert current_count == 1


def test_last_admin_and_worker_same_key_races_preserve_invariants(
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
    initial = admin.list_users(principal, page_size=50, cursor=None)["items"][0]
    second = admin.create_user(
        principal,
        username="second-race-admin@example.test",
        display_name="Second Race Admin",
        password="second-race-password",
        system_roles={"SYSTEM_ADMIN"},
        idempotency_key="create-user-race02",
        request_hash="sha256:" + "1" * 64,
    )
    admin_barrier = Barrier(2, timeout=5)

    def disable(user_id: str, suffix: str):
        admin_barrier.wait()
        return admin.update_user(
            principal,
            user_id=UUID(user_id),
            enabled=False,
            expected_version=1,
            idempotency_key=f"disable-race-{suffix:0>8}",
            request_hash="sha256:" + suffix * 64,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_outcome, lambda: disable(initial["user_id"], "2")),
            pool.submit(_outcome, lambda: disable(second["user_id"], "3")),
        ]
        outcomes = [future.result(timeout=10) for future in futures]
    assert sum(status == "ok" for status, _ in outcomes) == 1
    enabled_admins = run_transaction(
        identity.session_factory,
        lambda session: session.execute(
            select(func.count())
            .select_from(users)
            .join(system_role_grants, system_role_grants.c.user_id == users.c.user_id)
            .where(
                users.c.enabled.is_(True),
                system_role_grants.c.revoked_at.is_(None),
            )
        ).scalar_one(),
    )
    assert enabled_admins == 1

    worker_barrier = Barrier(2, timeout=5)

    def worker_bootstrap():
        worker_barrier.wait()
        return identity.bootstrap_worker(
            bootstrap_secret=b"b" * 32,
            source_allowed=True,
            installation_id=INSTALLATION_ID,
            credential_public_fingerprint=FINGERPRINT,
            idempotency_key="worker-race-same01",
            request_hash="sha256:" + "4" * 64,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker_outcomes = list(pool.map(lambda _: _outcome(worker_bootstrap), range(2)))
    assert sorted(status for status, _ in worker_outcomes) == ["409", "ok"]
    current_credentials = run_transaction(
        identity.session_factory,
        lambda session: session.execute(
            select(func.count())
            .select_from(worker_credentials)
            .where(worker_credentials.c.revoked_at.is_(None))
        ).scalar_one(),
    )
    assert current_credentials == 1


def test_disable_waits_for_inflight_token_creation_then_revokes_every_credential(
    migrated_postgres_engine, tmp_path
) -> None:
    identity = make_identity_service(migrated_postgres_engine, tmp_path)
    bootstrap_admin(identity)
    admin_login = identity.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.1",
    )
    admin_principal = identity.resolve_browser_session(admin_login.cookie).principal
    admin = AdminService(identity.session_factory, identity.settings, identity)
    user = admin.create_user(
        admin_principal,
        username="disable-race-user@example.test",
        display_name="Disable Race User",
        password="disable-race-password",
        system_roles=set(),
        idempotency_key="create-user-race03",
        request_hash="sha256:" + "5" * 64,
    )
    user_id = UUID(user["user_id"])
    user_login = identity.login(
        username="disable-race-user@example.test",
        password="disable-race-password",
        source="127.0.0.2",
    )
    user_principal = identity.resolve_browser_session(user_login.cookie).principal
    token_insert_attempted = Event()
    allow_token_insert = Event()
    disable_lock_attempted = Event()

    def observe_race(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("insert into cli_tokens"):
            token_insert_attempted.set()
            assert allow_token_insert.wait(timeout=5)
        if (
            "from users" in normalized
            and "where users.user_id =" in normalized
            and "for update" in normalized
        ):
            disable_lock_attempted.set()

    event.listen(migrated_postgres_engine, "before_cursor_execute", observe_race)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            token_future = pool.submit(
                identity.create_cli_token,
                user_principal,
                name="racing-token",
                scopes={TokenScope.TOKENS_WRITE},
                expires_in_seconds=300,
                idempotency_key="create-token-race01",
                request_hash="sha256:" + "6" * 64,
            )
            assert token_insert_attempted.wait(timeout=5)
            disable_future = pool.submit(
                admin.update_user,
                admin_principal,
                user_id=user_id,
                enabled=False,
                expected_version=1,
                idempotency_key="disable-user-race01",
                request_hash="sha256:" + "7" * 64,
            )
            assert disable_lock_attempted.wait(timeout=5)
            assert not disable_future.done()
            allow_token_insert.set()
            created = token_future.result(timeout=5)
            disabled = disable_future.result(timeout=5)
    finally:
        allow_token_insert.set()
        event.remove(migrated_postgres_engine, "before_cursor_execute", observe_race)

    assert disabled["enabled"] is False
    durable = run_transaction(
        identity.session_factory,
        lambda session: (
            session.execute(
                select(cli_tokens.c.revoked_at).where(
                    cli_tokens.c.token_id == UUID(created["token_id"])
                )
            ).scalar_one(),
            session.execute(
                select(browser_sessions.c.revoked_at).where(
                    browser_sessions.c.browser_session_id == UUID(user_principal.credential_id)
                )
            ).scalar_one(),
        ),
    )
    assert durable[0] is not None
    assert durable[1] is not None


def test_system_admin_revoke_linearizes_after_inflight_privileged_mutation(
    migrated_postgres_engine, tmp_path
) -> None:
    identity = make_identity_service(migrated_postgres_engine, tmp_path)
    bootstrap_admin(identity)
    first_login = identity.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.1",
    )
    first_principal = identity.resolve_browser_session(first_login.cookie).principal
    admin = AdminService(identity.session_factory, identity.settings, identity)
    second = admin.create_user(
        first_principal,
        username="mutation-race-admin@example.test",
        display_name="Mutation Race Admin",
        password="mutation-race-password",
        system_roles={"SYSTEM_ADMIN"},
        idempotency_key="create-user-race04",
        request_hash="sha256:" + "8" * 64,
    )
    second_id = UUID(second["user_id"])
    second_login = identity.login(
        username="mutation-race-admin@example.test",
        password="mutation-race-password",
        source="127.0.0.2",
    )
    second_principal = identity.resolve_browser_session(second_login.cookie).principal
    tenant_insert_attempted = Event()
    allow_tenant_insert = Event()
    revoke_lock_attempted = Event()

    def observe_race(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("insert into tenants"):
            tenant_insert_attempted.set()
            assert allow_tenant_insert.wait(timeout=5)
        if (
            "from users" in normalized
            and "where users.user_id =" in normalized
            and "for update" in normalized
        ):
            revoke_lock_attempted.set()

    event.listen(migrated_postgres_engine, "before_cursor_execute", observe_race)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            mutation_future = pool.submit(
                admin.create_tenant,
                second_principal,
                slug="linearized-before-revoke",
                display_name="Linearized Before Revoke",
                idempotency_key="create-tenant-race3",
                request_hash="sha256:" + "9" * 64,
            )
            assert tenant_insert_attempted.wait(timeout=5)
            revoke_future = pool.submit(
                admin.update_user,
                first_principal,
                user_id=second_id,
                system_roles=set(),
                expected_version=1,
                idempotency_key="revoke-admin-race1",
                request_hash="sha256:" + "a" * 64,
            )
            assert revoke_lock_attempted.wait(timeout=5)
            assert not revoke_future.done()
            allow_tenant_insert.set()
            tenant = mutation_future.result(timeout=5)
            revoked = revoke_future.result(timeout=5)
    finally:
        allow_tenant_insert.set()
        event.remove(migrated_postgres_engine, "before_cursor_execute", observe_race)

    assert tenant["slug"] == "linearized-before-revoke"
    assert revoked["system_roles"] == []
    with pytest.raises(ApplicationError) as no_longer_admin:
        admin.create_tenant(
            second_principal,
            slug="must-not-commit-after-revoke",
            display_name="Must Not Commit",
            idempotency_key="create-tenant-race4",
            request_hash="sha256:" + "b" * 64,
        )
    assert no_longer_admin.value.status in {401, 403}
