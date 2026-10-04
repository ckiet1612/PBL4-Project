"""B19-R14/R05: `storage-check` and `consistency-check` are read-only and exit 0/1/2."""

import json
import os

import pytest
from sqlalchemy import func, select, update
from typer.testing import CliRunner

from nexa.cli.main import app
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration._factories import seed_job, seed_tenant_graph
from tests.integration.test_artifacts_b07 import _principal
from tests.integration.test_checkpoint_b14 import CheckpointFixture
from tests.integration.test_control_b15 import _set_mode
from tests.integration.test_storage_gc_b19 import _service
from tests.integration.test_storage_pressure_b19 import _tenant, _upload

pytestmark = pytest.mark.postgres


@pytest.fixture
def cli(migrated_postgres_engine, tmp_path, monkeypatch):
    server_secret = tmp_path / "server-secret"
    bootstrap_secret = tmp_path / "bootstrap-secret"
    server_secret.write_bytes(b"s" * 32)
    bootstrap_secret.write_bytes(b"b" * 32)
    environment = {
        "NEXA_ENVIRONMENT": "test",
        "NEXA_DATABASE_URL": migrated_postgres_engine.url.render_as_string(hide_password=False),
        "NEXA_ARTIFACT_ROOT": str(tmp_path / "cli-artifacts"),
        "NEXA_PUBLIC_ORIGIN": "https://nexa.test",
        "NEXA_SERVER_SECRET_FILE": str(server_secret),
        "NEXA_BOOTSTRAP_SECRET_FILE": str(bootstrap_secret),
        "NEXA_INSTALLATION_ID": "018f05c4-a922-7d0d-9f55-f9084a72d0f1",
        "NEXA_LOCAL_WORKER_ID": "018f05c4-a922-7d0d-9f55-f9084a72d0f2",
        "NEXA_LOCAL_WORKER_FINGERPRINT": "sha256:" + "a" * 64,
        "NEXA_MAINTENANCE_CIDRS": "127.0.0.0/8",
    }
    # load_settings rejects unknown NEXA_* variables, so drop every test-only NEXA_TEST_* one.
    for key in [key for key in os.environ if key.startswith("NEXA_TEST_")]:
        monkeypatch.delenv(key)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    password = migrated_postgres_engine.url.password

    def run(*args):
        result = CliRunner().invoke(app, list(args))
        assert password is None or password not in result.output
        return result

    return run


def _writes(engine):
    """Rows a check must never add: audit records and counters."""
    with engine.connect() as connection:
        return tuple(
            connection.execute(select(func.count()).select_from(table)).scalar_one()
            for table in (s.audit_records, s.artifact_storage_counters, s.upload_sessions)
        )


def test_storage_check_reports_drift_without_repairing_it(cli, migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    service, _store = _service(engine, tmp_path)
    tenant_id = _tenant(engine, "b19-check-drift")
    _upload(service, _principal(tenant_id), tenant_id, "b19-check-drift-0001", b"data")
    _set_mode(engine, "WRITE_FROZEN")  # read-only checks still run
    before = _writes(engine)

    clean = cli("storage-check")
    assert clean.exit_code == 0, clean.output
    assert json.loads(clean.output) == {
        "consistent": True,
        "drift": [],
        "drift_tenants": 0,
        "retained_unpublished_artifacts": 0,
        "retained_unpublished_bytes": 0,
    }
    assert _writes(engine) == before
    with engine.begin() as connection:
        connection.execute(
            update(s.artifact_storage_counters)
            .where(s.artifact_storage_counters.c.tenant_id == tenant_id)
            .values(committed_bytes=99, reserved_bytes=7)
        )
    before = _writes(engine)
    drift = cli("storage-check")
    assert drift.exit_code == 1, drift.output
    summary = json.loads(drift.output)
    assert summary["drift"] == [
        {
            "tenant_id": str(tenant_id),
            "committed_counter": 99,
            "committed_artifacts": 4,
            "reserved_counter": 7,
            "reserved_sessions": 0,
        }
    ]
    assert _writes(engine) == before
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(s.artifact_storage_counters.c.committed_bytes).where(
                    s.artifact_storage_counters.c.tenant_id == tenant_id
                )
            ).scalar_one()
            == 99
        )


def test_storage_check_counts_bytes_retained_until_published(
    cli, migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        fixture = CheckpointFixture(engine, client, label="b19-retained")
        reservation = fixture.reserve().json()
        manifest, manifest_artifact, state_artifact = fixture.checkpoint(reservation)
        retained = cli("storage-check")
        summary = json.loads(retained.output)
        assert summary["retained_unpublished_artifacts"] == 2
        assert summary["retained_unpublished_bytes"] == (
            manifest_artifact["size_bytes"] + state_artifact["size_bytes"]
        )
        assert fixture.publish(manifest, manifest_artifact).status_code == 201
        published = json.loads(cli("storage-check").output)
        assert published["retained_unpublished_artifacts"] == 0


def test_consistency_check_reports_missing_ids_and_result_rules(
    cli, migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="b19-consistency")
        queued = seed_job(connection, graph)
    accepted = tmp_path / "accepted.txt"
    accepted.write_text(f"{queued['job_id']}\n\n")
    before = _writes(engine)

    clean = cli("consistency-check", "--accepted-ids", str(accepted))
    assert clean.exit_code == 0, clean.output
    assert json.loads(clean.output) == {
        "accepted": 1,
        "consistent": True,
        "missing_accepted": 0,
        "multiple_results": 0,
        "result_of_unsucceeded_job": 0,
        "succeeded": 0,
        "succeeded_without_result": 0,
    }
    accepted.write_text(f"{queued['job_id']}\n{new_uuid7()}\n")
    missing = cli("consistency-check", "--accepted-ids", str(accepted))
    assert missing.exit_code == 1, missing.output
    assert json.loads(missing.output)["missing_accepted"] == 1

    with engine.begin() as connection:
        connection.execute(
            update(s.jobs).where(s.jobs.c.job_id == queued["job_id"]).values(state="SUCCEEDED")
        )
    succeeded = cli("consistency-check", "--accepted-ids", str(accepted))
    assert succeeded.exit_code == 1
    assert json.loads(succeeded.output)["succeeded_without_result"] == 1
    assert _writes(engine) == before


def test_checks_exit_2_on_bad_input_or_an_unavailable_database(cli, tmp_path, monkeypatch):
    bad = tmp_path / "bad.txt"
    bad.write_text("not-a-job-id\n")
    assert cli("consistency-check", "--accepted-ids", str(bad)).exit_code == 2
    assert cli("consistency-check", "--accepted-ids", str(tmp_path / "absent")).exit_code == 2
    monkeypatch.setenv(
        "NEXA_DATABASE_URL", "postgresql+psycopg://nexa:unused@127.0.0.1:1/nexa?connect_timeout=2"
    )
    unavailable = cli("storage-check")
    assert unavailable.exit_code == 2
    assert "unused" not in unavailable.output
