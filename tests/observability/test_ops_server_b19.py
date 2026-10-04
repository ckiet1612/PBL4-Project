"""B19 ops listener: only GET /livez, /readyz, /metrics; bounded and isolated (R03)."""

import json
import socket
import threading
import time

import httpx
import pytest

from nexa.observability.ops_server import CachedReadiness, OpsServer, ReadinessReport


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _server(**overrides):
    options = {
        "live": lambda: True,
        "ready": lambda: ReadinessReport({"database": "ok", "storage": "skip"}),
        "metrics": lambda: b"# HELP nexa_up x\nnexa_up 1\n",
    }
    options.update(overrides)
    server = OpsServer(("127.0.0.1", 0), **options)
    server.start()
    return server


@pytest.fixture
def ops():
    servers = []

    def make(**overrides):
        server = _server(**overrides)
        servers.append(server)
        return f"http://127.0.0.1:{server.port}"

    yield make
    for server in servers:
        server.stop()


def test_probe_paths_and_bodies(ops):
    base = ops()
    live = httpx.get(f"{base}/livez")
    assert (live.status_code, live.json()) == (200, {"status": "live"})
    ready = httpx.get(f"{base}/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "checks": {"database": "ok", "storage": "skip"}}
    metrics = httpx.get(f"{base}/metrics")
    assert metrics.status_code == 200
    assert metrics.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert b"nexa_up 1" in metrics.content
    assert httpx.get(f"{base}/metrics?x=1").status_code == 200


def test_not_live_and_not_ready_return_503_with_extra_fields(ops):
    base = ops(
        live=lambda: False,
        ready=lambda: ReadinessReport({"database": "fail", "schema": "ok"}, {"role": "standby"}),
    )
    assert httpx.get(f"{base}/livez").status_code == 503
    ready = httpx.get(f"{base}/readyz")
    assert ready.status_code == 503
    assert ready.json() == {
        "status": "not_ready",
        "checks": {"database": "fail", "schema": "ok"},
        "role": "standby",
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("GET", "/v1/jobs"),
        ("GET", "/metrics/extra"),
        ("GET", "/healthz"),
        ("POST", "/metrics"),
        ("PUT", "/readyz"),
        ("DELETE", "/livez"),
        ("HEAD", "/metrics"),
        ("OPTIONS", "/metrics"),
    ],
)
def test_everything_else_is_404(ops, method, path):
    base = ops()
    response = httpx.request(method, f"{base}{path}", content=b"x" * 1024)
    assert response.status_code == 404


def test_metrics_failure_is_500_and_counted(ops):
    errors = []

    def boom() -> bytes:
        raise RuntimeError("collector exploded")

    base = ops(metrics=boom, on_internal_error=lambda: errors.append(1))
    response = httpx.get(f"{base}/metrics")
    assert response.status_code == 500
    assert b"collector exploded" not in response.content
    assert errors == [1]
    assert httpx.get(f"{base}/livez").status_code == 200


def test_readiness_probe_exception_is_not_ready(ops):
    def boom() -> ReadinessReport:
        raise RuntimeError("db down")

    response = httpx.get(f"{ops(ready=boom)}/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_client_disconnect_does_not_break_the_listener(ops):
    base = ops(metrics=lambda: b"x" * (4 * 1024 * 1024))
    port = int(base.rsplit(":", 1)[1])
    for _ in range(5):
        with socket.create_connection(("127.0.0.1", port)) as sock:
            sock.sendall(b"GET /metrics HTTP/1.1\r\nHost: x\r\n\r\n")
            sock.recv(10)
    with socket.create_connection(("127.0.0.1", port)) as sock:
        sock.sendall(b"GET /livez HTTP/1.0\r\n")  # never finishes the request head
    assert httpx.get(f"{base}/livez").status_code == 200


def test_concurrency_is_bounded_with_503(ops):
    release = threading.Event()
    entered = threading.Semaphore(0)

    def slow() -> bytes:
        entered.release()
        release.wait(5)
        return b"ok\n"

    base = ops(metrics=slow)
    results: list[int] = []
    threads = [
        threading.Thread(target=lambda: results.append(httpx.get(f"{base}/metrics").status_code))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for _ in range(4):
        assert entered.acquire(timeout=5)
    busy = httpx.get(f"{base}/livez")
    release.set()
    for thread in threads:
        thread.join(5)
    assert busy.status_code == 503
    assert results == [200, 200, 200, 200]


def test_bind_failure_raises():
    first = _server()
    try:
        with pytest.raises(OSError):
            OpsServer(
                ("127.0.0.1", first.port),
                live=lambda: True,
                ready=lambda: ReadinessReport({}),
                metrics=lambda: b"",
            ).start()
    finally:
        first.stop()


def test_cached_readiness_ttl_and_single_flight():
    clock = _Clock()
    calls = []

    def probe() -> ReadinessReport:
        calls.append(clock.now)
        return ReadinessReport({"database": "ok" if len(calls) == 1 else "fail"})

    cached = CachedReadiness(probe, ttl_seconds=2.0, clock=clock)
    assert cached().ready
    clock.now += 1.9
    assert cached().ready
    clock.now += 0.2
    assert not cached().ready
    assert len(calls) == 2
    with pytest.raises(ValueError):
        CachedReadiness(probe, ttl_seconds=2.5)


def test_report_body_and_ready_rule():
    assert ReadinessReport({"a": "ok", "b": "skip"}).ready
    assert not ReadinessReport({"a": "ok", "b": "fail"}).ready
    assert not ReadinessReport({}).ready
    with pytest.raises(ValueError):
        ReadinessReport({"a": "maybe"})
    body = ReadinessReport({"a": "ok"}, {"role": "leader"}).body()
    expected = {"status": "ready", "checks": {"a": "ok"}, "role": "leader"}
    assert json.loads(json.dumps(body)) == expected


def test_metrics_scrape_alone_refreshes_cached_readiness(ops):
    # B19-RV02: Prometheus scrapes only /metrics; the probe (which sets nexa_ready) must
    # still run, through the same cache and single flight as /readyz.
    clock = _Clock()
    calls = []

    def probe() -> ReadinessReport:
        calls.append(clock.now)
        return ReadinessReport({"database": "ok"})

    base = ops(ready=CachedReadiness(probe, clock=clock))
    assert httpx.get(f"{base}/metrics").status_code == 200
    assert httpx.get(f"{base}/metrics").status_code == 200
    assert calls == [100.0]  # cached for two seconds
    clock.now += 2.0
    assert httpx.get(f"{base}/metrics").status_code == 200
    assert calls == [100.0, 102.0]


def test_a_raising_probe_marks_readiness_failed_and_metrics_still_serve(ops):
    failures, errors = [], []

    def boom() -> ReadinessReport:
        raise RuntimeError("db down")

    base = ops(
        ready=boom,
        on_ready_failure=lambda: failures.append(1),
        on_internal_error=lambda: errors.append(1),
    )
    assert httpx.get(f"{base}/metrics").status_code == 200
    assert httpx.get(f"{base}/readyz").status_code == 503
    assert failures == [1, 1] and errors == [1, 1]


def test_slow_request_head_is_dropped_at_the_deadline(ops):
    # B19-RV06: clients trickling one byte at a time stay under the per-read timeout but
    # must not hold the four slots past the request-head deadline.
    base = ops(head_deadline_seconds=1.0)
    port = int(base.rsplit(":", 1)[1])
    stop = threading.Event()
    closed: list[float] = []

    def trickle() -> None:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            started = time.monotonic()
            sock.sendall(b"GET /livez HTTP/1.1\r\n")
            while not stop.wait(0.2):
                try:
                    sock.sendall(b"X")
                    if not sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT):
                        break  # the server closed the connection
                except BlockingIOError:
                    continue  # still open, nothing to read
                except OSError:
                    break
            if not stop.is_set():
                closed.append(time.monotonic() - started)

    threads = [threading.Thread(target=trickle) for _ in range(4)]
    for thread in threads:
        thread.start()
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                if httpx.get(f"{base}/livez", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, "slots never freed"
            stop.wait(0.05)
        for thread in threads:
            thread.join(5)
    finally:
        stop.set()
        for thread in threads:
            thread.join(5)
    # Every trickling client was disconnected by the server, near the deadline.
    assert len(closed) == 4 and all(0.9 <= elapsed < 3 for elapsed in closed), closed
