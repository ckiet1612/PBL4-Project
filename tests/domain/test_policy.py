import pytest

from nexa.domain.policy import OperationalMode, PolicyTransitionError, validate_mode_transition


@pytest.mark.parametrize(
    ("current", "target", "freeze_ready", "restore_verified", "readiness_verified"),
    [
        (OperationalMode.NORMAL, OperationalMode.ADMISSION_OFF, False, False, False),
        (OperationalMode.ADMISSION_OFF, OperationalMode.WRITE_FROZEN, True, False, False),
        (OperationalMode.WRITE_FROZEN, OperationalMode.ADMISSION_OFF, False, True, False),
        (OperationalMode.ADMISSION_OFF, OperationalMode.NORMAL, False, False, True),
    ],
)
def test_only_adjacent_mode_transitions_with_required_proof_are_allowed(
    current: OperationalMode,
    target: OperationalMode,
    freeze_ready: bool,
    restore_verified: bool,
    readiness_verified: bool,
) -> None:
    validate_mode_transition(
        current,
        target,
        freeze_ready=freeze_ready,
        restore_verified=restore_verified,
        readiness_verified=readiness_verified,
    )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OperationalMode.NORMAL, OperationalMode.WRITE_FROZEN),
        (OperationalMode.WRITE_FROZEN, OperationalMode.NORMAL),
        (OperationalMode.NORMAL, OperationalMode.NORMAL),
    ],
)
def test_direct_skip_or_noop_mode_transition_is_rejected(
    current: OperationalMode, target: OperationalMode
) -> None:
    with pytest.raises(PolicyTransitionError):
        validate_mode_transition(
            current,
            target,
            freeze_ready=True,
            restore_verified=True,
            readiness_verified=True,
        )


def test_missing_runtime_proof_fails_closed() -> None:
    with pytest.raises(PolicyTransitionError, match="reconcile"):
        validate_mode_transition(
            OperationalMode.ADMISSION_OFF,
            OperationalMode.WRITE_FROZEN,
            freeze_ready=False,
            restore_verified=False,
            readiness_verified=False,
        )
