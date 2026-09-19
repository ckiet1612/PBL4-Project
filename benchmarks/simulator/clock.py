import heapq
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class ClockError(ValueError):
    """Raised when virtual time would become invalid."""


class EventPhase(IntEnum):
    RELEASE = 0
    ARRIVAL = 1


@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    time_ms: int
    phase: EventPhase
    sequence: int
    payload: Any = field(compare=False)


class VirtualClock:
    def __init__(self, start_ms: int) -> None:
        if start_ms < 0:
            raise ClockError("virtual clock start must be non-negative")
        self._now_ms = start_ms
        self._next_sequence = 0
        self._events: list[tuple[int, int, int, ScheduledEvent]] = []

    @property
    def now_ms(self) -> int:
        return self._now_ms

    @property
    def has_events(self) -> bool:
        return bool(self._events)

    @property
    def next_time_ms(self) -> int:
        if not self._events:
            raise ClockError("virtual clock has no scheduled events")
        return self._events[0][0]

    def schedule(self, time_ms: int, phase: EventPhase, payload: Any) -> ScheduledEvent:
        if time_ms < self._now_ms:
            raise ClockError("cannot schedule an event before current virtual time")
        event = ScheduledEvent(time_ms, phase, self._next_sequence, payload)
        self._next_sequence += 1
        heapq.heappush(
            self._events,
            (event.time_ms, int(event.phase), event.sequence, event),
        )
        return event

    def pop_next(self) -> ScheduledEvent:
        if not self._events:
            raise ClockError("virtual clock has no scheduled events")
        _, _, _, event = heapq.heappop(self._events)
        self._now_ms = event.time_ms
        return event
