"""Bounded dispatch ticks, independent leadership renewal and ledger heartbeat.

The ledger heartbeat commits at most every 250 ms in its own transaction, so
ledger cadence does not depend on decision latency. No Docker access.
"""

import logging
import time
from threading import Event, Thread

_LOG = logging.getLogger(__name__)


def run(service, stop: Event) -> None:
    while not stop.is_set():
        try:
            epoch = service.acquire()
        except Exception:
            _LOG.warning("coordinator_acquire_unavailable")
            stop.wait(1)
            continue
        if epoch is None:
            stop.wait(1)
            continue
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
                start = time.monotonic()
                try:
                    service.tick(epoch)
                except Exception:
                    # Never continue mutations using a remembered lease after DB failure.
                    lost.set()
                    _LOG.warning("coordinator_tick_unavailable")
                stop.wait(max(0, 0.25 - (time.monotonic() - start)))
        finally:
            renewal_stop.set()
            thread.join(timeout=4)
            accounting.join(timeout=4)
        if not stop.is_set():
            stop.wait(1)
