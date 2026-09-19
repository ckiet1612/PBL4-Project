import pytest

from benchmarks.simulator.clock import ClockError, EventPhase, VirtualClock


def test_virtual_clock_advances_only_to_next_event() -> None:
    clock = VirtualClock(start_ms=10)
    clock.schedule(30, EventPhase.ARRIVAL, "later")
    clock.schedule(20, EventPhase.ARRIVAL, "next")

    event = clock.pop_next()

    assert event.payload == "next"
    assert clock.now_ms == 20


def test_same_timestamp_uses_phase_then_insertion_sequence() -> None:
    clock = VirtualClock(start_ms=0)
    clock.schedule(5, EventPhase.ARRIVAL, "arrival-first-inserted")
    clock.schedule(5, EventPhase.RELEASE, "release")
    clock.schedule(5, EventPhase.ARRIVAL, "arrival-second")

    assert [clock.pop_next().payload for _ in range(3)] == [
        "release",
        "arrival-first-inserted",
        "arrival-second",
    ]


def test_clock_rejects_scheduling_in_the_past() -> None:
    clock = VirtualClock(start_ms=10)

    with pytest.raises(ClockError, match="before current virtual time"):
        clock.schedule(9, EventPhase.ARRIVAL, "past")


def test_clock_rejects_negative_start() -> None:
    with pytest.raises(ClockError, match="non-negative"):
        VirtualClock(start_ms=-1)


def test_empty_clock_has_no_next_event() -> None:
    clock = VirtualClock(start_ms=0)

    with pytest.raises(ClockError, match="no scheduled events"):
        clock.pop_next()
