"""Replay mutation-time eligibility boundaries in bounded PostgreSQL batches."""

from datetime import datetime

from sqlalchemy import DateTime, cast, column, exists, select, update, values
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from nexa.application.errors import ApplicationError
from nexa.application.job_service import JobService
from nexa.infrastructure.persistence import schema as s

_BATCH_SIZE = 64


def _compatible(template, inventory):
    if inventory is None or template is None or not template["enabled"]:
        return False
    if template["adapter_id"] != "cpu.iterative":
        return False
    try:
        return JobService._inventory_supports(
            inventory, JobService._inventory_requirements(template)
        )
    except ApplicationError:
        return False


def _possible(spec, state, templates, cache):
    if (
        not state.get("enabled", True)
        or spec["gpu_count"] != 0
        or spec["cpu_millis"] > state["cpu"]
        or spec["memory_bytes"] > state["memory"]
    ):
        return False
    key = (spec["template_id"], spec["template_version"])
    inventory = state["inventory"]
    cache_key = (key, inventory.get("inventory_id") if inventory else None)
    if cache_key not in cache:
        cache[cache_key] = _compatible(templates.get(key), inventory)
    return cache[cache_key]


def pending_eligibility_tenants(session) -> set:
    return {
        tenant_id
        for tenant_id in session.execute(
            select(s.queue_eligibility_events.c.tenant_id)
            .where(s.queue_eligibility_events.c.completed_at.is_(None))
            .distinct()
        ).scalars()
    }


def process_eligibility_batch(session, now: datetime) -> set:
    """Apply one event page; pending tenants cannot dispatch from stale age."""
    older = s.queue_eligibility_events.alias("older_eligibility_event")
    event = (
        session.execute(
            select(s.queue_eligibility_events)
            .where(
                s.queue_eligibility_events.c.completed_at.is_(None),
                ~exists(
                    select(older.c.event_id).where(
                        older.c.tenant_id == s.queue_eligibility_events.c.tenant_id,
                        older.c.event_id < s.queue_eligibility_events.c.event_id,
                        older.c.completed_at.is_(None),
                    )
                ),
            )
            .order_by(
                s.queue_eligibility_events.c.last_processed_at.asc().nulls_first(),
                s.queue_eligibility_events.c.event_id,
            )
            .limit(1)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if event is not None:
        templates = {
            (row["template_id"], row["version"]): row
            for row in session.execute(
                select(s.template_versions, s.templates.c.enabled).join(
                    s.templates,
                    s.templates.c.template_id == s.template_versions.c.template_id,
                )
            ).mappings()
        }
        query = (
            select(s.jobs, s.job_specs)
            .join(s.job_specs, s.jobs.c.job_id == s.job_specs.c.job_id)
            .where(
                s.jobs.c.tenant_id == event["tenant_id"],
                s.jobs.c.state == "QUEUED",
                s.jobs.c.desired_state == "RUNNING",
                s.jobs.c.recovery_intent.is_(None),
                s.jobs.c.created_at <= event["occurred_at"],
            )
            .order_by(s.jobs.c.job_id)
            .limit(_BATCH_SIZE)
            .with_for_update(of=s.jobs)
        )
        if event["last_job_id"] is not None:
            query = query.where(s.jobs.c.job_id > event["last_job_id"])
        rows = session.execute(query).mappings().all()
        old_cache, new_cache = {}, {}
        changes = []
        for row in rows:
            if event["old_state"].get("rebuild_current_state"):
                new_possible = _possible(row, event["new_state"], templates, new_cache)
                age = row["eligible_since"] if new_possible else None
                if new_possible and age is None:
                    age = max(event["occurred_at"], row["retry_ready_at"] or event["occurred_at"])
                if row["eligible_since"] != age:
                    changes.append((row["job_id"], age))
                continue
            old_possible = _possible(row, event["old_state"], templates, old_cache)
            new_possible = _possible(row, event["new_state"], templates, new_cache)
            if old_possible == new_possible:
                continue
            age = None
            if new_possible:
                age = max(event["occurred_at"], row["retry_ready_at"] or event["occurred_at"])
            if row["eligible_since"] != age:
                changes.append((row["job_id"], age))
        if changes:
            changed_ages = (
                values(
                    column("job_id", PG_UUID(as_uuid=True)),
                    column("eligible_since", DateTime(timezone=True)),
                    name="b13_changed_ages",
                )
                .data(changes)
                .alias("b13_changed_ages")
            )
            session.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == changed_ages.c.job_id)
                .values(eligible_since=cast(changed_ages.c.eligible_since, DateTime(timezone=True)))
            )
        session.execute(
            update(s.queue_eligibility_events)
            .where(s.queue_eligibility_events.c.event_id == event["event_id"])
            .values(
                last_job_id=rows[-1]["job_id"] if rows else event["last_job_id"],
                last_processed_at=now,
                completed_at=now if len(rows) < _BATCH_SIZE else None,
            )
        )
    return pending_eligibility_tenants(session)
