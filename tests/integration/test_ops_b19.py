"""B19 API ops listener on PostgreSQL: /readyz checks, collectors, REST isolation, real uvicorn.

The listener binds 127.0.0.1 on a free port. Secret files are random per test and never
printed. The real-uvicorn test launches the compose command (`uvicorn nexa.api.main:app`)
so logging is configured exactly as in production.
"""

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from nexa.api.app import create_app
from nexa.api.collectors import ApiStateCollector, read_only_transaction
from nexa.api.ops import ApiOps
from nexa.application.storage_identity import bind_artifact_store
from nexa.config import load_settings
from nexa.infrastructure.artifacts.store import FilesystemArtifactStore
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.observability import metrics_api
from nexa.observability.metrics import exposition, new_registry
from nexa.observability.probes import DatabaseProbe
from tests.test_config import SAFE_ENV

pytestmark = pytest.mark.postgres


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _env(database_url: str, root: Path, **extra: str) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    server_secret = root / "server-secret"
    bootstrap_secret = root / "bootstrap-secret"
    server_secret.write_text(secrets.token_urlsafe(48))
    bootstrap_secret.write_text(secrets.token_urlsafe(48))
    return {
        **SAFE_ENV,
        "NEXA_DATABASE_URL": database_url,
        "NEXA_ARTIFACT_ROOT": str(root / "artifacts"),
        "NEXA_SERVER_SECRET_FILE": str(server_secret),
        "NEXA_BOOTSTRAP_SECRET_FILE": str(bootstrap_secret),
        **extra,
    }


def _get(port: int, path: str) -> tuple[int, str, dict[str, str]]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
            return response.status, response.read().decode(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(), dict(error.headers)


def _sample(body: str, prefix: str) -> float | None:
    for line in body.splitlines():
        if line.startswith(prefix + " "):
            return float(line.rsplit(" ", 1)[1])
    return None


def test_api_ops_listener_ready_metrics_and_rest_isolation(
    migrated_postgres_engine, clean_postgres_database, tmp_path
):
    port = _free_port()
    settings = load_settings(
        _env(clean_postgres_database, tmp_path, NEXA_OPS_BIND=f"127.0.0.1:{port}")
    )
    app = create_app(settings, engine=migrated_postgres_engine)
    with TestClient(app, base_url="https://nexa.test") as client:
        status, body, headers = _get(port, "/readyz")
        assert status == 200, body
        assert json.loads(body) == {
            "status": "ready",
            "checks": {"database": "ok", "schema": "ok", "storage": "ok"},
        }
        assert _get(port, "/livez")[0] == 200

        client.get("/v1/templates/cpu-iterative?secret=canary-query")
        status, metrics, headers = _get(port, "/metrics")
        assert status == 200
        assert headers["Content-Type"].startswith("text/plain; version=0.0.4")
        for collector in ("queue", "allocation", "fairness", "checkpoint", "worker", "storage"):
            assert _sample(metrics, f'nexa_collector_up{{collector="{collector}"}}') == 1.0
        assert _sample(metrics, 'nexa_jobs{state="QUEUED"}') == 0.0
        assert _sample(metrics, 'nexa_operational_mode{mode="NORMAL"}') == 1.0
        assert _sample(metrics, 'nexa_ready{check="database"}') == 1.0
        assert 'route="/v1/templates/{template_id}"' in metrics
        assert "canary-query" not in metrics
        assert "process_" not in metrics and "python_" not in metrics

        # The REST port never serves ops paths (they are not in /v1 or OpenAPI).
        for path in ("/metrics", "/readyz", "/livez", "/v1/metrics", "/v1/readyz"):
            assert client.get(path).status_code == 404, path
        paths = client.get("/openapi.json").json()["paths"]
        assert not [path for path in paths if path.rsplit("/", 1)[-1] in {"metrics", "readyz"}]
    # Lifespan exit stopped the listener and unregistered the collector.
    with pytest.raises(OSError):
        _get(port, "/livez")
    assert "nexa_collector_up" not in exposition(metrics_api.REGISTRY).decode()


def test_api_ops_bind_conflict_aborts_startup(
    migrated_postgres_engine, clean_postgres_database, tmp_path
):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        settings = load_settings(
            _env(clean_postgres_database, tmp_path, NEXA_OPS_BIND=f"127.0.0.1:{port}")
        )
        app = create_app(settings, engine=migrated_postgres_engine)
        with pytest.raises(OSError), TestClient(app, base_url="https://nexa.test"):
            pass


def test_readiness_fails_closed_for_schema_storage_and_database(
    migrated_postgres_engine, clean_postgres_database, tmp_path
):
    probe = DatabaseProbe(migrated_postgres_engine)
    assert probe.check() == {"database": "ok", "schema": "ok"}
    assert DatabaseProbe(migrated_postgres_engine, head=lambda: "20990101_9999").check() == {
        "database": "ok",
        "schema": "fail",
    }
    with migrated_postgres_engine.begin() as connection:
        connection.execute(text("ALTER TABLE alembic_version RENAME TO alembic_version_b19"))
    try:
        assert probe.check() == {"database": "ok", "schema": "fail"}
    finally:
        with migrated_postgres_engine.begin() as connection:
            connection.execute(text("ALTER TABLE alembic_version_b19 RENAME TO alembic_version"))

    unreachable = create_engine(
        f"postgresql+psycopg://nexa@127.0.0.1:{_free_port()}/nexa_b05_test_unreachable",
        connect_args={"connect_timeout": 2},
    )
    started = time.monotonic()
    assert DatabaseProbe(unreachable).check() == {"database": "fail", "schema": "skip"}
    assert time.monotonic() - started < 5
    unreachable.dispose()

    # Storage at the critical watermark (95 %) and statvfs errors are both not ready.
    settings = load_settings(
        _env(clean_postgres_database, tmp_path, NEXA_OPS_BIND=f"127.0.0.1:{_free_port()}")
    )
    real = os.statvfs(str(tmp_path))
    usage = {"bavail": real.f_blocks // 2, "error": False}

    def statvfs(_path):
        if usage["error"]:
            raise OSError(5, "I/O error")
        return os.statvfs_result(
            (
                real.f_bsize,
                real.f_frsize,
                real.f_blocks,
                usage["bavail"],
                usage["bavail"],
                real.f_files,
                real.f_ffree,
                real.f_favail,
                real.f_flag,
                real.f_namemax,
            )
        )

    store = FilesystemArtifactStore(settings.artifact_root, statvfs_fn=statvfs)
    bind_artifact_store(create_session_factory(migrated_postgres_engine), store)
    ops = ApiOps(settings, store)
    try:
        assert ops._probe().checks == {"database": "ok", "schema": "ok", "storage": "ok"}
        usage["bavail"] = real.f_blocks // 25  # 96 % used
        report = ops._probe()
        assert report.checks["storage"] == "fail" and not report.ready
        usage["error"] = True
        assert ops._probe().checks["storage"] == "fail"
    finally:
        ops.stop()


def test_collector_transactions_are_read_only_and_bounded(migrated_postgres_engine):
    with Session(migrated_postgres_engine) as session, session.begin():
        read_only_transaction(session, 1234)
        assert session.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
        assert session.execute(text("SHOW statement_timeout")).scalar_one() == "1234ms"
        assert session.execute(text("SHOW lock_timeout")).scalar_one() == "1234ms"
        with pytest.raises(Exception, match="read-only transaction"):
            session.execute(text("UPDATE policy_versions SET is_current = is_current"))


def test_collector_failure_drops_its_series_and_marks_down(migrated_postgres_engine):
    registry = new_registry()
    collector = ApiStateCollector(
        migrated_postgres_engine,
        disk_usage=lambda: (100, 50, 50.0),
        cache_seconds=1,
        statement_timeout_ms=200,
    )
    registry.register(collector)
    healthy = exposition(registry).decode()
    assert _sample(healthy, 'nexa_collector_up{collector="queue"}') == 1.0
    assert "nexa_jobs{" in healthy
    errors_before = metrics_api.COLLECTOR_ERRORS.labels("queue")._value.get()

    # Hold an exclusive lock on jobs: the queue collector hits lock_timeout, others continue.
    locker = migrated_postgres_engine.connect()
    transaction = locker.begin()
    try:
        locker.execute(text("LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE"))
        collector._refreshed_at = None
        degraded = exposition(registry).decode()
    finally:
        transaction.rollback()
        locker.close()
    assert _sample(degraded, 'nexa_collector_up{collector="queue"}') == 0.0
    assert "nexa_jobs{" not in degraded and "nexa_jobs_waiting{" not in degraded
    assert _sample(degraded, 'nexa_collector_up{collector="storage"}') == 1.0
    assert metrics_api.COLLECTOR_ERRORS.labels("queue")._value.get() == errors_before + 1

    collector._refreshed_at = None
    recovered = exposition(registry).decode()
    assert _sample(recovered, 'nexa_collector_up{collector="queue"}') == 1.0
    assert "nexa_jobs{" in recovered


class _Uvicorn:
    """The compose API command on free ports; `stdout`/`stderr` hold its output after exit."""

    def __init__(self, database_url: str, root: Path, **extra: str) -> None:
        self.port = _free_port()
        self.ops_port = _free_port()
        self.env = {
            **{key: value for key, value in os.environ.items() if not key.startswith("NEXA_")},
            **_env(database_url, root, NEXA_OPS_BIND=f"127.0.0.1:{self.ops_port}", **extra),
            "PYTHONPATH": "src:.",
        }
        self.stdout = self.stderr = ""

    def __enter__(self) -> "_Uvicorn":
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "nexa.api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    if _get(self.ops_port, "/readyz")[0] == 200:
                        return self
                except OSError:
                    pass
                assert self.process.poll() is None, "uvicorn exited early"
                assert time.monotonic() < deadline, "API did not become ready"
                time.sleep(0.1)
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_exc: object) -> None:
        self.process.send_signal(signal.SIGINT)
        self.stdout, self.stderr = self.process.communicate(timeout=30)

    @property
    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.stderr.splitlines() if line.startswith("{")]


def test_real_uvicorn_logs_json_access_without_query_or_credentials(
    migrated_postgres_engine, clean_postgres_database, tmp_path
):
    canary = "b19-canary-" + secrets.token_hex(8)
    with _Uvicorn(clean_postgres_database, tmp_path) as server:
        port = server.port
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/auth/login?password={canary}",
            data=json.dumps({"username": "nobody", "password": canary}).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://nexa.test",
                "Authorization": f"Bearer {canary}",
                "Cookie": f"nexa_session={canary}",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(request, timeout=10)
        _get(port, f"/v1/jobs?cursor={canary}")
    stdout, stderr = server.stdout, server.stderr
    output = stdout + stderr
    assert canary not in output
    records = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
    assert records, stderr[-2000:]
    # Every line is JSON (uvicorn's own startup lines go through the root JSON handler).
    assert all(line.startswith("{") for line in stderr.splitlines() if line.strip())
    access = [record for record in records if record["event"] == "http_request"]
    assert {record["route"] for record in access} >= {"/v1/auth/login", "/v1/jobs"}
    for record in access:
        assert record["service"] == "api" and record["logger"] == "nexa.api.app"
        assert isinstance(record["duration_ms"], (int, float)) and record["request_id"]
        assert "?" not in record["route"]
    assert not [record for record in records if record["logger"] == "uvicorn.access"]
    assert {record["service"] for record in records} == {"api"}


def _raw_request(port: int, method: str) -> tuple[float, bytes]:
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(
            f"{method} /v1/jobs HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode()
        )
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    return time.monotonic() - started, data


def test_real_uvicorn_long_method_does_not_stall_other_requests(
    migrated_postgres_engine, clean_postgres_database, tmp_path
):
    # B19-RV01: h11 accepts a ~16 KiB token as the method. Logging it must neither echo it
    # nor block the event loop for the requests served alongside it.
    method = "X" * 16000
    with (
        _Uvicorn(clean_postgres_database, tmp_path) as server,
        ThreadPoolExecutor(max_workers=4) as pool,
    ):
        hostile = [pool.submit(_raw_request, server.port, method) for _ in range(3)]
        started = time.monotonic()
        status, _body, _headers = _get(server.port, "/v1/jobs")
        concurrent = time.monotonic() - started
        results = [future.result(timeout=30) for future in hostile]
    assert status < 500
    assert concurrent < 0.5, concurrent
    for elapsed, data in results:
        assert data.startswith(b"HTTP/1.1 "), data[:64]
        assert elapsed < 0.5, elapsed
    assert method[:64] not in server.stdout + server.stderr
    labels = {r["method"] for r in server.records if r["event"] == "http_request"}
    assert labels == {"GET", "OTHER"}


def test_real_uvicorn_honours_nexa_log_level_for_uvicorn_loggers(
    migrated_postgres_engine, clean_postgres_database, tmp_path
):
    # B19-RV07: uvicorn's dictConfig pins "uvicorn"/"uvicorn.error" at INFO before the app
    # imports; configure_logging must apply NEXA_LOG_LEVEL to them as well.
    with _Uvicorn(clean_postgres_database, tmp_path, NEXA_LOG_LEVEL="WARNING") as server:
        _get(server.port, "/v1/jobs")
    assert "Started server process" not in server.stderr
    assert "Application startup complete" not in server.stderr
    levels = {record["level"] for record in server.records}
    assert levels <= {"WARNING", "ERROR", "CRITICAL"}, levels


def _poll_metric(port: int, sample: str, expected: float) -> str:
    """Scrape only /metrics until `sample` shows `expected` (readiness cache is ≤ 2 s)."""
    deadline = time.monotonic() + 10
    while True:
        status, body, _headers = _get(port, "/metrics")
        assert status == 200
        if _sample(body, sample) == expected:
            return body
        assert time.monotonic() < deadline, f"{sample} never became {expected}"
        time.sleep(0.2)


def test_api_metrics_scrape_alone_tracks_readiness(
    migrated_postgres_engine, clean_postgres_database, tmp_path
):
    # B19-RV02: no /readyz call at all; nexa_ready follows a failing check and recovery.
    port = _free_port()
    settings = load_settings(
        _env(clean_postgres_database, tmp_path, NEXA_OPS_BIND=f"127.0.0.1:{port}")
    )
    real = os.statvfs(str(tmp_path))
    usage = {"bavail": real.f_blocks // 2}

    def statvfs(_path):
        return os.statvfs_result(
            (real.f_bsize, real.f_frsize, real.f_blocks, usage["bavail"], usage["bavail"])
            + (real.f_files, real.f_ffree, real.f_favail, real.f_flag, real.f_namemax)
        )

    store = FilesystemArtifactStore(settings.artifact_root, statvfs_fn=statvfs)
    bind_artifact_store(create_session_factory(migrated_postgres_engine), store)
    ops = ApiOps(settings, store)
    ops.start()
    try:
        _poll_metric(port, 'nexa_ready{check="storage"}', 1.0)
        usage["bavail"] = real.f_blocks // 25  # 96 % used: above the critical watermark
        body = _poll_metric(port, 'nexa_ready{check="storage"}', 0.0)
        assert _sample(body, 'nexa_ready{check="database"}') == 1.0
        usage["bavail"] = real.f_blocks // 2
        _poll_metric(port, 'nexa_ready{check="storage"}', 1.0)
    finally:
        ops.stop()
