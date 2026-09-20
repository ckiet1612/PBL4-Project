import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from typer.testing import CliRunner

from nexa.infrastructure.persistence.schema import auth_control
from nexa.infrastructure.persistence.transactions import TransactionRetryExhausted

pytestmark = pytest.mark.postgres


def test_maintenance_cli_reopens_worker_bootstrap_window(
    migrated_postgres_engine, tmp_path, monkeypatch
) -> None:
    from nexa.cli.main import app

    server_secret = tmp_path / "server-secret"
    bootstrap_secret = tmp_path / "bootstrap-secret"
    server_secret.write_bytes(b"s" * 32)
    bootstrap_secret.write_bytes(b"b" * 32)
    database_url = migrated_postgres_engine.url.render_as_string(hide_password=False)
    environment = {
        "NEXA_ENVIRONMENT": "test",
        "NEXA_DATABASE_URL": database_url,
        "NEXA_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "NEXA_PUBLIC_ORIGIN": "https://nexa.test",
        "NEXA_SERVER_SECRET_FILE": str(server_secret),
        "NEXA_BOOTSTRAP_SECRET_FILE": str(bootstrap_secret),
        "NEXA_INSTALLATION_ID": "018f05c4-a922-7d0d-9f55-f9084a72d0f1",
        "NEXA_LOCAL_WORKER_ID": "018f05c4-a922-7d0d-9f55-f9084a72d0f2",
        "NEXA_LOCAL_WORKER_FINGERPRINT": "sha256:" + "a" * 64,
        "NEXA_MAINTENANCE_CIDRS": "127.0.0.0/8",
    }
    monkeypatch.delenv("NEXA_TEST_DATABASE_URL", raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            update(auth_control)
            .where(auth_control.c.singleton_key == "auth")
            .values(
                installation_id=environment["NEXA_INSTALLATION_ID"],
                local_worker_id=environment["NEXA_LOCAL_WORKER_ID"],
                worker_credential_fingerprint=environment["NEXA_LOCAL_WORKER_FINGERPRINT"],
                worker_window_opened_at=datetime(2026, 1, 1, tzinfo=UTC),
                worker_window_expires_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
            )
        )

    result = CliRunner().invoke(app, ["reopen-worker-bootstrap"])

    assert result.exit_code == 0, result.output
    response = json.loads(result.output)
    assert response["opened_at"] < response["expires_at"]
    with migrated_postgres_engine.connect() as connection:
        durable_expiry = connection.execute(
            select(auth_control.c.worker_window_expires_at).where(
                auth_control.c.singleton_key == "auth"
            )
        ).scalar_one()
    assert durable_expiry > datetime.now(UTC)


def test_maintenance_cli_maps_transaction_retry_exhaustion_to_safe_failure(
    migrated_postgres_engine, tmp_path, monkeypatch
) -> None:
    from nexa.cli import main

    server_secret = tmp_path / "server-secret"
    bootstrap_secret = tmp_path / "bootstrap-secret"
    server_secret.write_bytes(b"s" * 32)
    bootstrap_secret.write_bytes(b"b" * 32)
    environment = {
        "NEXA_ENVIRONMENT": "test",
        "NEXA_DATABASE_URL": migrated_postgres_engine.url.render_as_string(hide_password=False),
        "NEXA_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "NEXA_PUBLIC_ORIGIN": "https://nexa.test",
        "NEXA_SERVER_SECRET_FILE": str(server_secret),
        "NEXA_BOOTSTRAP_SECRET_FILE": str(bootstrap_secret),
        "NEXA_INSTALLATION_ID": "018f05c4-a922-7d0d-9f55-f9084a72d0f1",
        "NEXA_LOCAL_WORKER_ID": "018f05c4-a922-7d0d-9f55-f9084a72d0f2",
        "NEXA_LOCAL_WORKER_FINGERPRINT": "sha256:" + "a" * 64,
        "NEXA_MAINTENANCE_CIDRS": "127.0.0.0/8",
    }
    monkeypatch.delenv("NEXA_TEST_DATABASE_URL", raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    def exhaust_retries(_self) -> None:
        raise TransactionRetryExhausted("persistent serialization conflict")

    monkeypatch.setattr(main.IdentityService, "initialize", exhaust_retries)
    result = CliRunner().invoke(main.app, ["reopen-worker-bootstrap"])

    assert result.exit_code == 1
    assert result.output == "error: database dependency is unavailable\n"
    assert "serialization" not in result.output
