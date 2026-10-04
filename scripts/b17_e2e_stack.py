"""B17/B18 real-stack harness for the Web UI Playwright suites (W1, W2).

W1: PostgreSQL (guarded NEXA_TEST_DATABASE_URL) + API process + Caddy TLS serving web/dist.
W2: W1 + coordinator process + Docker worker container + CPU image built from this source.

    PYTHONPATH=src:. uv run --no-sync python scripts/b17_e2e_stack.py run --tier w1 -- \
        pnpm --dir web exec playwright test --project=w1-desktop --project=w1-mobile
    ... run --tier w1 -- pnpm --dir web exec playwright test --project=w1-admission
                                  # own stack: ADMISSION_OFF cannot go back to NORMAL (B17-R18)
    ... run --tier w2 -- pnpm --dir web exec playwright test --project=w2
    ... run --tier w1 -- pnpm --dir web exec playwright test --project=w1-admin
    ... run --tier w1 -- pnpm --dir web exec playwright test --project=w1-admin-mode
                                  # own stack: no worker, so NORMAL cannot be reopened (B19-R02)
    ... run --tier w1 --operational-mode WRITE_FROZEN -- \
        pnpm --dir web exec playwright test --project=w1-admin-frozen
                                  # WRITE_FROZEN is unreachable over the API (B18-R18): the
                                  # harness writes it into the test DB after seeding (D6)
    ... run --tier w2 -- pnpm --dir web exec playwright test --project=w2-admin \
        --project=w2-admin-mobile  # w2-admin starts with the B19 NORMAL reopen scenario
    ... run --tier w2 --prometheus [--alert-for 20s] -- python scripts/b19_scrape.py
                                  # B19: ops listeners on loopback + a pinned Prometheus
                                  # scraping the API, coordinator and worker; --alert-for
                                  # shortens only the `for:` of a generated rules copy
    ... run --tier w2 --prometheus --api-env NEXA_METRICS_CACHE_SECONDS=1 -- \
        python scripts/b19_outage.py  # B19 outage run; the harness then runs
                                  # nexa-maintenance consistency-check on the IDs it saved
    ... up --tier w1              # keep the stack until Ctrl-C (manual checks)
    ... cleanup                   # remove leftovers of an interrupted run

The database schema is reset and migrated at the start of every run. Test passwords and
server secrets are random per run and live only in a 0700 state directory outside the repo
(fixture.json, 0600), which Playwright reads through NEXA_B17_FIXTURE; the state directory,
processes and nexa_b17_* containers are removed on exit, also when the command fails.
Nothing secret is printed: output holds ports, IDs, digests and exit codes only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from nexa.application.template_registry import load_definition, register_template
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.worker.credentials import CredentialStore

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "web" / "dist"
CADDYFILE = ROOT / "deploy" / "web" / "Caddyfile"
TEMPLATES = ROOT / "deploy" / "templates"
CADDY_IMAGE = "caddy:2.10.2-alpine"
PROMETHEUS_DIR = ROOT / "deploy" / "prometheus"
# Same image and digest as the promtool evidence (docs/environment-inventory.md).
PROMETHEUS_IMAGE = (
    "prom/prometheus@sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996"
)
# --api-env may only tune observability and storage knobs, never secrets or identities.
API_ENV_PREFIXES = ("NEXA_METRICS_", "NEXA_GC_", "NEXA_STORAGE_", "NEXA_LOG_")
STATE_PREFIX = "nexa-b17-run-"
CONTAINER_PREFIX = "nexa_b17_"
CPU_INPUT_MEDIA = "application/vnd.nexa.cpu-iterative-input+json"
ARROW_MEDIA = "application/vnd.apache.arrow.file"
GIB = 1024**3

# Seeded identities: key -> (display name, SYSTEM_ADMIN?, {tenant key: role}).
USERS = {
    "member_a": ("Member A", False, {"a": "MEMBER"}),
    "member_a2": ("Member A2", False, {"a": "MEMBER"}),
    "admin_a": ("Tenant admin A", False, {"a": "TENANT_ADMIN"}),
    "member_b": ("Member B", False, {"b": "MEMBER"}),
    "multi": ("Multi tenant", False, {"a": "MEMBER", "b": "MEMBER"}),
    "quota": ("Low quota", False, {"q": "MEMBER"}),
    # Dedicated to login/logout/revocation so those specs do not spend the login rate
    # limit (10 per minute per source and username) of the users with a stored session.
    "login": ("Login flows", False, {"a": "MEMBER"}),
    "mobile": ("Mobile flows", False, {"a": "MEMBER"}),
    # B18: the second system administrator (two-admin 412 scenarios); no tenant membership.
    "admin2": ("Second admin", True, {}),
}
TENANTS = {"a": "b17-tenant-a", "b": "b17-tenant-b", "q": "b17-tenant-quota"}


def log(message: str) -> None:
    print(f"[b17-stack] {message}", flush=True)


def guarded_database_url() -> str:
    """The destructive target, with the rules of tests/conftest.py (never NEXA_DATABASE_URL)."""
    url = os.environ.get("NEXA_TEST_DATABASE_URL", "")
    if not url:
        raise SystemExit("NEXA_TEST_DATABASE_URL is required")
    parsed = urlsplit(url)
    if parsed.scheme != "postgresql+psycopg":
        raise SystemExit("NEXA_TEST_DATABASE_URL must use postgresql+psycopg")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("NEXA_TEST_DATABASE_URL must target a loopback host")
    if not parsed.path.removeprefix("/").startswith("nexa_b05_test_"):
        raise SystemExit("PostgreSQL test database name must start with nexa_b05_test_")
    if url == os.environ.get("NEXA_DATABASE_URL"):
        raise SystemExit("Refusing to use NEXA_DATABASE_URL as the destructive test target")
    return url


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)


def secret_text(length: int = 48) -> str:
    """Visible-ASCII random secret (header-safe, valid bootstrap secret)."""
    return secrets.token_urlsafe(length)[:length]


def docker(*args: str, timeout: float = 60, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed ({result.returncode}): {result.stderr[-400:]}")
    return result.stdout.strip()


def wait_until(predicate, detail: str, timeout: float) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {detail}")


def expect(response: httpx.Response, status: int, what: str) -> httpx.Response:
    if response.status_code != status:
        # Error envelopes carry code/message/request_id only; never request bodies.
        raise RuntimeError(f"{what}: HTTP {response.status_code} {response.text[:300]}")
    return response


def reset_database(url: str) -> None:
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            major = int(connection.execute(text("SHOW server_version_num")).scalar_one())
            if major // 10_000 != 17:
                raise SystemExit("B17 harness requires PostgreSQL 17")
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")


class Stack:
    def __init__(
        self,
        tier: str,
        network: str,
        operational_mode: str = "NORMAL",
        *,
        ops: bool = False,
        prometheus: bool = False,
        alert_for: str | None = None,
        api_env: dict[str, str] | None = None,
    ) -> None:
        self.tier = tier
        self.network = network
        self.operational_mode = operational_mode
        self.prometheus = prometheus
        self.alert_for = alert_for
        self.api_env = dict(api_env or {})
        # B19: one loopback ops port per process (API, coordinator, worker).
        self.ops_ports = (
            {"api": free_port(), "coordinator": free_port(), "worker": free_port()}
            if ops or prometheus
            else {}
        )
        self.prometheus_port = free_port() if prometheus else None
        self.api_environment: dict[str, str] = {}
        self.run_id = secrets.token_hex(4)
        self.state = Path(tempfile.mkdtemp(prefix=STATE_PREFIX))
        self.state.chmod(0o700)
        self.database_url = guarded_database_url()
        self.api_port = free_port()
        self.caddy_port = free_port()
        self.origin = f"https://localhost:{self.caddy_port}"
        self.installation_id = str(new_uuid7())
        self.worker_id = str(new_uuid7())
        self.fingerprint = "sha256:" + secrets.token_hex(32)
        self.bootstrap_secret = secret_text()
        self.processes: list[subprocess.Popen] = []
        self.containers: list[str] = []
        self.session_secrets: list[str] = []
        self.worker_credential: str | None = None
        self.worker_container: str | None = None
        self.fixture: dict = {}
        (self.state / "installation-id").write_text(self.installation_id)

    # -- lifecycle ---------------------------------------------------------------
    def close(self) -> None:
        for name in reversed(self.containers):
            docker("rm", "--force", name, check=False, timeout=30)
        if self.tier == "w2":
            remove_workload_containers(self.installation_id)
        for process in reversed(self.processes):
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        shutil.rmtree(self.state, ignore_errors=True)
        log(f"cleaned up run {self.run_id}")

    def start(self) -> None:
        if not (DIST / "index.html").is_file():
            raise SystemExit("web/dist is missing: run `pnpm --dir web run build` first")
        index = hashlib.sha256((DIST / "index.html").read_bytes()).hexdigest()
        log(f"run {self.run_id} tier={self.tier} network={self.network} dist index={index[:12]}")
        reset_database(self.database_url)
        log("database reset and migrated to head")
        self._write_secrets()
        self._start_api()
        self._start_caddy()
        self._seed()
        if self.operational_mode == "WRITE_FROZEN":
            self._freeze_writes()
        if self.tier == "w2":
            self._start_coordinator()
            self._start_worker()
        if self.prometheus:
            self._start_prometheus()
        self._write_fixture()

    # -- processes ---------------------------------------------------------------
    def _write_secrets(self) -> None:
        write_private(self.state / "server-secret", secret_text(64).encode())
        write_private(self.state / "bootstrap-secret", self.bootstrap_secret.encode())
        (self.state / "artifacts").mkdir(mode=0o700)

    def _clean_environment(self) -> dict[str, str]:
        # The API refuses unknown NEXA_* keys; CLI and test variables must never reach it.
        return {key: value for key, value in os.environ.items() if not key.startswith("NEXA_")}

    def _start_api(self) -> None:
        environment = self._clean_environment()
        environment.update(
            {
                "PYTHONPATH": str(ROOT / "src"),
                "NEXA_ENVIRONMENT": "test",
                "NEXA_DATABASE_URL": self.database_url,
                "NEXA_ARTIFACT_ROOT": str(self.state / "artifacts"),
                "NEXA_PUBLIC_ORIGIN": self.origin,
                "NEXA_SERVER_SECRET_FILE": str(self.state / "server-secret"),
                "NEXA_BOOTSTRAP_SECRET_FILE": str(self.state / "bootstrap-secret"),
                "NEXA_INSTALLATION_ID": self.installation_id,
                "NEXA_LOCAL_WORKER_ID": self.worker_id,
                "NEXA_LOCAL_WORKER_FINGERPRINT": self.fingerprint,
                "NEXA_MAINTENANCE_CIDRS": "127.0.0.0/8",
                **self.api_env,
            }
        )
        # Docker Desktop forwards host-gateway traffic to the host's loopback (measured: the
        # API sees 127.0.0.1), so the API stays on loopback. Only a Linux engine in bridge mode
        # reaches the host on its bridge address and needs every interface for the run.
        host = self._host_bind()
        if self.ops_ports:
            environment["NEXA_OPS_BIND"] = f"{host}:{self.ops_ports['api']}"
        # nexa-maintenance checks after the command load the same settings (never logged).
        self.api_environment = environment
        api_log = open(self.state / "api.log", "wb")  # noqa: SIM115 - closed with the state dir
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "nexa.api.main:app",
                "--host",
                host,
                "--port",
                str(self.api_port),
                "--log-level",
                "warning",
                "--no-access-log",
            ],
            cwd=ROOT,
            env=environment,
            stdout=api_log,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(process)

        def ready() -> bool:
            if process.poll() is not None:
                raise RuntimeError(f"API exited with {process.returncode} before readiness")
            try:
                url = f"http://127.0.0.1:{self.api_port}/openapi.json"
                return httpx.get(url, timeout=0.5).status_code == 200
            except httpx.RequestError:
                return False

        wait_until(ready, "API readiness", 30)
        log(f"API ready on port {self.api_port} (bind {host})")
        if self.ops_ports:
            self._wait_ops("api", process)

    def _host_bind(self) -> str:
        linux_bridge = self.network == "bridge" and platform.system() == "Linux"
        return "0.0.0.0" if linux_bridge else "127.0.0.1"

    def _wait_ops(self, name: str, process: subprocess.Popen | None = None) -> None:
        url = f"http://127.0.0.1:{self.ops_ports[name]}/livez"

        def live() -> bool:
            if process is not None and process.poll() is not None:
                raise RuntimeError(f"{name} exited with {process.returncode}")
            try:
                return httpx.get(url, timeout=0.5).status_code == 200
            except httpx.RequestError:
                return False

        wait_until(live, f"{name} ops listener", 60)
        log(f"{name} ops listener on port {self.ops_ports[name]}")

    def _start_caddy(self) -> None:
        name = f"{CONTAINER_PREFIX}caddy_{self.run_id}"
        if self.network == "bridge":
            network = [
                "--network",
                "bridge",
                "--add-host",
                "host.docker.internal:host-gateway",
                "--publish",
                f"127.0.0.1:{self.caddy_port}:{self.caddy_port}",
            ]
            bind, upstream = "0.0.0.0", f"host.docker.internal:{self.api_port}"
        else:
            network = ["--network", "host"]
            bind, upstream = "127.0.0.1", f"127.0.0.1:{self.api_port}"
        self.containers.append(name)
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--init",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--read-only",
            "--tmpfs",
            "/data:rw,size=16m",
            "--tmpfs",
            "/config:rw,size=4m",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_BIND_SERVICE",
            "--security-opt",
            "no-new-privileges",
            *network,
            "--env",
            f"NEXA_B17_PORT={self.caddy_port}",
            "--env",
            f"NEXA_B17_BIND={bind}",
            "--env",
            f"NEXA_B17_API_UPSTREAM={upstream}",
            "--mount",
            f"type=bind,src={CADDYFILE},dst=/etc/caddy/Caddyfile,readonly",
            "--mount",
            f"type=bind,src={DIST},dst=/srv,readonly",
            CADDY_IMAGE,
        )
        root = self.state / "caddy-root.crt"

        def certificate() -> bool:
            pem = docker(
                "exec",
                name,
                "cat",
                "/data/caddy/pki/authorities/local/root.crt",
                check=False,
                timeout=10,
            )
            if "BEGIN CERTIFICATE" not in pem:
                return False
            root.write_text(pem + "\n")
            return True

        wait_until(certificate, "Caddy local CA", 30)
        self.tls = ssl.create_default_context(cafile=str(root))

        def ready() -> bool:
            try:
                with httpx.Client(verify=self.tls, timeout=2) as client:
                    return client.get(self.origin + "/").status_code == 200
            except httpx.RequestError:
                return False

        wait_until(ready, "Caddy TLS readiness", 30)
        image = docker("image", "inspect", "--format", "{{.Id}}", CADDY_IMAGE)
        log(f"Caddy {CADDY_IMAGE} ({image[:19]}) serving {self.origin}")

    def _start_coordinator(self) -> None:
        environment = self._clean_environment()
        environment.update(
            {"PYTHONPATH": str(ROOT / "src"), "NEXA_DATABASE_URL": self.database_url}
        )
        if self.ops_ports:
            environment["NEXA_OPS_BIND"] = f"{self._host_bind()}:{self.ops_ports['coordinator']}"
        coordinator_log = open(self.state / "coordinator.log", "wb")  # noqa: SIM115
        process = subprocess.Popen(
            [sys.executable, "-m", "nexa.coordinator.main"],
            cwd=ROOT,
            env=environment,
            stdout=coordinator_log,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(process)
        time.sleep(0.5)
        if process.poll() is not None:
            raise RuntimeError(f"coordinator exited with {process.returncode}")
        log("coordinator started")
        if self.ops_ports:
            self._wait_ops("coordinator", process)

    def _start_worker(self) -> None:
        cpu_image = os.environ.get("NEXA_B17_CPU_IMAGE_REF", "")
        worker_image = os.environ.get("NEXA_B17_WORKER_IMAGE", "")
        if "@sha256:" not in cpu_image or not worker_image:
            raise SystemExit(
                "W2 needs NEXA_B17_CPU_IMAGE_REF (digest pinned) and NEXA_B17_WORKER_IMAGE"
            )
        shared = (self.state / "worker-shared").resolve()
        shared.mkdir(mode=0o700)
        bootstrap = expect(
            httpx.post(
                f"http://127.0.0.1:{self.api_port}/v1/internal/worker-bootstrap",
                headers={
                    "X-Nexa-Bootstrap-Secret": self.bootstrap_secret,
                    "Idempotency-Key": f"b17-worker-bootstrap-{self.run_id}",
                },
                json={
                    "installation_id": self.installation_id,
                    "credential_public_fingerprint": self.fingerprint,
                },
                timeout=10,
            ),
            201,
            "worker bootstrap",
        ).json()
        self.worker_credential = bootstrap["credential"]
        CredentialStore(shared / "credential.json").save(
            worker_id=self.worker_id,
            installation_id=self.installation_id,
            credential=bootstrap["credential"],
        )
        write_private(shared / "bootstrap-secret", self.bootstrap_secret.encode())
        if self.network == "bridge":
            network = ["--network", "bridge", "--add-host", "host.docker.internal:host-gateway"]
            api_url = f"http://host.docker.internal:{self.api_port}"
        else:
            network = ["--network", "host"]
            api_url = f"http://127.0.0.1:{self.api_port}"
        ops: list[str] = []
        if self.ops_ports:
            port = self.ops_ports["worker"]
            if self.network == "bridge":
                # Published on the host's loopback only (B19-R03: never public).
                ops = ["--env", f"NEXA_OPS_BIND=0.0.0.0:{port}"]
                ops += ["--publish", f"127.0.0.1:{port}:{port}"]
            else:
                ops = ["--env", f"NEXA_OPS_BIND=127.0.0.1:{port}"]
        name = f"{CONTAINER_PREFIX}worker_{self.run_id}"
        self.worker_container = name
        self.containers.append(name)
        docker(
            "run",
            "--detach",
            "--name",
            name,
            *network,
            "--mount",
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--mount",
            f"type=bind,src={shared},dst={shared}",
            "--env",
            f"NEXA_WORKER_API_URL={api_url}",
            "--env",
            f"NEXA_WORKER_STATE_ROOT={shared}",
            "--env",
            f"NEXA_INSTALLATION_ID={self.installation_id}",
            "--env",
            f"NEXA_LOCAL_WORKER_ID={self.worker_id}",
            "--env",
            f"NEXA_LOCAL_WORKER_FINGERPRINT={self.fingerprint}",
            "--env",
            f"NEXA_BOOTSTRAP_SECRET_FILE={shared / 'bootstrap-secret'}",
            "--env",
            f"NEXA_CPU_IMAGE_REF={cpu_image}",
            *ops,
            worker_image,
            "nexa-worker",
        )

        def ready() -> bool:
            state = docker("inspect", "--format", "{{.State.Status}}", name, check=False)
            if state not in {"running", "created"}:
                raise RuntimeError(f"worker container is {state}")
            page = self.admin.get("/v1/admin/workers", params={"page_size": 10})
            expect(page, 200, "list workers")
            return any(
                item["worker_id"] == self.worker_id and item["health"] == "READY"
                for item in page.json()["items"]
            )

        wait_until(ready, "worker READY", 120)
        worker_digest = docker("image", "inspect", "--format", "{{.Id}}", worker_image)
        cpu_digest = cpu_image.rsplit("@", 1)[1]
        log(f"worker READY: cpu image {cpu_digest[:19]}, worker {worker_digest[:19]}")
        if self.ops_ports:
            self._wait_ops("worker")

    def _start_prometheus(self) -> None:
        """B19: a pinned, read-only Prometheus scraping the three ops listeners.

        The scrape config and rules are generated in the state directory from
        deploy/prometheus: only the targets, the 5 s intervals and (with --alert-for)
        the rules' `for:` durations differ from the shipped samples.
        """
        if self.tier != "w2":
            raise SystemExit("--prometheus needs --tier w2 (coordinator and worker)")
        config_dir = self.state / "prometheus"
        config_dir.mkdir(mode=0o755)
        config_dir.chmod(0o755)  # read by the image's `nobody` user; holds no secret
        if self.network == "bridge":
            worker_ip = docker(
                "inspect",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                self.worker_container,
            )
            targets = {
                "api": f"host.docker.internal:{self.ops_ports['api']}",
                "coordinator": f"host.docker.internal:{self.ops_ports['coordinator']}",
                "worker": f"{worker_ip}:{self.ops_ports['worker']}",
            }
            network = ["--network", "bridge", "--add-host", "host.docker.internal:host-gateway"]
            network += ["--publish", f"127.0.0.1:{self.prometheus_port}:9090"]
            listen = "0.0.0.0:9090"
        else:
            targets = {name: f"127.0.0.1:{port}" for name, port in self.ops_ports.items()}
            network = ["--network", "host"]
            listen = f"127.0.0.1:{self.prometheus_port}"
        config = (PROMETHEUS_DIR / "prometheus.yml").read_text()
        for name, sample in (("api", 9464), ("coordinator", 9465), ("worker", 9466)):
            config = config.replace(f'"127.0.0.1:{sample}"', f'"{targets[name]}"')
        config = re.sub(r"(?m)^(  (?:scrape|evaluation)_interval:) 15s$", r"\1 5s", config)
        rules = (PROMETHEUS_DIR / "alerts.yml").read_text()
        if self.alert_for:
            rules = re.sub(r"(?m)^(\s+for:) \S+$", rf"\1 {self.alert_for}", rules)
            header = f"# GENERATED by b17_e2e_stack.py: every `for:` set to {self.alert_for}\n"
            rules = header + rules
        for file_name, content in (("prometheus.yml", config), ("alerts.yml", rules)):
            (config_dir / file_name).write_text(content)
            (config_dir / file_name).chmod(0o644)
        name = f"{CONTAINER_PREFIX}prometheus_{self.run_id}"
        self.containers.append(name)
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--pids-limit",
            "128",
            "--memory",
            "512m",
            "--read-only",
            "--tmpfs",
            "/prometheus:rw,size=256m,uid=65534,gid=65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            *network,
            "--mount",
            f"type=bind,src={config_dir},dst=/etc/nexa-prometheus,readonly",
            PROMETHEUS_IMAGE,
            "--config.file=/etc/nexa-prometheus/prometheus.yml",
            "--storage.tsdb.path=/prometheus",
            "--storage.tsdb.retention.time=1h",
            f"--web.listen-address={listen}",
        )
        self.prometheus_container = name
        url = f"http://127.0.0.1:{self.prometheus_port}"

        def ready() -> bool:
            try:
                return httpx.get(url + "/-/ready", timeout=1).status_code == 200
            except httpx.RequestError:
                return False

        wait_until(ready, "Prometheus readiness", 60)
        log(f"Prometheus {PROMETHEUS_IMAGE.rsplit('@', 1)[1][:19]} on port {self.prometheus_port}")

    # -- seed over REST ------------------------------------------------------------
    def _session(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.origin, verify=self.tls, timeout=20, headers={"Origin": self.origin}
        )

    def _login(self, username: str, password: str) -> tuple[httpx.Client, dict[str, str]]:
        client = self._session()
        login = expect(
            client.post("/v1/auth/login", json={"username": username, "password": password}),
            200,
            f"login {username}",
        ).json()
        self.session_secrets.append(login["csrf_token"])
        self.session_secrets.extend(cookie.value for cookie in client.cookies.jar if cookie.value)
        return client, {"X-CSRF-Token": login["csrf_token"]}

    def _seed(self) -> None:
        users: dict[str, dict] = {}
        admin_password = secret_text(32)
        admin_name = "b17-admin@example.test"
        # admin-bootstrap is accepted only from NEXA_MAINTENANCE_CIDRS: straight to the API.
        bootstrap = expect(
            httpx.post(
                f"http://127.0.0.1:{self.api_port}/v1/internal/admin-bootstrap",
                headers={
                    "X-Nexa-Bootstrap-Secret": self.bootstrap_secret,
                    "Idempotency-Key": f"b17-admin-bootstrap-{self.run_id}",
                },
                json={
                    "username": admin_name,
                    "display_name": "B17 admin",
                    "password": admin_password,
                },
                timeout=10,
            ),
            201,
            "admin bootstrap",
        ).json()
        users["admin"] = {
            "username": admin_name,
            "password": admin_password,
            "user_id": bootstrap["user_id"],
            "system_admin": True,
            "memberships": {},
        }
        self.admin, write = self._login(admin_name, admin_password)
        admin = self.admin

        def key(label: str) -> dict[str, str]:
            return {**write, "Idempotency-Key": f"b17-seed-{self.run_id}-{label}"}

        tenants = {}
        membership_etags = {}
        for tenant_key, slug in TENANTS.items():
            created = expect(
                admin.post(
                    "/v1/admin/tenants",
                    headers=key(f"tenant-{tenant_key}"),
                    json={"slug": slug, "display_name": slug},
                ),
                201,
                f"create tenant {tenant_key}",
            ).json()
            tenants[tenant_key] = created["tenant_id"]
            policy = expect(
                admin.get(f"/v1/admin/tenants/{created['tenant_id']}/policy"), 200, "read policy"
            )
            changes = {
                "resource_limit": {"cpu_millis": 16_000, "memory_bytes": 32 * GIB, "gpu_count": 0},
                "outstanding_limit": 1 if tenant_key == "q" else 1_000,
                "user_outstanding_limit": 1 if tenant_key == "q" else 1_000,
                # Pagination specs seed more than two pages of jobs through the API.
                "submit_rate_per_second": "100",
                "submit_burst": 200,
                "user_submit_rate_per_second": "100",
                "user_submit_burst": 200,
            }
            expect(
                admin.patch(
                    f"/v1/admin/tenants/{created['tenant_id']}/policy",
                    headers={**key(f"policy-{tenant_key}"), "If-Match": policy.headers["ETag"]},
                    json=changes,
                ),
                200,
                f"tenant policy {tenant_key}",
            )
            # If-Match on memberships names the tenant's MembershipSet version.
            listed = admin.get(f"/v1/admin/tenants/{created['tenant_id']}/memberships")
            membership_etags[tenant_key] = expect(listed, 200, "list memberships").headers["ETag"]
        for user_key, (display_name, system_admin, memberships) in USERS.items():
            username = f"b17-{user_key.replace('_', '-')}@example.test"
            password = secret_text(32)
            created = expect(
                admin.post(
                    "/v1/admin/users",
                    headers=key(f"user-{user_key}"),
                    json={
                        "username": username,
                        "display_name": display_name,
                        "password": password,
                        "system_roles": ["SYSTEM_ADMIN"] if system_admin else [],
                    },
                ),
                201,
                f"create user {user_key}",
            )
            user_id = created.json()["user_id"]
            for tenant_key, role in memberships.items():
                upsert = expect(
                    admin.post(
                        f"/v1/admin/tenants/{tenants[tenant_key]}/memberships",
                        headers={
                            **key(f"member-{user_key}-{tenant_key}"),
                            "If-Match": membership_etags[tenant_key],
                        },
                        json={"user_id": user_id, "role": role},
                    ),
                    200,
                    f"membership {user_key}/{tenant_key}",
                )
                membership_etags[tenant_key] = upsert.headers["ETag"]
            users[user_key] = {
                "username": username,
                "password": password,
                "user_id": user_id,
                "system_admin": system_admin,
                "memberships": {tenants[t]: role for t, role in memberships.items()},
            }
        templates = self._register_templates()
        artifacts = self._seed_artifacts(users, tenants)
        self.fixture = {
            "tier": self.tier,
            "run_id": self.run_id,
            "base_url": self.origin,
            "storage_dir": str(self.state / "storage"),
            "tenants": tenants,
            "users": users,
            "templates": templates,
            "artifacts": artifacts,
            "worker_id": self.worker_id,
            "operational_mode": self.operational_mode,
        }
        log(f"seeded {len(tenants)} tenants, {len(users)} users, {len(templates)} templates")

    def _freeze_writes(self) -> None:
        """Test-only (B18 D6): the API refuses ADMISSION_OFF -> WRITE_FROZEN while the recovery
        proof provider is fail-closed (B18-R18), so the UI's frozen state is reached by adding
        the next policy version straight to this guarded test database once seeding is done.

        This is not what the API's transition does (B18-RV09): it skips the transition rules
        and proofs, writes no audit row or policy event, and does not charge the ledgers
        before the freeze (`account_locked`). It only gives the UI a current WRITE_FROZEN
        policy version to read; it runs on fresh test data with no allocation to charge."""
        engine = create_engine(self.database_url)
        try:
            with engine.begin() as connection:
                current = connection.execute(
                    text(
                        "SELECT policy_version, global_outstanding_limit FROM policy_versions"
                        " WHERE is_current FOR UPDATE"
                    )
                ).one()
                connection.execute(
                    text("UPDATE policy_versions SET is_current = false WHERE is_current")
                )
                connection.execute(
                    text(
                        "INSERT INTO policy_versions (policy_version, global_outstanding_limit,"
                        " operational_mode, created_by_user_id, created_at, is_current)"
                        " VALUES (:version, :limit, 'WRITE_FROZEN', :user, now(), true)"
                    ),
                    {
                        "version": current.policy_version + 1,
                        "limit": current.global_outstanding_limit,
                        "user": self.fixture["users"]["admin"]["user_id"],
                    },
                )
        finally:
            engine.dispose()
        log(f"test DB set to WRITE_FROZEN at policy version {current.policy_version + 1}")

    def _register_templates(self) -> dict[str, str]:
        cpu_image = os.environ.get("NEXA_B17_CPU_IMAGE_REF", "")
        engine = create_engine(self.database_url)
        registered = {}
        try:
            factory = create_session_factory(engine)
            for name in ("cpu-iterative", "pytorch-cifar10-cnn", "batch-inference"):
                definition = load_definition((TEMPLATES / f"{name}.v1.json").read_bytes())
                if self.tier == "w2" and name == "cpu-iterative":
                    digest = cpu_image.rsplit("@", 1)[1]
                else:
                    # W1 never runs a workload; W2 runs only cpu-iterative.
                    digest = "sha256:" + hashlib.sha256(f"b17-{name}".encode()).hexdigest()
                register_template(factory, definition, digest)
                registered[name] = digest
        finally:
            engine.dispose()
        return registered

    def _seed_artifacts(self, users: dict, tenants: dict) -> dict[str, dict]:
        cpu_input = json.dumps({"initial_value": 5}, separators=(",", ":")).encode()
        # W1 AI jobs only wait for a worker: the bytes are placeholders, never executed.
        dataset = b"B17 placeholder dataset bytes (not an Arrow file)\n"
        model = b"B17 placeholder model bytes\n"
        plan = {
            "a_input": ("member_a", "a", "INPUT", CPU_INPUT_MEDIA, cpu_input),
            "a_dataset": ("member_a", "a", "DATASET", ARROW_MEDIA, dataset),
            "a_model": ("member_a", "a", "MODEL", "application/octet-stream", model),
            "b_input": ("member_b", "b", "INPUT", CPU_INPUT_MEDIA, cpu_input + b" "),
            "q_input": ("quota", "q", "INPUT", CPU_INPUT_MEDIA, cpu_input + b"  "),
        }
        sessions: dict[str, tuple[httpx.Client, dict[str, str]]] = {}
        artifacts = {}
        try:
            for label, (user_key, tenant_key, kind, media_type, content) in plan.items():
                if user_key not in sessions:
                    sessions[user_key] = self._login(
                        users[user_key]["username"], users[user_key]["password"]
                    )
                client, write = sessions[user_key]
                checksum = "sha256:" + hashlib.sha256(content).hexdigest()
                created = expect(
                    client.post(
                        "/v1/artifacts",
                        content=content,
                        headers={
                            **write,
                            "X-Nexa-Tenant-Id": tenants[tenant_key],
                            "Idempotency-Key": f"b17-seed-{self.run_id}-artifact-{label}",
                            "X-Artifact-Checksum": checksum,
                            "X-Artifact-Size": str(len(content)),
                            "X-Artifact-Kind": kind,
                            "X-Artifact-Media-Type": media_type,
                            "Content-Type": "application/octet-stream",
                        },
                    ),
                    201,
                    f"upload {label}",
                ).json()
                artifacts[label] = {
                    "artifact_id": created["artifact_id"],
                    "tenant_id": tenants[tenant_key],
                    "kind": kind,
                    "media_type": media_type,
                    "checksum": checksum,
                    "size_bytes": len(content),
                }
        finally:
            for client, _ in sessions.values():
                client.close()
        return artifacts

    def _write_fixture(self) -> None:
        (self.state / "storage").mkdir(mode=0o700)
        if self.ops_ports:
            # B19 scripts: loopback-only URLs and names; no credential is added here.
            self.fixture["ops"] = {
                name: f"http://127.0.0.1:{port}" for name, port in self.ops_ports.items()
            }
            self.fixture["ca_file"] = str(self.state / "caddy-root.crt")
            self.fixture["accepted_ids_file"] = str(self.accepted_ids_path)
            self.fixture["worker_container"] = self.worker_container
            self.fixture["database_name"] = urlsplit(self.database_url).path.removeprefix("/")
        if self.prometheus:
            self.fixture["prometheus"] = {
                "url": f"http://127.0.0.1:{self.prometheus_port}",
                "container": self.prometheus_container,
                "alert_for": self.alert_for,
            }
        write_private(self.state / "fixture.json", json.dumps(self.fixture, indent=2).encode())

    @property
    def fixture_path(self) -> Path:
        return self.state / "fixture.json"

    @property
    def accepted_ids_path(self) -> Path:
        return self.state / "accepted-ids.txt"

    def maintenance_checks(self) -> int:
        """B19: when the command saved accepted job IDs, run the read-only checks with the
        API's settings and log their JSON summaries (counts and IDs only)."""
        if not self.accepted_ids_path.is_file():
            return 0
        worst = 0
        for args in (
            ["consistency-check", "--accepted-ids", str(self.accepted_ids_path)],
            ["storage-check"],
        ):
            result = subprocess.run(
                [sys.executable, "-c", "from nexa.cli.main import app; app()", *args],
                cwd=ROOT,
                env=self.api_environment,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            log(f"nexa-maintenance {args[0]} exit={result.returncode}")
            for line in (result.stdout + result.stderr).splitlines():
                log(f"  {line}")
            worst = max(worst, result.returncode)
        return worst

    def canary_scan(self) -> int:
        """B19 (§7.B): no secret or payload of this run may appear in the API, coordinator
        or worker logs. The values are compared in memory and never printed: a hit names
        only its kind and the log it was found in."""
        logs = {
            name: (self.state / f"{name}.log").read_bytes()
            for name in ("api", "coordinator")
            if (self.state / f"{name}.log").is_file()
        }
        if self.worker_container:
            worker = subprocess.run(
                ["docker", "logs", self.worker_container],
                capture_output=True,
                timeout=60,
                check=False,
            )
            logs["worker"] = worker.stdout + worker.stderr
        canaries: dict[str, list[bytes]] = {
            "server secret": [(self.state / "server-secret").read_bytes()],
            "bootstrap secret": [self.bootstrap_secret.encode()],
            "user password": [
                user["password"].encode() for user in self.fixture.get("users", {}).values()
            ],
            "session cookie or CSRF token": [value.encode() for value in self.session_secrets],
            "database password": [
                (urlsplit(self.database_url).password or "").encode(),
            ],
            "worker credential": [(self.worker_credential or "").encode()],
            "input bytes": [json.dumps({"initial_value": 5}, separators=(",", ":")).encode()],
            "committed blob bytes": [
                blob.read_bytes()[:64]
                for blob in (self.state / "artifacts" / "committed").glob("*")
                if blob.is_file() and not blob.is_symlink() and blob.stat().st_size >= 16
            ],
        }
        hits = [
            f"{kind} in {name}"
            for kind, values in canaries.items()
            for value in values
            if len(value) >= 8
            for name, content in logs.items()
            if value in content
        ]
        counted = sum(len([v for v in values if len(v) >= 8]) for values in canaries.values())
        sizes = ", ".join(f"{name} {len(content)} B" for name, content in logs.items())
        log(f"canary scan: {counted} values x {len(logs)} logs ({sizes}): {len(hits)} hit(s)")
        for hit in sorted(set(hits)):
            log(f"  canary hit: {hit}")
        return 1 if hits else 0


def remove_workload_containers(installation_id: str) -> None:
    leaked = docker(
        "ps",
        "--all",
        "--quiet",
        "--filter",
        f"label=nexa.installation_id={installation_id}",
        check=False,
    ).split()
    if leaked:
        docker("rm", "--force", *leaked, check=False, timeout=60)


def cleanup_leftovers() -> int:
    names = docker(
        "ps",
        "--all",
        "--format",
        "{{.Names}}",
        "--filter",
        f"name=^{CONTAINER_PREFIX}",
        check=False,
    ).split()
    names = [name for name in names if name.startswith(CONTAINER_PREFIX)]
    if names:
        docker("rm", "--force", *names, check=False, timeout=60)
    removed_dirs = 0
    for state in Path(tempfile.gettempdir()).glob(STATE_PREFIX + "*"):
        installation = state / "installation-id"
        if installation.is_file():
            remove_workload_containers(installation.read_text().strip())
        shutil.rmtree(state, ignore_errors=True)
        removed_dirs += 1
    log(f"removed {len(names)} container(s) and {removed_dirs} state dir(s)")
    return 0


def child_environment(stack: Stack) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("NEXA_") or key.startswith("NEXA_B17_") and "IMAGE" not in key
    }
    environment["NEXA_B17_FIXTURE"] = str(stack.fixture_path)
    return environment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("run", "up"):
        item = sub.add_parser(action)
        item.add_argument("--tier", choices=("w1", "w2"), required=True)
        item.add_argument(
            "--network",
            choices=("bridge", "host"),
            default="host" if platform.system() == "Linux" else "bridge",
        )
        item.add_argument(
            "--operational-mode", choices=("NORMAL", "WRITE_FROZEN"), default="NORMAL"
        )
        item.add_argument("--ops", action="store_true", help="B19: enable NEXA_OPS_BIND")
        item.add_argument(
            "--prometheus", action="store_true", help="B19: --ops plus a pinned Prometheus"
        )
        item.add_argument("--alert-for", help="B19: shorten every rule's `for:` (e.g. 20s)")
        item.add_argument(
            "--api-env",
            action="append",
            default=[],
            metavar="KEY=VALUE",
            help="B19: extra API setting (" + ", ".join(p + "*" for p in API_ENV_PREFIXES) + ")",
        )
        if action == "run":
            item.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("cleanup")
    args = parser.parse_args(argv)
    if args.action == "cleanup":
        return cleanup_leftovers()
    command_line = [part for part in getattr(args, "command", []) if part != "--"]
    if args.action == "run" and not command_line:
        parser.error("run needs a command after --")
    with ExitStack() as stack_context:
        if args.tier == "w2" and args.operational_mode != "NORMAL":
            parser.error("--operational-mode WRITE_FROZEN is a W1 (no worker) stack only")
        if args.alert_for and not re.fullmatch(r"[0-9]+[smh]", args.alert_for):
            parser.error("--alert-for must look like 20s, 2m or 1h")
        api_env = {}
        for item in args.api_env:
            key, separator, value = item.partition("=")
            if not separator or not key.startswith(API_ENV_PREFIXES):
                parser.error(f"--api-env accepts {', '.join(API_ENV_PREFIXES)} keys only")
            api_env[key] = value
        stack = Stack(
            args.tier,
            args.network,
            args.operational_mode,
            ops=args.ops,
            prometheus=args.prometheus,
            alert_for=args.alert_for,
            api_env=api_env,
        )
        stack_context.callback(stack.close)
        interrupted = []

        def stop(signum, _frame):
            interrupted.append(signum)
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        try:
            stack.start()
            if args.action == "up":
                log(f"stack up at {stack.origin}; fixture {stack.fixture_path}; Ctrl-C to stop")
                while True:
                    time.sleep(3600)
            log("running: " + " ".join(command_line))
            started = time.monotonic()
            result = subprocess.run(
                command_line, cwd=ROOT, env=child_environment(stack), check=False
            )
            log(f"command exit={result.returncode} in {time.monotonic() - started:.1f}s")
            checks = stack.maintenance_checks()
            if stack.ops_ports:
                checks = max(checks, stack.canary_scan())
            return result.returncode or checks
        except KeyboardInterrupt:
            log("interrupted")
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
