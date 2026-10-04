"""B15 admin worker control (drain/disable/enable) and bounded recovery reads.

Mutations follow the control pipeline: authorize -> idempotency (replay before
If-Match) -> 428 -> WRITE_FROZEN 409 -> worker lock -> 412 -> state guard 409, then
one transaction. Disable locks policy -> worker -> job -> session -> attempt -> lease
-> allocation -> grant per live lease in job_id order, the same order as cleanup and
the lease reaper. No Docker access; the worker stops containers after its next renew.
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.orm import Session

from nexa.application.admin_queries import AdminQueryMixin
from nexa.application.admin_service import AdminService
from nexa.application.errors import ApplicationError
from nexa.application.idempotency import begin_idempotency, complete_idempotency
from nexa.application.job_recovery import fence_attempt, revoke_leftover_authority
from nexa.application.json_codec import json_wire_value
from nexa.application.preconditions import ExpectedVersion, resolve_expected_version
from nexa.application.worker_service import WorkerService
from nexa.domain.identity import Principal
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import clock_timestamp, transaction_timestamp
from nexa.infrastructure.persistence.schema_v16 import RECOVERY_EVENT_TYPES
from nexa.infrastructure.persistence.transactions import run_transaction
from nexa.infrastructure.security import CursorCodec, CursorError

_ACTIVE = ("DISPATCHING", "RUNNING", "PAUSING")
# The same freshness window dispatch uses (JobService._worker_is_ready).
_HEARTBEAT_FRESH = timedelta(seconds=30)


def _conflict(message: str) -> ApplicationError:
    return ApplicationError(code="state_conflict", status=409, message=message)


class AdminWorkerService(AdminQueryMixin, AdminService):
    # ---- views -------------------------------------------------------------

    @staticmethod
    def _inventory_view(session: Session, worker: Any) -> dict[str, Any] | None:
        if worker["current_inventory_version"] is None:
            return None
        row = (
            session.execute(
                select(s.worker_inventories).where(
                    s.worker_inventories.c.worker_id == worker["worker_id"],
                    s.worker_inventories.c.inventory_version == worker["current_inventory_version"],
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        devices = (
            session.execute(
                select(s.gpu_devices)
                .where(
                    s.gpu_devices.c.worker_id == worker["worker_id"],
                    s.gpu_devices.c.inventory_version == row["inventory_version"],
                )
                .order_by(s.gpu_devices.c.gpu_uuid)
            )
            .mappings()
            .all()
        )
        workload = row["workload_capabilities"]
        return {
            "architecture": row["architecture"],
            "host_cpu_millis": row["host_cpu_millis"],
            "host_memory_bytes": row["host_memory_bytes"],
            "allocatable": {
                "cpu_millis": row["allocatable_cpu_millis"],
                "memory_bytes": row["allocatable_memory_bytes"],
                "gpu_count": row["allocatable_gpu_count"],
            },
            "runtime": row["runtime_capabilities"],
            "adapters": workload["adapters"],
            "images": workload["images"],
            "frameworks": workload["frameworks"],
            "gpu_devices": [
                {
                    "uuid": device["gpu_uuid"],
                    "model": device["model"],
                    "memory_bytes": device["memory_bytes"],
                    "compute_capability": device["compute_capability"],
                    "driver_version": device["driver_version"],
                    "cuda_driver_api_version": device["api_version"],
                    "healthy": device["healthy"],
                }
                for device in devices
            ],
            "discovered_at": row["observed_at"],
        }

    def _worker_view(self, session: Session, worker: Any) -> dict[str, Any]:
        return {
            "worker_id": worker["worker_id"],
            "current_incarnation_id": worker["current_incarnation_id"],
            "health": worker["health"],
            "admin_state": worker["admin_state"],
            "inventory": self._inventory_view(session, worker),
            "version": worker["version"],
            "last_heartbeat_at": worker["last_heartbeat_at"],
            "ready_at": worker["ready_at"],
        }

    @staticmethod
    def _allocation_view(row: Any, gpu_uuids: list[str]) -> dict[str, Any]:
        return {
            "allocation_id": row["allocation_id"],
            "tenant_id": row["tenant_id"],
            "job_id": row["job_id"],
            "attempt_id": row["attempt_id"],
            "worker_id": row["worker_id"],
            "resources": {
                "cpu_millis": row["cpu_millis"],
                "memory_bytes": row["memory_bytes"],
                "gpu_count": row["gpu_count"],
            },
            "gpu_uuids": gpu_uuids,
            "state": row["state"],
            "held_at": row["held_at"],
            "quarantined_at": row["quarantined_at"],
            "released_at": row["released_at"],
        }

    @staticmethod
    def _event_view(row: Any) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "tenant_id": row["tenant_id"],
            "job_id": row["job_id"],
            "sequence": row["sequence"],
            "type": row["event_type"],
            "reason": row["reason"],
            "actor_type": row["actor_type"],
            "created_at": row["created_at"],
        }

    def _id_cursor(self, now: datetime) -> CursorCodec:
        return CursorCodec(
            self._cursor_key, ttl_seconds=self.settings.cursor_ttl_seconds, now=lambda: now
        )

    def _decode_id_cursor(self, cursor: str, *, binding: dict[str, str], now: datetime) -> UUID:
        try:
            return UUID(self._id_cursor(now).decode(cursor, expected_binding=binding)["id"])
        except (CursorError, KeyError, ValueError):
            raise ApplicationError(
                code="invalid_cursor",
                status=400,
                message="The pagination cursor is invalid for this request",
            ) from None

    # ---- reads -------------------------------------------------------------

    def list_workers(
        self, principal: Principal, *, page_size: int, cursor: str | None
    ) -> dict[str, Any]:
        self._validate_page_size(page_size)

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=False)
            now = transaction_timestamp(session)
            binding = {"actor_id": str(live.user_id), "operation_id": "adminListWorkers"}
            statement = select(s.workers)
            if cursor is not None:
                after = self._decode_id_cursor(cursor, binding=binding, now=now)
                statement = statement.where(s.workers.c.worker_id > after)
            rows = (
                session.execute(statement.order_by(s.workers.c.worker_id).limit(page_size + 1))
                .mappings()
                .all()
            )
            visible = rows[:page_size]
            next_cursor = (
                self._id_cursor(now).encode(
                    binding=binding, position={"id": str(visible[-1]["worker_id"])}
                )
                if len(rows) > page_size
                else None
            )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.worker.list",
                target_type="WORKER_COLLECTION",
                target_id="all",
                reason="Worker list read",
            )
            return json_wire_value(
                {
                    "items": [self._worker_view(session, row) for row in visible],
                    "page": {"next_cursor": next_cursor, "page_size": page_size},
                }
            )

        return run_transaction(self.session_factory, operation)

    def get_worker(self, principal: Principal, worker_id: UUID) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=False)
            row = self._worker(session, worker_id, lock=False)
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.worker.get",
                target_type="WORKER",
                target_id=str(worker_id),
                reason="Worker read",
            )
            return json_wire_value(self._worker_view(session, row))

        return run_transaction(self.session_factory, operation)

    def list_allocations(
        self,
        principal: Principal,
        *,
        page_size: int,
        cursor: str | None,
        state: str | None,
    ) -> dict[str, Any]:
        self._validate_page_size(page_size)

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=False)
            now = transaction_timestamp(session)
            binding = {
                "actor_id": str(live.user_id),
                "operation_id": "adminListAllocations",
                "state": state or "",
            }
            # UUIDv7 ids are time-ordered, so the primary key is the newest-first keyset.
            statement = select(s.allocations)
            if state is not None:
                statement = statement.where(s.allocations.c.state == state)
            if cursor is not None:
                before = self._decode_id_cursor(cursor, binding=binding, now=now)
                statement = statement.where(s.allocations.c.allocation_id < before)
            rows = (
                session.execute(
                    statement.order_by(s.allocations.c.allocation_id.desc()).limit(page_size + 1)
                )
                .mappings()
                .all()
            )
            visible = rows[:page_size]
            claims: dict[UUID, list[str]] = {row["allocation_id"]: [] for row in visible}
            if claims:
                for allocation_id, gpu_uuid in session.execute(
                    select(
                        s.allocation_gpu_claims.c.allocation_id,
                        s.allocation_gpu_claims.c.gpu_uuid,
                    ).where(s.allocation_gpu_claims.c.allocation_id.in_(list(claims)))
                ):
                    claims[allocation_id].append(gpu_uuid)
            next_cursor = (
                self._id_cursor(now).encode(
                    binding=binding, position={"id": str(visible[-1]["allocation_id"])}
                )
                if len(rows) > page_size
                else None
            )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.allocation.list",
                target_type="ALLOCATION_COLLECTION",
                target_id="all",
                reason="Allocation list read",
            )
            return json_wire_value(
                {
                    "items": [
                        self._allocation_view(row, claims[row["allocation_id"]]) for row in visible
                    ],
                    "page": {"next_cursor": next_cursor, "page_size": page_size},
                }
            )

        return run_transaction(self.session_factory, operation)

    def list_recovery_events(
        self,
        principal: Principal,
        *,
        page_size: int,
        cursor: str | None,
        from_at: datetime,
        to_at: datetime,
    ) -> dict[str, Any]:
        self._validate_page_size(page_size)
        if from_at > to_at:
            raise ApplicationError(
                code="validation_failed",
                status=400,
                message="Recovery event from timestamp must not be after to timestamp",
            )

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=False)
            now = transaction_timestamp(session)
            binding = {
                "actor_id": str(live.user_id),
                "operation_id": "adminListRecoveryEvents",
                "from": from_at.isoformat(),
                "to": to_at.isoformat(),
            }
            events = s.events
            # Matches the partial index ix_events_b15_recovery (created_at, event_id).
            statement = select(events).where(
                events.c.event_type.in_(RECOVERY_EVENT_TYPES),
                events.c.created_at >= from_at,
                events.c.created_at <= to_at,
            )
            if cursor is not None:
                created_at, event_id = self._decode_cursor(cursor, binding=binding, now=now)
                statement = statement.where(
                    or_(
                        events.c.created_at < created_at,
                        and_(events.c.created_at == created_at, events.c.event_id < event_id),
                    )
                )
            rows = (
                session.execute(
                    statement.order_by(events.c.created_at.desc(), events.c.event_id.desc()).limit(
                        page_size + 1
                    )
                )
                .mappings()
                .all()
            )
            visible = rows[:page_size]
            next_cursor = (
                self._encode_cursor(visible[-1], id_name="event_id", binding=binding, now=now)
                if len(rows) > page_size
                else None
            )
            self._audit(
                session,
                actor_id=live.user_id,
                action="admin.recovery_event.list",
                target_type="EVENT_COLLECTION",
                target_id="recovery",
                reason="Recovery events read",
            )
            return json_wire_value(
                {
                    "items": [self._event_view(row) for row in visible],
                    "page": {"next_cursor": next_cursor, "page_size": page_size},
                }
            )

        return run_transaction(self.session_factory, operation)

    # ---- mutations ---------------------------------------------------------

    @staticmethod
    def _worker(session: Session, worker_id: UUID, *, lock: bool) -> Any:
        statement = select(s.workers).where(s.workers.c.worker_id == worker_id)
        if lock:
            statement = statement.with_for_update()
        row = session.execute(statement).mappings().one_or_none()
        if row is None:
            raise ApplicationError(
                code="resource_not_found", status=404, message="Worker was not found"
            )
        return row

    def _mutate_worker(
        self,
        principal: Principal,
        *,
        worker_id: UUID,
        operation_id: str,
        action: str,
        reason: str,
        status: int,
        expected_version: ExpectedVersion,
        idempotency_key: str,
        request_hash: str,
        apply,
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, write=True)
            started = transaction_timestamp(session)
            outcome = begin_idempotency(
                session,
                context="GLOBAL",
                principal_id=str(live.user_id),
                operation_id=operation_id,
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=started + timedelta(days=30),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if outcome.replay is not None:
                return dict(outcome.replay.body or {})
            required_version = resolve_expected_version(expected_version)
            self._ensure_mutable(session)
            worker = self._worker(session, worker_id, lock=True)
            if worker["version"] != required_version:
                raise ApplicationError(
                    code="version_conflict",
                    status=412,
                    message="Worker version does not match If-Match",
                )
            now = clock_timestamp(session)
            values = apply(session, worker, now, live)
            updated = (
                session.execute(
                    update(s.workers)
                    .where(s.workers.c.worker_id == worker_id)
                    .values(**values, version=worker["version"] + 1, updated_at=now)
                    .returning(s.workers)
                )
                .mappings()
                .one()
            )
            # The free-text reason is stored verbatim only in the audit record.
            self._audit(
                session,
                actor_id=live.user_id,
                action=action,
                target_type="WORKER",
                target_id=str(worker_id),
                reason=reason,
                before_version=worker["version"],
                after_version=updated["version"],
            )
            response = json_wire_value(self._worker_view(session, updated))
            complete_idempotency(
                session,
                outcome.record_id,
                status=status,
                body=response,
                headers={"ETag": f'"v{updated["version"]}"'},
                resource_id=worker_id,
            )
            return response

        return run_transaction(self.session_factory, operation)

    def drain_worker(self, principal: Principal, *, worker_id: UUID, reason: str, **request):
        def apply(session, worker, now, live):
            if worker["admin_state"] != "ENABLED":
                raise _conflict(f"A {worker['admin_state']} worker cannot be drained")
            # New allocations stop at once (dispatch requires ENABLED); attempts continue.
            return {"admin_state": "DRAINING"}

        return self._mutate_worker(
            principal,
            worker_id=worker_id,
            operation_id="adminDrainWorker",
            action="admin.worker.drain",
            reason=reason,
            status=202,
            apply=apply,
            **request,
        )

    def disable_worker(self, principal: Principal, *, worker_id: UUID, reason: str, **request):
        def apply(session, worker, now, live):
            if worker["admin_state"] == "DISABLED":
                raise _conflict("The worker is already disabled")
            self._fence_worker_attempts(session, worker_id, now, live.user_id)
            # A disabled worker never advertises READY; heartbeats keep it STARTING.
            return {"admin_state": "DISABLED", "health": "STARTING", "ready_at": None}

        return self._mutate_worker(
            principal,
            worker_id=worker_id,
            operation_id="adminDisableWorker",
            action="admin.worker.disable",
            reason=reason,
            status=202,
            apply=apply,
            **request,
        )

    def enable_worker(self, principal: Principal, *, worker_id: UUID, reason: str, **request):
        def apply(session, worker, now, live):
            if worker["admin_state"] == "ENABLED":
                raise _conflict("The worker is already enabled")
            self._require_enable_ready(session, worker, now)
            # Health is left as is: dispatch waits for a heartbeat to report READY.
            return {"admin_state": "ENABLED"}

        return self._mutate_worker(
            principal,
            worker_id=worker_id,
            operation_id="adminEnableWorker",
            action="admin.worker.enable",
            reason=reason,
            status=200,
            apply=apply,
            **request,
        )

    @staticmethod
    def _require_enable_ready(session: Session, worker: Any, now: datetime) -> None:
        """Guarded readiness recheck; never releases quarantine or revives authority.

        A DISABLED worker is held at STARTING by its heartbeats, so its READY evidence
        is the incarnation's `ready_checked_at`: the latest heartbeat passed every
        READY check (storage, inventory, reconciliation, containers) except the admin
        state. Reconciliation, quarantine and inventory are rechecked here because
        they may have changed since that heartbeat (finding B15-R07).
        """
        if worker["admin_state"] == "DRAINING" and worker["health"] != "READY":
            raise _conflict("The worker is not READY")
        failure = worker_readiness_failure(session, worker, now)
        if failure is not None:
            raise _conflict(failure)
        pending = session.execute(
            select(s.allocations.c.allocation_id)
            .where(
                s.allocations.c.worker_id == worker["worker_id"],
                s.allocations.c.state == "QUARANTINED",
            )
            .limit(1)
        ).scalar_one_or_none()
        if pending is not None:
            raise _conflict("Quarantined allocations still await verified cleanup")

    @staticmethod
    def _fence_worker_attempts(session: Session, worker_id: UUID, now, actor_id: UUID) -> None:
        """Fence every live lease on the worker; allocations stay QUARANTINED and charged."""
        probe = session.execute(
            select(s.attempt_leases.c.job_id, s.attempt_leases.c.lease_id)
            .join(s.allocations, s.allocations.c.allocation_id == s.attempt_leases.c.allocation_id)
            .where(
                s.allocations.c.worker_id == worker_id,
                s.attempt_leases.c.revoked_at.is_(None),
            )
            .order_by(s.attempt_leases.c.job_id, s.attempt_leases.c.lease_id)
        ).all()
        for job_id, lease_id in probe:
            job = (
                session.execute(select(s.jobs).where(s.jobs.c.job_id == job_id).with_for_update())
                .mappings()
                .one()
            )
            session.execute(
                select(s.logical_sessions)
                .where(s.logical_sessions.c.job_id == job_id)
                .with_for_update()
            ).all()
            lease = (
                session.execute(
                    select(s.attempt_leases).where(s.attempt_leases.c.lease_id == lease_id)
                )
                .mappings()
                .one()
            )
            attempt = (
                session.execute(
                    select(s.attempts)
                    .where(s.attempts.c.attempt_id == lease["attempt_id"])
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            lease = (
                session.execute(
                    select(s.attempt_leases)
                    .where(s.attempt_leases.c.lease_id == lease_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            session.execute(
                select(s.allocations)
                .where(s.allocations.c.allocation_id == lease["allocation_id"])
                .with_for_update()
            ).all()
            session.execute(
                select(s.attempt_authority_grants)
                .where(
                    s.attempt_authority_grants.c.attempt_id == attempt["attempt_id"],
                    s.attempt_authority_grants.c.ended_at.is_(None),
                )
                .with_for_update()
            ).all()
            # CAS recheck: a reaper, cancel or cleanup committed first leaves nothing to do.
            if lease["revoked_at"] is not None:
                continue
            if job["state"] in _ACTIVE and attempt["job_fence"] == job["job_fence"]:
                fence_attempt(
                    session,
                    attempt_id=attempt["attempt_id"],
                    now=now,
                    state="STOPPING",
                    failure_class="INFRASTRUCTURE",
                    failure_reason="WORKER_DISABLED",
                    revoke_reason="WORKER_DISABLED",
                )
                # Desired state is kept: a PAUSING job recovers toward PAUSED.
                _job_event(
                    session,
                    job,
                    now,
                    actor_id,
                    "ATTEMPT_FENCED",
                    state="RECOVERING",
                    job_fence=job["job_fence"] + 1,
                )
            else:
                revoke_leftover_authority(
                    session,
                    attempt_id=attempt["attempt_id"],
                    allocation_id=lease["allocation_id"],
                    now=now,
                    revoke_reason="WORKER_DISABLED",
                )
                _job_event(session, job, now, actor_id, "LEASE_REVOKED", keep_version=True)


def _job_event(
    session: Session,
    job: Any,
    now,
    actor_id: UUID,
    event_type: str,
    *,
    keep_version: bool = False,
    **changes,
):
    """One job UPDATE with its event and audit (see job_recovery.extend_retention).

    An event without a state change keeps the job version (L1).
    """
    version = job["version"] if keep_version else job["version"] + 1
    session.execute(
        update(s.jobs)
        .where(s.jobs.c.job_id == job["job_id"])
        .values(
            **changes,
            version=version,
            event_sequence=job["event_sequence"] + 1,
            updated_at=now,
        )
    )
    session.execute(
        insert(s.events).values(
            event_id=new_uuid7(),
            tenant_id=job["tenant_id"],
            job_id=job["job_id"],
            sequence=job["event_sequence"] + 1,
            event_type=event_type,
            reason="WORKER_DISABLED",
            actor_type="ADMIN",
            actor_id=str(actor_id),
            created_at=now,
        )
    )
    session.execute(
        insert(s.audit_records).values(
            audit_id=new_uuid7(),
            actor_type="ADMIN",
            actor_id=str(actor_id),
            tenant_id=job["tenant_id"],
            action=event_type,
            target_type="JOB",
            target_id=str(job["job_id"]),
            before_version=job["version"],
            after_version=version,
            reason="WORKER_DISABLED",
            created_at=now,
        )
    )


def worker_readiness_failure(session: Session, worker: Any, now: datetime) -> str | None:
    """READY evidence shared by worker enable and reopening NORMAL (B19-R02).

    Returns the first failed condition, or None: a heartbeat within 30 s of DB time
    that passed the READY checks, a reconciled current incarnation and an inventory
    that passes capability checks. Health/admin state and quarantine are the
    caller's own conditions.
    """
    heartbeat = worker["last_heartbeat_at"]
    if heartbeat is None or heartbeat < now - _HEARTBEAT_FRESH:
        return "The worker has no fresh heartbeat"
    incarnation = (
        session.execute(
            select(s.worker_incarnations).where(
                s.worker_incarnations.c.worker_incarnation_id == worker["current_incarnation_id"]
            )
        )
        .mappings()
        .one_or_none()
    )
    if incarnation is not None and (
        incarnation["ready_checked_at"] is None or incarnation["ready_checked_at"] != heartbeat
    ):
        return "The latest worker heartbeat did not pass the READY checks"
    if (
        incarnation is None
        or incarnation["ended_at"] is not None
        or not incarnation["reconciliation_drained"]
        or incarnation["reconciliation_snapshot"]
        != WorkerService._reconciliation_snapshot(session, worker["worker_id"])
    ):
        return "The current worker incarnation is not reconciled"
    inventory = AdminWorkerService._inventory_view(session, worker)
    if inventory is None or not WorkerService._inventory_can_be_ready(inventory):
        return "The worker inventory does not pass capability checks"
    return None
