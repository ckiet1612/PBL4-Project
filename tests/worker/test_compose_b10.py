from pathlib import Path


def test_worker_uses_tls_proxy_restart_and_persistent_state() -> None:
    compose = Path("compose.yaml").read_text(encoding="utf-8")

    assert "caddy:" in compose
    assert "NEXA_WORKER_API_URL: https://caddy:8443" in compose
    assert "SSL_CERT_FILE: /caddy-data/caddy/pki/authorities/local/root.crt" in compose
    assert "restart: unless-stopped" in compose
    assert "worker_state:/var/lib/nexa-worker" in compose
    assert "/var/run/docker.sock:/var/run/docker.sock" in compose
    worker = compose.split("  worker:", 1)[1].split("networks:", 1)[0]
    assert "NEXA_DATABASE_URL" not in worker


def test_worker_image_installs_the_docker_cli_only() -> None:
    dockerfile = Path("deploy/b10/Dockerfile").read_text(encoding="utf-8")

    assert "apt-get install -y --no-install-recommends docker-cli" in dockerfile
    assert "--no-install-recommends docker.io" not in dockerfile
