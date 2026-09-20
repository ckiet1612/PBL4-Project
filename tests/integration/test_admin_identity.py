from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from nexa.application.admin_service import AdminService
from nexa.application.errors import ApplicationError
from nexa.domain.identity import TokenScope
from nexa.infrastructure.persistence.schema import tenant_policies
from nexa.infrastructure.persistence.transactions import run_transaction

from .identity_support import bootstrap_admin, make_identity_service

pytestmark = pytest.mark.postgres


def _admin_context(migrated_postgres_engine, tmp_path):
    identity = make_identity_service(migrated_postgres_engine, tmp_path)
    bootstrap_admin(identity)
    login = identity.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.1",
    )
    principal = identity.resolve_browser_session(login.cookie).principal
    return (
        identity,
        AdminService(identity.session_factory, identity.settings, identity),
        login,
        principal,
    )


def test_tenant_membership_and_policy_are_created_without_implicit_membership(
    migrated_postgres_engine, tmp_path
) -> None:
    identity, admin, _, principal = _admin_context(migrated_postgres_engine, tmp_path)

    tenant = admin.create_tenant(
        principal,
        slug="Research-Team",
        display_name="Research Team",
        idempotency_key="create-tenant-0001",
        request_hash="sha256:" + "b" * 64,
    )
    user = admin.create_user(
        principal,
        username="Member@Example.Test",
        display_name="Member",
        password="member-password-value",
        system_roles=set(),
        idempotency_key="create-user-00001",
        request_hash="sha256:" + "c" * 64,
    )

    assert tenant["slug"] == "research-team"
    assert user["username"] == "member@example.test"
    empty = admin.list_memberships(principal, UUID(tenant["tenant_id"]), page_size=50, cursor=None)
    assert empty["membership_set_version"] == 1
    assert empty["items"] == []
    policy = run_transaction(
        identity.session_factory,
        lambda session: (
            session.execute(
                select(tenant_policies).where(
                    tenant_policies.c.tenant_id == UUID(tenant["tenant_id"]),
                    tenant_policies.c.is_current.is_(True),
                )
            )
            .mappings()
            .one()
        ),
    )
    assert policy["version"] == 1
    assert (policy["cpu_limit_millis"], policy["memory_limit_bytes"], policy["gpu_limit"]) == (
        0,
        0,
        0,
    )

    member_login = identity.login(
        username="member@example.test",
        password="member-password-value",
        source="127.0.0.2",
    )
    assert identity.resolve_browser_session(member_login.cookie).principal.memberships == ()
    membership = admin.upsert_membership(
        principal,
        tenant_id=UUID(tenant["tenant_id"]),
        user_id=UUID(user["user_id"]),
        role="MEMBER",
        expected_version=1,
        idempotency_key="upsert-member-0001",
        request_hash="sha256:" + "d" * 64,
    )
    assert membership.version == 2
    assert membership.body["role"] == "MEMBER"
    with pytest.raises(ApplicationError) as revoked:
        identity.resolve_browser_session(member_login.cookie)
    assert revoked.value.status == 401
    with pytest.raises(ApplicationError) as stale:
        admin.upsert_membership(
            principal,
            tenant_id=UUID(tenant["tenant_id"]),
            user_id=UUID(user["user_id"]),
            role="TENANT_ADMIN",
            expected_version=1,
            idempotency_key="upsert-member-0002",
            request_hash="sha256:" + "e" * 64,
        )
    assert stale.value.status == 412
    assert (
        admin.delete_membership(
            principal,
            tenant_id=UUID(tenant["tenant_id"]),
            user_id=UUID(user["user_id"]),
            expected_version=2,
            idempotency_key="delete-member-0001",
            request_hash="sha256:" + "6" * 64,
        )
        == 3
    )
    after_delete = admin.list_memberships(
        principal, UUID(tenant["tenant_id"]), page_size=50, cursor=None
    )
    assert after_delete["membership_set_version"] == 3
    assert after_delete["items"] == []


def test_last_enabled_system_admin_is_serialized_and_credentials_are_revoked(
    migrated_postgres_engine, tmp_path
) -> None:
    identity, admin, login, principal = _admin_context(migrated_postgres_engine, tmp_path)
    initial = admin.list_users(principal, page_size=50, cursor=None)["items"][0]

    with pytest.raises(ApplicationError) as last_admin:
        admin.update_user(
            principal,
            user_id=UUID(initial["user_id"]),
            enabled=False,
            expected_version=1,
            idempotency_key="disable-admin-0001",
            request_hash="sha256:" + "f" * 64,
        )
    assert last_admin.value.status == 409

    admin.create_user(
        principal,
        username="second-admin@example.test",
        display_name="Second Admin",
        password="second-admin-password",
        system_roles={"SYSTEM_ADMIN"},
        idempotency_key="create-user-00002",
        request_hash="sha256:" + "1" * 64,
    )
    updated = admin.update_user(
        principal,
        user_id=UUID(initial["user_id"]),
        enabled=False,
        expected_version=1,
        idempotency_key="disable-admin-0002",
        request_hash="sha256:" + "2" * 64,
    )
    assert updated["enabled"] is False
    with pytest.raises(ApplicationError):
        identity.resolve_browser_session(login.cookie)


def test_cli_admin_read_scope_does_not_grant_admin_write(
    migrated_postgres_engine, tmp_path
) -> None:
    identity, admin, _, principal = _admin_context(migrated_postgres_engine, tmp_path)
    token = identity.create_cli_token(
        principal,
        name="read-only-admin",
        scopes={TokenScope.ADMIN_READ, TokenScope.TOKENS_WRITE},
        expires_in_seconds=300,
        idempotency_key="create-cli-token-ro",
        request_hash="sha256:" + "3" * 64,
    )
    cli = identity.resolve_cli_token(token["token"])

    assert admin.list_tenants(cli, page_size=50, cursor=None)["items"] == []
    with pytest.raises(ApplicationError) as denied:
        admin.create_tenant(
            cli,
            slug="denied-tenant",
            display_name="Denied",
            idempotency_key="create-tenant-0002",
            request_hash="sha256:" + "4" * 64,
        )
    assert denied.value.status == 403


def test_password_hashing_waits_for_authorization_preconditions_and_replay(
    migrated_postgres_engine, tmp_path, monkeypatch
) -> None:
    identity, admin, _, principal = _admin_context(migrated_postgres_engine, tmp_path)
    calls = 0
    original_hash_password = identity.hash_password

    def counted_hash(password: str) -> str:
        nonlocal calls
        calls += 1
        return original_hash_password(password)

    monkeypatch.setattr(identity, "hash_password", counted_hash)
    token = identity.create_cli_token(
        principal,
        name="read-only-admin",
        scopes={TokenScope.ADMIN_READ, TokenScope.TOKENS_WRITE},
        expires_in_seconds=300,
        idempotency_key="hash-order-token",
        request_hash="sha256:" + "1" * 64,
    )
    read_only = identity.resolve_cli_token(token["token"])
    with pytest.raises(ApplicationError) as denied:
        admin.create_user(
            read_only,
            username="denied@example.test",
            display_name="Denied",
            password="password-value",
            system_roles=set(),
            idempotency_key="hash-order-denied",
            request_hash="sha256:" + "2" * 64,
        )
    assert denied.value.status == 403
    assert calls == 0

    created = admin.create_user(
        principal,
        username="hashed@example.test",
        display_name="Hashed",
        password="password-value",
        system_roles=set(),
        idempotency_key="hash-order-create",
        request_hash="sha256:" + "3" * 64,
    )
    assert calls == 1
    replay = admin.create_user(
        principal,
        username="hashed@example.test",
        display_name="Hashed",
        password="password-value",
        system_roles=set(),
        idempotency_key="hash-order-create",
        request_hash="sha256:" + "3" * 64,
    )
    assert replay == created
    assert calls == 1

    admin.update_user(
        principal,
        user_id=UUID(created["user_id"]),
        display_name="Hashed Updated",
        expected_version=1,
        idempotency_key="hash-order-update",
        request_hash="sha256:" + "4" * 64,
    )
    assert calls == 1
    with pytest.raises(ApplicationError) as stale:
        admin.update_user(
            principal,
            user_id=UUID(created["user_id"]),
            password="new-password-value",
            expected_version=1,
            idempotency_key="hash-order-stale",
            request_hash="sha256:" + "5" * 64,
        )
    assert stale.value.status == 412
    assert calls == 1


def test_tenant_update_replays_before_etag_and_audit_filter_is_actor_bound(
    migrated_postgres_engine, tmp_path
) -> None:
    _, admin, _, principal = _admin_context(migrated_postgres_engine, tmp_path)
    tenant = admin.create_tenant(
        principal,
        slug="operations-team",
        display_name="Operations",
        idempotency_key="create-tenant-0003",
        request_hash="sha256:" + "7" * 64,
    )
    tenant_id = UUID(tenant["tenant_id"])
    updated = admin.update_tenant(
        principal,
        tenant_id=tenant_id,
        display_name="Operations Updated",
        expected_version=1,
        idempotency_key="update-tenant-0001",
        request_hash="sha256:" + "8" * 64,
    )
    replay = admin.update_tenant(
        principal,
        tenant_id=tenant_id,
        display_name="Operations Updated",
        expected_version=1,
        idempotency_key="update-tenant-0001",
        request_hash="sha256:" + "8" * 64,
    )
    assert replay == updated
    assert updated["version"] == 2
    with pytest.raises(ApplicationError) as stale:
        admin.update_tenant(
            principal,
            tenant_id=tenant_id,
            enabled=False,
            expected_version=1,
            idempotency_key="update-tenant-0002",
            request_hash="sha256:" + "9" * 64,
        )
    assert stale.value.status == 412

    page = admin.list_audit_records(
        principal,
        page_size=50,
        cursor=None,
        action="admin.tenant.update",
        from_at=datetime.now(UTC) - timedelta(minutes=5),
        to_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert [item["action"] for item in page["items"]] == ["admin.tenant.update"]
    assert page["items"][0]["target_id"] == str(tenant_id)
