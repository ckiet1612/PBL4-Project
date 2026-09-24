"""Durable held-resource segments; caller holds policy and worker locks first.

Call account_locked before any allocation, capacity or weight mutation, then
rebase_locked afterwards in the same transaction. Neither function commits.
"""

from datetime import UTC, datetime
from decimal import Decimal, localcontext

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7


def epoch_ms(value: datetime) -> int:
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def _elapsed_seconds(start, end):
    return Decimal(epoch_ms(end) - epoch_ms(start)) / Decimal(1000)


def _ledger_rows(session, now):
    tenant_ids = (
        session.execute(
            select(s.tenant_policies.c.tenant_id)
            .where(s.tenant_policies.c.is_current.is_(True))
            .order_by(s.tenant_policies.c.tenant_id)
        )
        .scalars()
        .all()
    )
    session.execute(
        insert(s.fairness_state)
        .values(singleton_key="local", virtual_floor=Decimal(0))
        .on_conflict_do_nothing()
    )
    for tenant_id in tenant_ids:
        session.execute(
            insert(s.fairness_ledgers)
            .values(
                tenant_id=tenant_id,
                virtual_score=Decimal(0),
                accounted_through=now,
                had_eligible_demand=False,
            )
            .on_conflict_do_nothing()
        )
    return (
        session.execute(
            select(s.fairness_ledgers).order_by(s.fairness_ledgers.c.tenant_id).with_for_update()
        )
        .mappings()
        .all()
    )


def account_locked(session, now):
    """Charge each interval once at the previously committed share and weight."""
    ledgers = _ledger_rows(session, now)
    segments = (
        session.execute(
            select(s.allocation_ledger_segments)
            .where(s.allocation_ledger_segments.c.ended_at.is_(None))
            .order_by(s.allocation_ledger_segments.c.allocation_id)
            .with_for_update()
        )
        .mappings()
        .all()
    )
    with localcontext() as context:
        context.prec = 50
        for ledger in ledgers:
            if now < ledger["accounted_through"]:
                raise RuntimeError("accounting DB time regressed")
            owned = [segment for segment in segments if segment["tenant_id"] == ledger["tenant_id"]]
            charge = Decimal(0)
            if owned:
                start = max(ledger["accounted_through"], owned[0]["started_at"])
                if any(
                    segment["started_at"] != owned[0]["started_at"]
                    or segment["weight"] != owned[0]["weight"]
                    for segment in owned
                ):
                    raise RuntimeError("inconsistent tenant accounting boundary")
                elapsed = _elapsed_seconds(start, now)
                share = sum((segment["dominant_share"] for segment in owned), Decimal(0))
                charge = share * elapsed / owned[0]["weight"]
                assigned = Decimal(0)
                for index, segment in enumerate(owned):
                    amount = (
                        charge - assigned
                        if index == len(owned) - 1
                        else segment["dominant_share"] * elapsed / segment["weight"]
                    )
                    assigned += amount
                    session.execute(
                        update(s.allocation_ledger_segments)
                        .where(s.allocation_ledger_segments.c.segment_id == segment["segment_id"])
                        .values(charged_amount=segment["charged_amount"] + amount)
                    )
            session.execute(
                update(s.fairness_ledgers)
                .where(s.fairness_ledgers.c.tenant_id == ledger["tenant_id"])
                .values(
                    virtual_score=ledger["virtual_score"] + charge,
                    accounted_through=now,
                    version=ledger["version"] + 1,
                    updated_at=now,
                )
            )


def rebase_locked(session, now):
    """Close old segments and open the current dominant-axis contributions.

    The dominant axis is chosen for aggregate tenant holdings (B04), so summing
    per-allocation contributions never charges max(cpu,ram) separately per job.
    """
    held = (
        session.execute(
            select(s.allocations)
            .where(s.allocations.c.state != "RELEASED")
            .order_by(s.allocations.c.allocation_id)
        )
        .mappings()
        .all()
    )
    policies = {
        row["tenant_id"]: row
        for row in session.execute(
            select(s.tenant_policies).where(s.tenant_policies.c.is_current.is_(True))
        ).mappings()
    }
    inventory = (
        session.execute(
            select(s.worker_inventories).join(
                s.workers,
                (s.workers.c.worker_id == s.worker_inventories.c.worker_id)
                & (
                    s.workers.c.current_inventory_version
                    == s.worker_inventories.c.inventory_version
                ),
            )
        )
        .mappings()
        .one_or_none()
    )
    if inventory is None and held:
        raise RuntimeError("held allocation has no current inventory")
    version = session.execute(
        select(s.policy_versions.c.policy_version).where(s.policy_versions.c.is_current.is_(True))
    ).scalar_one()
    session.execute(
        update(s.allocation_ledger_segments)
        .where(s.allocation_ledger_segments.c.ended_at.is_(None))
        .values(ended_at=now)
    )
    if not held:
        return
    capacities = (
        inventory["allocatable_cpu_millis"],
        inventory["allocatable_memory_bytes"],
        inventory["allocatable_gpu_count"],
    )
    fields = ("cpu_millis", "memory_bytes", "gpu_count")
    with localcontext() as context:
        context.prec = 50
        for tenant_id, policy in policies.items():
            owned = [row for row in held if row["tenant_id"] == tenant_id]
            if not owned:
                continue
            totals = [sum(row[field] for row in owned) for field in fields]
            if any(
                total and capacity <= 0 for total, capacity in zip(totals, capacities, strict=True)
            ):
                raise RuntimeError("held resources exceed current capacity")
            axis = max(
                range(3),
                key=lambda i: (
                    Decimal(totals[i]) / Decimal(capacities[i]) if capacities[i] else Decimal(0)
                ),
            )
            aggregate_share = Decimal(totals[axis]) / Decimal(capacities[axis])
            assigned_share = Decimal(0)
            for index, allocation in enumerate(owned):
                share = (
                    aggregate_share - assigned_share
                    if index == len(owned) - 1
                    else Decimal(allocation[fields[axis]]) / Decimal(capacities[axis])
                )
                assigned_share += share
                session.execute(
                    insert(s.allocation_ledger_segments).values(
                        segment_id=new_uuid7(),
                        allocation_id=allocation["allocation_id"],
                        tenant_id=tenant_id,
                        started_at=now,
                        dominant_share=share,
                        weight=policy["weight"],
                        charged_amount=Decimal(0),
                        policy_version=version,
                    )
                )
