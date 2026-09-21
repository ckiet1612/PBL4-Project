import pytest

from nexa.workloads.deadline import DeadlineController, DeadlineStateError


def test_deadline_uses_first_send_candidate_and_never_rebases() -> None:
    controller = DeadlineController(lease_duration_seconds=45, safety_margin_seconds=5)
    candidate = controller.first_send("cb-1", 100.0)
    assert candidate == 140.0
    assert controller.accept_ack("cb-1", sent_at=100.0, ack_at=101.0) == 140.0
    assert controller.set_authority_deadline("cb-1", 140.0, applied_at=102.0) == 140.0
    assert controller.retry_first_send("cb-1", now=200.0) == 100.0
    with pytest.raises(DeadlineStateError, match="late"):
        controller.accept_ack("cb-1", sent_at=100.0, ack_at=141.0)
    with pytest.raises(DeadlineStateError, match="late"):
        controller.accept_ack("cb-2", sent_at=100.0, ack_at=140.0)


def test_deadline_does_not_move_back_or_extend_on_failed_callback() -> None:
    controller = DeadlineController(lease_duration_seconds=45, safety_margin_seconds=5)
    controller.first_send("cb-1", 100.0)
    assert controller.fail("cb-1") is None
    with pytest.raises(DeadlineStateError, match="acknowledged"):
        controller.set_authority_deadline("cb-1", 140.0, applied_at=101.0)


def test_deadline_value_is_bound_to_the_acknowledged_callback_candidate() -> None:
    controller = DeadlineController(lease_duration_seconds=45, safety_margin_seconds=5)
    controller.first_send("cb-1", 100.0)
    controller.accept_ack("cb-1", sent_at=100.0, ack_at=101.0)
    with pytest.raises(DeadlineStateError, match="candidate"):
        controller.set_authority_deadline("cb-1", 1_000_000.0, applied_at=102.0)
    with pytest.raises(DeadlineStateError, match="acknowledged"):
        controller.set_authority_deadline("cb-2", 140.0, applied_at=102.0)


def test_expired_authority_cannot_be_revived() -> None:
    controller = DeadlineController(lease_duration_seconds=45, safety_margin_seconds=5)
    controller.first_send("cb-1", 100.0)
    controller.accept_ack("cb-1", sent_at=100.0, ack_at=101.0)
    with pytest.raises(DeadlineStateError, match="expired"):
        controller.set_authority_deadline("cb-1", 140.0, applied_at=140.0)
