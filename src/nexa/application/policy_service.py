from datetime import timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from nexa.application.errors import ApplicationError
from nexa.application.idempotency import begin_idempotency, complete_idempotency
from nexa.application.identity_service import IdentityService
from nexa.application.json_codec import json_wire_value
from nexa.application.preconditions import ExpectedVersion, resolve_expected_version
from nexa.config import Settings
from nexa.coordinator.accounting import account_locked, rebase_locked
from nexa.domain.identity import AuthorizationError, Principal, require_admin
from nexa.domain.policy import (
    OperationalMode,
    PolicyTransitionError,
    validate_mode_transition,
)
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import clock_timestamp, transaction_timestamp
from nexa.infrastructure.persistence.schema import (
    admission_counters,
    allocations,
    audit_records,
    policy_versions,
    tenant_policies,
)
from nexa.infrastructure.persistence.transactions import run_transaction


class RecoveryProofProvider(Protocol):
    def freeze_ready(self, session: Session) -> bool: ...

    def restore_verified(self, session: Session) -> bool: ...

    def readiness_verified(self, session: Session) -> bool: ...


class FailClosedRecoveryProofProvider:
    def freeze_ready(self, session: Session) -> bool:
        return False

    def restore_verified(self, session: Session) -> bool:
        return False

    def readiness_verified(self, session: Session) -> bool:
        return False


class PolicyService:
    _MAX_CPU_MILLIS = 100_000_000
    _MAX_MEMORY_BYTES = 9_223_372_036_854_775_807

    def __init__(
        self,
        session_factory,
        settings: Settings,
        identity_service: IdentityService,
        *,
        proof_provider: RecoveryProofProvider | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.identity = identity_service
        self.proofs = proof_provider or FailClosedRecoveryProofProvider()

    def _authorize(self, session: Session, principal: Principal, *, write: bool) -> Principal:
        live = self.identity.revalidate_principal(session, principal)
        try:
            require_admin(live, write=write)
        except AuthorizationError as exc:
            raise ApplicationError(code="permission_denied", status=403, message=str(exc)) from exc
        return live

    @staticmethod
    def _audit(
        session: Session,
        *,
        actor_id: UUID,
        action: str,
        target_type: str,
        target_id: str,
        reason: str,
        before_version: int | None,
        after_version: int | None,
        tenant_id: UUID | None = None,
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
    def _global_view(row: Any) -> dict[str, Any]:
        return {
            "version": row["policy_version"],
            "global_outstanding_limit": row["global_outstanding_limit"],
            "operational_mode": row["operational_mode"],
            "updated_at": row["created_at"],
        }

    @staticmethod
    def _tenant_view(row: Any) -> dict[str, Any]:
        return {
            "tenant_id": row["tenant_id"],
            "version": row["version"],
            "weight": row["weight"],
            "outstanding_limit": row["outstanding_limit"],
            "user_outstanding_limit": row["user_outstanding_limit"],
            "concurrent_attempt_limit": row["tenant_active_limit"],
            "user_concurrent_attempt_limit": row["user_active_limit"],
            "resource_limit": {
                "cpu_millis": row["cpu_limit_millis"],
                "memory_bytes": row["memory_limit_bytes"],
                "gpu_count": row["gpu_limit"],
            },
            "submit_rate_per_second": row["tenant_rate_per_second"],
            "submit_burst": int(row["tenant_rate_burst"]),
            "user_submit_rate_per_second": row["user_rate_per_second"],
            "user_submit_burst": int(row["user_rate_burst"]),
            "updated_at": row["updated_at"],
        }

    def get_global_policy(self, principal: Principal) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=False)
            row = (
                session.execute(
                    select(policy_versions).where(policy_versions.c.is_current.is_(True))
                )
                .mappings()
                .one()
            )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.policy.global.get",
                target_type="GLOBAL_POLICY",
                target_id="current",
                reason="Global policy read",
                before_version=row["policy_version"],
                after_version=row["policy_version"],
            )
            return json_wire_value(self._global_view(row))

        return run_transaction(self.session_factory, operation)

    def get_tenant_policy(self, principal: Principal, tenant_id: UUID) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=False)
            row = (
                session.execute(
                    select(tenant_policies).where(
                        tenant_policies.c.tenant_id == tenant_id,
                        tenant_policies.c.is_current.is_(True),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Tenant policy was not found"
                )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.policy.tenant.get",
                target_type="TENANT_POLICY",
                target_id=str(tenant_id),
                tenant_id=tenant_id,
                reason="Tenant policy read",
                before_version=row["version"],
                after_version=row["version"],
            )
            return json_wire_value(self._tenant_view(row))

        return run_transaction(self.session_factory, operation)

    def update_global_policy(
        self,
        principal: Principal,
        *,
        expected_version: ExpectedVersion,
        global_outstanding_limit: int | None,
        operational_mode: str | None,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any]:
        if global_outstanding_limit is None and operational_mode is None:
            raise ApplicationError(
                code="validation_failed",
                status=422,
                message="At least one global policy field must be updated",
            )
        if global_outstanding_limit is not None and not 1 <= global_outstanding_limit <= 1_000_000:
            raise ApplicationError(
                code="validation_failed",
                status=422,
                message="Global outstanding limit is outside the approved bounds",
            )
        try:
            target_mode = (
                OperationalMode(operational_mode) if operational_mode is not None else None
            )
        except ValueError:
            raise ApplicationError(
                code="validation_failed", status=422, message="Unknown operational mode"
            ) from None

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=True)
            now = transaction_timestamp(session)
            outcome = begin_idempotency(
                session,
                context="GLOBAL",
                principal_id=str(live.user_id),
                operation_id="adminUpdateGlobalPolicy",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=now + timedelta(days=30),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if outcome.replay is not None:
                return dict(outcome.replay.body or {})
            required_version = resolve_expected_version(expected_version)
            current = (
                session.execute(
                    select(policy_versions)
                    .where(policy_versions.c.is_current.is_(True))
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if current["policy_version"] != required_version:
                raise ApplicationError(
                    code="version_conflict",
                    status=412,
                    message="Global policy version does not match If-Match",
                )
            current_mode = OperationalMode(current["operational_mode"])
            if current_mode is OperationalMode.WRITE_FROZEN and target_mode not in {
                OperationalMode.ADMISSION_OFF
            }:
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Only a verified restore transition may mutate frozen policy",
                )
            if (
                current_mode is OperationalMode.WRITE_FROZEN
                and global_outstanding_limit is not None
            ):
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Frozen recovery transition cannot change admission limits",
                )
            new_limit = (
                current["global_outstanding_limit"]
                if global_outstanding_limit is None
                else global_outstanding_limit
            )
            counter = (
                session.execute(
                    select(admission_counters)
                    .where(
                        admission_counters.c.scope_type == "GLOBAL",
                        admission_counters.c.scope_id == "global",
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if counter is not None and new_limit < counter["outstanding"]:
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Global outstanding limit is below the committed counter",
                )
            new_mode = current_mode if target_mode is None else target_mode
            if new_mode is not current_mode:
                unreleased = session.execute(
                    select(func.count())
                    .select_from(allocations)
                    .where(allocations.c.state != "RELEASED")
                ).scalar_one()
                try:
                    validate_mode_transition(
                        current_mode,
                        new_mode,
                        freeze_ready=unreleased == 0 and self.proofs.freeze_ready(session),
                        restore_verified=self.proofs.restore_verified(session),
                        readiness_verified=self.proofs.readiness_verified(session),
                    )
                except PolicyTransitionError as exc:
                    raise ApplicationError(
                        code="state_conflict", status=409, message=str(exc)
                    ) from exc
                if new_mode is OperationalMode.WRITE_FROZEN:
                    # The coordinator heartbeat reads the mode after locking
                    # ledger rows; charging here orders it before the freeze.
                    account_locked(session, clock_timestamp(session))
            new_version = current["policy_version"] + 1
            session.execute(
                update(policy_versions)
                .where(policy_versions.c.policy_version == current["policy_version"])
                .values(is_current=False)
            )
            row = (
                session.execute(
                    insert(policy_versions)
                    .values(
                        policy_version=new_version,
                        global_outstanding_limit=new_limit,
                        operational_mode=new_mode.value,
                        created_by_user_id=live.user_id,
                        created_at=now,
                        is_current=True,
                    )
                    .returning(policy_versions)
                )
                .mappings()
                .one()
            )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.policy.global.update",
                target_type="GLOBAL_POLICY",
                target_id=str(new_version),
                reason="Global policy version updated",
                before_version=current["policy_version"],
                after_version=new_version,
            )
            response = json_wire_value(self._global_view(row))
            complete_idempotency(
                session,
                outcome.record_id,
                status=200,
                body=response,
                headers={"ETag": f'"v{new_version}"'},
            )
            return response

        return run_transaction(self.session_factory, operation)

    @staticmethod
    def _positive_decimal(value: Any, name: str, *, maximum: Decimal | None = None) -> Decimal:
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ApplicationError(
                code="validation_failed", status=422, message=f"{name} must be positive"
            )
        if maximum is not None and value > maximum:
            raise ApplicationError(
                code="validation_failed", status=422, message=f"{name} exceeds its maximum"
            )
        return value

    @staticmethod
    def _positive_int(value: Any, name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ApplicationError(
                code="validation_failed", status=422, message=f"{name} must be positive"
            )
        return value

    def _validated_tenant_changes(self, changes: dict[str, Any]) -> dict[str, Any]:
        if not changes:
            raise ApplicationError(
                code="validation_failed",
                status=422,
                message="At least one tenant policy field must be updated",
            )
        allowed = {
            "weight",
            "outstanding_limit",
            "user_outstanding_limit",
            "concurrent_attempt_limit",
            "user_concurrent_attempt_limit",
            "resource_limit",
            "submit_rate_per_second",
            "submit_burst",
            "user_submit_rate_per_second",
            "user_submit_burst",
        }
        if not changes.keys() <= allowed:
            raise ApplicationError(
                code="validation_failed", status=422, message="Unknown tenant policy field"
            )
        values: dict[str, Any] = {}
        if "weight" in changes:
            values["weight"] = self._positive_decimal(
                changes["weight"], "weight", maximum=Decimal("1000")
            )
        integer_fields = {
            "outstanding_limit": "outstanding_limit",
            "user_outstanding_limit": "user_outstanding_limit",
            "concurrent_attempt_limit": "tenant_active_limit",
            "user_concurrent_attempt_limit": "user_active_limit",
        }
        for wire_name, column_name in integer_fields.items():
            if wire_name in changes:
                values[column_name] = self._positive_int(changes[wire_name], wire_name)
        decimal_fields = {
            "submit_rate_per_second": "tenant_rate_per_second",
            "user_submit_rate_per_second": "user_rate_per_second",
        }
        for wire_name, column_name in decimal_fields.items():
            if wire_name in changes:
                values[column_name] = self._positive_decimal(changes[wire_name], wire_name)
        burst_fields = {
            "submit_burst": "tenant_rate_burst",
            "user_submit_burst": "user_rate_burst",
        }
        for wire_name, column_name in burst_fields.items():
            if wire_name in changes:
                values[column_name] = Decimal(self._positive_int(changes[wire_name], wire_name))
        if "resource_limit" in changes:
            resource = changes["resource_limit"]
            if not isinstance(resource, dict) or set(resource) != {
                "cpu_millis",
                "memory_bytes",
                "gpu_count",
            }:
                raise ApplicationError(
                    code="validation_failed",
                    status=422,
                    message="resource_limit must contain CPU, memory and GPU values",
                )
            maximums = {
                "cpu_millis": self._MAX_CPU_MILLIS,
                "memory_bytes": self._MAX_MEMORY_BYTES,
                "gpu_count": 1,
            }
            for field in ("cpu_millis", "memory_bytes", "gpu_count"):
                value = resource[field]
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ApplicationError(
                        code="validation_failed",
                        status=422,
                        message=f"resource_limit.{field} must be nonnegative",
                    )
                if value > maximums[field]:
                    raise ApplicationError(
                        code="validation_failed",
                        status=422,
                        message=f"resource_limit.{field} exceeds its maximum",
                    )
            values.update(
                cpu_limit_millis=resource["cpu_millis"],
                memory_limit_bytes=resource["memory_bytes"],
                gpu_limit=resource["gpu_count"],
            )
        return values

    def update_tenant_policy(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        expected_version: ExpectedVersion,
        changes: dict[str, Any],
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any]:
        changed_values = self._validated_tenant_changes(changes)

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=True)
            now = transaction_timestamp(session)
            outcome = begin_idempotency(
                session,
                context="GLOBAL",
                principal_id=str(live.user_id),
                operation_id="adminUpdateTenantPolicy",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=now + timedelta(days=30),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if outcome.replay is not None:
                return dict(outcome.replay.body or {})
            required_version = resolve_expected_version(expected_version)
            mode = session.execute(
                select(policy_versions.c.operational_mode)
                .where(policy_versions.c.is_current.is_(True))
                .with_for_update()
            ).scalar_one()
            if mode == "WRITE_FROZEN":
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Tenant policy mutation is disabled while writes are frozen",
                )
            current = (
                session.execute(
                    select(tenant_policies)
                    .where(
                        tenant_policies.c.tenant_id == tenant_id,
                        tenant_policies.c.is_current.is_(True),
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if current is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Tenant policy was not found"
                )
            if current["version"] != required_version:
                raise ApplicationError(
                    code="version_conflict",
                    status=412,
                    message="Tenant policy version does not match If-Match",
                )
            final = dict(current)
            final.update(changed_values)
            tenant_counter = (
                session.execute(
                    select(admission_counters)
                    .where(
                        admission_counters.c.scope_type == "TENANT",
                        admission_counters.c.scope_id == str(tenant_id),
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if tenant_counter is not None and (
                final["outstanding_limit"] < tenant_counter["outstanding"]
                or final["tenant_active_limit"] < tenant_counter["active_attempts"]
            ):
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Tenant policy is below a committed admission counter",
                )
            user_counters = (
                session.execute(
                    select(admission_counters)
                    .where(
                        admission_counters.c.scope_type == "USER",
                        admission_counters.c.scope_id.like(f"{tenant_id}:%"),
                    )
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            if any(
                final["user_outstanding_limit"] < row["outstanding"]
                or final["user_active_limit"] < row["active_attempts"]
                for row in user_counters
            ):
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Tenant policy is below a committed user counter",
                )
            held = session.execute(
                select(
                    func.coalesce(func.sum(allocations.c.cpu_millis), 0),
                    func.coalesce(func.sum(allocations.c.memory_bytes), 0),
                    func.coalesce(func.sum(allocations.c.gpu_count), 0),
                ).where(
                    allocations.c.tenant_id == tenant_id,
                    allocations.c.state != "RELEASED",
                )
            ).one()
            if (
                final["cpu_limit_millis"] < held[0]
                or final["memory_limit_bytes"] < held[1]
                or final["gpu_limit"] < held[2]
            ):
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Tenant resource policy is below unreleased allocations",
                )
            # Charge at the old weight before publishing the new policy.
            now = clock_timestamp(session)
            account_locked(session, now)
            new_version = current["version"] + 1
            session.execute(
                update(tenant_policies)
                .where(
                    tenant_policies.c.tenant_id == tenant_id,
                    tenant_policies.c.version == current["version"],
                )
                .values(is_current=False, updated_at=now)
            )
            insert_values = {
                key: final[key]
                for key in (
                    "weight",
                    "cpu_limit_millis",
                    "memory_limit_bytes",
                    "gpu_limit",
                    "outstanding_limit",
                    "user_outstanding_limit",
                    "tenant_active_limit",
                    "user_active_limit",
                    "tenant_rate_per_second",
                    "tenant_rate_burst",
                    "user_rate_per_second",
                    "user_rate_burst",
                )
            }
            row = (
                session.execute(
                    insert(tenant_policies)
                    .values(
                        tenant_id=tenant_id,
                        version=new_version,
                        is_current=True,
                        created_at=now,
                        updated_at=now,
                        **insert_values,
                    )
                    .returning(tenant_policies)
                )
                .mappings()
                .one()
            )
            if tenant_counter is not None and (
                current["tenant_active_limit"]
                <= tenant_counter["active_attempts"]
                < final["tenant_active_limit"]
            ):
                session.execute(
                    update(admission_counters)
                    .where(
                        admission_counters.c.scope_type == "TENANT",
                        admission_counters.c.scope_id == str(tenant_id),
                    )
                    .values(eligible_resumed_at=now)
                )
            for counter in user_counters:
                if (
                    current["user_active_limit"]
                    <= counter["active_attempts"]
                    < final["user_active_limit"]
                ):
                    session.execute(
                        update(admission_counters)
                        .where(
                            admission_counters.c.scope_type == "USER",
                            admission_counters.c.scope_id == counter["scope_id"],
                        )
                        .values(eligible_resumed_at=now)
                    )
            rebase_locked(session, now)
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.policy.tenant.update",
                target_type="TENANT_POLICY",
                target_id=str(tenant_id),
                tenant_id=tenant_id,
                reason="Tenant policy version updated",
                before_version=current["version"],
                after_version=new_version,
            )
            response = json_wire_value(self._tenant_view(row))
            complete_idempotency(
                session,
                outcome.record_id,
                status=200,
                body=response,
                headers={"ETag": f'"v{new_version}"'},
            )
            return response

        return run_transaction(self.session_factory, operation)
