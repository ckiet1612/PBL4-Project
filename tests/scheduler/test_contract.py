from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from nexa.domain.scheduling import (
    AccountingRegression,
    AllocationState,
    Candidate,
    CandidateBoundExceeded,
    CandidateState,
    CandidateWindow,
    Dispatch,
    DuplicateGpuUuid,
    HeldAllocation,
    InvalidSnapshot,
    NonPositiveWeight,
    ResourceCapacity,
    ResourceRequest,
    TenantLedger,
    TenantPolicySnapshot,
)


def test_contract_values_are_immutable() -> None:
    request = ResourceRequest(cpu_millis=1000, memory_bytes=1024, gpu_count=0)

    with pytest.raises(FrozenInstanceError):
        request.cpu_millis = 2000  # type: ignore[misc]


def test_dispatch_preserves_expected_versions() -> None:
    decision = Dispatch(
        job_id="job-7",
        expected_job_version=11,
        expected_policy_version=4,
        reservation_id="reservation-1",
    )

    assert decision == Dispatch("job-7", 11, 4, "reservation-1")


def test_candidate_window_and_accounting_values_use_structural_equality() -> None:
    candidate = Candidate(
        job_id="job-1",
        tenant_id="tenant-a",
        user_id="user-a",
        job_version=2,
        ready_sequence=3,
        resources=ResourceRequest(1000, 2048, 1),
        base_priority=1,
        eligible_wait_seconds=60,
        retry_ready_at_ms=0,
        template_version=5,
        required_capabilities=frozenset({"cuda"}),
        tenant_active_attempts=0,
        user_active_attempts=0,
        state=CandidateState.QUEUED,
    )
    window = CandidateWindow(
        normal=(candidate,),
        oldest_eligible=candidate,
        continuation_cursor=b"next",
    )
    allocation = HeldAllocation(
        allocation_id="allocation-1",
        tenant_id="tenant-a",
        resources=candidate.resources,
        gpu_uuids=("GPU-a",),
        state=AllocationState.QUARANTINED,
    )
    ledger = TenantLedger("tenant-a", Decimal("2.5"), 1000, True)
    limits = TenantPolicySnapshot(
        tenant_id="tenant-a",
        weight=Decimal("2"),
        resource_quota=ResourceCapacity(4000, 8192, 1),
        max_active_attempts=2,
        max_user_active_attempts=1,
    )

    assert window.normal[0] is window.oldest_eligible
    assert allocation.state is AllocationState.QUARANTINED
    assert ledger.virtual_score == Decimal("2.5")
    assert limits.weight == Decimal(2)


def test_policy_errors_remain_distinct_typed_results() -> None:
    errors = (
        InvalidSnapshot("bad policy version"),
        AccountingRegression("tenant-a", 2000, 1000),
        DuplicateGpuUuid("GPU-a"),
        NonPositiveWeight("tenant-a", Decimal(0)),
        CandidateBoundExceeded("tenant-a", 17),
    )

    assert [type(error).__name__ for error in errors] == [
        "InvalidSnapshot",
        "AccountingRegression",
        "DuplicateGpuUuid",
        "NonPositiveWeight",
        "CandidateBoundExceeded",
    ]
