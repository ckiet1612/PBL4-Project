from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from nexa.api.schemas import AdoptRequest, Authority, HeartbeatRequest, RenewRequest
from nexa.application.errors import ApplicationError
from nexa.application.idempotency import begin_idempotency, complete_idempotency
from nexa.application.identity_service import IdentityService
from nexa.application.json_codec import jcs_request_hash, json_wire_value
from nexa.config import Settings
from nexa.infrastructure.artifacts.store import ArtifactError
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import clock_timestamp, transaction_timestamp
from nexa.infrastructure.persistence.schema import (
    allocation_gpu_claims,
    allocations,
    attempt_authority_grants,
    attempt_leases,
    attempts,
    callback_receipts,
    checkpoint_reservations,
    container_identities,
    gpu_devices,
    jobs,
    policy_versions,
    result_reservations,
    worker_incarnations,
    worker_inventories,
    workers,
)
from nexa.infrastructure.persistence.transactions import run_transaction
from nexa.infrastructure.security import CursorCodec, CursorError, read_secret_file

_LEASE_DURATION = timedelta(seconds=45)


class WorkerService:
    def __init__(
        self,
        session_factory,
        settings: Settings,
        identity: IdentityService,
        *,
        storage_readiness: Callable[[], None] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.identity = identity
        self._storage_readiness = storage_readiness or (lambda: None)
        self._cursor_key = read_secret_file(settings.server_secret_file)

    @staticmethod
    def _mode(session: Session) -> str:
        return session.execute(
            select(policy_versions.c.operational_mode)
            .where(policy_versions.c.is_current.is_(True))
            .with_for_update(read=True)
        ).scalar_one()

    def _worker_auth(
        self,
        session: Session,
        *,
        worker_id: UUID,
        credential: str,
    ) -> dict[str, Any]:
        worker = (
            session.execute(
                select(workers).where(workers.c.worker_id == worker_id).with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if worker is None:
            raise ApplicationError(
                code="authentication_required", status=401, message="Invalid credentials"
            )
        self.identity.revalidate_worker_credential(
            session,
            credential,
            expected_worker_id=worker_id,
        )
        return dict(worker)

    @staticmethod
    def _require_current_incarnation(
        session: Session,
        worker: dict[str, Any],
        incarnation_id: UUID,
        *,
        require_reconciling: bool,
    ) -> dict[str, Any]:
        if worker["current_incarnation_id"] != incarnation_id:
            raise ApplicationError(
                code="state_conflict", status=409, message="Worker incarnation is stale"
            )
        incarnation = (
            session.execute(
                select(worker_incarnations).where(
                    worker_incarnations.c.worker_id == worker["worker_id"],
                    worker_incarnations.c.worker_incarnation_id == incarnation_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if incarnation is None or incarnation["ended_at"] is not None:
            raise ApplicationError(
                code="state_conflict", status=409, message="Worker incarnation is stale"
            )
        if require_reconciling and incarnation["reconcile_completed_at"] is not None:
            raise ApplicationError(
                code="state_conflict", status=409, message="Worker reconciliation is complete"
            )
        return dict(incarnation)

    @staticmethod
    def _begin_callback(
        session: Session,
        *,
        worker_id: UUID,
        operation_id: str,
        callback_id: UUID,
        payload_hash: str,
    ) -> tuple[UUID | None, dict[str, Any] | None]:
        receipt_id = new_uuid7()
        inserted = session.execute(
            pg_insert(callback_receipts)
            .values(
                receipt_id=receipt_id,
                worker_id=worker_id,
                operation_id=operation_id,
                callback_id=callback_id,
                payload_hash=payload_hash,
                acknowledgment={},
            )
            .on_conflict_do_nothing(
                index_elements=[
                    callback_receipts.c.worker_id,
                    callback_receipts.c.operation_id,
                    callback_receipts.c.callback_id,
                ]
            )
            .returning(callback_receipts.c.receipt_id)
        ).scalar_one_or_none()
        if inserted is not None:
            return inserted, None
        row = (
            session.execute(
                select(callback_receipts)
                .where(
                    callback_receipts.c.worker_id == worker_id,
                    callback_receipts.c.operation_id == operation_id,
                    callback_receipts.c.callback_id == callback_id,
                )
                .with_for_update()
            )
            .mappings()
            .one()
        )
        if row["payload_hash"] != payload_hash:
            raise ApplicationError(
                code="idempotency_conflict",
                status=409,
                message="Callback ID was already used with a different request",
            )
        acknowledgment = dict(row["acknowledgment"])
        if not acknowledgment:
            raise ApplicationError(
                code="idempotency_in_progress",
                status=409,
                message="An identical callback is still in progress",
                retry_after=1,
            )
        return None, acknowledgment

    @staticmethod
    def _complete_callback(session: Session, receipt_id: UUID, body: dict[str, Any]) -> None:
        result = session.execute(
            update(callback_receipts)
            .where(
                callback_receipts.c.receipt_id == receipt_id,
                callback_receipts.c.acknowledgment == {},
            )
            .values(acknowledgment=body)
        )
        if result.rowcount != 1:
            raise RuntimeError("Callback receipt is not pending")

    def create_incarnation(
        self,
        *,
        worker_id: UUID,
        credential: str,
        process_start_nonce: UUID,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            idempotency_now = transaction_timestamp(session)
            idempotency = begin_idempotency(
                session,
                context=f"WORKER:{worker_id}",
                principal_id=str(worker_id),
                operation_id="workerCreateIncarnation",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=idempotency_now + timedelta(days=30),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            mode = self._mode(session)
            worker = self._worker_auth(session, worker_id=worker_id, credential=credential)
            if idempotency.replay is not None:
                return dict(idempotency.replay.body or {})
            if mode == "WRITE_FROZEN":
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Worker incarnation creation is disabled while writes are frozen",
                )
            duplicate_nonce = session.execute(
                select(worker_incarnations.c.worker_incarnation_id).where(
                    worker_incarnations.c.worker_id == worker_id,
                    worker_incarnations.c.process_start_nonce == process_start_nonce,
                )
            ).scalar_one_or_none()
            if duplicate_nonce is not None:
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Process start nonce already belongs to an incarnation",
                )
            now = clock_timestamp(session)
            previous_id = worker["current_incarnation_id"]
            previous_sequence = 0
            if previous_id is not None:
                previous = (
                    session.execute(
                        select(worker_incarnations)
                        .where(
                            worker_incarnations.c.worker_id == worker_id,
                            worker_incarnations.c.worker_incarnation_id == previous_id,
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .one()
                )
                previous_sequence = int(previous["sequence"])
                session.execute(
                    update(worker_incarnations)
                    .where(
                        worker_incarnations.c.worker_id == worker_id,
                        worker_incarnations.c.worker_incarnation_id == previous_id,
                        worker_incarnations.c.ended_at.is_(None),
                    )
                    .values(ended_at=now)
                )
            else:
                previous_sequence = int(
                    session.execute(
                        select(func.coalesce(func.max(worker_incarnations.c.sequence), 0)).where(
                            worker_incarnations.c.worker_id == worker_id
                        )
                    ).scalar_one()
                )
            incarnation_id = new_uuid7()
            sequence = previous_sequence + 1
            session.execute(
                insert(worker_incarnations).values(
                    worker_incarnation_id=incarnation_id,
                    worker_id=worker_id,
                    sequence=sequence,
                    process_start_nonce=process_start_nonce,
                    process_started_at=now,
                )
            )
            session.execute(
                update(workers)
                .where(workers.c.worker_id == worker_id)
                .values(
                    current_incarnation_id=incarnation_id,
                    current_inventory_version=None,
                    health="STARTING",
                    last_heartbeat_at=None,
                    ready_at=None,
                    version=workers.c.version + 1,
                    updated_at=now,
                )
            )
            response = json_wire_value(
                {
                    "worker_id": worker_id,
                    "worker_incarnation_id": incarnation_id,
                    "sequence": sequence,
                    "process_start_nonce": process_start_nonce,
                    "health": "STARTING",
                    "created_at": now,
                }
            )
            complete_idempotency(
                session,
                idempotency.record_id,
                status=201,
                body=response,
                headers={},
                resource_id=incarnation_id,
            )
            return response

        return run_transaction(self.session_factory, operation)

    @staticmethod
    def _reconciliation_snapshot(session: Session, worker_id: UUID) -> str:
        sequence = session.execute(
            select(allocations.c.reconciliation_sequence)
            .where(
                allocations.c.worker_id == worker_id,
                allocations.c.state != "RELEASED",
            )
            .order_by(allocations.c.reconciliation_sequence.desc())
            .limit(1)
        ).scalar_one_or_none()
        return str(int(sequence or 0))

    def _cursor(self, now) -> CursorCodec:
        return CursorCodec(
            self._cursor_key,
            ttl_seconds=self.settings.cursor_ttl_seconds,
            now=lambda: now,
        )

    def get_reconciliation(
        self,
        *,
        worker_id: UUID,
        credential: str,
        incarnation_id: UUID,
        page_size: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            mode = self._mode(session)
            worker = self._worker_auth(session, worker_id=worker_id, credential=credential)
            incarnation = self._require_current_incarnation(
                session, worker, incarnation_id, require_reconciling=False
            )
            if mode == "WRITE_FROZEN":
                raise ApplicationError(
                    code="state_conflict", status=409, message="Writes are frozen"
                )
            now = clock_timestamp(session)
            snapshot = (
                self._reconciliation_snapshot(session, worker_id)
                if cursor is None
                else incarnation["reconciliation_snapshot"]
            )
            if snapshot is None:
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Reconciliation pagination changed; restart from the first page",
                )
            binding = {
                "operation": "workerGetReconciliation",
                "worker_id": str(worker_id),
                "worker_incarnation_id": str(incarnation_id),
                "snapshot": snapshot,
            }
            after: UUID | None = None
            if cursor is not None:
                try:
                    position = self._cursor(now).decode(cursor, expected_binding=binding)
                    after = UUID(position["allocation_id"])
                except (CursorError, KeyError, ValueError):
                    raise ApplicationError(
                        code="invalid_cursor",
                        status=400,
                        message="The reconciliation cursor is invalid or stale",
                    ) from None
                if (
                    incarnation["reconciliation_after"] != after
                    or incarnation["reconciliation_drained"]
                ):
                    raise ApplicationError(
                        code="state_conflict",
                        status=409,
                        message="Reconciliation pagination changed; restart from the first page",
                    )
            statement = (
                select(allocations)
                .where(
                    allocations.c.worker_id == worker_id,
                    allocations.c.state != "RELEASED",
                    allocations.c.reconciliation_sequence <= int(snapshot),
                )
                .order_by(allocations.c.allocation_id)
                .limit(page_size + 1)
            )
            if after is not None:
                statement = statement.where(allocations.c.allocation_id > after)
            rows = list(session.execute(statement).mappings().all())
            visible = rows[:page_size]
            items = [self._reconciliation_item(session, dict(row), now) for row in visible]
            next_cursor = None
            if len(rows) > page_size:
                next_cursor = self._cursor(now).encode(
                    binding=binding,
                    position={"allocation_id": str(visible[-1]["allocation_id"])},
                )
            session.execute(
                update(worker_incarnations)
                .where(worker_incarnations.c.worker_incarnation_id == incarnation_id)
                .values(
                    reconciliation_snapshot=snapshot,
                    reconciliation_after=visible[-1]["allocation_id"] if next_cursor else None,
                    reconciliation_drained=next_cursor is None,
                    reconcile_completed_at=None
                    if cursor is None
                    else incarnation["reconcile_completed_at"],
                    ready_at=None if cursor is None else incarnation["ready_at"],
                )
            )
            return json_wire_value(
                {
                    "server_time": now,
                    "items": items,
                    "page": {"next_cursor": next_cursor, "page_size": page_size},
                }
            )

        return run_transaction(self.session_factory, operation)

    @staticmethod
    def _reconciliation_item(session: Session, allocation: dict[str, Any], now) -> dict[str, Any]:
        attempt = (
            session.execute(
                select(attempts).where(attempts.c.attempt_id == allocation["attempt_id"])
            )
            .mappings()
            .one()
        )
        lease = (
            session.execute(
                select(attempt_leases)
                .where(attempt_leases.c.attempt_id == allocation["attempt_id"])
                .order_by(attempt_leases.c.issued_at.desc(), attempt_leases.c.lease_id.desc())
                .limit(1)
            )
            .mappings()
            .one()
        )
        grant = (
            session.execute(
                select(attempt_authority_grants)
                .where(attempt_authority_grants.c.attempt_id == allocation["attempt_id"])
                .order_by(
                    attempt_authority_grants.c.granted_at.desc(),
                    attempt_authority_grants.c.grant_id.desc(),
                )
                .limit(1)
            )
            .mappings()
            .one()
        )
        job = session.execute(
            select(jobs.c.desired_state).where(jobs.c.job_id == allocation["job_id"])
        ).scalar_one()
        container = (
            session.execute(
                select(container_identities)
                .where(container_identities.c.attempt_id == allocation["attempt_id"])
                .order_by(container_identities.c.created_at.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        gpu_uuids = list(
            session.execute(
                select(allocation_gpu_claims.c.gpu_uuid)
                .where(
                    allocation_gpu_claims.c.allocation_id == allocation["allocation_id"],
                    allocation_gpu_claims.c.released_at.is_(None),
                )
                .order_by(allocation_gpu_claims.c.gpu_uuid)
            ).scalars()
        )
        live = (
            lease["revoked_at"] is None
            and lease["expires_at"] > now
            and grant["ended_at"] is None
            and allocation["state"] == "HELD"
        )
        if container is not None:
            claim_state = "STARTED"
        elif attempt["state"] == "CREATED":
            claim_state = "UNCLAIMED"
        else:
            claim_state = "CLAIMED"
        expected_container = None
        if container is not None:
            expected_container = {
                "container_id": container["container_id"],
                "runtime_identity_digest": container["runtime_identity_digest"],
            }
        return {
            "authority": {
                "worker_id": allocation["worker_id"],
                "worker_incarnation_id": grant["worker_incarnation_id"],
                "attempt_id": allocation["attempt_id"],
                "allocation_id": allocation["allocation_id"],
                "lease_id": lease["lease_id"],
                "job_fence": attempt["job_fence"],
            },
            "authority_state": "LIVE" if live else "REVOKED",
            "lease_expires_at": lease["expires_at"],
            "desired_state": job,
            "allocation": {
                "allocation_id": allocation["allocation_id"],
                "tenant_id": allocation["tenant_id"],
                "job_id": allocation["job_id"],
                "attempt_id": allocation["attempt_id"],
                "worker_id": allocation["worker_id"],
                "resources": {
                    "cpu_millis": allocation["cpu_millis"],
                    "memory_bytes": allocation["memory_bytes"],
                    "gpu_count": allocation["gpu_count"],
                },
                "gpu_uuids": gpu_uuids,
                "state": allocation["state"],
                "held_at": allocation["held_at"],
                "quarantined_at": allocation["quarantined_at"],
                "released_at": allocation["released_at"],
            },
            "startup_nonce": attempt["startup_nonce"],
            "claim_state": claim_state,
            "expected_container": expected_container,
        }

    @staticmethod
    def _inventory_payload(request: HeartbeatRequest) -> dict[str, Any]:
        return json_wire_value(request.inventory.model_dump())

    @staticmethod
    def _inventory_can_be_ready(inventory: dict[str, Any]) -> bool:
        floor_cpu = max(1_000, math.ceil(inventory["host_cpu_millis"] * 0.20))
        floor_memory = max(2 * 1024**3, math.ceil(inventory["host_memory_bytes"] * 0.20))
        allocatable = inventory["allocatable"]
        return (
            inventory["runtime"]["seccomp_available"] is True
            and allocatable["cpu_millis"] <= inventory["host_cpu_millis"] - floor_cpu
            and allocatable["memory_bytes"] <= inventory["host_memory_bytes"] - floor_memory
            and all(image["verified"] for image in inventory["images"])
        )

    @staticmethod
    def _held_capacity(session: Session, worker_id: UUID) -> tuple[int, int, int]:
        row = session.execute(
            select(
                func.coalesce(func.sum(allocations.c.cpu_millis), 0),
                func.coalesce(func.sum(allocations.c.memory_bytes), 0),
                func.coalesce(func.sum(allocations.c.gpu_count), 0),
            ).where(
                allocations.c.worker_id == worker_id,
                allocations.c.state != "RELEASED",
            )
        ).one()
        return int(row[0]), int(row[1]), int(row[2])

    @staticmethod
    def _unreleased_allocation_ids(session: Session, worker_id: UUID) -> Iterator[UUID]:
        after: UUID | None = None
        while True:
            statement = (
                select(allocations.c.allocation_id)
                .where(
                    allocations.c.worker_id == worker_id,
                    allocations.c.state != "RELEASED",
                )
                .order_by(allocations.c.allocation_id)
                .limit(100)
            )
            if after is not None:
                statement = statement.where(allocations.c.allocation_id > after)
            batch = list(session.execute(statement).scalars())
            if not batch:
                return
            yield from batch
            after = batch[-1]

    @staticmethod
    def _can_be_ready(
        session: Session,
        *,
        worker_id: UUID,
        incarnation_id: UUID,
        observed_containers: set[tuple[str, str]],
        now,
    ) -> bool:
        for allocation_id in WorkerService._unreleased_allocation_ids(session, worker_id):
            allocation = (
                session.execute(
                    select(allocations).where(allocations.c.allocation_id == allocation_id)
                )
                .mappings()
                .one()
            )
            if allocation["state"] != "HELD":
                return False
            attempt = (
                session.execute(
                    select(attempts).where(attempts.c.attempt_id == allocation["attempt_id"])
                )
                .mappings()
                .one()
            )
            lease = (
                session.execute(
                    select(attempt_leases)
                    .where(attempt_leases.c.attempt_id == allocation["attempt_id"])
                    .order_by(attempt_leases.c.issued_at.desc())
                    .limit(1)
                )
                .mappings()
                .one()
            )
            grant = (
                session.execute(
                    select(attempt_authority_grants)
                    .where(attempt_authority_grants.c.attempt_id == allocation["attempt_id"])
                    .order_by(attempt_authority_grants.c.granted_at.desc())
                    .limit(1)
                )
                .mappings()
                .one()
            )
            container = (
                session.execute(
                    select(container_identities)
                    .where(container_identities.c.attempt_id == allocation["attempt_id"])
                    .order_by(container_identities.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            desired = session.execute(
                select(jobs.c.desired_state).where(jobs.c.job_id == allocation["job_id"])
            ).scalar_one()
            if (
                desired != "RUNNING"
                or attempt["worker_incarnation_id"] != incarnation_id
                or lease["current_worker_incarnation_id"] != incarnation_id
                or lease["revoked_at"] is not None
                or lease["expires_at"] <= now
                or grant["worker_incarnation_id"] != incarnation_id
                or grant["ended_at"] is not None
                or container is None
                or (
                    container["container_id"],
                    container["runtime_identity_digest"],
                )
                not in observed_containers
            ):
                return False
            observed_containers.remove(
                (container["container_id"], container["runtime_identity_digest"])
            )
        return not observed_containers

    def heartbeat(
        self,
        *,
        worker_id: UUID,
        credential: str,
        callback_id: UUID,
        payload_hash: str,
        request: HeartbeatRequest,
    ) -> dict[str, Any]:
        storage_ready = True
        if request.observed_health == "READY" and request.reconcile_complete:
            try:
                self._storage_readiness()
            except (ArtifactError, OSError):
                storage_ready = False

        def operation(session: Session) -> dict[str, Any]:
            receipt_id, replay = self._begin_callback(
                session,
                worker_id=worker_id,
                operation_id="workerHeartbeat",
                callback_id=callback_id,
                payload_hash=payload_hash,
            )
            mode = self._mode(session)
            worker = self._worker_auth(session, worker_id=worker_id, credential=credential)
            incarnation = self._require_current_incarnation(
                session,
                worker,
                UUID(str(request.worker_incarnation_id)),
                require_reconciling=False,
            )
            if replay is not None:
                return replay
            assert receipt_id is not None
            if mode == "WRITE_FROZEN":
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Worker heartbeat is disabled while writes are frozen",
                )
            now = clock_timestamp(session)
            inventory = self._inventory_payload(request)
            checksum = jcs_request_hash(inventory)
            current_version = worker["current_inventory_version"]
            current_checksum = None
            if current_version is not None:
                current_checksum = session.execute(
                    select(worker_inventories.c.checksum).where(
                        worker_inventories.c.worker_id == worker_id,
                        worker_inventories.c.inventory_version == current_version,
                    )
                ).scalar_one()
            if checksum != current_checksum:
                held_cpu, held_memory, held_gpu = self._held_capacity(session, worker_id)
                allocatable = inventory["allocatable"]
                if (
                    held_cpu > allocatable["cpu_millis"]
                    or held_memory > allocatable["memory_bytes"]
                    or held_gpu > allocatable["gpu_count"]
                ):
                    raise ApplicationError(
                        code="state_conflict",
                        status=409,
                        message="Inventory cannot cover unreleased allocations",
                    )
                last_version = session.execute(
                    select(
                        func.coalesce(func.max(worker_inventories.c.inventory_version), 0)
                    ).where(worker_inventories.c.worker_id == worker_id)
                ).scalar_one()
                inventory_version = int(last_version) + 1
                inventory_id = new_uuid7()
                session.execute(
                    insert(worker_inventories).values(
                        inventory_id=inventory_id,
                        worker_id=worker_id,
                        worker_incarnation_id=request.worker_incarnation_id,
                        inventory_version=inventory_version,
                        architecture=inventory["architecture"],
                        host_cpu_millis=inventory["host_cpu_millis"],
                        host_memory_bytes=inventory["host_memory_bytes"],
                        allocatable_cpu_millis=allocatable["cpu_millis"],
                        allocatable_memory_bytes=allocatable["memory_bytes"],
                        allocatable_gpu_count=allocatable["gpu_count"],
                        runtime_capabilities=inventory["runtime"],
                        workload_capabilities={
                            "adapters": inventory["adapters"],
                            "images": inventory["images"],
                            "frameworks": inventory["frameworks"],
                        },
                        checksum=checksum,
                        observed_at=request.inventory.discovered_at,
                    )
                )
                for device in inventory["gpu_devices"]:
                    session.execute(
                        insert(gpu_devices).values(
                            worker_id=worker_id,
                            inventory_version=inventory_version,
                            inventory_id=inventory_id,
                            gpu_uuid=device["uuid"],
                            model=device["model"],
                            memory_bytes=device["memory_bytes"],
                            compute_capability=device["compute_capability"],
                            driver_version=device["driver_version"],
                            api_version=device["cuda_driver_api_version"],
                            healthy=device["healthy"],
                        )
                    )
                current_version = inventory_version
            observed = {
                (item.container_id, item.runtime_identity_digest)
                for item in request.observed_containers
            }
            ready = (
                request.observed_health == "READY"
                and request.reconcile_complete
                and storage_ready
                and self._inventory_can_be_ready(inventory)
                and worker["admin_state"] != "DISABLED"
                and incarnation["reconciliation_drained"]
                and incarnation["reconciliation_snapshot"]
                == self._reconciliation_snapshot(session, worker_id)
                and self._can_be_ready(
                    session,
                    worker_id=worker_id,
                    incarnation_id=UUID(str(request.worker_incarnation_id)),
                    observed_containers=observed,
                    now=now,
                )
            )
            health = "READY" if ready else "STARTING"
            worker_values: dict[str, Any] = {
                "health": health,
                "current_inventory_version": current_version,
                "last_heartbeat_at": now,
                "version": workers.c.version + 1,
                "updated_at": now,
            }
            if ready:
                worker_values["ready_at"] = worker["ready_at"] or now
                session.execute(
                    update(worker_incarnations)
                    .where(
                        worker_incarnations.c.worker_incarnation_id == request.worker_incarnation_id
                    )
                    .values(
                        reconcile_completed_at=incarnation["reconcile_completed_at"] or now,
                        ready_at=incarnation["ready_at"] or now,
                    )
                )
            else:
                worker_values["ready_at"] = None
            session.execute(
                update(workers).where(workers.c.worker_id == worker_id).values(**worker_values)
            )
            response = json_wire_value(
                {
                    "server_time": now,
                    "admin_state": worker["admin_state"],
                    "accepted_incarnation_id": request.worker_incarnation_id,
                    "next_heartbeat_seconds": 5,
                }
            )
            self._complete_callback(session, receipt_id, response)
            return response

        return run_transaction(self.session_factory, operation)

    def poll(
        self,
        *,
        worker_id: UUID,
        credential: str,
        incarnation_id: UUID,
        long_poll_seconds: int,
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            mode = self._mode(session)
            worker = self._worker_auth(session, worker_id=worker_id, credential=credential)
            self._require_current_incarnation(
                session, worker, incarnation_id, require_reconciling=False
            )
            if (
                mode != "NORMAL"
                or worker["health"] != "READY"
                or worker["admin_state"] != "ENABLED"
            ):
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Worker is not eligible to poll for dispatch",
                )
            now = clock_timestamp(session)
            return json_wire_value(
                {
                    "server_time": now,
                    "offer": None,
                    "retry_after_seconds": min(max(long_poll_seconds, 0), 30),
                }
            )

        return run_transaction(self.session_factory, operation)

    def sweep_health(self) -> int:
        def operation(session: Session) -> int:
            if self._mode(session) == "WRITE_FROZEN":
                return 0
            rows = list(
                session.execute(
                    select(
                        workers.c.worker_id,
                        workers.c.health,
                        workers.c.last_heartbeat_at,
                        worker_incarnations.c.process_started_at,
                    )
                    .join(
                        worker_incarnations,
                        worker_incarnations.c.worker_incarnation_id
                        == workers.c.current_incarnation_id,
                    )
                    .with_for_update(of=workers)
                ).mappings()
            )
            now = clock_timestamp(session)
            changed = 0
            for row in rows:
                reference = row["last_heartbeat_at"] or row["process_started_at"]
                age = (now - reference).total_seconds()
                health = "UNAVAILABLE" if age >= 30 else "SUSPECT" if age >= 15 else None
                if health is not None and row["health"] != health:
                    session.execute(
                        update(workers)
                        .where(workers.c.worker_id == row["worker_id"])
                        .values(
                            health=health,
                            ready_at=None,
                            version=workers.c.version + 1,
                            updated_at=now,
                        )
                    )
                    changed += 1
            return changed

        return run_transaction(self.session_factory, operation)

    @staticmethod
    def _authority_rows(
        session: Session, authority: Authority, *, worker_id: UUID
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        if authority.worker_id != worker_id:
            raise ApplicationError(
                code="permission_denied", status=403, message="Worker authority mismatch"
            )
        attempt_ref = session.execute(
            select(attempts.c.job_id).where(attempts.c.attempt_id == authority.attempt_id)
        ).scalar_one_or_none()
        if attempt_ref is None:
            raise ApplicationError(
                code="state_conflict", status=409, message="Attempt is not available"
            )
        job = (
            session.execute(select(jobs).where(jobs.c.job_id == attempt_ref).with_for_update())
            .mappings()
            .one()
        )
        attempt = (
            session.execute(
                select(attempts)
                .where(attempts.c.attempt_id == authority.attempt_id)
                .with_for_update()
            )
            .mappings()
            .one()
        )
        lease = (
            session.execute(
                select(attempt_leases)
                .where(attempt_leases.c.lease_id == authority.lease_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        allocation = (
            session.execute(
                select(allocations)
                .where(allocations.c.allocation_id == authority.allocation_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        grant = (
            session.execute(
                select(attempt_authority_grants)
                .where(
                    attempt_authority_grants.c.attempt_id == authority.attempt_id,
                    attempt_authority_grants.c.ended_at.is_(None),
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if (
            lease is None
            or allocation is None
            or grant is None
            or job["job_id"] != attempt["job_id"]
            or job["job_fence"] != authority.job_fence
            or attempt["job_fence"] != authority.job_fence
            or attempt["worker_id"] != worker_id
            or lease["attempt_id"] != authority.attempt_id
            or lease["allocation_id"] != authority.allocation_id
            or lease["worker_id"] != worker_id
            or lease["job_fence"] != authority.job_fence
            or allocation["attempt_id"] != authority.attempt_id
            or allocation["job_id"] != job["job_id"]
            or allocation["worker_id"] != worker_id
            or grant["worker_incarnation_id"] != authority.worker_incarnation_id
            or grant["lease_id"] != authority.lease_id
            or grant["allocation_id"] != authority.allocation_id
            or grant["worker_id"] != worker_id
            or grant["job_fence"] != authority.job_fence
        ):
            raise ApplicationError(
                code="state_conflict", status=409, message="Worker authority is stale"
            )
        return dict(job), dict(attempt), dict(lease), dict(allocation), dict(grant)

    @staticmethod
    def _live_authority(job: dict, attempt: dict, lease: dict, allocation: dict, now) -> None:
        normal_run = (
            job["desired_state"] == "RUNNING"
            and job["state"] in {"STARTING", "RUNNING", "CHECKPOINTING"}
            and attempt["execution_intent"] == "RUN"
        )
        checkpoint_for_pause = (
            job["desired_state"] == "PAUSED"
            and job["state"] == "PAUSING"
            and attempt["execution_intent"] == "CHECKPOINT_FOR_PAUSE"
        )
        if (
            not (normal_run or checkpoint_for_pause)
            or attempt["state"] not in {"STARTING", "RUNNING", "CHECKPOINTING"}
            or allocation["state"] != "HELD"
            or lease["revoked_at"] is not None
            or lease["expires_at"] <= now
        ):
            raise ApplicationError(code="state_conflict", status=409, message="Attempt is not live")

    def adopt_attempt(
        self,
        *,
        worker_id: UUID,
        credential: str,
        attempt_id: UUID,
        callback_id: UUID,
        payload_hash: str,
        request: AdoptRequest,
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            receipt_id, replay = self._begin_callback(
                session,
                worker_id=worker_id,
                operation_id="workerAdoptAttempt",
                callback_id=callback_id,
                payload_hash=payload_hash,
            )
            mode = self._mode(session)
            worker = self._worker_auth(session, worker_id=worker_id, credential=credential)
            incarnation = self._require_current_incarnation(
                session, worker, request.current_worker_incarnation_id, require_reconciling=True
            )
            prior = request.prior_authority
            if prior.attempt_id != attempt_id or prior.worker_id != worker_id:
                raise ApplicationError(
                    code="state_conflict", status=409, message="Authority mismatch"
                )
            predecessor = (
                session.execute(
                    select(worker_incarnations).where(
                        worker_incarnations.c.worker_incarnation_id == prior.worker_incarnation_id,
                        worker_incarnations.c.worker_id == worker_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                predecessor is None
                or predecessor["ended_at"] is None
                or predecessor["sequence"] + 1 != incarnation["sequence"]
            ):
                raise ApplicationError(
                    code="state_conflict", status=409, message="Not the prior incarnation"
                )
            if replay is not None:
                return replay
            if mode == "WRITE_FROZEN":
                raise ApplicationError(
                    code="state_conflict", status=409, message="Writes are frozen"
                )
            job, attempt, lease, allocation, grant = self._authority_rows(
                session, prior, worker_id=worker_id
            )
            container = (
                session.execute(
                    select(container_identities)
                    .where(
                        container_identities.c.attempt_id == attempt_id,
                        container_identities.c.container_id == request.container.container_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if (
                container is None
                or container["allocation_id"] != prior.allocation_id
                or container["startup_nonce"] != attempt["startup_nonce"]
                or container["runtime_identity_digest"] != request.container.runtime_identity_digest
                or container["stopped_at"] is not None
            ):
                raise ApplicationError(
                    code="state_conflict", status=409, message="Container identity mismatch"
                )
            checkpoint = (
                session.execute(
                    select(checkpoint_reservations)
                    .where(
                        checkpoint_reservations.c.attempt_id == attempt_id,
                        checkpoint_reservations.c.state == "RESERVED",
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            result = (
                session.execute(
                    select(result_reservations)
                    .where(
                        result_reservations.c.attempt_id == attempt_id,
                        result_reservations.c.state == "ACTIVE",
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            for reservation in (checkpoint, result):
                if (
                    reservation is not None
                    and reservation["authority_grant_id"] != grant["grant_id"]
                ):
                    raise ApplicationError(
                        code="state_conflict", status=409, message="Reservation lineage mismatch"
                    )
            self.identity.revalidate_worker_credential(
                session, credential, expected_worker_id=worker_id
            )
            now = clock_timestamp(session)
            self._live_authority(job, attempt, lease, allocation, now)
            if (
                attempt["worker_incarnation_id"] != prior.worker_incarnation_id
                or lease["current_worker_incarnation_id"] != prior.worker_incarnation_id
            ):
                raise ApplicationError(
                    code="state_conflict", status=409, message="Authority mismatch"
                )
            next_grant = new_uuid7()
            session.execute(
                update(attempt_authority_grants)
                .where(attempt_authority_grants.c.grant_id == grant["grant_id"])
                .values(ended_at=now)
            )
            session.execute(
                insert(attempt_authority_grants).values(
                    grant_id=next_grant,
                    tenant_id=attempt["tenant_id"],
                    job_id=job["job_id"],
                    attempt_id=attempt_id,
                    allocation_id=prior.allocation_id,
                    lease_id=prior.lease_id,
                    worker_id=worker_id,
                    worker_incarnation_id=request.current_worker_incarnation_id,
                    job_fence=prior.job_fence,
                    predecessor_grant_id=grant["grant_id"],
                    callback_id=callback_id,
                    granted_at=now,
                )
            )
            expiry = now + _LEASE_DURATION
            session.execute(
                update(attempts)
                .where(attempts.c.attempt_id == attempt_id)
                .values(worker_incarnation_id=request.current_worker_incarnation_id, updated_at=now)
            )
            session.execute(
                update(attempt_leases)
                .where(attempt_leases.c.lease_id == prior.lease_id)
                .values(
                    current_worker_incarnation_id=request.current_worker_incarnation_id,
                    expires_at=expiry,
                )
            )
            if checkpoint is not None:
                session.execute(
                    update(checkpoint_reservations)
                    .where(checkpoint_reservations.c.checkpoint_id == checkpoint["checkpoint_id"])
                    .values(authority_grant_id=next_grant)
                )
            if result is not None:
                session.execute(
                    update(result_reservations)
                    .where(result_reservations.c.result_id == result["result_id"])
                    .values(authority_grant_id=next_grant)
                )
            response = json_wire_value(
                {
                    "callback_id": callback_id,
                    "accepted": True,
                    "server_time": now,
                    "authority": {
                        "worker_id": worker_id,
                        "worker_incarnation_id": request.current_worker_incarnation_id,
                        "attempt_id": attempt_id,
                        "allocation_id": prior.allocation_id,
                        "lease_id": prior.lease_id,
                        "job_fence": prior.job_fence,
                    },
                    "transferred_checkpoint_reservation": None
                    if checkpoint is None
                    else {
                        key: checkpoint[key]
                        for key in (
                            "callback_id",
                            "checkpoint_id",
                            "job_id",
                            "attempt_id",
                            "sequence",
                            "reserved_at",
                        )
                    },
                    "transferred_result_reservation": None
                    if result is None
                    else {
                        key: result[key]
                        for key in (
                            "callback_id",
                            "result_id",
                            "job_id",
                            "attempt_id",
                            "reserved_at",
                        )
                    },
                    "lease_expires_at": expiry,
                    "lease_duration_seconds": 45,
                    "renew_interval_seconds": 5,
                    "safety_margin_seconds": 5,
                }
            )
            assert receipt_id is not None
            self._complete_callback(session, receipt_id, response)
            return response

        return run_transaction(self.session_factory, operation)

    def renew_attempt(
        self,
        *,
        worker_id: UUID,
        credential: str,
        attempt_id: UUID,
        callback_id: UUID,
        payload_hash: str,
        request: RenewRequest,
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            receipt_id, replay = self._begin_callback(
                session,
                worker_id=worker_id,
                operation_id="workerRenewAttempt",
                callback_id=callback_id,
                payload_hash=payload_hash,
            )
            mode = self._mode(session)
            worker = self._worker_auth(session, worker_id=worker_id, credential=credential)
            authority = request.authority
            if (
                authority.attempt_id != attempt_id
                or worker["current_incarnation_id"] != authority.worker_incarnation_id
            ):
                raise ApplicationError(
                    code="state_conflict", status=409, message="Authority is stale"
                )
            self._require_current_incarnation(
                session, worker, authority.worker_incarnation_id, require_reconciling=False
            )
            if replay is not None:
                return replay
            if mode == "WRITE_FROZEN":
                raise ApplicationError(
                    code="state_conflict", status=409, message="Writes are frozen"
                )
            job, attempt, lease, allocation, _ = self._authority_rows(
                session, authority, worker_id=worker_id
            )
            self.identity.revalidate_worker_credential(
                session, credential, expected_worker_id=worker_id
            )
            now = clock_timestamp(session)
            self._live_authority(job, attempt, lease, allocation, now)
            if (
                attempt["worker_incarnation_id"] != authority.worker_incarnation_id
                or lease["current_worker_incarnation_id"] != authority.worker_incarnation_id
            ):
                raise ApplicationError(
                    code="state_conflict", status=409, message="Authority is stale"
                )
            progress = (
                json_wire_value(request.progress.model_dump())
                if request.progress is not None
                else None
            )
            sequence = request.progress_sequence
            if sequence < attempt["progress_sequence"] or (
                sequence == attempt["progress_sequence"]
                and progress != attempt["progress_snapshot"]
            ):
                raise ApplicationError(
                    code="state_conflict", status=409, message="Progress sequence conflicts"
                )
            if sequence > attempt["progress_sequence"]:
                session.execute(
                    update(attempts)
                    .where(attempts.c.attempt_id == attempt_id)
                    .values(progress_sequence=sequence, progress_snapshot=progress, updated_at=now)
                )
            expiry = now + _LEASE_DURATION
            session.execute(
                update(attempt_leases)
                .where(attempt_leases.c.lease_id == authority.lease_id)
                .values(expires_at=expiry)
            )
            response = json_wire_value(
                {
                    "server_time": now,
                    "lease_expires_at": expiry,
                    "desired_state": job["desired_state"],
                    "renew_interval_seconds": 5,
                    "safety_margin_seconds": 5,
                }
            )
            assert receipt_id is not None
            self._complete_callback(session, receipt_id, response)
            return response

        return run_transaction(self.session_factory, operation)
