from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event
from uuid import UUID

import pytest
from sqlalchemy import event, select, text, update

from nexa.application.admin_service import AdminService
from nexa.application.errors import ApplicationError
from nexa.application.identity_service import IdentityService
from nexa.config import load_settings
from nexa.domain.identity import TokenScope
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.locking import transaction_timestamp
from nexa.infrastructure.persistence.schema import (
    auth_control,
    browser_sessions,
    cli_tokens,
    login_rate_limits,
    policy_versions,
    worker_credentials,
)
from nexa.infrastructure.persistence.transactions import run_transaction

pytestmark = pytest.mark.postgres

INSTALLATION_ID = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f1")
WORKER_ID = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f2")
FINGERPRINT = "sha256:" + "a" * 64


def _service(migrated_postgres_engine, tmp_path: Path) -> IdentityService:
    server_secret = tmp_path / "server-secret"
    bootstrap_secret = tmp_path / "bootstrap-secret"
    server_secret.write_bytes(b"s" * 32)
    bootstrap_secret.write_bytes(b"b" * 32)
    settings = load_settings(
        {
            "NEXA_ENVIRONMENT": "test",
            "NEXA_DATABASE_URL": str(migrated_postgres_engine.url).replace("***", "unused"),
            "NEXA_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            "NEXA_PUBLIC_ORIGIN": "https://nexa.test",
            "NEXA_SERVER_SECRET_FILE": str(server_secret),
            "NEXA_BOOTSTRAP_SECRET_FILE": str(bootstrap_secret),
            "NEXA_INSTALLATION_ID": str(INSTALLATION_ID),
            "NEXA_LOCAL_WORKER_ID": str(WORKER_ID),
            "NEXA_LOCAL_WORKER_FINGERPRINT": FINGERPRINT,
            "NEXA_MAINTENANCE_CIDRS": "127.0.0.0/8",
        }
    )
    settings = replace(settings, database_url="postgresql+psycopg://redacted/redacted")
    service = IdentityService(create_session_factory(migrated_postgres_engine), settings)
    service.initialize()
    return service


def test_admin_bootstrap_is_atomic_replayable_and_permanently_latched(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)

    created = service.bootstrap_admin(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        username="Admin@Example.Test",
        display_name="Initial Admin",
        password="correct-horse-battery-staple",
        idempotency_key="bootstrap-admin-0001",
        request_hash="sha256:" + "a" * 64,
    )

    replay = service.bootstrap_admin(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        username="Admin@Example.Test",
        display_name="Initial Admin",
        password="correct-horse-battery-staple",
        idempotency_key="bootstrap-admin-0001",
        request_hash="sha256:" + "a" * 64,
    )
    assert replay == created
    assert created["user_id"] == str(UUID(created["user_id"]))
    assert created["username"] == "admin@example.test"
    assert created["system_roles"] == ["SYSTEM_ADMIN"]

    restarted = IdentityService(service.session_factory, service.settings)
    restarted.initialize()
    with pytest.raises(ApplicationError) as closed:
        restarted.bootstrap_admin(
            bootstrap_secret=b"b" * 32,
            source_allowed=True,
            username="second@example.test",
            display_name="Second Admin",
            password="correct-horse-battery-staple",
            idempotency_key="bootstrap-admin-0002",
            request_hash="sha256:" + "b" * 64,
        )
    assert closed.value.status == 409


def test_initialize_cannot_bind_deployment_identity_while_write_frozen(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    before_version = run_transaction(
        service.session_factory,
        lambda session: (
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
            ),
            session.execute(
                update(policy_versions)
                .where(policy_versions.c.is_current.is_(True))
                .values(operational_mode="WRITE_FROZEN")
            ),
            session.execute(
                select(auth_control.c.version).where(auth_control.c.singleton_key == "auth")
            ).scalar_one(),
        )[-1],
    )

    with pytest.raises(ApplicationError) as frozen:
        IdentityService(service.session_factory, service.settings).initialize()
    assert frozen.value.status == 409

    durable = run_transaction(
        service.session_factory,
        lambda session: session.execute(
            select(
                auth_control.c.installation_id,
                auth_control.c.local_worker_id,
                auth_control.c.version,
            ).where(auth_control.c.singleton_key == "auth")
        ).one(),
    )
    assert durable == (None, None, before_version)


def test_login_session_csrf_and_cli_token_survive_service_restart(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    service.bootstrap_admin(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        username="admin@example.test",
        display_name="Initial Admin",
        password="correct-horse-battery-staple",
        idempotency_key="bootstrap-admin-0001",
        request_hash="sha256:" + "a" * 64,
    )
    login = service.login(
        username="ADMIN@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.1",
    )
    authenticated = service.resolve_browser_session(login.cookie)
    assert authenticated.csrf_token == login.session["csrf_token"]
    assert authenticated.principal.system_admin is True

    created = service.create_cli_token(
        authenticated.principal,
        name="admin-cli",
        scopes={TokenScope.TOKENS_WRITE, TokenScope.ADMIN_READ},
        expires_in_seconds=300,
        idempotency_key="create-cli-token-01",
        request_hash="sha256:" + "c" * 64,
    )
    assert "token" in created

    restarted = IdentityService(service.session_factory, service.settings)
    restarted.initialize()
    after_restart = restarted.resolve_browser_session(login.cookie)
    assert after_restart.csrf_token == login.session["csrf_token"]
    cli_principal = restarted.resolve_cli_token(created["token"])
    assert cli_principal.has_scope(TokenScope.ADMIN_READ)
    assert cli_principal.has_scope(TokenScope.ADMIN_WRITE) is False

    with pytest.raises(ApplicationError) as replay:
        restarted.create_cli_token(
            after_restart.principal,
            name="admin-cli",
            scopes={TokenScope.TOKENS_WRITE, TokenScope.ADMIN_READ},
            expires_in_seconds=300,
            idempotency_key="create-cli-token-01",
            request_hash="sha256:" + "c" * 64,
        )
    assert replay.value.code == "one_time_secret_unavailable"

    restarted.revoke_cli_token(
        after_restart.principal,
        created["token_id"],
        idempotency_key="revoke-cli-token-1",
        request_hash="sha256:" + "d" * 64,
    )
    with pytest.raises(ApplicationError) as revoked:
        restarted.resolve_cli_token(created["token"])
    assert revoked.value.status == 401


def test_idle_expired_browser_principal_cannot_commit_a_later_mutation(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    service.bootstrap_admin(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        username="admin@example.test",
        display_name="Initial Admin",
        password="correct-horse-battery-staple",
        idempotency_key="bootstrap-admin-0001",
        request_hash="sha256:" + "a" * 64,
    )
    login = service.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.1",
    )
    principal = service.resolve_browser_session(login.cookie).principal

    def expire_idle(session) -> None:
        now = transaction_timestamp(session)
        session.execute(
            update(browser_sessions)
            .where(browser_sessions.c.browser_session_id == UUID(principal.credential_id))
            .values(
                last_seen_at=now
                - timedelta(seconds=service.settings.browser_session_idle_ttl_seconds + 1)
            )
        )

    run_transaction(service.session_factory, expire_idle)

    with pytest.raises(ApplicationError) as expired:
        service.create_cli_token(
            principal,
            name="must-not-exist",
            scopes={TokenScope.TOKENS_WRITE},
            expires_in_seconds=300,
            idempotency_key="idle-expired-token1",
            request_hash="sha256:" + "b" * 64,
        )
    assert expired.value.status == 401
    token_count = run_transaction(
        service.session_factory,
        lambda session: session.execute(select(cli_tokens.c.token_id)).all(),
    )
    assert token_count == []


def test_failed_login_attempts_are_durable_and_rate_limited(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    service.bootstrap_admin(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        username="admin@example.test",
        display_name="Initial Admin",
        password="correct-horse-battery-staple",
        idempotency_key="bootstrap-admin-0001",
        request_hash="sha256:" + "a" * 64,
    )

    for _ in range(10):
        with pytest.raises(ApplicationError) as invalid:
            service.login(
                username="admin@example.test",
                password="wrong-password-value",
                source="127.0.0.1",
            )
        assert invalid.value.status == 401

    restarted = IdentityService(service.session_factory, service.settings)
    restarted.initialize()
    with pytest.raises(ApplicationError) as limited:
        restarted.login(
            username="admin@example.test",
            password="correct-horse-battery-staple",
            source="127.0.0.1",
        )
    assert limited.value.status == 429
    assert limited.value.retry_after is not None


def test_frozen_login_does_not_write_rate_metadata_for_forbidden_callers(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    service.bootstrap_admin(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        username="admin@example.test",
        display_name="Initial Admin",
        password="correct-horse-battery-staple",
        idempotency_key="frozen-login-admin",
        request_hash="sha256:" + "a" * 64,
    )
    admin = AdminService(service.session_factory, service.settings, service)
    admin_principal = service.resolve_browser_session(
        service.login(
            username="admin@example.test",
            password="correct-horse-battery-staple",
            source="127.0.0.1",
        ).cookie
    ).principal
    admin.create_user(
        admin_principal,
        username="member@example.test",
        display_name="Member",
        password="member-password",
        system_roles=set(),
        idempotency_key="frozen-login-member",
        request_hash="sha256:" + "b" * 64,
    )
    disabled = admin.create_user(
        admin_principal,
        username="disabled@example.test",
        display_name="Disabled",
        password="disabled-password",
        system_roles=set(),
        idempotency_key="frozen-login-disabled",
        request_hash="sha256:" + "c" * 64,
    )
    admin.update_user(
        admin_principal,
        user_id=UUID(disabled["user_id"]),
        enabled=False,
        expected_version=1,
        idempotency_key="frozen-login-disable",
        request_hash="sha256:" + "d" * 64,
    )
    before_freeze = run_transaction(
        service.session_factory,
        lambda session: session.execute(select(login_rate_limits)).mappings().all(),
    )
    run_transaction(
        service.session_factory,
        lambda session: session.execute(
            update(policy_versions)
            .where(policy_versions.c.is_current.is_(True))
            .values(operational_mode="WRITE_FROZEN")
        ),
    )

    with pytest.raises(ApplicationError) as unknown:
        service.login(username="unknown@example.test", password="wrong", source="127.0.0.9")
    assert unknown.value.status == 401
    count = run_transaction(
        service.session_factory,
        lambda session: session.execute(select(login_rate_limits)).mappings().all(),
    )
    assert count == before_freeze

    with pytest.raises(ApplicationError) as disabled_login:
        service.login(username="disabled@example.test", password="wrong", source="127.0.0.12")
    assert disabled_login.value.status == 401
    count_disabled = run_transaction(
        service.session_factory,
        lambda session: session.execute(select(login_rate_limits)).mappings().all(),
    )
    assert count_disabled == before_freeze

    with pytest.raises(ApplicationError) as allowed_non_admin:
        service.login(username="member@example.test", password="wrong", source="127.0.0.10")
    assert allowed_non_admin.value.status == 401
    count_after = run_transaction(
        service.session_factory,
        lambda session: session.execute(select(login_rate_limits)).mappings().all(),
    )
    assert count_after == before_freeze

    service.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.11",
    )
    count_for_admin = run_transaction(
        service.session_factory,
        lambda session: session.execute(select(login_rate_limits)).mappings().all(),
    )
    assert len(count_for_admin) == len(before_freeze) + 1


def test_login_rate_limit_has_bounded_buckets_and_prunes_stale_rows(
    migrated_postgres_engine, tmp_path, monkeypatch
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    monkeypatch.setattr(service, "_LOGIN_RATE_MAX_BUCKETS", 3)
    for index in range(3):
        assert (
            service._record_login_attempt(f"unknown-{index}@example.test", f"127.0.0.{index + 1}")
            is None
        )
    with pytest.raises(ApplicationError) as capped:
        service._record_login_attempt("unknown-3@example.test", "127.0.0.4")
    assert capped.value.status == 429
    count = run_transaction(
        service.session_factory,
        lambda session: session.execute(select(login_rate_limits)).mappings().all(),
    )
    assert len(count) == 3

    run_transaction(
        service.session_factory,
        lambda session: session.execute(
            update(login_rate_limits).values(
                updated_at=transaction_timestamp(session) - timedelta(hours=2)
            )
        ),
    )
    assert service._record_login_attempt("fresh@example.test", "127.0.0.5") is None
    remaining = run_transaction(
        service.session_factory,
        lambda session: session.execute(select(login_rate_limits.c.username)).scalars().all(),
    )
    assert remaining == ["fresh@example.test"]


def _lock_and_expire(
    engine,
    statement,
    expire_statement,
    operation,
) -> tuple[int, str]:
    blocker = engine.connect()
    transaction = blocker.begin()
    blocker.execute(statement)
    started = Event()
    release = Event()

    def pause_after_lock_request(_conn, _cursor, sql, _parameters, _context, _many):
        normalized = " ".join(sql.lower().split())
        if "for update" in normalized and (
            "from browser_sessions" in normalized
            or "from cli_tokens" in normalized
            or "from auth_control" in normalized
            or "from worker_credentials" in normalized
        ):
            started.set()
            if not release.wait(5):
                raise RuntimeError("expiry probe did not receive its release")

    event.listen(engine, "before_cursor_execute", pause_after_lock_request)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(operation)
            assert started.wait(5)
            blocker.execute(expire_statement)
            transaction.commit()
            release.set()
            return future.result(timeout=10)
    finally:
        event.remove(engine, "before_cursor_execute", pause_after_lock_request)
        blocker.close()


def test_browser_session_expiry_is_checked_after_row_lock(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    service.bootstrap_admin(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        username="admin@example.test",
        display_name="Initial Admin",
        password="correct-horse-battery-staple",
        idempotency_key="expiry-browser-bootstrap",
        request_hash="sha256:" + "a" * 64,
    )
    login = service.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.1",
    )
    session_id = login.session["session_id"]
    outcome = _lock_and_expire(
        migrated_postgres_engine,
        select(browser_sessions.c.browser_session_id)
        .where(browser_sessions.c.browser_session_id == session_id)
        .with_for_update(),
        update(browser_sessions)
        .where(browser_sessions.c.browser_session_id == session_id)
        .values(expires_at=text("CURRENT_TIMESTAMP - INTERVAL '1 second'")),
        lambda: (
            (
                200,
                "ok",
            )
            if _resolve_without_error(service, login.cookie)
            else (401, "authentication_required")
        ),
    )
    assert outcome == (401, "authentication_required")


def _resolve_without_error(service: IdentityService, cookie: str) -> bool:
    try:
        service.resolve_browser_session(cookie)
    except ApplicationError as exc:
        if exc.status == 401:
            return False
        raise
    return True


def test_cli_token_expiry_is_checked_after_row_lock(migrated_postgres_engine, tmp_path) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    service.bootstrap_admin(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        username="admin@example.test",
        display_name="Initial Admin",
        password="correct-horse-battery-staple",
        idempotency_key="expiry-cli-bootstrap",
        request_hash="sha256:" + "a" * 64,
    )
    principal = service.resolve_browser_session(
        service.login(
            username="admin@example.test",
            password="correct-horse-battery-staple",
            source="127.0.0.1",
        ).cookie
    ).principal
    token = service.create_cli_token(
        principal,
        name="expiry-probe",
        scopes={TokenScope.TOKENS_WRITE},
        expires_in_seconds=300,
        idempotency_key="expiry-cli-token",
        request_hash="sha256:" + "b" * 64,
    )
    token_id = UUID(token["token_id"])
    outcome = _lock_and_expire(
        migrated_postgres_engine,
        select(cli_tokens.c.token_id).where(cli_tokens.c.token_id == token_id).with_for_update(),
        update(cli_tokens)
        .where(cli_tokens.c.token_id == token_id)
        .values(expires_at=text("CURRENT_TIMESTAMP - INTERVAL '1 second'")),
        lambda: _resolve_cli_without_error(service, token["token"]),
    )
    assert outcome == (401, "authentication_required")


def _resolve_cli_without_error(service: IdentityService, token: str) -> tuple[int, str]:
    try:
        service.resolve_cli_token(token)
    except ApplicationError as exc:
        return exc.status, exc.code
    return 200, "ok"


def test_worker_credential_expiry_is_checked_after_row_lock(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    created = service.bootstrap_worker(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        installation_id=INSTALLATION_ID,
        credential_public_fingerprint=FINGERPRINT,
        idempotency_key="expiry-worker-credential",
        request_hash="sha256:" + "e" * 64,
    )
    credential_id = UUID(created["credential_id"])
    outcome = _lock_and_expire(
        migrated_postgres_engine,
        select(worker_credentials.c.credential_id)
        .where(worker_credentials.c.credential_id == credential_id)
        .with_for_update(),
        update(worker_credentials)
        .where(worker_credentials.c.credential_id == credential_id)
        .values(expires_at=text("CURRENT_TIMESTAMP - INTERVAL '1 second'")),
        lambda: _resolve_worker_without_error(service, created["credential"]),
    )
    assert outcome == (401, "authentication_required")


def _resolve_worker_without_error(service: IdentityService, credential: str) -> tuple[int, str]:
    try:
        service.resolve_worker_credential(credential)
    except ApplicationError as exc:
        return exc.status, exc.code
    return 200, "ok"


def test_admin_bootstrap_window_is_checked_after_control_lock(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    outcome = _lock_and_expire(
        migrated_postgres_engine,
        select(auth_control.c.singleton_key)
        .where(auth_control.c.singleton_key == "auth")
        .with_for_update(),
        update(auth_control)
        .where(auth_control.c.singleton_key == "auth")
        .values(
            admin_window_opened_at=text("CURRENT_TIMESTAMP - INTERVAL '2 seconds'"),
            admin_window_expires_at=text("CURRENT_TIMESTAMP - INTERVAL '1 second'"),
        ),
        lambda: _bootstrap_admin_without_error(service),
    )
    assert outcome == (403, "permission_denied")


def _bootstrap_admin_without_error(service: IdentityService) -> tuple[int, str]:
    try:
        service.bootstrap_admin(
            bootstrap_secret=b"b" * 32,
            source_allowed=True,
            username="admin@example.test",
            display_name="Initial Admin",
            password="correct-horse-battery-staple",
            idempotency_key="expiry-admin-bootstrap",
            request_hash="sha256:" + "c" * 64,
        )
    except ApplicationError as exc:
        return exc.status, exc.code
    return 201, "ok"


def test_worker_bootstrap_window_is_checked_after_control_lock(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    outcome = _lock_and_expire(
        migrated_postgres_engine,
        select(auth_control.c.singleton_key)
        .where(auth_control.c.singleton_key == "auth")
        .with_for_update(),
        update(auth_control)
        .where(auth_control.c.singleton_key == "auth")
        .values(
            worker_window_opened_at=text("CURRENT_TIMESTAMP - INTERVAL '2 seconds'"),
            worker_window_expires_at=text("CURRENT_TIMESTAMP - INTERVAL '1 second'"),
        ),
        lambda: _bootstrap_worker_without_error(service),
    )
    assert outcome == (403, "permission_denied")


def _bootstrap_worker_without_error(service: IdentityService) -> tuple[int, str]:
    try:
        service.bootstrap_worker(
            bootstrap_secret=b"b" * 32,
            source_allowed=True,
            installation_id=INSTALLATION_ID,
            credential_public_fingerprint=FINGERPRINT,
            idempotency_key="expiry-worker-bootstrap",
            request_hash="sha256:" + "d" * 64,
        )
    except ApplicationError as exc:
        return exc.status, exc.code
    return 201, "ok"


def test_first_concurrent_login_attempts_serialize_without_losing_rate_charge(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    service.bootstrap_admin(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        username="admin@example.test",
        display_name="Initial Admin",
        password="correct-horse-battery-staple",
        idempotency_key="bootstrap-admin-0001",
        request_hash="sha256:" + "a" * 64,
    )
    service = IdentityService(
        service.session_factory,
        replace(service.settings, login_rate_per_minute=1),
    )
    first_read = Barrier(2, timeout=5)

    def synchronize_first_insert(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("insert into login_rate_limits"):
            first_read.wait()

    event.listen(migrated_postgres_engine, "before_cursor_execute", synchronize_first_insert)

    def attempt() -> tuple[int | str, str]:
        try:
            service.login(
                username="admin@example.test",
                password="wrong-password-value",
                source="127.0.0.1",
            )
        except ApplicationError as exc:
            return exc.status, exc.code
        except Exception as exc:  # The RED state exposes the database race here.
            return type(exc).__name__, "unexpected"
        return 200, "ok"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt) for _ in range(2)]
            outcomes = [future.result(timeout=10) for future in futures]
    finally:
        event.remove(migrated_postgres_engine, "before_cursor_execute", synchronize_first_insert)

    assert outcomes.count((401, "authentication_required")) == 1
    assert outcomes.count((429, "rate_limited")) == 1
    attempts = run_transaction(
        service.session_factory,
        lambda session: session.execute(
            select(login_rate_limits.c.attempts).where(
                login_rate_limits.c.username == "admin@example.test"
            )
        ).scalar_one(),
    )
    assert attempts == 1


def test_worker_bootstrap_rotates_unknown_current_credential_without_ready_state(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)

    first = service.bootstrap_worker(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        installation_id=INSTALLATION_ID,
        credential_public_fingerprint=FINGERPRINT,
        idempotency_key="worker-bootstrap-01",
        request_hash="sha256:" + "e" * 64,
    )
    second = service.bootstrap_worker(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        installation_id=INSTALLATION_ID,
        credential_public_fingerprint=FINGERPRINT,
        idempotency_key="worker-bootstrap-02",
        request_hash="sha256:" + "f" * 64,
    )

    assert first["worker_id"] == second["worker_id"] == str(WORKER_ID)
    assert first["credential"] != second["credential"]
    with pytest.raises(ApplicationError):
        service.resolve_worker_credential(first["credential"])
    assert service.resolve_worker_credential(second["credential"]) == WORKER_ID
    worker = service.get_worker_bootstrap_state()
    assert worker == {"health": "STARTING", "current_incarnation_id": None, "ready_at": None}


def test_logout_requires_session_bound_csrf_and_revokes_the_cookie(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    service.bootstrap_admin(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        username="admin@example.test",
        display_name="Initial Admin",
        password="correct-horse-battery-staple",
        idempotency_key="bootstrap-admin-0001",
        request_hash="sha256:" + "a" * 64,
    )
    first = service.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.1",
    )
    second = service.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.2",
    )

    with pytest.raises(ApplicationError) as cross_session:
        service.logout(first.cookie, second.session["csrf_token"])
    assert cross_session.value.status == 403

    service.logout(first.cookie, first.session["csrf_token"])
    with pytest.raises(ApplicationError) as revoked:
        service.resolve_browser_session(first.cookie)
    assert revoked.value.status == 401
    assert service.resolve_browser_session(second.cookie).principal.system_admin is True


def test_cli_token_list_returns_only_metadata_with_actor_bound_cursor(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    service.bootstrap_admin(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        username="admin@example.test",
        display_name="Initial Admin",
        password="correct-horse-battery-staple",
        idempotency_key="bootstrap-admin-0001",
        request_hash="sha256:" + "a" * 64,
    )
    login = service.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.1",
    )
    principal = service.resolve_browser_session(login.cookie).principal
    for index, name in enumerate(("first", "second"), start=1):
        service.create_cli_token(
            principal,
            name=name,
            scopes={TokenScope.TOKENS_WRITE},
            expires_in_seconds=300,
            idempotency_key=f"create-cli-token-{index:02d}",
            request_hash="sha256:" + str(index) * 64,
        )

    first_page = service.list_cli_tokens(principal, page_size=1, cursor=None)
    assert len(first_page["items"]) == 1
    assert first_page["items"][0]["name"] == "second"
    assert "token" not in first_page["items"][0]
    assert first_page["page"]["next_cursor"] is not None

    second_page = service.list_cli_tokens(
        principal,
        page_size=1,
        cursor=first_page["page"]["next_cursor"],
    )
    assert [item["name"] for item in second_page["items"]] == ["first"]
    assert second_page["page"] == {"next_cursor": None, "page_size": 1}


def test_cli_token_list_requires_the_exact_tokens_write_scope(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    service.bootstrap_admin(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        username="admin@example.test",
        display_name="Initial Admin",
        password="correct-horse-battery-staple",
        idempotency_key="bootstrap-admin-0001",
        request_hash="sha256:" + "a" * 64,
    )
    login = service.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.1",
    )
    browser = service.resolve_browser_session(login.cookie).principal
    created = service.create_cli_token(
        browser,
        name="admin-read-only",
        scopes={TokenScope.ADMIN_READ},
        expires_in_seconds=300,
        idempotency_key="create-cli-token-read",
        request_hash="sha256:" + "b" * 64,
    )
    cli = service.resolve_cli_token(created["token"])

    with pytest.raises(ApplicationError) as denied:
        service.list_cli_tokens(cli, page_size=50, cursor=None)

    assert denied.value.status == 403


def test_session_idle_absolute_and_token_expiry_are_authoritative(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    service.bootstrap_admin(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        username="admin@example.test",
        display_name="Initial Admin",
        password="correct-horse-battery-staple",
        idempotency_key="bootstrap-admin-0001",
        request_hash="sha256:" + "a" * 64,
    )
    idle_login = service.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.1",
    )
    idle_session_id = idle_login.session["session_id"]

    def expire_idle(session):
        now = transaction_timestamp(session)
        session.execute(
            update(browser_sessions)
            .where(browser_sessions.c.browser_session_id == idle_session_id)
            .values(
                last_seen_at=now
                - timedelta(seconds=service.settings.browser_session_idle_ttl_seconds)
            )
        )

    run_transaction(service.session_factory, expire_idle)
    with pytest.raises(ApplicationError):
        service.resolve_browser_session(idle_login.cookie)

    absolute_login = service.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.2",
    )
    absolute_session_id = absolute_login.session["session_id"]
    run_transaction(
        service.session_factory,
        lambda session: session.execute(
            update(browser_sessions)
            .where(browser_sessions.c.browser_session_id == absolute_session_id)
            .values(expires_at=transaction_timestamp(session))
        ),
    )
    with pytest.raises(ApplicationError):
        service.resolve_browser_session(absolute_login.cookie)

    active = service.login(
        username="admin@example.test",
        password="correct-horse-battery-staple",
        source="127.0.0.3",
    )
    principal = service.resolve_browser_session(active.cookie).principal
    created = service.create_cli_token(
        principal,
        name="short-lived",
        scopes={TokenScope.TOKENS_WRITE},
        expires_in_seconds=300,
        idempotency_key="create-cli-expiry01",
        request_hash="sha256:" + "b" * 64,
    )
    token_id = UUID(created["token_id"])
    stored = run_transaction(
        service.session_factory,
        lambda session: session.execute(
            select(cli_tokens.c.token_hash).where(cli_tokens.c.token_id == token_id)
        ).scalar_one(),
    )
    assert isinstance(stored, bytes)
    assert created["token"].encode() not in stored
    run_transaction(
        service.session_factory,
        lambda session: session.execute(
            update(cli_tokens)
            .where(cli_tokens.c.token_id == token_id)
            .values(expires_at=transaction_timestamp(session))
        ),
    )
    with pytest.raises(ApplicationError):
        service.resolve_cli_token(created["token"])


def test_bootstrap_rejects_wrong_source_and_expired_window(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    with pytest.raises(ApplicationError) as wrong_source:
        service.bootstrap_admin(
            bootstrap_secret=b"b" * 32,
            source_allowed=False,
            username="admin@example.test",
            display_name="Initial Admin",
            password="correct-horse-battery-staple",
            idempotency_key="bootstrap-admin-0001",
            request_hash="sha256:" + "a" * 64,
        )
    assert wrong_source.value.status == 401

    run_transaction(
        service.session_factory,
        lambda session: session.execute(
            update(auth_control)
            .where(auth_control.c.singleton_key == "auth")
            .values(admin_window_expires_at=transaction_timestamp(session))
        ),
    )
    with pytest.raises(ApplicationError) as expired:
        service.bootstrap_admin(
            bootstrap_secret=b"b" * 32,
            source_allowed=True,
            username="admin@example.test",
            display_name="Initial Admin",
            password="correct-horse-battery-staple",
            idempotency_key="bootstrap-admin-0002",
            request_hash="sha256:" + "b" * 64,
        )
    assert expired.value.status == 403


def test_worker_bootstrap_cannot_rotate_credentials_while_writes_are_frozen(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    run_transaction(
        service.session_factory,
        lambda session: session.execute(
            update(policy_versions)
            .where(policy_versions.c.is_current.is_(True))
            .values(operational_mode="WRITE_FROZEN")
        ),
    )

    with pytest.raises(ApplicationError) as frozen:
        service.bootstrap_worker(
            bootstrap_secret=b"b" * 32,
            source_allowed=True,
            installation_id=INSTALLATION_ID,
            credential_public_fingerprint=FINGERPRINT,
            idempotency_key="worker-bootstrap-frozen",
            request_hash="sha256:" + "c" * 64,
        )

    assert frozen.value.status == 409


def test_operator_can_explicitly_reopen_an_expired_worker_bootstrap_window(
    migrated_postgres_engine, tmp_path
) -> None:
    service = _service(migrated_postgres_engine, tmp_path)
    run_transaction(
        service.session_factory,
        lambda session: session.execute(
            update(auth_control)
            .where(auth_control.c.singleton_key == "auth")
            .values(worker_window_expires_at=transaction_timestamp(session))
        ),
    )

    with pytest.raises(ApplicationError) as expired:
        service.bootstrap_worker(
            bootstrap_secret=b"b" * 32,
            source_allowed=True,
            installation_id=INSTALLATION_ID,
            credential_public_fingerprint=FINGERPRINT,
            idempotency_key="worker-bootstrap-expired",
            request_hash="sha256:" + "d" * 64,
        )
    assert expired.value.status == 403

    with pytest.raises(ApplicationError) as wrong_secret:
        service.reopen_worker_bootstrap_window(bootstrap_secret=b"x" * 32)
    assert wrong_secret.value.status == 401

    reopened = service.reopen_worker_bootstrap_window(bootstrap_secret=b"b" * 32)
    assert reopened["expires_at"] > reopened["opened_at"]

    rotated = service.bootstrap_worker(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        installation_id=INSTALLATION_ID,
        credential_public_fingerprint=FINGERPRINT,
        idempotency_key="worker-bootstrap-reopened",
        request_hash="sha256:" + "e" * 64,
    )
    assert rotated["worker_id"] == str(WORKER_ID)
