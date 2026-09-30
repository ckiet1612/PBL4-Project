"""RETRY_WAIT -> QUEUED once the durable backoff elapsed.

Cleanup already fixed the retry budget, backoff and reason; promotion only
closes the schedule and re-enters the queue with a new ready sequence. Locks
follow the tick/cleanup order (policy -> GLOBAL counter -> job -> schedule), so
promotion adds no new lock cycle. Outstanding counters do not change. An unlocked
probe keeps the locks for ticks that have something to write.
"""

from sqlalchemy import and_, or_, select, update

from nexa.application.job_service import JobService
from nexa.coordinator.dispatch import _event
from nexa.coordinator.eligibility import _compatible
from nexa.infrastructure.persistence import schema as s

BATCH_SIZE = 16
_BLOCKED = ("waiting_for_compatibility", "waiting_for_quota")
# SM:35 — a retry enters the queue to run, or to checkpoint for a pending pause (SM:31).
_PROMOTABLE = (("RUNNING", None), ("PAUSED", "CHECKPOINT_FOR_PAUSE"))


def _due(now):
    return (
        s.retry_schedules.c.closed_at.is_(None),
        s.retry_schedules.c.ready_at <= now,
        s.retry_schedules.c.retry_number == s.jobs.c.retry_count,
        s.jobs.c.state == "RETRY_WAIT",
        or_(
            and_(s.jobs.c.desired_state == "RUNNING", s.jobs.c.recovery_intent.is_(None)),
            and_(
                s.jobs.c.desired_state == "PAUSED",
                s.jobs.c.recovery_intent == "CHECKPOINT_FOR_PAUSE",
            ),
        ),
    )


def _candidates(now, limit):
    return (
        select(s.jobs)
        .join(s.retry_schedules, s.retry_schedules.c.job_id == s.jobs.c.job_id)
        .where(*_due(now))
        # Retries already known to be blocked cannot starve newly due ones.
        .order_by(
            s.jobs.c.waiting_reason.in_(_BLOCKED),
            s.retry_schedules.c.ready_at,
            s.retry_schedules.c.job_id,
        )
        .limit(limit)
    )


def retry_actionable(session, now, limit=BATCH_SIZE) -> bool:
    """Unlocked probe: does the batch `promote_due_locked` would take have anything to write?

    It reads the same candidates and block rules without locks, so a due retry whose
    block reason is unchanged costs no decision lock each tick (B14-K1). This read
    decides nothing: the locked path re-reads and re-checks every row, and a change
    committed after it is seen by that path or by the next probe.
    """
    candidates = session.execute(_candidates(now, limit)).mappings().all()
    if not candidates:
        return False
    _worker_row, inventory = _worker(session)
    for job in candidates:
        blocked = _blocked_reason(session, job, inventory)
        # Promotion, or a new reason to announce as RETRY_BLOCKED.
        if blocked is None or blocked != job["waiting_reason"]:
            return True
    return False


def _worker(session):
    workers = session.execute(select(s.workers)).mappings().all()
    if len(workers) != 1:
        return None, None
    worker = workers[0]
    inventory = (
        session.execute(
            select(s.worker_inventories).where(
                s.worker_inventories.c.worker_id == worker["worker_id"],
                s.worker_inventories.c.inventory_version == worker["current_inventory_version"],
                s.worker_inventories.c.worker_incarnation_id == worker["current_incarnation_id"],
            )
        )
        .mappings()
        .one_or_none()
    )
    return worker, inventory


def _blocked_reason(session, job, inventory):
    spec = (
        session.execute(select(s.job_specs).where(s.job_specs.c.job_id == job["job_id"]))
        .mappings()
        .one()
    )
    if inventory is not None:
        template = (
            session.execute(
                select(s.template_versions, s.templates.c.enabled)
                .join(s.templates, s.templates.c.template_id == s.template_versions.c.template_id)
                .where(
                    s.template_versions.c.template_id == spec["template_id"],
                    s.template_versions.c.version == spec["template_version"],
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            spec["gpu_count"] != 0
            or spec["cpu_millis"] > inventory["allocatable_cpu_millis"]
            or spec["memory_bytes"] > inventory["allocatable_memory_bytes"]
            or not _compatible(template, dict(inventory))
        ):
            return "waiting_for_compatibility"
    policy = (
        session.execute(
            select(s.tenant_policies).where(
                s.tenant_policies.c.tenant_id == job["tenant_id"],
                s.tenant_policies.c.is_current.is_(True),
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        policy is None
        or spec["cpu_millis"] > policy["cpu_limit_millis"]
        or spec["memory_bytes"] > policy["memory_limit_bytes"]
        or spec["gpu_count"] > policy["gpu_limit"]
    ):
        return "waiting_for_quota"
    return None


def promote_due_locked(session, *, now, holder_id, limit=BATCH_SIZE) -> int:
    """Promote at most `limit` due retries; caller holds leadership and the policy lock."""
    due = session.execute(_candidates(now, limit)).mappings()
    job_ids = sorted(row["job_id"] for row in due)
    if not job_ids:
        return 0
    counter = JobService._counter_lock(session, "GLOBAL", "global")
    worker, inventory = _worker(session)
    ready = worker is not None and JobService._worker_is_ready(dict(worker), now)
    sequence = int(counter["version"])
    promoted = 0
    for job_id in job_ids:
        job = (
            session.execute(select(s.jobs).where(s.jobs.c.job_id == job_id).with_for_update())
            .mappings()
            .one()
        )
        schedule = (
            session.execute(
                select(s.retry_schedules)
                .where(
                    s.retry_schedules.c.job_id == job_id,
                    s.retry_schedules.c.retry_number == job["retry_count"],
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if (
            schedule is None
            or schedule["closed_at"] is not None
            or schedule["ready_at"] > now
            or job["state"] != "RETRY_WAIT"
            or (job["desired_state"], job["recovery_intent"]) not in _PROMOTABLE
        ):
            continue
        blocked = _blocked_reason(session, job, inventory)
        if blocked is not None:
            if job["waiting_reason"] != blocked:
                _event(
                    session, job, now, holder_id, "RETRY_BLOCKED", blocked, waiting_reason=blocked
                )
            continue
        closed = session.execute(
            update(s.retry_schedules)
            .where(
                s.retry_schedules.c.job_id == job_id,
                s.retry_schedules.c.retry_number == schedule["retry_number"],
                s.retry_schedules.c.closed_at.is_(None),
            )
            .values(closed_at=now)
        ).rowcount
        if closed != 1:
            raise RuntimeError("retry promotion lost its compare-and-set")
        # The job row is locked and re-checked above; one UPDATE only (B15-R04).
        _event(
            session,
            job,
            now,
            holder_id,
            "RETRY_READY",
            "BACKOFF_ELAPSED",
            state="QUEUED",
            ready_sequence=sequence,
            waiting_reason=None if ready else "waiting_for_worker",
        )
        sequence += 1
        promoted += 1
    if promoted:
        session.execute(
            update(s.admission_counters)
            .where(
                s.admission_counters.c.scope_type == "GLOBAL",
                s.admission_counters.c.scope_id == "global",
            )
            .values(version=sequence, updated_at=now)
        )
    return promoted
