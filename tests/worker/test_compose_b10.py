from pathlib import Path


def test_worker_uses_tls_proxy_restart_and_persistent_state() -> None:
    compose = Path("compose.yaml").read_text(encoding="utf-8")

    assert "caddy:" in compose
    assert "NEXA_WORKER_API_URL: https://caddy:8443" in compose
    assert "SSL_CERT_FILE: /caddy-data/caddy/pki/authorities/local/root.crt" in compose
    assert "restart: unless-stopped" in compose
    assert "source: ${NEXA_WORKER_STATE_ROOT:?set absolute shared worker state path}" in compose
    assert "target: ${NEXA_WORKER_STATE_ROOT:?set absolute shared worker state path}" in compose
    assert "create_host_path: false" in compose
    assert "/var/run/docker.sock:/var/run/docker.sock" in compose
    worker = compose.split("  worker:", 1)[1].split("networks:", 1)[0]
    assert "NEXA_DATABASE_URL" not in worker


def test_worker_image_installs_the_docker_cli_only() -> None:
    dockerfile = Path("deploy/b10/Dockerfile").read_text(encoding="utf-8")

    assert "apt-get install -y --no-install-recommends docker-cli" in dockerfile
    assert "--no-install-recommends docker.io" not in dockerfile


def test_caddy_reaps_its_healthcheck_helpers_and_bounds_pids() -> None:
    """ENV-01: BusyBox ``wget`` leaves its ``ssl_client`` helper to PID 1 on every check.

    Caddy as PID 1 never reaps it, so each 5-second health check leaked one zombie PID. A
    reaping init must be PID 1, and a PID limit keeps any leak inside the container.
    """
    compose = Path("compose.yaml").read_text(encoding="utf-8")
    caddy = compose.split("\n  caddy:\n", 1)[1].split("\n  worker:\n", 1)[0]

    assert "\n    init: true\n" in caddy
    [limit] = [
        line.split(":", 1)[1].strip()
        for line in caddy.splitlines()
        if line.startswith("    pids_limit:")
    ]
    assert 0 < int(limit) <= 1024
    assert "https://caddy:8443/openapi.json" in caddy
