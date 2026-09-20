import hashlib
import hmac
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, func, insert, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from nexa.application.errors import ApplicationError
from nexa.application.idempotency import begin_idempotency, complete_idempotency
from nexa.application.json_codec import json_wire_value
from nexa.config import Settings
from nexa.domain.identity import (
    AuthorizationError,
    CredentialKind,
    Principal,
    TenantMembership,
    TokenScope,
    validate_requested_token_scopes,
)
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import clock_timestamp, transaction_timestamp
from nexa.infrastructure.persistence.schema import (
    audit_records,
    auth_control,
    browser_sessions,
    cli_tokens,
    login_rate_limits,
    memberships,
    policy_versions,
    system_role_grants,
    tenants,
    users,
    worker_credentials,
    workers,
)
from nexa.infrastructure.persistence.transactions import run_transaction
from nexa.infrastructure.security import (
    CsrfCodec,
    CursorCodec,
    CursorError,
    PasswordHasher,
    SecretCodec,
    read_header_secret_file,
    read_secret_file,
)

_AUTHENTICATION_ERROR = ApplicationError(
    code="authentication_required",
    status=401,
    message="Invalid credentials",
)


@dataclass(frozen=True, slots=True)
class LoginResult:
    cookie: str
    session: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    principal: Principal
    session_id: UUID
    csrf_token: str
    expires_at: datetime


class IdentityService:
    _LOGIN_RATE_RETENTION = timedelta(hours=1)
    _LOGIN_RATE_MAX_BUCKETS = 10_000
    _LOGIN_RATE_ADVISORY_LOCK_KEY = 8_420_617_091_337

    def __init__(self, session_factory, settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self._server_secret = read_secret_file(settings.server_secret_file)
        self._bootstrap_secret = read_header_secret_file(settings.bootstrap_secret_file)
        self._passwords = PasswordHasher(
            memory_kib=settings.argon2_memory_kib,
            time_cost=settings.argon2_time_cost,
            parallelism=settings.argon2_parallelism,
            max_concurrency=settings.password_hash_concurrency,
        )
        self._secrets = SecretCodec()
        self._csrf = CsrfCodec(self._server_secret)
        self._dummy_password_hash = self._passwords.hash("nexa-dummy-password-value")

    def initialize(self) -> None:
        def operation(session: Session) -> None:
            mode = session.execute(
                select(policy_versions.c.operational_mode)
                .where(policy_versions.c.is_current.is_(True))
                .with_for_update(read=True)
            ).scalar_one()
            row = (
                session.execute(
                    select(auth_control)
                    .where(auth_control.c.singleton_key == "auth")
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            now = transaction_timestamp(session)
            if row["installation_id"] is None:
                if mode == "WRITE_FROZEN":
                    raise ApplicationError(
                        code="state_conflict",
                        status=409,
                        message="Deployment identity cannot be initialized while writes are frozen",
                    )
                session.execute(
                    update(auth_control)
                    .where(auth_control.c.singleton_key == "auth")
                    .values(
                        installation_id=self.settings.installation_id,
                        local_worker_id=self.settings.local_worker_id,
                        worker_credential_fingerprint=self.settings.local_worker_fingerprint,
                        admin_window_opened_at=now,
                        admin_window_expires_at=now
                        + timedelta(seconds=self.settings.admin_bootstrap_window_seconds),
                        worker_window_opened_at=now,
                        worker_window_expires_at=now
                        + timedelta(seconds=self.settings.worker_bootstrap_window_seconds),
                        updated_at=now,
                        version=auth_control.c.version + 1,
                    )
                )
                return
            configured = (
                row["installation_id"],
                row["local_worker_id"],
                row["worker_credential_fingerprint"],
            )
            expected = (
                self.settings.installation_id,
                self.settings.local_worker_id,
                self.settings.local_worker_fingerprint,
            )
            if configured != expected:
                raise ApplicationError(
                    code="dependency_unavailable",
                    status=503,
                    message="Configured deployment identity does not match durable bootstrap state",
                    retry_after=1,
                )

        run_transaction(self.session_factory, operation)

    def _verify_bootstrap(self, presented: bytes, source_allowed: bool) -> None:
        if not source_allowed or not hmac.compare_digest(presented, self._bootstrap_secret):
            raise _AUTHENTICATION_ERROR

    @staticmethod
    def _normalize_username(username: str) -> str:
        # The frozen B05 index is lower-based; lower the case-fold result to keep it compatible.
        normalized = username.strip().casefold().lower()
        if not 3 <= len(normalized) <= 254:
            raise ApplicationError(
                code="validation_failed",
                status=422,
                message="Username length must be between 3 and 254 characters",
            )
        return normalized

    @staticmethod
    def _audit(
        session: Session,
        *,
        actor_type: str,
        actor_id: str,
        action: str,
        target_type: str,
        target_id: str,
        reason: str,
        tenant_id: UUID | None = None,
        before_version: int | None = None,
        after_version: int | None = None,
        safe_metadata: dict[str, Any] | None = None,
    ) -> None:
        session.execute(
            insert(audit_records).values(
                audit_id=new_uuid7(),
                actor_type=actor_type,
                actor_id=actor_id,
                tenant_id=tenant_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                before_version=before_version,
                after_version=after_version,
                reason=reason,
                safe_metadata=safe_metadata or {},
            )
        )

    @staticmethod
    def _system_admin(session: Session, user_id: UUID) -> bool:
        return (
            session.execute(
                select(system_role_grants.c.user_id).where(
                    system_role_grants.c.user_id == user_id,
                    system_role_grants.c.role == "SYSTEM_ADMIN",
                    system_role_grants.c.revoked_at.is_(None),
                )
            ).scalar_one_or_none()
            is not None
        )

    @staticmethod
    def _memberships(session: Session, user_id: UUID) -> tuple[TenantMembership, ...]:
        rows = session.execute(
            select(memberships.c.tenant_id, memberships.c.role)
            .join(tenants, tenants.c.tenant_id == memberships.c.tenant_id)
            .where(memberships.c.user_id == user_id, tenants.c.enabled.is_(True))
            .order_by(memberships.c.tenant_id)
            .limit(101)
        ).all()
        if len(rows) > 100:
            raise ApplicationError(
                code="dependency_unavailable",
                status=503,
                message="Principal membership context exceeds the API bound",
                retry_after=1,
            )
        return tuple(TenantMembership(tenant_id=row.tenant_id, role=row.role) for row in rows)

    @classmethod
    def _principal(
        cls,
        session: Session,
        *,
        user_id: UUID,
        credential_kind: CredentialKind,
        credential_id: str,
        scopes: frozenset[TokenScope] = frozenset(),
    ) -> Principal:
        return Principal(
            user_id=user_id,
            credential_kind=credential_kind,
            credential_id=credential_id,
            scopes=scopes,
            memberships=cls._memberships(session, user_id),
            system_admin=cls._system_admin(session, user_id),
        )

    @classmethod
    def _user_view(cls, session: Session, row: Any) -> dict[str, Any]:
        return {
            "user_id": row["user_id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "enabled": row["enabled"],
            "system_roles": ["SYSTEM_ADMIN"] if cls._system_admin(session, row["user_id"]) else [],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def bootstrap_admin(
        self,
        *,
        bootstrap_secret: bytes,
        source_allowed: bool,
        username: str,
        display_name: str,
        password: str,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any]:
        self._verify_bootstrap(bootstrap_secret, source_allowed)
        normalized_username = self._normalize_username(username)

        def operation(session: Session) -> dict[str, Any]:
            idempotency_now = transaction_timestamp(session)
            idempotency = begin_idempotency(
                session,
                context="BOOTSTRAP",
                principal_id="admin-bootstrap",
                operation_id="bootstrapInitialAdmin",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=idempotency_now + timedelta(days=30),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if idempotency.replay is not None:
                return dict(idempotency.replay.body or {})
            mode = session.execute(
                select(policy_versions.c.operational_mode)
                .where(policy_versions.c.is_current.is_(True))
                .with_for_update(read=True)
            ).scalar_one()
            if mode == "WRITE_FROZEN":
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Administrator bootstrap is disabled while writes are frozen",
                )
            control = (
                session.execute(
                    select(auth_control)
                    .where(auth_control.c.singleton_key == "auth")
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            now = clock_timestamp(session)
            if control["admin_bootstrap_completed_at"] is not None:
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Initial administrator bootstrap is permanently closed",
                )
            if (
                control["admin_window_expires_at"] is None
                or now >= control["admin_window_expires_at"]
            ):
                raise ApplicationError(
                    code="permission_denied",
                    status=403,
                    message="Administrator bootstrap window is closed",
                )
            enabled_admin = session.execute(
                select(users.c.user_id)
                .join(
                    system_role_grants,
                    system_role_grants.c.user_id == users.c.user_id,
                )
                .where(
                    users.c.enabled.is_(True),
                    system_role_grants.c.role == "SYSTEM_ADMIN",
                    system_role_grants.c.revoked_at.is_(None),
                )
                .limit(1)
            ).scalar_one_or_none()
            if enabled_admin is not None:
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="An enabled system administrator already exists",
                )
            password_hash = self._passwords.hash(password)
            user_id = new_uuid7()
            user_row = (
                session.execute(
                    insert(users)
                    .values(
                        user_id=user_id,
                        username=normalized_username,
                        display_name=display_name,
                        password_hash=password_hash,
                    )
                    .returning(users)
                )
                .mappings()
                .one()
            )
            session.execute(
                insert(system_role_grants).values(
                    user_id=user_id,
                    role="SYSTEM_ADMIN",
                    granted_by_user_id=None,
                    granted_at=now,
                )
            )
            session.execute(
                update(auth_control)
                .where(auth_control.c.singleton_key == "auth")
                .values(
                    admin_bootstrap_completed_at=now,
                    updated_at=now,
                    version=auth_control.c.version + 1,
                )
            )
            self._audit(
                session,
                actor_type="SYSTEM",
                actor_id="admin-bootstrap",
                action="identity.bootstrap_admin",
                target_type="USER",
                target_id=str(user_id),
                reason="Initial system administrator created",
                after_version=1,
            )
            response = json_wire_value(self._user_view(session, user_row))
            complete_idempotency(
                session,
                idempotency.record_id,
                status=201,
                body=response,
                headers={
                    "Location": f"/v1/admin/users/{user_id}",
                    "ETag": f'"v{response["version"]}"',
                },
                resource_id=user_id,
            )
            return response

        return run_transaction(self.session_factory, operation)

    def _record_login_attempt(self, username: str, source: str) -> dict[str, Any] | None:
        source_hash = hmac.new(
            self._server_secret, source.strip().lower().encode("utf-8"), hashlib.sha256
        ).digest()

        def operation(session: Session) -> dict[str, Any] | None:
            mode = session.execute(
                select(policy_versions.c.operational_mode)
                .where(policy_versions.c.is_current.is_(True))
                .with_for_update(read=True)
            ).scalar_one()
            user_query = select(users).where(users.c.username == username)
            if mode == "WRITE_FROZEN":
                user_query = user_query.with_for_update()
            observed_user = session.execute(user_query).mappings().one_or_none()
            if mode == "WRITE_FROZEN":
                if observed_user is None or not observed_user["enabled"]:
                    raise _AUTHENTICATION_ERROR
                admin_grant = session.execute(
                    select(system_role_grants.c.user_id)
                    .where(
                        system_role_grants.c.user_id == observed_user["user_id"],
                        system_role_grants.c.role == "SYSTEM_ADMIN",
                        system_role_grants.c.revoked_at.is_(None),
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if admin_grant is None:
                    raise _AUTHENTICATION_ERROR

            now = clock_timestamp(session)
            stale_before = now - self._LOGIN_RATE_RETENTION
            session.execute(
                delete(login_rate_limits).where(login_rate_limits.c.updated_at < stale_before)
            )
            row = (
                session.execute(
                    select(login_rate_limits)
                    .where(
                        login_rate_limits.c.source_hash == source_hash,
                        login_rate_limits.c.username == username,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                advisory_acquired = session.execute(
                    select(func.pg_try_advisory_xact_lock(self._LOGIN_RATE_ADVISORY_LOCK_KEY))
                ).scalar_one()
                if not advisory_acquired:
                    session.execute(
                        pg_insert(login_rate_limits).from_select(
                            [
                                "source_hash",
                                "username",
                                "window_started_at",
                                "attempts",
                                "updated_at",
                            ],
                            select(
                                literal(source_hash),
                                literal(username),
                                literal(now),
                                literal(1),
                                literal(now),
                            ).where(literal(False)),
                        )
                    )
                    session.execute(
                        select(func.pg_advisory_xact_lock(self._LOGIN_RATE_ADVISORY_LOCK_KEY))
                    )
                    row = (
                        session.execute(
                            select(login_rate_limits)
                            .where(
                                login_rate_limits.c.source_hash == source_hash,
                                login_rate_limits.c.username == username,
                            )
                            .with_for_update()
                        )
                        .mappings()
                        .one_or_none()
                    )
                    advisory_acquired = True
                if advisory_acquired and row is None:
                    bucket_count = session.execute(
                        select(func.count()).select_from(login_rate_limits)
                    ).scalar_one()
                    if bucket_count >= self._LOGIN_RATE_MAX_BUCKETS:
                        raise ApplicationError(
                            code="rate_limited",
                            status=429,
                            message="Login rate-limit capacity is temporarily exhausted",
                            retry_after=60,
                        )
                    session.execute(
                        insert(login_rate_limits).values(
                            source_hash=source_hash,
                            username=username,
                            window_started_at=now,
                            attempts=1,
                            updated_at=now,
                        )
                    )
                    return observed_user
            if now >= row["window_started_at"] + timedelta(minutes=1):
                session.execute(
                    update(login_rate_limits)
                    .where(
                        login_rate_limits.c.source_hash == source_hash,
                        login_rate_limits.c.username == username,
                    )
                    .values(window_started_at=now, attempts=1, updated_at=now)
                )
            elif row["attempts"] >= self.settings.login_rate_per_minute:
                remaining = 60 - (now - row["window_started_at"]).total_seconds()
                raise ApplicationError(
                    code="rate_limited",
                    status=429,
                    message="Too many login attempts",
                    retry_after=max(1, math.ceil(remaining)),
                )
            else:
                session.execute(
                    update(login_rate_limits)
                    .where(
                        login_rate_limits.c.source_hash == source_hash,
                        login_rate_limits.c.username == username,
                    )
                    .values(attempts=row["attempts"] + 1, updated_at=now)
                )
            return observed_user

        return run_transaction(self.session_factory, operation)

    def login(self, *, username: str, password: str, source: str) -> LoginResult:
        normalized_username = self._normalize_username(username)
        observed_user = self._record_login_attempt(normalized_username, source)
        encoded = (
            observed_user["password_hash"]
            if observed_user is not None
            else self._dummy_password_hash
        )
        valid_password = self._passwords.verify(encoded, password)
        if not valid_password or observed_user is None or not observed_user["enabled"]:
            raise _AUTHENTICATION_ERROR

        session_id = new_uuid7()
        cookie = self._secrets.issue(session_id)
        csrf_token = self._csrf.issue(cookie)

        def operation(session: Session) -> dict[str, Any]:
            user_row = (
                session.execute(
                    select(users)
                    .where(users.c.user_id == observed_user["user_id"])
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if (
                not user_row["enabled"]
                or user_row["password_hash"] != observed_user["password_hash"]
            ):
                raise _AUTHENTICATION_ERROR
            mode = session.execute(
                select(policy_versions.c.operational_mode)
                .where(policy_versions.c.is_current.is_(True))
                .with_for_update(read=True)
            ).scalar_one()
            system_admin = self._system_admin(session, user_row["user_id"])
            if mode == "WRITE_FROZEN" and not system_admin:
                raise ApplicationError(
                    code="permission_denied",
                    status=403,
                    message="Only an enabled system administrator may login while frozen",
                )
            now = clock_timestamp(session)
            expires_at = now + timedelta(seconds=self.settings.browser_session_absolute_ttl_seconds)
            session.execute(
                insert(browser_sessions).values(
                    browser_session_id=session_id,
                    user_id=user_row["user_id"],
                    secret_hash=self._secrets.digest(cookie),
                    csrf_secret_hash=self._csrf.digest(csrf_token),
                    expires_at=expires_at,
                    last_seen_at=now,
                )
            )
            self._audit(
                session,
                actor_type="USER",
                actor_id=str(user_row["user_id"]),
                action="auth.login",
                target_type="BROWSER_SESSION",
                target_id=str(session_id),
                reason="Browser session established",
            )
            principal = self._principal(
                session,
                user_id=user_row["user_id"],
                credential_kind=CredentialKind.BROWSER,
                credential_id=str(session_id),
            )
            return self._session_view(
                principal=principal,
                session_id=session_id,
                csrf_token=csrf_token,
                expires_at=expires_at,
            )

        return LoginResult(cookie=cookie, session=run_transaction(self.session_factory, operation))

    @staticmethod
    def _session_view(
        *, principal: Principal, session_id: UUID, csrf_token: str, expires_at: datetime
    ) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "user_id": principal.user_id,
            "csrf_token": csrf_token,
            "expires_at": expires_at,
            "memberships": [
                {
                    "tenant_id": membership.tenant_id,
                    "user_id": principal.user_id,
                    "role": membership.role,
                }
                for membership in principal.memberships
            ],
            "system_roles": ["SYSTEM_ADMIN"] if principal.system_admin else [],
        }

    def resolve_browser_session(self, cookie: str) -> AuthenticatedSession:
        try:
            parsed = self._secrets.parse(cookie)
            digest = self._secrets.digest(cookie)
        except ValueError:
            raise _AUTHENTICATION_ERROR from None

        def operation(session: Session) -> AuthenticatedSession:
            row = (
                session.execute(
                    select(browser_sessions, users.c.enabled)
                    .join(users, users.c.user_id == browser_sessions.c.user_id)
                    .where(browser_sessions.c.browser_session_id == parsed.identifier)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None or not hmac.compare_digest(row["secret_hash"], digest):
                raise _AUTHENTICATION_ERROR
            mode = session.execute(
                select(policy_versions.c.operational_mode)
                .where(policy_versions.c.is_current.is_(True))
                .with_for_update(read=True)
            ).scalar_one()
            now = clock_timestamp(session)
            idle_expiry = row["last_seen_at"] + timedelta(
                seconds=self.settings.browser_session_idle_ttl_seconds
            )
            if (
                row["revoked_at"] is not None
                or now >= row["expires_at"]
                or now >= idle_expiry
                or not row["enabled"]
            ):
                raise _AUTHENTICATION_ERROR
            principal = self._principal(
                session,
                user_id=row["user_id"],
                credential_kind=CredentialKind.BROWSER,
                credential_id=str(parsed.identifier),
            )
            if mode == "WRITE_FROZEN" and not principal.system_admin:
                raise _AUTHENTICATION_ERROR
            if mode != "WRITE_FROZEN":
                session.execute(
                    update(browser_sessions)
                    .where(browser_sessions.c.browser_session_id == parsed.identifier)
                    .values(last_seen_at=now, updated_at=now)
                )
            csrf_token = self._csrf.issue(cookie)
            if not hmac.compare_digest(self._csrf.digest(csrf_token), row["csrf_secret_hash"]):
                raise _AUTHENTICATION_ERROR
            return AuthenticatedSession(
                principal=principal,
                session_id=parsed.identifier,
                csrf_token=csrf_token,
                expires_at=row["expires_at"],
            )

        return run_transaction(self.session_factory, operation)

    def get_browser_session(self, cookie: str) -> dict[str, Any]:
        authenticated = self.resolve_browser_session(cookie)
        return json_wire_value(
            self._session_view(
                principal=authenticated.principal,
                session_id=authenticated.session_id,
                csrf_token=authenticated.csrf_token,
                expires_at=authenticated.expires_at,
            )
        )

    def logout(self, cookie: str, csrf_token: str) -> None:
        try:
            parsed = self._secrets.parse(cookie)
            digest = self._secrets.digest(cookie)
        except ValueError:
            raise _AUTHENTICATION_ERROR from None

        def operation(session: Session) -> None:
            row = (
                session.execute(
                    select(browser_sessions, users.c.enabled)
                    .join(users, users.c.user_id == browser_sessions.c.user_id)
                    .where(browser_sessions.c.browser_session_id == parsed.identifier)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None or not hmac.compare_digest(row["secret_hash"], digest):
                raise _AUTHENTICATION_ERROR
            mode = session.execute(
                select(policy_versions.c.operational_mode)
                .where(policy_versions.c.is_current.is_(True))
                .with_for_update(read=True)
            ).scalar_one()
            now = clock_timestamp(session)
            if (
                row["revoked_at"] is not None
                or now >= row["expires_at"]
                or now
                >= row["last_seen_at"]
                + timedelta(seconds=self.settings.browser_session_idle_ttl_seconds)
                or not row["enabled"]
            ):
                raise _AUTHENTICATION_ERROR
            if not self._csrf.verify(cookie, csrf_token, row["csrf_secret_hash"]):
                raise ApplicationError(
                    code="invalid_csrf",
                    status=403,
                    message="The CSRF token is missing or does not match this session",
                )
            if mode == "WRITE_FROZEN" and not self._system_admin(session, row["user_id"]):
                raise _AUTHENTICATION_ERROR
            session.execute(
                update(browser_sessions)
                .where(browser_sessions.c.browser_session_id == parsed.identifier)
                .values(revoked_at=now, updated_at=now)
            )
            self._audit(
                session,
                actor_type="USER",
                actor_id=str(row["user_id"]),
                action="auth.logout",
                target_type="BROWSER_SESSION",
                target_id=str(parsed.identifier),
                reason="Browser session revoked",
            )

        run_transaction(self.session_factory, operation)

    def _revalidate_principal(self, session: Session, principal: Principal) -> Principal:
        try:
            credential_id = UUID(principal.credential_id)
        except ValueError:
            raise _AUTHENTICATION_ERROR from None
        if principal.credential_kind is CredentialKind.BROWSER:
            row = (
                session.execute(
                    select(browser_sessions, users.c.enabled)
                    .join(users, users.c.user_id == browser_sessions.c.user_id)
                    .where(browser_sessions.c.browser_session_id == credential_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            now = clock_timestamp(session)
            if (
                row is None
                or row["revoked_at"] is not None
                or now >= row["expires_at"]
                or now
                >= row["last_seen_at"]
                + timedelta(seconds=self.settings.browser_session_idle_ttl_seconds)
                or not row["enabled"]
            ):
                raise _AUTHENTICATION_ERROR
            return self._principal(
                session,
                user_id=row["user_id"],
                credential_kind=CredentialKind.BROWSER,
                credential_id=principal.credential_id,
            )
        if principal.credential_kind is CredentialKind.CLI:
            row = (
                session.execute(
                    select(cli_tokens, users.c.enabled)
                    .join(users, users.c.user_id == cli_tokens.c.user_id)
                    .where(cli_tokens.c.token_id == credential_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            now = clock_timestamp(session)
            if (
                row is None
                or row["revoked_at"] is not None
                or now >= row["expires_at"]
                or not row["enabled"]
            ):
                raise _AUTHENTICATION_ERROR
            return self._principal(
                session,
                user_id=row["user_id"],
                credential_kind=CredentialKind.CLI,
                credential_id=principal.credential_id,
                scopes=frozenset(TokenScope(scope) for scope in row["scopes"]),
            )
        raise _AUTHENTICATION_ERROR

    def revalidate_principal(self, session: Session, principal: Principal) -> Principal:
        return self._revalidate_principal(session, principal)

    def hash_password(self, password: str) -> str:
        return self._passwords.hash(password)

    @staticmethod
    def revoke_user_credentials(session: Session, user_id: UUID, revoked_at: datetime) -> None:
        session.execute(
            update(browser_sessions)
            .where(
                browser_sessions.c.user_id == user_id,
                browser_sessions.c.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at, updated_at=revoked_at)
        )
        session.execute(
            update(cli_tokens)
            .where(cli_tokens.c.user_id == user_id, cli_tokens.c.revoked_at.is_(None))
            .values(revoked_at=revoked_at, updated_at=revoked_at)
        )

    def list_cli_tokens(
        self,
        principal: Principal,
        *,
        page_size: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        if not 1 <= page_size <= 100:
            raise ApplicationError(
                code="validation_failed",
                status=422,
                message="Page size must be between 1 and 100",
            )

        def operation(session: Session) -> dict[str, Any]:
            live = self._revalidate_principal(session, principal)
            if live.user_id is None:
                raise _AUTHENTICATION_ERROR
            if live.credential_kind is CredentialKind.CLI and not live.has_scope(
                TokenScope.TOKENS_WRITE
            ):
                raise ApplicationError(
                    code="permission_denied",
                    status=403,
                    message="The exact tokens:write scope is required",
                )
            now = transaction_timestamp(session)
            codec = CursorCodec(
                self._server_secret,
                ttl_seconds=self.settings.cursor_ttl_seconds,
                now=lambda: now,
            )
            binding = {
                "actor_id": str(live.user_id),
                "operation_id": "listCliTokens",
            }
            statement = select(cli_tokens).where(cli_tokens.c.user_id == live.user_id)
            if cursor is not None:
                try:
                    position = codec.decode(cursor, expected_binding=binding)
                    created_at = datetime.fromisoformat(position["created_at"])
                    token_id = UUID(position["token_id"])
                except (CursorError, KeyError, ValueError):
                    raise ApplicationError(
                        code="invalid_cursor",
                        status=400,
                        message="The pagination cursor is invalid for this request",
                    ) from None
                statement = statement.where(
                    or_(
                        cli_tokens.c.created_at < created_at,
                        and_(
                            cli_tokens.c.created_at == created_at,
                            cli_tokens.c.token_id < token_id,
                        ),
                    )
                )
            rows = (
                session.execute(
                    statement.order_by(
                        cli_tokens.c.created_at.desc(), cli_tokens.c.token_id.desc()
                    ).limit(page_size + 1)
                )
                .mappings()
                .all()
            )
            visible = rows[:page_size]
            next_cursor = None
            if len(rows) > page_size:
                last = visible[-1]
                next_cursor = codec.encode(
                    binding=binding,
                    position={
                        "created_at": last["created_at"].isoformat(),
                        "token_id": str(last["token_id"]),
                    },
                )
            return json_wire_value(
                {
                    "items": [self._token_view(row) for row in visible],
                    "page": {"next_cursor": next_cursor, "page_size": page_size},
                }
            )

        return run_transaction(self.session_factory, operation)

    def create_cli_token(
        self,
        principal: Principal,
        *,
        name: str,
        scopes: set[TokenScope],
        expires_in_seconds: int,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any]:
        if not 300 <= expires_in_seconds <= 2_592_000:
            raise ApplicationError(
                code="validation_failed",
                status=422,
                message="Token expiry must be between 300 and 2592000 seconds",
            )
        if not 1 <= len(name) <= 64:
            raise ApplicationError(
                code="validation_failed", status=422, message="Token name is invalid"
            )
        token_id = new_uuid7()
        raw_token = self._secrets.issue(token_id)

        def operation(session: Session) -> dict[str, Any]:
            live = self._revalidate_principal(session, principal)
            try:
                validate_requested_token_scopes(live, scopes)
            except AuthorizationError as exc:
                raise ApplicationError(
                    code="permission_denied", status=403, message=str(exc)
                ) from exc
            now = transaction_timestamp(session)
            idempotency = begin_idempotency(
                session,
                context="GLOBAL",
                principal_id=str(live.user_id),
                operation_id="createCliToken",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=now + timedelta(days=30),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            mode = session.execute(
                select(policy_versions.c.operational_mode)
                .where(policy_versions.c.is_current.is_(True))
                .with_for_update(read=True)
            ).scalar_one()
            if mode == "WRITE_FROZEN":
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Credential mutation is disabled while writes are frozen",
                )
            expires_at = now + timedelta(seconds=expires_in_seconds)
            row = (
                session.execute(
                    insert(cli_tokens)
                    .values(
                        token_id=token_id,
                        user_id=live.user_id,
                        token_hash=self._secrets.digest(raw_token),
                        name=name,
                        scopes=sorted(scope.value for scope in scopes),
                        expires_at=expires_at,
                    )
                    .returning(cli_tokens)
                )
                .mappings()
                .one()
            )
            self._audit(
                session,
                actor_type="USER",
                actor_id=str(live.user_id),
                action="token.create",
                target_type="CLI_TOKEN",
                target_id=str(token_id),
                reason="CLI token created",
                safe_metadata={"scopes": sorted(scope.value for scope in scopes)},
            )
            response = json_wire_value(self._token_view(row))
            complete_idempotency(
                session,
                idempotency.record_id,
                status=201,
                body=response,
                headers={"Location": f"/v1/tokens/{token_id}"},
                resource_id=token_id,
                one_time_secret=True,
            )
            return response

        response = run_transaction(self.session_factory, operation)
        return {**response, "token": raw_token}

    @staticmethod
    def _token_view(row: Any) -> dict[str, Any]:
        return {
            "token_id": row["token_id"],
            "name": row["name"],
            "scopes": list(row["scopes"]),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
        }

    def resolve_cli_token(self, token: str) -> Principal:
        try:
            parsed = self._secrets.parse(token)
            digest = self._secrets.digest(token)
        except ValueError:
            raise _AUTHENTICATION_ERROR from None

        def operation(session: Session) -> Principal:
            row = (
                session.execute(
                    select(cli_tokens, users.c.enabled)
                    .join(users, users.c.user_id == cli_tokens.c.user_id)
                    .where(cli_tokens.c.token_id == parsed.identifier)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            now = clock_timestamp(session)
            if (
                row is None
                or not hmac.compare_digest(row["token_hash"], digest)
                or row["revoked_at"] is not None
                or now >= row["expires_at"]
                or not row["enabled"]
            ):
                raise _AUTHENTICATION_ERROR
            try:
                scopes = frozenset(TokenScope(scope) for scope in row["scopes"])
            except ValueError:
                raise _AUTHENTICATION_ERROR from None
            return self._principal(
                session,
                user_id=row["user_id"],
                credential_kind=CredentialKind.CLI,
                credential_id=str(parsed.identifier),
                scopes=scopes,
            )

        return run_transaction(self.session_factory, operation)

    def revoke_cli_token(
        self,
        principal: Principal,
        token_id: UUID,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> None:
        def operation(session: Session) -> None:
            live = self._revalidate_principal(session, principal)
            if live.credential_kind is CredentialKind.CLI and not live.has_scope(
                TokenScope.TOKENS_WRITE
            ):
                raise ApplicationError(
                    code="permission_denied",
                    status=403,
                    message="The exact tokens:write scope is required",
                )
            now = transaction_timestamp(session)
            idempotency = begin_idempotency(
                session,
                context="GLOBAL",
                principal_id=str(live.user_id),
                operation_id="revokeCliToken",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=now + timedelta(days=30),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if idempotency.replay is not None:
                return
            mode = session.execute(
                select(policy_versions.c.operational_mode)
                .where(policy_versions.c.is_current.is_(True))
                .with_for_update(read=True)
            ).scalar_one()
            if mode == "WRITE_FROZEN":
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Credential mutation is disabled while writes are frozen",
                )
            row = (
                session.execute(
                    select(cli_tokens).where(cli_tokens.c.token_id == token_id).with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["user_id"] != live.user_id:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Token was not found"
                )
            if row["revoked_at"] is None:
                session.execute(
                    update(cli_tokens)
                    .where(cli_tokens.c.token_id == token_id)
                    .values(revoked_at=now, updated_at=now)
                )
            self._audit(
                session,
                actor_type="USER",
                actor_id=str(live.user_id),
                action="token.revoke",
                target_type="CLI_TOKEN",
                target_id=str(token_id),
                reason="CLI token revoked",
            )
            complete_idempotency(
                session,
                idempotency.record_id,
                status=204,
                body=None,
                headers={},
                resource_id=token_id,
            )

        run_transaction(self.session_factory, operation)

    def bootstrap_worker(
        self,
        *,
        bootstrap_secret: bytes,
        source_allowed: bool,
        installation_id: UUID,
        credential_public_fingerprint: str,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any]:
        self._verify_bootstrap(bootstrap_secret, source_allowed)
        credential_id = new_uuid7()
        raw_credential = self._secrets.issue(credential_id)

        def operation(session: Session) -> dict[str, Any]:
            idempotency_now = transaction_timestamp(session)
            idempotency = begin_idempotency(
                session,
                context="BOOTSTRAP",
                principal_id=f"worker:{installation_id}",
                operation_id="bootstrapLocalWorker",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=idempotency_now + timedelta(days=30),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            mode = session.execute(
                select(policy_versions.c.operational_mode)
                .where(policy_versions.c.is_current.is_(True))
                .with_for_update(read=True)
            ).scalar_one()
            if mode == "WRITE_FROZEN":
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Worker bootstrap is disabled while writes are frozen",
                )
            control = (
                session.execute(
                    select(auth_control)
                    .where(auth_control.c.singleton_key == "auth")
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            now = clock_timestamp(session)
            if (
                installation_id != control["installation_id"]
                or credential_public_fingerprint != control["worker_credential_fingerprint"]
            ):
                raise ApplicationError(
                    code="permission_denied",
                    status=403,
                    message="Worker bootstrap identity does not match this deployment",
                )
            if (
                control["worker_window_expires_at"] is None
                or now >= control["worker_window_expires_at"]
            ):
                raise ApplicationError(
                    code="permission_denied",
                    status=403,
                    message="Worker bootstrap window is closed",
                )
            worker_id = control["local_worker_id"]
            existing_worker = (
                session.execute(
                    select(workers).where(workers.c.worker_id == worker_id).with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if existing_worker is None:
                session.execute(
                    insert(workers).values(
                        worker_id=worker_id,
                        admin_state="ENABLED",
                        health="STARTING",
                    )
                )
            session.execute(
                update(worker_credentials)
                .where(
                    worker_credentials.c.worker_id == worker_id,
                    worker_credentials.c.revoked_at.is_(None),
                )
                .values(revoked_at=now, updated_at=now)
            )
            expires_at = now + timedelta(seconds=self.settings.worker_credential_ttl_seconds)
            session.execute(
                insert(worker_credentials).values(
                    credential_id=credential_id,
                    worker_id=worker_id,
                    credential_hash=self._secrets.digest(raw_credential),
                    scopes=["worker:local"],
                    expires_at=expires_at,
                )
            )
            self._audit(
                session,
                actor_type="SYSTEM",
                actor_id=f"worker-bootstrap:{installation_id}",
                action="worker.bootstrap_credential",
                target_type="WORKER",
                target_id=str(worker_id),
                reason="Local worker credential issued or rotated",
            )
            response = json_wire_value(
                {
                    "worker_id": worker_id,
                    "credential_id": credential_id,
                    "credential_expires_at": expires_at,
                }
            )
            complete_idempotency(
                session,
                idempotency.record_id,
                status=201,
                body=response,
                headers={"Location": f"/v1/admin/workers/{worker_id}"},
                resource_id=credential_id,
                one_time_secret=True,
            )
            return response

        response = run_transaction(self.session_factory, operation)
        return {**response, "credential": raw_credential}

    def reopen_worker_bootstrap_window(self, *, bootstrap_secret: bytes) -> dict[str, Any]:
        self._verify_bootstrap(bootstrap_secret, source_allowed=True)

        def operation(session: Session) -> dict[str, Any]:
            now = transaction_timestamp(session)
            mode = session.execute(
                select(policy_versions.c.operational_mode)
                .where(policy_versions.c.is_current.is_(True))
                .with_for_update(read=True)
            ).scalar_one()
            if mode == "WRITE_FROZEN":
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Worker bootstrap window cannot be reopened while writes are frozen",
                )
            control = (
                session.execute(
                    select(auth_control)
                    .where(auth_control.c.singleton_key == "auth")
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            expires_at = now + timedelta(seconds=self.settings.worker_bootstrap_window_seconds)
            session.execute(
                update(auth_control)
                .where(auth_control.c.singleton_key == "auth")
                .values(
                    worker_window_opened_at=now,
                    worker_window_expires_at=expires_at,
                    updated_at=now,
                    version=control["version"] + 1,
                )
            )
            self._audit(
                session,
                actor_type="SYSTEM",
                actor_id="local-operator",
                action="worker.bootstrap_window.reopen",
                target_type="AUTH_CONTROL",
                target_id="auth",
                reason="Local operator reopened the worker bootstrap window",
                before_version=control["version"],
                after_version=control["version"] + 1,
            )
            return json_wire_value({"opened_at": now, "expires_at": expires_at})

        return run_transaction(self.session_factory, operation)

    def resolve_worker_credential(self, credential: str) -> UUID:
        try:
            parsed = self._secrets.parse(credential)
            digest = self._secrets.digest(credential)
        except ValueError:
            raise _AUTHENTICATION_ERROR from None

        def operation(session: Session) -> UUID:
            row = (
                session.execute(
                    select(worker_credentials)
                    .where(worker_credentials.c.credential_id == parsed.identifier)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            now = clock_timestamp(session)
            if (
                row is None
                or not hmac.compare_digest(row["credential_hash"], digest)
                or row["revoked_at"] is not None
                or now >= row["expires_at"]
                or row["scopes"] != ["worker:local"]
            ):
                raise _AUTHENTICATION_ERROR
            return row["worker_id"]

        return run_transaction(self.session_factory, operation)

    def get_worker_bootstrap_state(self) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            row = (
                session.execute(
                    select(workers).where(workers.c.worker_id == self.settings.local_worker_id)
                )
                .mappings()
                .one()
            )
            return {
                "health": row["health"],
                "current_incarnation_id": row["current_incarnation_id"],
                "ready_at": row["ready_at"],
            }

        return run_transaction(self.session_factory, operation)
