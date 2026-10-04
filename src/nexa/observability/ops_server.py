"""Separate ops listener serving only GET /livez, /readyz and /metrics (B19-R03).

The listener is a stdlib threading HTTP server on its own bind (NEXA_OPS_BIND), off by
default and outside `/v1`, OpenAPI and Caddy. It never reads a request body, bounds
concurrent requests, and times out slow clients: every socket operation, and the whole
request head, has a deadline. A failure inside one probe answers that request with 5xx
and leaves the process and the REST API untouched.

Prometheus scrapes only /metrics, so serving /metrics first refreshes the cached
readiness probe; the probe sets `nexa_ready{check}` (B19-RV02).
"""

import json
import logging
import select
import socket
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from nexa.observability.logging import log_event

METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
CHECK_VALUES = frozenset({"ok", "fail", "skip"})
MAX_READY_CACHE_SECONDS = 2.0
SOCKET_TIMEOUT_SECONDS = 5.0
REQUEST_HEAD_DEADLINE_SECONDS = 5.0
_LOG = logging.getLogger(__name__)
_BUSY = b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


@dataclass(frozen=True)
class ReadinessReport:
    """`checks` maps a check name to ok/fail/skip; ready when none fails and one ran."""

    checks: Mapping[str, str]
    extra: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if any(value not in CHECK_VALUES for value in self.checks.values()):
            raise ValueError("readiness check values must be ok, fail or skip")

    @property
    def ready(self) -> bool:
        values = set(self.checks.values())
        return "ok" in values and "fail" not in values

    def body(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.ready else "not_ready",
            "checks": dict(self.checks),
            **dict(self.extra),
        }


class CachedReadiness:
    """Single-flight readiness probe cached for at most two seconds."""

    def __init__(
        self,
        probe: Callable[[], ReadinessReport],
        *,
        ttl_seconds: float = MAX_READY_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 < ttl_seconds <= MAX_READY_CACHE_SECONDS:
            raise ValueError("readiness cache must be between 0 and 2 seconds")
        self._probe = probe
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._value: tuple[float, ReadinessReport] | None = None

    def __call__(self) -> ReadinessReport:
        with self._lock:
            now = self._clock()
            if self._value is not None and now - self._value[0] < self._ttl:
                return self._value[1]
            report = self._probe()
            self._value = (self._clock(), report)
            return report


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], ops: "OpsServer") -> None:
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.ops = ops
        self._slots = threading.BoundedSemaphore(ops.max_concurrency)
        super().__init__(address, _Handler)

    def process_request(self, request, client_address) -> None:  # noqa: ANN001
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(0.5)
                _discard_pending(request)
                request.sendall(_BUSY)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:  # noqa: ANN001
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        # Client disconnects and protocol errors are not process failures; never print
        # the default traceback to stderr.
        log_event(_LOG, logging.DEBUG, "ops_request_failed")


def _discard_pending(sock: socket.socket, limit: int = 65536) -> None:
    """Drop bytes the client already sent so closing does not reset the response."""
    remaining = limit
    while remaining > 0:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return
        try:
            chunk = sock.recv(min(remaining, 8192))
        except OSError:
            return
        if not chunk:
            return
        remaining -= len(chunk)


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "nexa-ops"
    sys_version = ""
    timeout = SOCKET_TIMEOUT_SECONDS

    def setup(self) -> None:
        super().setup()
        # A per-read timeout alone lets a client trickle one byte at a time and hold a
        # slot forever; the whole request head must arrive before this deadline (RV06).
        self._head_timer = threading.Timer(self.server.ops.head_deadline_seconds, self._expire)
        self._head_timer.daemon = True
        self._head_timer.start()

    def _expire(self) -> None:
        with suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        self._head_timer.cancel()
        return parsed

    def finish(self) -> None:
        self._head_timer.cancel()
        super().finish()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        ops = self.server.ops
        if path == "/livez":
            try:
                live = bool(ops.live())
            except Exception:  # noqa: BLE001 - a broken probe means not live
                live = False
                ops.internal_error()
            self._json(200 if live else 503, {"status": "live" if live else "not_live"})
        elif path == "/readyz":
            try:
                report = ops.ready()
            except Exception:  # noqa: BLE001 - a broken probe means not ready
                ops.ready_failed()
                report = ReadinessReport({"probe": "fail"})
            self._json(200 if report.ready else 503, report.body())
        elif path == "/metrics":
            ops.refresh_readiness()
            try:
                payload = ops.metrics()
            except Exception:  # noqa: BLE001 - never expose the failure text
                ops.internal_error()
                self._send(500, b"metrics unavailable\n", "text/plain; charset=utf-8")
                return
            self._send(200, payload, METRICS_CONTENT_TYPE)
        else:
            self._not_found()

    def _not_found(self) -> None:
        _discard_pending(self.connection)
        self._json(404, {"status": "not_found"})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _not_found

    def _json(self, status: int, body: Mapping[str, Any]) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode()
        self._send(status, payload, "application/json")

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class OpsServer:
    def __init__(
        self,
        bind: tuple[str, int],
        *,
        live: Callable[[], bool],
        ready: Callable[[], ReadinessReport],
        metrics: Callable[[], bytes],
        on_internal_error: Callable[[], None] | None = None,
        on_ready_failure: Callable[[], None] | None = None,
        max_concurrency: int = 4,
        head_deadline_seconds: float = REQUEST_HEAD_DEADLINE_SECONDS,
    ) -> None:
        self.bind = bind
        self.live = live
        self.ready = ready
        self.metrics = metrics
        self.max_concurrency = max_concurrency
        self.head_deadline_seconds = head_deadline_seconds
        self._on_internal_error = on_internal_error
        self._on_ready_failure = on_ready_failure
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def internal_error(self) -> None:
        if self._on_internal_error is not None:
            with suppress(Exception):  # counting must never fail a probe
                self._on_internal_error()

    def ready_failed(self) -> None:
        """A probe that raised: count it and mark every readiness gauge failed."""
        self.internal_error()
        if self._on_ready_failure is not None:
            with suppress(Exception):
                self._on_ready_failure()

    def refresh_readiness(self) -> None:
        """Run the cached readiness probe so a /metrics-only scrape sees current checks."""
        try:
            self.ready()
        except Exception:  # noqa: BLE001 - /metrics is still served
            self.ready_failed()

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("ops listener is not running")
        return int(self._server.server_address[1])

    def start(self) -> None:
        """Bind and serve in a daemon thread; a bind failure raises OSError."""
        try:
            self._server = _Server(self.bind, self)
        except OSError as exc:
            log_event(_LOG, logging.ERROR, "ops_listener_bind_failed", reason=type(exc).__name__)
            raise
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="nexa-ops-listener",
            daemon=True,
        )
        self._thread.start()
        log_event(_LOG, logging.INFO, "ops_listener_started", port=self.port)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
