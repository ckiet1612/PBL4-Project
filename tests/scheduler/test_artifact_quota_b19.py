"""B19-R07: the policy sees only a boolean for the tenant artifact byte quota."""

from dataclasses import replace

from nexa.domain.scheduling import CreateReservation, Dispatch, NoDecision, Ok, ResourceCapacity
from nexa.scheduler.accounting import candidate_ineligibility_reason
from nexa.scheduler.policy import WeightedDominantResourceTimePolicy
from tests.scheduler.test_policy import _candidate, _limit, _snapshot, _window


def _decide(snapshot):
    result = WeightedDominantResourceTimePolicy().decide(snapshot, 0)
    assert isinstance(result, Ok)
    return result.value


def test_exhausted_tenant_candidates_are_ineligible_with_a_closed_reason():
    candidate = _candidate("job-a", "tenant-a", 1)
    limits = _limit("tenant-a")
    assert limits.artifact_quota_available is True
    held = ResourceCapacity(0, 0, 0)
    capacity = ResourceCapacity(8, 8, 1)
    assert (
        candidate_ineligibility_reason(
            candidate, now_ms=0, capacity=capacity, held=held, limits=limits
        )
        is None
    )
    assert (
        candidate_ineligibility_reason(
            candidate,
            now_ms=0,
            capacity=capacity,
            held=held,
            limits=replace(limits, artifact_quota_available=False),
        )
        == "artifact_quota_exhausted"
    )


def test_other_tenants_still_dispatch_while_one_tenant_is_over_quota():
    blocked = _candidate("job-a", "tenant-a", 1, priority=2, age=500)
    other = _candidate("job-b", "tenant-b", 2, priority=0)
    limits = (
        replace(_limit("tenant-a"), artifact_quota_available=False),
        _limit("tenant-b"),
    )
    snapshot = _snapshot(
        (
            ("tenant-a", _window(blocked, oldest=blocked)),
            ("tenant-b", _window(other)),
        ),
        limits=limits,
    )
    decision = _decide(snapshot)
    assert isinstance(decision, Dispatch) and decision.job_id == "job-b"


def test_an_aged_job_of_an_exhausted_tenant_gets_no_dispatch_and_no_reservation():
    aged = _candidate("job-a", "tenant-a", 1, age=500)
    snapshot = _snapshot(
        (("tenant-a", _window(aged, oldest=aged)),),
        limits=(replace(_limit("tenant-a"), artifact_quota_available=False),),
    )
    decision = _decide(snapshot)
    assert isinstance(decision, NoDecision)
    # Back under quota, the same aged Job is reserved again as usual.
    unblocked = _snapshot((("tenant-a", _window(aged, oldest=aged)),))
    assert _decide(unblocked) == CreateReservation("job-a", "tenant-a", "eligible_wait_threshold")
