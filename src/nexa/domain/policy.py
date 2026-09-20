from enum import StrEnum


class OperationalMode(StrEnum):
    NORMAL = "NORMAL"
    ADMISSION_OFF = "ADMISSION_OFF"
    WRITE_FROZEN = "WRITE_FROZEN"


class PolicyTransitionError(ValueError):
    pass


def validate_mode_transition(
    current: OperationalMode,
    target: OperationalMode,
    *,
    freeze_ready: bool,
    restore_verified: bool,
    readiness_verified: bool,
) -> None:
    if current is target:
        raise PolicyTransitionError("Operational mode transition must change state")
    if current is OperationalMode.NORMAL and target is OperationalMode.ADMISSION_OFF:
        return
    if current is OperationalMode.ADMISSION_OFF and target is OperationalMode.WRITE_FROZEN:
        if not freeze_ready:
            raise PolicyTransitionError(
                "Freeze requires stopped containers and reconciled unreleased allocations"
            )
        return
    if current is OperationalMode.WRITE_FROZEN and target is OperationalMode.ADMISSION_OFF:
        if not restore_verified:
            raise PolicyTransitionError(
                "Restore verification is required before leaving frozen mode"
            )
        return
    if current is OperationalMode.ADMISSION_OFF and target is OperationalMode.NORMAL:
        if not readiness_verified:
            raise PolicyTransitionError("Readiness and worker reconciliation are required")
        return
    raise PolicyTransitionError("Operational mode transitions cannot skip an intermediate state")
