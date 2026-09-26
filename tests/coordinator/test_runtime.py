from threading import Event


def test_runtime_stop_before_start_performs_no_mutation():
    from nexa.coordinator.runtime import run

    class NeverCalled:
        def acquire(self):
            raise AssertionError("stopped runtime acquired leadership")

    stop = Event()
    stop.set()
    run(NeverCalled(), stop)


def test_runtime_accounting_heartbeat_is_independent_of_slow_ticks():
    import time
    from threading import Thread

    from nexa.coordinator.runtime import run

    stop = Event()
    accounted = []

    class SlowTicks:
        acquired = False

        def acquire(self):
            if self.acquired:
                stop.wait(5)
                return None
            self.acquired = True
            return 1

        def renew(self, epoch):
            return True

        def tick(self, epoch):
            stop.wait(1.2)

        def account(self, epoch):
            accounted.append(time.monotonic())

    thread = Thread(target=run, args=(SlowTicks(), stop))
    thread.start()
    time.sleep(1.6)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(accounted) >= 5
    assert max(b - a for a, b in zip(accounted, accounted[1:], strict=False)) < 0.5


def test_runtime_accounting_failure_fails_closed():
    from threading import Thread

    from nexa.coordinator.runtime import run

    stop = Event()
    calls = {"acquire": 0, "tick_after_failure": 0}
    failed = Event()

    class FailingAccount:
        def acquire(self):
            calls["acquire"] += 1
            if calls["acquire"] > 1:
                stop.set()
                return None
            return 1

        def renew(self, epoch):
            return True

        def tick(self, epoch):
            if failed.is_set():
                calls["tick_after_failure"] += 1
            calls["ticks"] = calls.get("ticks", 0) + 1
            if calls["ticks"] > 40:
                stop.set()
            stop.wait(0.05)

        def account(self, epoch):
            failed.set()
            raise RuntimeError("database unavailable")

    thread = Thread(target=run, args=(FailingAccount(), stop))
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert calls["acquire"] == 2
    assert calls["tick_after_failure"] <= 1
