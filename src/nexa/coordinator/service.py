"""Leadership is independent from worker incarnation and attempt authority."""

import logging
import time
from datetime import timedelta
from uuid import UUID

from sqlalchemy import bindparam, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import clock_timestamp
from nexa.infrastructure.persistence.schema import coordinator_leadership
from nexa.infrastructure.persistence.transactions import run_transaction
from nexa.observability import metrics_coordinator
from nexa.observability.metrics import after_commit

_RETRY_PROBE_SECONDS = 1.0
# A blocked tenant's queue is re-marked this often, for Jobs that entered QUEUED since.
_QUOTA_RESCAN_SECONDS = 30.0
_LOG = logging.getLogger(__name__)


class LeadershipLost(RuntimeError):
    def __init__(self):
        super().__init__("coordinator leadership is no longer live")


class CoordinatorService:
    def __init__(
        self,
        session_factory,
        *,
        holder_id: UUID | None = None,
        artifact_quota_bytes: int | None = None,
    ):
        self.session_factory = session_factory
        self.holder_id = holder_id or new_uuid7()
        self.cursors = {}
        self._retry_probe_at = 0.0
        # None disables the byte-quota dispatch gate (B19-R07), e.g. in older tests.
        self.artifact_quota_bytes = artifact_quota_bytes
        # Exhausted tenant -> monotonic time its queue was fully marked (None: in progress).
        self._quota_blocked: dict = {}
        # Tenants whose quota reason is still being cleared; None = clear every tenant once.
        self._quota_clearing: set | None = None

    @staticmethod
    def _timeouts(session):
        session.execute(text("SET LOCAL lock_timeout = '1000ms'"))
        session.execute(text("SET LOCAL statement_timeout = '3000ms'"))
        # Short indexed reads; a 100-tenant queue estimate crosses the JIT
        # thresholds and compilation alone took ~1.1 s per statement.
        session.execute(text("SET LOCAL jit = off"))

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

    def account(self, epoch: int):
        """Commit the ledger boundary without waiting for decision-path locks.

        Only ledger/segment rows are locked. Every mutation of segment shares
        holds policy locks and calls account_locked, which refunds any overlap
        this heartbeat committed after that caller's DB time.
        """
        from nexa.coordinator.accounting import charge_locked, lock_ledgers
        from nexa.infrastructure.persistence import schema as s

        def operation(session):
            self._timeouts(session)
            self._live(session, epoch)
            ledgers = lock_ledgers(session, clock_timestamp(session), ensure_state=False)
            # Read after the ledger locks: a freeze charges under the same locks.
            mode = session.execute(
                select(s.policy_versions.c.operational_mode).where(
                    s.policy_versions.c.is_current.is_(True)
                )
            ).scalar_one()
            if mode == "WRITE_FROZEN":
                return None
            boundary = self._live(session, epoch)
            charge_locked(session, ledgers, boundary, strict=True)
            return boundary

        return run_transaction(self.session_factory, operation)

    def _live(self, session, epoch):
        # Unlocked read so the heartbeat never queues behind a decision tick.
        row = session.execute(select(coordinator_leadership)).mappings().one_or_none()
        now = clock_timestamp(session)
        if (
            row is None
            or row["holder_id"] != self.holder_id
            or row["epoch"] != epoch
            or row["lease_expires_at"] <= now
        ):
            raise LeadershipLost()
        return now

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

    def promote_retries(self, epoch: int) -> int:
        """Move due RETRY_WAIT jobs back to QUEUED under live leadership."""
        from nexa.coordinator import retry
        from nexa.infrastructure.persistence import schema as s

        def operation(session):
            self._timeouts(session)
            # No lock until the batch has something to write (B14-K1).
            if not retry.retry_actionable(session, func.clock_timestamp()):
                return 0
            self._leader(session, epoch)
            policy = (
                session.execute(
                    select(s.policy_versions)
                    .where(s.policy_versions.c.is_current.is_(True))
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            now = self._leader(session, epoch)
            if policy["operational_mode"] == "WRITE_FROZEN":
                return 0
            promoted = retry.promote_due_locked(session, now=now, holder_id=self.holder_id)
            self._leader(session, epoch)
            return promoted

        return run_transaction(self.session_factory, operation)

    def track_artifact_quota(self, epoch: int) -> int:
        """Keep `waiting_for_quota` on QUEUED Jobs of byte-quota-exhausted tenants (B19-R07).

        Dispatch is already gated by the snapshot boolean; this only maintains the
        derived reason, in bounded batches. In-process state changes only after commit.
        """
        if self.artifact_quota_bytes is None:
            return 0
        from nexa.coordinator import artifact_quota
        from nexa.infrastructure.persistence import schema as s

        clock = time.monotonic()

        def operation(session):
            self._timeouts(session)
            exhausted = artifact_quota.exhausted_tenants(session, self.artifact_quota_bytes)
            if self._quota_clearing is None:
                every = set(session.execute(select(s.tenants.c.tenant_id)).scalars())
                clearing = every - exhausted
            else:
                clearing = (self._quota_clearing | (self._quota_blocked.keys() - exhausted)) - (
                    exhausted
                )
            marking = [
                tenant
                for tenant in sorted(exhausted)
                if self._quota_blocked.get(tenant) is None
                or clock - self._quota_blocked[tenant] >= _QUOTA_RESCAN_SECONDS
            ]
            marked, cleared, wrote = {}, {}, False
            if marking or clearing:
                self._leader(session, epoch)
                mode = session.execute(
                    select(s.policy_versions.c.operational_mode)
                    .where(s.policy_versions.c.is_current.is_(True))
                    .with_for_update(read=True)
                ).scalar_one()
                now = self._leader(session, epoch)
                if mode != "WRITE_FROZEN":
                    for tenant in marking:
                        marked[tenant] = artifact_quota.mark_batch(
                            session, tenant, blocked=True, now=now
                        )
                    for tenant in sorted(clearing):
                        cleared[tenant] = artifact_quota.mark_batch(
                            session, tenant, blocked=False, now=now
                        )
                    self._leader(session, epoch)
                    wrote = True
            return exhausted, clearing, marked, cleared, wrote

        exhausted, clearing, marked, cleared, wrote = run_transaction(
            self.session_factory, operation
        )
        newly = exhausted - self._quota_blocked.keys()
        gone = self._quota_blocked.keys() - exhausted
        metrics_coordinator.quota_transition("blocked", len(newly))
        metrics_coordinator.quota_transition("unblocked", len(gone))
        for tenant in gone:
            del self._quota_blocked[tenant]
        for tenant in exhausted:
            if tenant in marked:
                full = marked[tenant] >= artifact_quota.BATCH_SIZE
                self._quota_blocked[tenant] = None if full else clock
            else:
                self._quota_blocked.setdefault(tenant, None)
        if wrote or (self._quota_clearing is None and not clearing):
            self._quota_clearing = {
                tenant
                for tenant in clearing
                if cleared.get(tenant, artifact_quota.BATCH_SIZE) >= artifact_quota.BATCH_SIZE
            }
        return sum(marked.values()) + sum(cleared.values())

    def reap_leases(self, epoch: int) -> int:
        """Revoke and fence expired leases, one transaction per lease, under live leadership."""
        from nexa.coordinator.reaper import expired_leases, reap_lease_locked
        from nexa.infrastructure.persistence import schema as s

        def probe(session):
            self._timeouts(session)
            return expired_leases(session)

        def reap(lease_id):
            def operation(session):
                self._timeouts(session)
                self._leader(session, epoch)
                mode = session.execute(
                    select(s.policy_versions.c.operational_mode)
                    .where(s.policy_versions.c.is_current.is_(True))
                    .with_for_update()
                ).scalar_one()
                if mode == "WRITE_FROZEN":
                    return False
                reaped = reap_lease_locked(
                    session,
                    lease_id=lease_id,
                    holder_id=self.holder_id,
                    now_fn=lambda: self._leader(session, epoch),
                )
                return reaped

            return run_transaction(self.session_factory, operation)

        reaped = 0
        for lease_id in run_transaction(self.session_factory, probe):
            try:
                if reap(lease_id):
                    reaped += 1
                    metrics_coordinator.leases_expired(1)
            except LeadershipLost:
                raise
            except Exception:
                # One lease (e.g. its job locked past lock_timeout) must not stop the
                # others; it is still due, so a later probe returns it (B15-R26).
                _LOG.warning("coordinator_reap_lease_failed")
        return reaped

    def sweep_idempotency(self, epoch: int) -> int:
        """Delete one bounded batch of expired job-request records under live leadership.

        Records of the batch that are not due are deferred in the same transaction.
        """
        from nexa.coordinator.retention import sweep_batch
        from nexa.infrastructure.persistence import schema as s

        records = s.idempotency_records

        def operation(session):
            self._timeouts(session)
            batch = sweep_batch(session)
            if not batch.due and not batch.deferred:
                return 0
            self._leader(session, epoch)
            mode = session.execute(
                select(s.policy_versions.c.operational_mode)
                .where(s.policy_versions.c.is_current.is_(True))
                .with_for_update(read=True)
            ).scalar_one()
            if mode == "WRITE_FROZEN":
                return 0
            if batch.due:
                session.execute(delete(records).where(records.c.idempotency_id.in_(batch.due)))
            if batch.deferred:
                session.execute(
                    update(records)
                    .where(records.c.idempotency_id == bindparam("record_id"))
                    .values(expires_at=func.greatest(records.c.expires_at, bindparam("until"))),
                    [{"record_id": record, "until": until} for record, until in batch.deferred],
                )
            self._leader(session, epoch)
            return len(batch.due)

        return run_transaction(self.session_factory, operation)

    def tick(self, epoch: int):
        from nexa.domain.scheduling import CreateReservation, Dispatch, InvalidateReservation

        decision = self._tick(epoch)
        # run_transaction returned, so the decision (or its absence) is committed.
        if isinstance(decision, Dispatch):
            metrics_coordinator.decision("offer")
        elif isinstance(decision, CreateReservation):
            metrics_coordinator.decision("reservation")
        elif isinstance(decision, InvalidateReservation):
            metrics_coordinator.decision("invalidate")
        else:
            metrics_coordinator.decision("none")
        return decision

    def _tick(self, epoch: int):
        from nexa.application.job_service import JobService
        from nexa.coordinator.accounting import account_locked, account_now_locked, epoch_ms
        from nexa.coordinator.dispatch import apply_decision
        from nexa.coordinator.snapshot import persist_fairness_locked, read_snapshot
        from nexa.domain.scheduling import (
            CreateReservation,
            Dispatch,
            Err,
            InvalidateReservation,
            NoDecision,
        )
        from nexa.infrastructure.persistence import schema as s
        from nexa.scheduler.policy import WeightedDominantResourceTimePolicy

        def reservation_replay_pending(session, snapshot):
            reservation = snapshot.active_reservation
            return (
                reservation is not None
                and session.execute(
                    select(s.queue_eligibility_events.c.event_id)
                    .where(
                        s.queue_eligibility_events.c.tenant_id == UUID(reservation.tenant_id),
                        s.queue_eligibility_events.c.completed_at.is_(None),
                    )
                    .limit(1)
                ).first()
                is not None
            )

        def snapshot_operation(session):
            self._timeouts(session)
            self._leader(session, epoch)
            policy, worker, inventory = self._locks(session)
            now = self._leader(session, epoch)
            # ADMISSION_OFF rejects new dispatch (SM:110); committed offers, running
            # attempts, the reaper and held-allocation charging continue (B15-OBS-02).
            if policy["operational_mode"] != "NORMAL":
                return None
            if inventory is None or not JobService._worker_is_ready(dict(worker), now):
                return None
            snapshot = read_snapshot(
                session,
                now,
                policy,
                worker,
                inventory,
                self.cursors,
                artifact_quota_bytes=self.artifact_quota_bytes,
            )
            # Ledger rows are locked only here, so the heartbeat never waits
            # for the replay and candidate reads above.
            persist_fairness_locked(session, snapshot, account_now_locked(session))
            self._leader(session, epoch)
            return snapshot, epoch_ms(now), reservation_replay_pending(session, snapshot)

        # At most one retry/reaper/sweep probe per second keeps the idle tick path unchanged.
        if time.monotonic() >= self._retry_probe_at:
            self._retry_probe_at = time.monotonic() + _RETRY_PROBE_SECONDS
            # Each maintenance step commits on its own; a failure in one is retried on
            # the next probe and never keeps dispatch from running (B15-R26).
            for name, step in (
                ("reap", self.reap_leases),
                ("promote", self.promote_retries),
                ("sweep", self.sweep_idempotency),
                ("quota", self.track_artifact_quota),
            ):
                try:
                    step(epoch)
                except LeadershipLost:
                    raise
                except Exception:
                    metrics_coordinator.maintenance_failed(name)
                    _LOG.warning("coordinator_maintenance_failed", extra={"step": name})
        prepared = run_transaction(self.session_factory, snapshot_operation)
        if prepared is None:
            return NoDecision("worker_or_mode_unavailable")
        snapshot, now_ms, reservation_pending = prepared
        if reservation_pending:
            return NoDecision("reservation_eligibility_replay_pending")
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
            if inventory is None or policy["operational_mode"] != "NORMAL":
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
            fresh = read_snapshot(
                session,
                now,
                policy,
                worker,
                inventory,
                self.cursors,
                replay_eligibility=False,
                artifact_quota_bytes=self.artifact_quota_bytes,
            )
            if reservation_replay_pending(session, fresh):
                return NoDecision("reservation_eligibility_replay_pending")
            checked = WeightedDominantResourceTimePolicy().decide(fresh, epoch_ms(now))
            if isinstance(checked, Err) or checked.value != decision:
                return NoDecision("proposal_changed")
            # Segment boundaries must equal allocation times, so charge through
            # `now`; account_locked refunds any later heartbeat overlap.
            account_locked(session, now)
            persist_fairness_locked(session, fresh, now)
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
            if isinstance(decision, Dispatch) and job["eligible_since"] is not None:
                waited = (now - job["eligible_since"]).total_seconds()
                after_commit(session, lambda: metrics_coordinator.dispatch_wait(waited))
            return decision

        return run_transaction(self.session_factory, commit_operation)
