"""Bounded dispatch ticks, independent leadership renewal and ledger heartbeat.

The ledger heartbeat commits at most every 250 ms in its own transaction, so
ledger cadence does not depend on decision latency. No Docker access.
"""

import logging
import time
from collections.abc import Callable
from threading import Event, Thread

from nexa.observability import metrics_coordinator

_LOG = logging.getLogger(__name__)
LIVENESS_STALE_SECONDS = 60.0


class CoordinatorStatus:
    """Process-local liveness beat and role for the ops listener; never used for decisions."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._beat = clock()
        self.leader = False

    def beat(self) -> None:
        self._beat = self._clock()

    def live(self) -> bool:
        return self._clock() - self._beat < LIVENESS_STALE_SECONDS

    def set_leader(self, leader: bool) -> None:
        self.leader = leader
        metrics_coordinator.leader(leader)


def run(service, stop: Event, status: CoordinatorStatus | None = None) -> None:
    status = status or CoordinatorStatus()
    while not stop.is_set():
        status.beat()
        try:
            epoch = service.acquire()
        except Exception:
            status.set_leader(False)
            _LOG.warning("coordinator_acquire_unavailable")
            stop.wait(1)
            continue
        if epoch is None:
            status.set_leader(False)
            stop.wait(1)
            continue
        status.set_leader(True)
        lost = Event()
        renewal_stop = Event()

        def renew(epoch=epoch, lost=lost, renewal_stop=renewal_stop):
            while not renewal_stop.wait(5):
                try:
                    if not service.renew(epoch):
                        lost.set()
                        return
                except Exception:
                    lost.set()
                    _LOG.warning("coordinator_renew_unavailable")
                    return

        def account(epoch=epoch, lost=lost, renewal_stop=renewal_stop):
            while not renewal_stop.is_set() and not lost.is_set():
                start = time.monotonic()
                try:
                    service.account(epoch)
                except Exception:
                    # Catch-up happens from the committed boundary after reacquire.
                    lost.set()
                    _LOG.warning("coordinator_accounting_unavailable")
                    return
                renewal_stop.wait(max(0, 0.25 - (time.monotonic() - start)))

        thread = Thread(target=renew, name="coordinator-renew", daemon=True)
        thread.start()
        accounting = Thread(target=account, name="coordinator-accounting", daemon=True)
        accounting.start()
        try:
            while not stop.is_set() and not lost.is_set():
                status.beat()
                start = time.monotonic()
                try:
                    service.tick(epoch)
                except Exception:
                    # Never continue mutations using a remembered lease after DB failure.
                    lost.set()
                    _LOG.warning("coordinator_tick_unavailable")
                metrics_coordinator.tick(time.monotonic() - start)
                stop.wait(max(0, 0.25 - (time.monotonic() - start)))
        finally:
            status.set_leader(False)
            renewal_stop.set()
            thread.join(timeout=4)
            accounting.join(timeout=4)
        if not stop.is_set():
            stop.wait(1)
