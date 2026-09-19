from decimal import Decimal

import pytest

from benchmarks.b04.adapter import ProductSnapshotAdapter
from benchmarks.simulator.model import (
    Allocation,
    JobSpec,
    ResourceVector,
    SimulationConfig,
    TenantSpec,
)
from nexa.domain.scheduling import ReservationSnapshot, TenantLedger


def _config() -> SimulationConfig:
    return SimulationConfig(
        capacity=ResourceVector(4, 4, 1),
        gpu_uuids=("GPU-a",),
        tenants=(
            TenantSpec("tenant-a", 1, ResourceVector(8, 8, 1), 4, 0),
            TenantSpec("tenant-b", 1, ResourceVector(8, 8, 1), 4, 1),
        ),
    )


def _job(job_id: str, tenant_id: str, sequence: int, cpu: int = 1) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        tenant_id=tenant_id,
        ready_sequence=sequence,
        arrival_ms=0,
        resources=ResourceVector(cpu, 1, 0),
        duration_ms=1000,
    )


def test_adapter_keeps_non_fitting_job_and_bounds_normal_window() -> None:
    large = _job("large", "tenant-a", 0, cpu=4)
    small = tuple(_job(f"small-{index}", "tenant-a", index + 1) for index in range(20))
    held = Allocation(
        job_id="running",
        tenant_id="tenant-b",
        resources=ResourceVector(1, 1, 0),
        gpu_uuids=(),
        start_ms=0,
        release_ms=1000,
    )

    snapshot = ProductSnapshotAdapter().build(
        config=_config(),
        now_ms=120_000,
        queued_jobs=(large, *small),
        held_allocations=(held,),
        tenant_ledgers=(
            TenantLedger("tenant-a", Decimal(0), 120_000, True),
            TenantLedger("tenant-b", Decimal(0), 120_000, False),
        ),
        virtual_floor=Decimal(0),
        active_reservation=None,
        eligible_wait_overrides=tuple((job.job_id, 120) for job in (large, *small)),
    )

    windows = dict(snapshot.candidates_by_tenant)
    assert len(windows["tenant-a"].normal) == 16
    assert windows["tenant-a"].oldest_eligible is not None
    assert windows["tenant-a"].oldest_eligible.job_id == "large"
    assert windows["tenant-a"].oldest_eligible.eligible_wait_seconds == 120
    assert windows["tenant-a"].continuation_cursor is not None


def test_adapter_reports_active_counts_and_preserves_reservation() -> None:
    queued = _job("queued", "tenant-a", 2)
    held = Allocation(
        job_id="running",
        tenant_id="tenant-a",
        resources=ResourceVector(1, 1, 1),
        gpu_uuids=("GPU-a",),
        start_ms=0,
        release_ms=1000,
    )
    reservation = ReservationSnapshot("reservation-1", "queued", "tenant-a", 1)

    snapshot = ProductSnapshotAdapter().build(
        config=_config(),
        now_ms=500,
        queued_jobs=(queued,),
        held_allocations=(held,),
        tenant_ledgers=(
            TenantLedger("tenant-a", Decimal(0), 500, True),
            TenantLedger("tenant-b", Decimal(0), 500, False),
        ),
        virtual_floor=Decimal(0),
        active_reservation=reservation,
        eligible_wait_overrides=((queued.job_id, 0),),
    )

    candidate = dict(snapshot.candidates_by_tenant)["tenant-a"].normal[0]
    assert candidate.tenant_active_attempts == 1
    assert candidate.user_active_attempts == 1
    assert snapshot.active_reservation == reservation


def test_adapter_oldest_eligible_respects_remaining_tenant_quota() -> None:
    config = SimulationConfig(
        capacity=ResourceVector(8, 8, 0),
        gpu_uuids=(),
        tenants=(TenantSpec("tenant-a", 1, ResourceVector(8, 8, 0), 4, 0),),
    )
    held = Allocation(
        job_id="running",
        tenant_id="tenant-a",
        resources=ResourceVector(7, 7, 0),
        gpu_uuids=(),
        start_ms=0,
        release_ms=1000,
    )
    quota_blocked_oldest = _job("old-blocked", "tenant-a", 1, cpu=2)
    eligible_later = _job("later-eligible", "tenant-a", 2, cpu=1)

    snapshot = ProductSnapshotAdapter().build(
        config=config,
        now_ms=120_000,
        queued_jobs=(quota_blocked_oldest, eligible_later),
        held_allocations=(held,),
        tenant_ledgers=(TenantLedger("tenant-a", Decimal(0), 120_000, True),),
        virtual_floor=Decimal(0),
        active_reservation=None,
        eligible_wait_overrides=(
            (quota_blocked_oldest.job_id, 120),
            (eligible_later.job_id, 120),
        ),
    )

    oldest = dict(snapshot.candidates_by_tenant)["tenant-a"].oldest_eligible
    assert oldest is not None
    assert oldest.job_id == "later-eligible"


@pytest.mark.parametrize(
    "overrides, error",
    [
        ((("oldest", 0),), "must match queued jobs"),
        (
            (("oldest", 0), ("oldest", 1), ("later", 0)),
            "duplicate eligible wait override",
        ),
    ],
)
def test_adapter_rejects_incomplete_or_duplicate_eligible_wait(
    overrides: tuple[tuple[str, int], ...], error: str
) -> None:
    oldest = _job("oldest", "tenant-a", 1)
    later = _job("later", "tenant-a", 2)

    with pytest.raises(ValueError, match=error):
        ProductSnapshotAdapter().build(
            config=_config(),
            now_ms=200_000,
            queued_jobs=(oldest, later),
            held_allocations=(),
            tenant_ledgers=(
                TenantLedger("tenant-a", Decimal(0), 200_000, True),
                TenantLedger("tenant-b", Decimal(0), 200_000, False),
            ),
            virtual_floor=Decimal(0),
            active_reservation=None,
            eligible_wait_overrides=overrides,
        )
