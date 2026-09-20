import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nexa.application.errors import ApplicationError
from nexa.application.idempotency import begin_idempotency, complete_idempotency
from nexa.application.identity_service import IdentityService
from nexa.application.json_codec import json_wire_value
from nexa.application.preconditions import ExpectedVersion, resolve_expected_version
from nexa.config import Settings
from nexa.domain.identity import AuthorizationError, Principal, require_admin
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import transaction_timestamp
from nexa.infrastructure.persistence.schema import (
    audit_records,
    auth_control,
    membership_sets,
    memberships,
    policy_versions,
    system_role_grants,
    tenant_policies,
    tenants,
    users,
)
from nexa.infrastructure.persistence.transactions import run_transaction
from nexa.infrastructure.security import CursorCodec, CursorError, read_secret_file

_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,61}[a-z0-9]$")


@dataclass(frozen=True, slots=True)
class MembershipMutationResult:
    body: dict[str, Any]
    version: int


class AdminService:
    def __init__(
        self,
        session_factory,
        settings: Settings,
        identity_service: IdentityService,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.identity = identity_service
        self._cursor_key = read_secret_file(settings.server_secret_file)

    def _authorize(self, session: Session, principal: Principal, *, write: bool) -> Principal:
        live = self.identity.revalidate_principal(session, principal)
        try:
            require_admin(live, write=write)
        except AuthorizationError as exc:
            raise ApplicationError(
                code="permission_denied",
                status=403,
                message=str(exc),
            ) from exc
        return live

    @staticmethod
    def _ensure_mutable(session: Session) -> None:
        mode = session.execute(
            select(policy_versions.c.operational_mode)
            .where(policy_versions.c.is_current.is_(True))
            .with_for_update(read=True)
        ).scalar_one()
        if mode == "WRITE_FROZEN":
            raise ApplicationError(
                code="state_conflict",
                status=409,
                message="Administrative mutations are disabled while writes are frozen",
            )

    @staticmethod
    def _audit(
        session: Session,
        *,
        actor_id: UUID,
        action: str,
        target_type: str,
        target_id: str,
        reason: str,
        tenant_id: UUID | None = None,
        before_version: int | None = None,
        after_version: int | None = None,
    ) -> None:
        session.execute(
            insert(audit_records).values(
                audit_id=new_uuid7(),
                actor_type="ADMIN",
                actor_id=str(actor_id),
                tenant_id=tenant_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                before_version=before_version,
                after_version=after_version,
                reason=reason,
                safe_metadata={},
            )
        )

    @staticmethod
    def _tenant_view(row: Any) -> dict[str, Any]:
        return {
            "tenant_id": row["tenant_id"],
            "slug": row["slug"],
            "display_name": row["display_name"],
            "enabled": row["enabled"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _membership_view(row: Any) -> dict[str, Any]:
        return {
            "tenant_id": row["tenant_id"],
            "user_id": row["user_id"],
            "role": row["role"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _user_view(self, session: Session, row: Any) -> dict[str, Any]:
        active_admin = (
            session.execute(
                select(system_role_grants.c.user_id).where(
                    system_role_grants.c.user_id == row["user_id"],
                    system_role_grants.c.role == "SYSTEM_ADMIN",
                    system_role_grants.c.revoked_at.is_(None),
                )
            ).scalar_one_or_none()
            is not None
        )
        return {
            "user_id": row["user_id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "enabled": row["enabled"],
            "system_roles": ["SYSTEM_ADMIN"] if active_admin else [],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _normalize_slug(slug: str) -> str:
        normalized = slug.strip().lower()
        if not _SLUG_PATTERN.fullmatch(normalized):
            raise ApplicationError(
                code="validation_failed",
                status=422,
                message="Tenant slug does not match the approved format",
            )
        return normalized

    @staticmethod
    def _normalize_username(username: str) -> str:
        # Keep application normalization aligned with the frozen lower-based username index.
        normalized = username.strip().casefold().lower()
        if not 3 <= len(normalized) <= 254:
            raise ApplicationError(
                code="validation_failed",
                status=422,
                message="Username length must be between 3 and 254 characters",
            )
        return normalized

    @staticmethod
    def _validate_display_name(display_name: str) -> str:
        normalized = display_name.strip()
        if not 1 <= len(normalized) <= 100:
            raise ApplicationError(
                code="validation_failed",
                status=422,
                message="Display name length must be between 1 and 100 characters",
            )
        return normalized

    def _decode_cursor(
        self,
        cursor: str,
        *,
        binding: dict[str, str],
        now: datetime,
    ) -> tuple[datetime, UUID]:
        codec = CursorCodec(
            self._cursor_key,
            ttl_seconds=self.settings.cursor_ttl_seconds,
            now=lambda: now,
        )
        try:
            position = codec.decode(cursor, expected_binding=binding)
            return datetime.fromisoformat(position["created_at"]), UUID(position["id"])
        except (CursorError, KeyError, ValueError):
            raise ApplicationError(
                code="invalid_cursor",
                status=400,
                message="The pagination cursor is invalid for this request",
            ) from None

    def _encode_cursor(
        self,
        row: Any,
        *,
        id_name: str,
        binding: dict[str, str],
        now: datetime,
    ) -> str:
        return CursorCodec(
            self._cursor_key,
            ttl_seconds=self.settings.cursor_ttl_seconds,
            now=lambda: now,
        ).encode(
            binding=binding,
            position={"created_at": row["created_at"].isoformat(), "id": str(row[id_name])},
        )

    @staticmethod
    def _validate_page_size(page_size: int) -> None:
        if not 1 <= page_size <= 100:
            raise ApplicationError(
                code="validation_failed",
                status=422,
                message="Page size must be between 1 and 100",
            )

    def create_tenant(
        self,
        principal: Principal,
        *,
        slug: str,
        display_name: str,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any]:
        normalized_slug = self._normalize_slug(slug)
        normalized_name = self._validate_display_name(display_name)
        tenant_id = new_uuid7()

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=True)
            now = transaction_timestamp(session)
            outcome = begin_idempotency(
                session,
                context="GLOBAL",
                principal_id=str(live.user_id),
                operation_id="adminCreateTenant",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=now + timedelta(days=30),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if outcome.replay is not None:
                return dict(outcome.replay.body or {})
            self._ensure_mutable(session)
            row = (
                session.execute(
                    insert(tenants)
                    .values(
                        tenant_id=tenant_id,
                        slug=normalized_slug,
                        display_name=normalized_name,
                    )
                    .returning(tenants)
                )
                .mappings()
                .one()
            )
            session.execute(insert(membership_sets).values(tenant_id=tenant_id))
            session.execute(
                insert(tenant_policies).values(
                    tenant_id=tenant_id,
                    version=1,
                    weight=Decimal("1"),
                    cpu_limit_millis=0,
                    memory_limit_bytes=0,
                    gpu_limit=0,
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
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.tenant.create",
                target_type="TENANT",
                target_id=str(tenant_id),
                reason="Tenant created",
                tenant_id=tenant_id,
                after_version=1,
            )
            response = json_wire_value(self._tenant_view(row))
            complete_idempotency(
                session,
                outcome.record_id,
                status=201,
                body=response,
                headers={},
                resource_id=tenant_id,
            )
            return response

        try:
            return run_transaction(self.session_factory, operation)
        except IntegrityError as exc:
            raise ApplicationError(
                code="state_conflict",
                status=409,
                message="Tenant slug already exists",
            ) from exc

    def list_tenants(
        self, principal: Principal, *, page_size: int, cursor: str | None
    ) -> dict[str, Any]:
        self._validate_page_size(page_size)

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=False)
            now = transaction_timestamp(session)
            binding = {"actor_id": str(live.user_id), "operation_id": "adminListTenants"}
            statement = select(tenants)
            if cursor is not None:
                created_at, tenant_id = self._decode_cursor(cursor, binding=binding, now=now)
                statement = statement.where(
                    or_(
                        tenants.c.created_at < created_at,
                        and_(
                            tenants.c.created_at == created_at,
                            tenants.c.tenant_id < tenant_id,
                        ),
                    )
                )
            rows = (
                session.execute(
                    statement.order_by(
                        tenants.c.created_at.desc(), tenants.c.tenant_id.desc()
                    ).limit(page_size + 1)
                )
                .mappings()
                .all()
            )
            visible = rows[:page_size]
            next_cursor = (
                self._encode_cursor(
                    visible[-1],
                    id_name="tenant_id",
                    binding=binding,
                    now=now,
                )
                if len(rows) > page_size
                else None
            )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.tenant.list",
                target_type="TENANT_COLLECTION",
                target_id="all",
                reason="Tenant list read",
            )
            return json_wire_value(
                {
                    "items": [self._tenant_view(row) for row in visible],
                    "page": {"next_cursor": next_cursor, "page_size": page_size},
                }
            )

        return run_transaction(self.session_factory, operation)

    def get_tenant(self, principal: Principal, tenant_id: UUID) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=False)
            row = (
                session.execute(select(tenants).where(tenants.c.tenant_id == tenant_id))
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Tenant was not found"
                )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.tenant.get",
                target_type="TENANT",
                target_id=str(tenant_id),
                tenant_id=tenant_id,
                reason="Tenant read",
            )
            return json_wire_value(self._tenant_view(row))

        return run_transaction(self.session_factory, operation)

    def update_tenant(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        expected_version: ExpectedVersion,
        idempotency_key: str,
        request_hash: str,
        display_name: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        if display_name is None and enabled is None:
            raise ApplicationError(
                code="validation_failed",
                status=422,
                message="At least one tenant field must be updated",
            )
        normalized_name = (
            self._validate_display_name(display_name) if display_name is not None else None
        )

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=True)
            now = transaction_timestamp(session)
            outcome = begin_idempotency(
                session,
                context="GLOBAL",
                principal_id=str(live.user_id),
                operation_id="adminUpdateTenant",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=now + timedelta(days=30),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if outcome.replay is not None:
                return dict(outcome.replay.body or {})
            required_version = resolve_expected_version(expected_version)
            self._ensure_mutable(session)
            row = (
                session.execute(
                    select(tenants).where(tenants.c.tenant_id == tenant_id).with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Tenant was not found"
                )
            if row["version"] != required_version:
                raise ApplicationError(
                    code="version_conflict",
                    status=412,
                    message="Tenant version does not match If-Match",
                )
            values: dict[str, Any] = {"version": row["version"] + 1, "updated_at": now}
            if normalized_name is not None:
                values["display_name"] = normalized_name
            if enabled is not None:
                values["enabled"] = enabled
            updated = (
                session.execute(
                    update(tenants)
                    .where(tenants.c.tenant_id == tenant_id)
                    .values(**values)
                    .returning(tenants)
                )
                .mappings()
                .one()
            )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.tenant.update",
                target_type="TENANT",
                target_id=str(tenant_id),
                tenant_id=tenant_id,
                reason="Tenant updated",
                before_version=row["version"],
                after_version=updated["version"],
            )
            response = json_wire_value(self._tenant_view(updated))
            complete_idempotency(
                session,
                outcome.record_id,
                status=200,
                body=response,
                headers={"ETag": f'"v{updated["version"]}"'},
                resource_id=tenant_id,
            )
            return response

        return run_transaction(self.session_factory, operation)

    def create_user(
        self,
        principal: Principal,
        *,
        username: str,
        display_name: str,
        password: str,
        system_roles: set[str],
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any]:
        normalized_username = self._normalize_username(username)
        normalized_name = self._validate_display_name(display_name)
        if not system_roles <= {"SYSTEM_ADMIN"}:
            raise ApplicationError(
                code="validation_failed", status=422, message="Unknown system role"
            )
        user_id = new_uuid7()

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=True)
            now = transaction_timestamp(session)
            outcome = begin_idempotency(
                session,
                context="GLOBAL",
                principal_id=str(live.user_id),
                operation_id="adminCreateUser",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=now + timedelta(days=30),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if outcome.replay is not None:
                return dict(outcome.replay.body or {})
            self._ensure_mutable(session)
            session.execute(
                select(auth_control.c.singleton_key)
                .where(auth_control.c.singleton_key == "auth")
                .with_for_update()
            ).one()
            password_hash = self.identity.hash_password(password)
            row = (
                session.execute(
                    insert(users)
                    .values(
                        user_id=user_id,
                        username=normalized_username,
                        display_name=normalized_name,
                        password_hash=password_hash,
                    )
                    .returning(users)
                )
                .mappings()
                .one()
            )
            if "SYSTEM_ADMIN" in system_roles:
                session.execute(
                    insert(system_role_grants).values(
                        user_id=user_id,
                        role="SYSTEM_ADMIN",
                        granted_by_user_id=live.user_id,
                        granted_at=now,
                    )
                )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.user.create",
                target_type="USER",
                target_id=str(user_id),
                reason="User created",
                after_version=1,
            )
            response = json_wire_value(self._user_view(session, row))
            complete_idempotency(
                session,
                outcome.record_id,
                status=201,
                body=response,
                headers={},
                resource_id=user_id,
            )
            return response

        try:
            return run_transaction(self.session_factory, operation)
        except IntegrityError as exc:
            raise ApplicationError(
                code="state_conflict",
                status=409,
                message="Username already exists",
            ) from exc

    def list_users(
        self, principal: Principal, *, page_size: int, cursor: str | None
    ) -> dict[str, Any]:
        self._validate_page_size(page_size)

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=False)
            now = transaction_timestamp(session)
            binding = {"actor_id": str(live.user_id), "operation_id": "adminListUsers"}
            statement = select(users)
            if cursor is not None:
                created_at, user_id = self._decode_cursor(cursor, binding=binding, now=now)
                statement = statement.where(
                    or_(
                        users.c.created_at < created_at,
                        and_(users.c.created_at == created_at, users.c.user_id < user_id),
                    )
                )
            rows = (
                session.execute(
                    statement.order_by(users.c.created_at.desc(), users.c.user_id.desc()).limit(
                        page_size + 1
                    )
                )
                .mappings()
                .all()
            )
            visible = rows[:page_size]
            next_cursor = (
                self._encode_cursor(visible[-1], id_name="user_id", binding=binding, now=now)
                if len(rows) > page_size
                else None
            )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.user.list",
                target_type="USER_COLLECTION",
                target_id="all",
                reason="User list read",
            )
            return json_wire_value(
                {
                    "items": [self._user_view(session, row) for row in visible],
                    "page": {"next_cursor": next_cursor, "page_size": page_size},
                }
            )

        return run_transaction(self.session_factory, operation)

    def get_user(self, principal: Principal, user_id: UUID) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=False)
            row = (
                session.execute(select(users).where(users.c.user_id == user_id))
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="User was not found"
                )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.user.get",
                target_type="USER",
                target_id=str(user_id),
                reason="User read",
            )
            return json_wire_value(self._user_view(session, row))

        return run_transaction(self.session_factory, operation)

    def update_user(
        self,
        principal: Principal,
        *,
        user_id: UUID,
        expected_version: ExpectedVersion,
        idempotency_key: str,
        request_hash: str,
        display_name: str | None = None,
        password: str | None = None,
        enabled: bool | None = None,
        system_roles: set[str] | None = None,
    ) -> dict[str, Any]:
        if system_roles is not None and not system_roles <= {"SYSTEM_ADMIN"}:
            raise ApplicationError(
                code="validation_failed", status=422, message="Unknown system role"
            )
        normalized_name = (
            self._validate_display_name(display_name) if display_name is not None else None
        )

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=True)
            now = transaction_timestamp(session)
            outcome = begin_idempotency(
                session,
                context="GLOBAL",
                principal_id=str(live.user_id),
                operation_id="adminUpdateUser",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=now + timedelta(days=30),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if outcome.replay is not None:
                return dict(outcome.replay.body or {})
            required_version = resolve_expected_version(expected_version)
            self._ensure_mutable(session)
            session.execute(
                select(auth_control.c.singleton_key)
                .where(auth_control.c.singleton_key == "auth")
                .with_for_update()
            ).one()
            row = (
                session.execute(select(users).where(users.c.user_id == user_id).with_for_update())
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="User was not found"
                )
            if row["version"] != required_version:
                raise ApplicationError(
                    code="version_conflict",
                    status=412,
                    message="User version does not match If-Match",
                )
            grant = (
                session.execute(
                    select(system_role_grants)
                    .where(
                        system_role_grants.c.user_id == user_id,
                        system_role_grants.c.role == "SYSTEM_ADMIN",
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            currently_admin = grant is not None and grant["revoked_at"] is None
            desired_admin = (
                currently_admin if system_roles is None else "SYSTEM_ADMIN" in system_roles
            )
            desired_enabled = row["enabled"] if enabled is None else enabled
            if currently_admin and row["enabled"] and (not desired_admin or not desired_enabled):
                other = session.execute(
                    select(users.c.user_id)
                    .join(system_role_grants, system_role_grants.c.user_id == users.c.user_id)
                    .where(
                        users.c.enabled.is_(True),
                        users.c.user_id != user_id,
                        system_role_grants.c.role == "SYSTEM_ADMIN",
                        system_role_grants.c.revoked_at.is_(None),
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if other is None:
                    raise ApplicationError(
                        code="state_conflict",
                        status=409,
                        message=(
                            "The last enabled system administrator cannot be disabled or revoked"
                        ),
                    )
            password_hash = self.identity.hash_password(password) if password is not None else None
            values: dict[str, Any] = {"version": row["version"] + 1, "updated_at": now}
            if normalized_name is not None:
                values["display_name"] = normalized_name
            if password_hash is not None:
                values["password_hash"] = password_hash
            if enabled is not None:
                values["enabled"] = enabled
            updated = (
                session.execute(
                    update(users)
                    .where(users.c.user_id == user_id)
                    .values(**values)
                    .returning(users)
                )
                .mappings()
                .one()
            )
            role_changed = desired_admin != currently_admin
            if role_changed and desired_admin:
                if grant is None:
                    session.execute(
                        insert(system_role_grants).values(
                            user_id=user_id,
                            role="SYSTEM_ADMIN",
                            granted_by_user_id=live.user_id,
                            granted_at=now,
                        )
                    )
                else:
                    session.execute(
                        update(system_role_grants)
                        .where(
                            system_role_grants.c.user_id == user_id,
                            system_role_grants.c.role == "SYSTEM_ADMIN",
                        )
                        .values(
                            revoked_at=None,
                            granted_by_user_id=live.user_id,
                            granted_at=now,
                            version=grant["version"] + 1,
                        )
                    )
            elif role_changed:
                session.execute(
                    update(system_role_grants)
                    .where(
                        system_role_grants.c.user_id == user_id,
                        system_role_grants.c.role == "SYSTEM_ADMIN",
                    )
                    .values(revoked_at=now, version=grant["version"] + 1)
                )
            if password_hash is not None or enabled is False or role_changed:
                self.identity.revoke_user_credentials(session, user_id, now)
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.user.update",
                target_type="USER",
                target_id=str(user_id),
                reason="User updated",
                before_version=row["version"],
                after_version=updated["version"],
            )
            response = json_wire_value(self._user_view(session, updated))
            complete_idempotency(
                session,
                outcome.record_id,
                status=200,
                body=response,
                headers={"ETag": f'"v{updated["version"]}"'},
                resource_id=user_id,
            )
            return response

        return run_transaction(self.session_factory, operation)

    def list_memberships(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        page_size: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        self._validate_page_size(page_size)

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=False)
            now = transaction_timestamp(session)
            membership_set = (
                session.execute(
                    select(membership_sets).where(membership_sets.c.tenant_id == tenant_id)
                )
                .mappings()
                .one_or_none()
            )
            if membership_set is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Tenant was not found"
                )
            binding = {
                "actor_id": str(live.user_id),
                "operation_id": "adminListMemberships",
                "tenant_id": str(tenant_id),
            }
            statement = select(memberships).where(memberships.c.tenant_id == tenant_id)
            if cursor is not None:
                created_at, user_id = self._decode_cursor(cursor, binding=binding, now=now)
                statement = statement.where(
                    or_(
                        memberships.c.created_at < created_at,
                        and_(
                            memberships.c.created_at == created_at,
                            memberships.c.user_id < user_id,
                        ),
                    )
                )
            rows = (
                session.execute(
                    statement.order_by(
                        memberships.c.created_at.desc(), memberships.c.user_id.desc()
                    ).limit(page_size + 1)
                )
                .mappings()
                .all()
            )
            visible = rows[:page_size]
            next_cursor = (
                self._encode_cursor(visible[-1], id_name="user_id", binding=binding, now=now)
                if len(rows) > page_size
                else None
            )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.membership.list",
                target_type="MEMBERSHIP_SET",
                target_id=str(tenant_id),
                tenant_id=tenant_id,
                reason="Membership list read",
            )
            return json_wire_value(
                {
                    "membership_set_version": membership_set["version"],
                    "items": [self._membership_view(row) for row in visible],
                    "page": {"next_cursor": next_cursor, "page_size": page_size},
                }
            )

        return run_transaction(self.session_factory, operation)

    def upsert_membership(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        user_id: UUID,
        role: str,
        expected_version: ExpectedVersion,
        idempotency_key: str,
        request_hash: str,
    ) -> MembershipMutationResult:
        if role not in {"MEMBER", "TENANT_ADMIN"}:
            raise ApplicationError(
                code="validation_failed", status=422, message="Unknown tenant role"
            )

        def operation(session: Session) -> MembershipMutationResult:
            live = self._authorize(session, principal, write=True)
            now = transaction_timestamp(session)
            outcome = begin_idempotency(
                session,
                context="GLOBAL",
                principal_id=str(live.user_id),
                operation_id="adminUpsertMembership",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=now + timedelta(days=30),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if outcome.replay is not None:
                version = int(outcome.replay.headers["ETag"].strip('"v'))
                return MembershipMutationResult(dict(outcome.replay.body or {}), version)
            required_version = resolve_expected_version(expected_version)
            self._ensure_mutable(session)
            aggregate = (
                session.execute(
                    select(membership_sets)
                    .where(membership_sets.c.tenant_id == tenant_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if aggregate is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Tenant was not found"
                )
            if aggregate["version"] != required_version:
                raise ApplicationError(
                    code="version_conflict",
                    status=412,
                    message="MembershipSet version does not match If-Match",
                )
            if (
                session.execute(
                    select(users.c.user_id).where(users.c.user_id == user_id)
                ).scalar_one_or_none()
                is None
            ):
                raise ApplicationError(
                    code="resource_not_found", status=404, message="User was not found"
                )
            row = (
                session.execute(
                    pg_insert(memberships)
                    .values(tenant_id=tenant_id, user_id=user_id, role=role)
                    .on_conflict_do_update(
                        index_elements=[memberships.c.tenant_id, memberships.c.user_id],
                        set_={"role": role, "updated_at": now},
                    )
                    .returning(memberships)
                )
                .mappings()
                .one()
            )
            new_version = aggregate["version"] + 1
            session.execute(
                update(membership_sets)
                .where(membership_sets.c.tenant_id == tenant_id)
                .values(version=new_version, updated_at=now)
            )
            self.identity.revoke_user_credentials(session, user_id, now)
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.membership.upsert",
                target_type="MEMBERSHIP",
                target_id=f"{tenant_id}:{user_id}",
                tenant_id=tenant_id,
                reason="Tenant membership created or replaced",
                before_version=aggregate["version"],
                after_version=new_version,
            )
            body = json_wire_value(self._membership_view(row))
            complete_idempotency(
                session,
                outcome.record_id,
                status=200,
                body=body,
                headers={"ETag": f'"v{new_version}"'},
                resource_id=user_id,
            )
            return MembershipMutationResult(body=body, version=new_version)

        return run_transaction(self.session_factory, operation)

    def delete_membership(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        user_id: UUID,
        expected_version: ExpectedVersion,
        idempotency_key: str,
        request_hash: str,
    ) -> int:
        def operation(session: Session) -> int:
            live = self._authorize(session, principal, write=True)
            now = transaction_timestamp(session)
            outcome = begin_idempotency(
                session,
                context="GLOBAL",
                principal_id=str(live.user_id),
                operation_id="adminDeleteMembership",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=now + timedelta(days=30),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if outcome.replay is not None:
                return int(outcome.replay.headers["ETag"].strip('"v'))
            required_version = resolve_expected_version(expected_version)
            self._ensure_mutable(session)
            aggregate = (
                session.execute(
                    select(membership_sets)
                    .where(membership_sets.c.tenant_id == tenant_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if aggregate is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Tenant was not found"
                )
            if aggregate["version"] != required_version:
                raise ApplicationError(
                    code="version_conflict",
                    status=412,
                    message="MembershipSet version does not match If-Match",
                )
            result = session.execute(
                delete(memberships).where(
                    memberships.c.tenant_id == tenant_id,
                    memberships.c.user_id == user_id,
                )
            )
            if result.rowcount != 1:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Membership was not found"
                )
            new_version = aggregate["version"] + 1
            session.execute(
                update(membership_sets)
                .where(membership_sets.c.tenant_id == tenant_id)
                .values(version=new_version, updated_at=now)
            )
            self.identity.revoke_user_credentials(session, user_id, now)
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.membership.delete",
                target_type="MEMBERSHIP",
                target_id=f"{tenant_id}:{user_id}",
                tenant_id=tenant_id,
                reason="Tenant membership removed",
                before_version=aggregate["version"],
                after_version=new_version,
            )
            complete_idempotency(
                session,
                outcome.record_id,
                status=204,
                body=None,
                headers={"ETag": f'"v{new_version}"'},
                resource_id=user_id,
            )
            return new_version

        return run_transaction(self.session_factory, operation)

    @staticmethod
    def _audit_view(row: Any) -> dict[str, Any]:
        return {
            "audit_id": row["audit_id"],
            "actor_type": row["actor_type"],
            "actor_id": row["actor_id"],
            "tenant_id": row["tenant_id"],
            "action": row["action"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "reason": row["reason"],
            "created_at": row["created_at"],
        }

    def list_audit_records(
        self,
        principal: Principal,
        *,
        page_size: int,
        cursor: str | None,
        action: str | None,
        from_at: datetime,
        to_at: datetime,
    ) -> dict[str, Any]:
        self._validate_page_size(page_size)
        if from_at > to_at:
            raise ApplicationError(
                code="validation_failed",
                status=400,
                message="Audit from timestamp must not be after to timestamp",
            )

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=False)
            now = transaction_timestamp(session)
            binding = {
                "actor_id": str(live.user_id),
                "operation_id": "adminListAuditRecords",
                "action": action or "",
                "from": from_at.isoformat(),
                "to": to_at.isoformat(),
            }
            statement = select(audit_records).where(
                audit_records.c.created_at >= from_at,
                audit_records.c.created_at <= to_at,
            )
            if action is not None:
                statement = statement.where(audit_records.c.action == action)
            if cursor is not None:
                created_at, audit_id = self._decode_cursor(cursor, binding=binding, now=now)
                statement = statement.where(
                    or_(
                        audit_records.c.created_at < created_at,
                        and_(
                            audit_records.c.created_at == created_at,
                            audit_records.c.audit_id < audit_id,
                        ),
                    )
                )
            rows = (
                session.execute(
                    statement.order_by(
                        audit_records.c.created_at.desc(), audit_records.c.audit_id.desc()
                    ).limit(page_size + 1)
                )
                .mappings()
                .all()
            )
            visible = rows[:page_size]
            next_cursor = (
                self._encode_cursor(visible[-1], id_name="audit_id", binding=binding, now=now)
                if len(rows) > page_size
                else None
            )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.audit.list",
                target_type="AUDIT_COLLECTION",
                target_id="all",
                reason="Audit records read",
            )
            return json_wire_value(
                {
                    "items": [self._audit_view(row) for row in visible],
                    "page": {"next_cursor": next_cursor, "page_size": page_size},
                }
            )

        return run_transaction(self.session_factory, operation)
