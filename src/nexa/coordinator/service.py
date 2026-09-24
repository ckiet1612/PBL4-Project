"""Leadership is independent from worker incarnation and attempt authority."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import clock_timestamp
from nexa.infrastructure.persistence.schema import coordinator_leadership
from nexa.infrastructure.persistence.transactions import run_transaction


class LeadershipLost(RuntimeError):
    def __init__(self):
        super().__init__("coordinator leadership is no longer live")


class CoordinatorService:
    def __init__(self, session_factory, *, holder_id: UUID | None = None):
        self.session_factory = session_factory
        self.holder_id = holder_id or new_uuid7()
        self.cursors = {}

    @staticmethod
    def _timeouts(session):
        session.execute(text("SET LOCAL lock_timeout = '1000ms'"))
        session.execute(text("SET LOCAL statement_timeout = '3000ms'"))

    def acquire(self) -> int | None:
        def operation(session):
            self._timeouts(session)
            session.execute(
                insert(coordinator_leadership)
                .values(singleton_key="coordinator", epoch=0)
                .on_conflict_do_nothing()
            )
            row = session.execute(select(coordinator_leadership).with_for_update()).mappings().one()
            now = clock_timestamp(session)
            live = row["lease_expires_at"] is not None and row["lease_expires_at"] > now
            if live:
                return row["epoch"] if row["holder_id"] == self.holder_id else None
            epoch = row["epoch"] + 1
            session.execute(
                update(coordinator_leadership).values(
                    holder_id=self.holder_id,
                    epoch=epoch,
                    renewed_at=now,
                    lease_expires_at=now + timedelta(seconds=15),
                )
            )
            return epoch

        return run_transaction(self.session_factory, operation)

    def _leader(self, session, epoch):
        row = (
            session.execute(select(coordinator_leadership).with_for_update())
            .mappings()
            .one_or_none()
        )
        now = clock_timestamp(session)
        if (
            row is None
            or row["holder_id"] != self.holder_id
            or row["epoch"] != epoch
            or row["lease_expires_at"] <= now
        ):
            raise LeadershipLost()
        return now

    def renew(self, epoch: int) -> bool:
        def operation(session):
            self._timeouts(session)
            now = self._leader(session, epoch)
            session.execute(
                update(coordinator_leadership).values(
                    renewed_at=now, lease_expires_at=now + timedelta(seconds=15)
                )
            )
            return True

        try:
            return run_transaction(self.session_factory, operation)
        except LeadershipLost:
            return False

    def _locks(self, session):
        from nexa.infrastructure.persistence import schema as s

        policy = (
            session.execute(
                select(s.policy_versions)
                .where(s.policy_versions.c.is_current.is_(True))
                .with_for_update()
            )
            .mappings()
            .one()
        )
        session.execute(
            select(s.tenant_policies)
            .where(s.tenant_policies.c.is_current.is_(True))
            .order_by(s.tenant_policies.c.tenant_id)
            .with_for_update()
        ).all()
        for scope in ("GLOBAL", "TENANT", "USER"):
            session.execute(
                select(s.admission_counters)
                .where(s.admission_counters.c.scope_type == scope)
                .order_by(s.admission_counters.c.scope_id)
                .with_for_update()
            ).all()
        worker_rows = (
            session.execute(select(s.workers).order_by(s.workers.c.worker_id).with_for_update())
            .mappings()
            .all()
        )
        if len(worker_rows) != 1:
            return policy, None, None
        worker = worker_rows[0]
        inventory = (
            session.execute(
                select(s.worker_inventories).where(
                    s.worker_inventories.c.worker_id == worker["worker_id"],
                    s.worker_inventories.c.inventory_version == worker["current_inventory_version"],
                    s.worker_inventories.c.worker_incarnation_id
                    == worker["current_incarnation_id"],
                )
            )
            .mappings()
            .one_or_none()
        )
        return policy, worker, inventory

    def tick(self, epoch: int):
        from nexa.application.job_service import JobService
        from nexa.coordinator.accounting import account_locked, epoch_ms
        from nexa.coordinator.dispatch import apply_decision
        from nexa.coordinator.snapshot import read_snapshot
        from nexa.domain.scheduling import (
            CreateReservation,
            Dispatch,
            Err,
            InvalidateReservation,
            NoDecision,
        )
        from nexa.infrastructure.persistence import schema as s
        from nexa.scheduler.policy import WeightedDominantResourceTimePolicy

        def snapshot_operation(session):
            self._timeouts(session)
            self._leader(session, epoch)
            policy, worker, inventory = self._locks(session)
            now = self._leader(session, epoch)
            if policy["operational_mode"] == "WRITE_FROZEN":
                return None
            account_locked(session, now)
            if inventory is None or not JobService._worker_is_ready(dict(worker), now):
                return None
            snapshot = read_snapshot(session, now, policy, worker, inventory, self.cursors)
            self._leader(session, epoch)
            return snapshot, epoch_ms(now)

        prepared = run_transaction(self.session_factory, snapshot_operation)
        if prepared is None:
            return NoDecision("worker_or_mode_unavailable")
        snapshot, now_ms = prepared
        proposal = WeightedDominantResourceTimePolicy().decide(snapshot, now_ms)
        if isinstance(proposal, Err):
            raise RuntimeError(f"invalid policy proposal: {proposal.error}")
        decision = proposal.value
        if isinstance(decision, NoDecision):
            for tenant_id, cursor in decision.continuation_cursors:
                priority, sequence, job_id = cursor.decode().split(":", 2)
                self.cursors[tenant_id] = (int(priority), int(sequence), job_id)
            return decision
        if not isinstance(decision, (Dispatch, CreateReservation, InvalidateReservation)):
            return decision

        def commit_operation(session):
            self._timeouts(session)
            self._leader(session, epoch)
            policy, worker, inventory = self._locks(session)
            if inventory is None or policy["operational_mode"] == "WRITE_FROZEN":
                return NoDecision("worker_or_mode_changed")
            job_id = getattr(decision, "job_id", None)
            if isinstance(decision, InvalidateReservation):
                job_id = session.execute(
                    select(s.reservations.c.job_id).where(
                        s.reservations.c.reservation_id == UUID(decision.reservation_id)
                    )
                ).scalar_one_or_none()
            job = (
                session.execute(
                    select(s.jobs).where(s.jobs.c.job_id == UUID(str(job_id))).with_for_update()
                )
                .mappings()
                .one_or_none()
                if job_id
                else None
            )
            if job is not None:
                session.execute(
                    select(s.logical_sessions)
                    .where(s.logical_sessions.c.job_id == job["job_id"])
                    .with_for_update()
                ).all()
                session.execute(
                    select(s.attempts)
                    .where(s.attempts.c.job_id == job["job_id"])
                    .order_by(s.attempts.c.attempt_id)
                    .with_for_update()
                ).all()
                session.execute(
                    select(s.attempt_leases)
                    .where(s.attempt_leases.c.job_id == job["job_id"])
                    .order_by(s.attempt_leases.c.lease_id)
                    .with_for_update()
                ).all()
            session.execute(
                select(s.allocations)
                .where(s.allocations.c.state != "RELEASED")
                .order_by(s.allocations.c.allocation_id)
                .with_for_update()
            ).all()
            now = self._leader(session, epoch)
            if not JobService._worker_is_ready(dict(worker), now):
                return NoDecision("worker_changed")
            account_locked(session, now)
            fresh = read_snapshot(session, now, policy, worker, inventory, self.cursors)
            checked = WeightedDominantResourceTimePolicy().decide(fresh, epoch_ms(now))
            if isinstance(checked, Err) or checked.value != decision:
                return NoDecision("proposal_changed")
            apply_decision(
                session,
                decision,
                worker=worker,
                job=job,
                now=now,
                epoch=epoch,
                holder_id=self.holder_id,
            )
            self._leader(session, epoch)
            return decision

        return run_transaction(self.session_factory, commit_operation)
