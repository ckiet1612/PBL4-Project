from threading import Event


def test_runtime_stop_before_start_performs_no_mutation():
    from nexa.coordinator.runtime import run

    class NeverCalled:
        def acquire(self):
            raise AssertionError("stopped runtime acquired leadership")

    stop = Event()
    stop.set()
    run(NeverCalled(), stop)
