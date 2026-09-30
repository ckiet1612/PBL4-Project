"""B16 template catalog: local immutable registration, tenant-context list/get, CPU-only bounds."""

import copy
import json
import os
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import func, select, text, update
from typer.testing import CliRunner

from nexa.application.errors import ApplicationError
from nexa.application.template_registry import load_definition, register_template
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration.test_control_b15 import _set_mode
from tests.integration.test_jobs_b08 import _bootstrap_member

pytestmark = pytest.mark.postgres

TEMPLATES = Path(__file__).resolve().parents[2] / "deploy" / "templates"
CPU_DIGEST = "sha256:" + "1" * 64
ML_DIGEST = "sha256:" + "2" * 64


def _definition(name):
    return load_definition((TEMPLATES / name).read_bytes())


def _register_all(engine):
    factory = create_session_factory(engine)
    return [
        register_template(factory, _definition("cpu-iterative.v1.json"), CPU_DIGEST),
        register_template(factory, _definition("pytorch-cifar10-cnn.v1.json"), ML_DIGEST),
        register_template(factory, _definition("batch-inference.v1.json"), ML_DIGEST),
    ]


def _audits(engine):
    with engine.connect() as connection:
        return (
            connection.execute(
                select(s.audit_records.c.target_id, s.audit_records.c.safe_metadata)
                .where(s.audit_records.c.action == "template.version.register")
                .order_by(s.audit_records.c.audit_id)
            )
            .mappings()
            .all()
        )


def test_registration_is_idempotent_immutable_and_audited(migrated_postgres_engine):
    engine = migrated_postgres_engine
    first = _register_all(engine)
    assert [r["status"] for r in first] == ["REGISTERED"] * 3
    assert [r["current_version"] for r in first] == [1, 1, 1]
    replay = _register_all(engine)
    assert [r["status"] for r in replay] == ["UNCHANGED"] * 3
    assert [r["content_checksum"] for r in replay] == [r["content_checksum"] for r in first]
    audits = _audits(engine)
    assert [a["target_id"] for a in audits] == [
        "cpu-iterative:1",
        "pytorch-cifar10-cnn:1",
        "batch-inference:1",
    ]
    assert audits[1]["safe_metadata"]["image_digest"] == ML_DIGEST

    factory = create_session_factory(engine)
    changed = copy.deepcopy(_definition("pytorch-cifar10-cnn.v1.json"))
    changed["parameter_schema"][0]["maximum"] = 50
    with pytest.raises(ApplicationError) as conflict:
        register_template(factory, changed, ML_DIGEST)
    assert conflict.value.status == 409
    with pytest.raises(ApplicationError) as rebuilt:
        register_template(factory, _definition("pytorch-cifar10-cnn.v1.json"), "sha256:" + "3" * 64)
    assert rebuilt.value.status == 409

    changed["version"] = 2
    advanced = register_template(factory, changed, ML_DIGEST)
    assert (advanced["status"], advanced["current_version"]) == ("REGISTERED", 2)
    with engine.connect() as connection:
        row = connection.execute(
            select(s.template_versions.c.parameter_schema).where(
                s.template_versions.c.template_id == "pytorch-cifar10-cnn",
                s.template_versions.c.version == 1,
            )
        ).scalar_one()
        current = connection.execute(
            select(s.templates.c.current_version).where(
                s.templates.c.template_id == "pytorch-cifar10-cnn"
            )
        ).scalar_one()
    # Version 1 is untouched; only the catalog pointer advanced, with one more audit row.
    assert row[0]["maximum"] == 100
    assert current == 2
    assert len(_audits(engine)) == 4


def test_registration_is_refused_while_writes_are_frozen(migrated_postgres_engine):
    _set_mode(migrated_postgres_engine, "WRITE_FROZEN")
    factory = create_session_factory(migrated_postgres_engine)
    with pytest.raises(ApplicationError) as frozen:
        register_template(factory, _definition("cpu-iterative.v1.json"), CPU_DIGEST)
    assert frozen.value.status == 409
    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(s.templates)).scalar_one() == 0
    assert _audits(migrated_postgres_engine) == []


def _member(client):
    admin_id, csrf = _bootstrap_member(client)
    write = {"Origin": "https://nexa.test", "X-CSRF-Token": csrf}
    tenant_ids = []
    for slug in ("b16-team", "b16-other"):
        tenant = client.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": f"b16-create-{slug}"},
            json={"slug": slug, "display_name": slug},
        )
        assert tenant.status_code == 201, tenant.text
        tenant_ids.append(tenant.json()["tenant_id"])
    membership = client.post(
        f"/v1/admin/tenants/{tenant_ids[0]}/memberships",
        headers={**write, "Idempotency-Key": "b16-add-membership", "If-Match": '"v1"'},
        json={"user_id": admin_id, "role": "MEMBER"},
    )
    assert membership.status_code == 200, membership.text
    login = client.post(
        "/v1/auth/login",
        headers={"Origin": "https://nexa.test"},
        json={"username": "b08-admin@example.test", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200, login.text
    return tenant_ids, {"Origin": "https://nexa.test", "X-CSRF-Token": login.json()["csrf_token"]}


def _token(client, write, scope):
    response = client.post(
        "/v1/tokens",
        headers={**write, "Idempotency-Key": f"b16-token-{scope.replace(':', '-')}"},
        json={"name": f"b16-{scope}", "scopes": [scope], "expires_in_seconds": 300},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_template_reads_are_tenant_scoped_and_contract_shaped(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, other_tenant_id), write = _member(client)
        read = _token(client, write, "jobs:read")
        artifacts_only = _token(client, write, "artifacts:read")
        with _client(engine, tmp_path) as cli:
            cli.cookies.clear()
            tenant = {"X-Nexa-Tenant-Id": tenant_id}
            listed = cli.get("/v1/templates", headers={**read, **tenant})
            assert listed.status_code == 200, listed.text
            assert [t["template_id"] for t in listed.json()] == [
                "batch-inference",
                "cpu-iterative",
                "pytorch-cifar10-cnn",
            ]
            training = cli.get("/v1/templates/pytorch-cifar10-cnn", headers={**read, **tenant})
            assert training.status_code == 200, training.text
            body = training.json()
            assert set(body) == {
                "template_id",
                "version",
                "display_name",
                "adapter_id",
                "adapter_version",
                "image_digest",
                "enabled",
                "checkpointable",
                "restart_safe",
                "allowed_devices",
                "capability_requirement",
                "parameter_schema",
            }
            assert body["allowed_devices"] == ["CPU"]
            assert body["capability_requirement"]["image_digest"] == ML_DIGEST
            assert body["capability_requirement"]["framework_version"] == "2.12.1"
            assert [p["name"] for p in body["parameter_schema"]][-1] == "subset_size"
            assert body == listed.json()[2]

            # The browser session reads the same catalog (no CLI scope applies to sessions).
            assert client.get("/v1/templates", headers=tenant).json() == listed.json()

            assert cli.get("/v1/templates?enabled=false", headers={**read, **tenant}).json() == []
            with engine.begin() as connection:
                connection.execute(
                    update(s.templates)
                    .where(s.templates.c.template_id == "batch-inference")
                    .values(enabled=False)
                )
            disabled = cli.get("/v1/templates?enabled=false", headers={**read, **tenant})
            assert [t["template_id"] for t in disabled.json()] == ["batch-inference"]
            assert disabled.json()[0]["enabled"] is False
            assert len(cli.get("/v1/templates", headers={**read, **tenant}).json()) == 2

            missing = cli.get("/v1/templates/unknown-template", headers={**read, **tenant})
            assert (missing.status_code, missing.json()["code"]) == (404, "resource_not_found")
            # A malformed path parameter is wire input: 400 (B16-R08).
            malformed = cli.get("/v1/templates/Bad_Id", headers={**read, **tenant})
            assert (malformed.status_code, malformed.json()["code"]) == (400, "validation_failed")
            foreign = cli.get(
                "/v1/templates", headers={**read, "X-Nexa-Tenant-Id": other_tenant_id}
            )
            assert foreign.status_code == 403
            scoped = cli.get("/v1/templates", headers={**artifacts_only, **tenant})
            assert scoped.status_code == 403
            assert cli.get("/v1/templates", headers=tenant).status_code == 401


def test_gpu_request_is_rejected_by_every_cpu_template(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, _), write = _member(client)
        artifact_id = new_uuid7()
        with engine.begin() as connection:
            connection.execute(
                s.artifacts.insert().values(
                    artifact_id=artifact_id,
                    tenant_id=tenant_id,
                    kind="DATASET",
                    media_type="application/vnd.apache.arrow.file",
                    size_bytes=7,
                    checksum=f"sha256:{sha256(b'dataset').hexdigest()}",
                    blob_key=f"tenant/{tenant_id}/blob/{artifact_id}",
                    state="COMMITTED",
                    version=1,
                )
            )
            connection.execute(
                text(
                    "UPDATE tenant_policies SET cpu_limit_millis = 8000, "
                    "memory_limit_bytes = 8589934592, gpu_limit = 1 "
                    "WHERE tenant_id = :tenant_id AND is_current"
                ),
                {"tenant_id": tenant_id},
            )
        spec = {
            "template_id": "pytorch-cifar10-cnn",
            "template_version": 1,
            "input_artifact_id": str(artifact_id),
            "resources": {"cpu_millis": 2000, "memory_bytes": 1024**3, "gpu_count": 1},
            "priority": 1,
            "runtime_limit_seconds": 120,
            "checkpoint_interval_seconds": 10,
            "parameters": {
                "epochs": 1,
                "batch_size": 64,
                "learning_rate": 0.05,
                "seed": 7,
                "subset_size": 5000,
            },
        }
        headers = {**write, "X-Nexa-Tenant-Id": tenant_id}
        gpu = client.post(
            "/v1/jobs",
            headers={**headers, "Idempotency-Key": "b16-gpu-submit-0001"},
            json={"spec": spec},
        )
        assert gpu.status_code == 422, gpu.text
        with engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(s.jobs)).scalar_one() == 0
        cpu = client.post(
            "/v1/jobs",
            headers={**headers, "Idempotency-Key": "b16-cpu-submit-0001"},
            json={"spec": {**spec, "resources": {**spec["resources"], "gpu_count": 0}}},
        )
        assert cpu.status_code == 202, cpu.text


def test_maintenance_command_registers_and_reports_conflicts(
    migrated_postgres_engine, tmp_path, monkeypatch
):
    from nexa.cli.main import app

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
    # load_settings rejects unknown NEXA_* variables, so drop every test-only NEXA_TEST_* one.
    for key in [key for key in os.environ if key.startswith("NEXA_TEST_")]:
        monkeypatch.delenv(key)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    source = str(TEMPLATES / "batch-inference.v1.json")
    command = ["register-template", "--file", source, "--image-digest", ML_DIGEST]
    runner = CliRunner()
    first = runner.invoke(app, command)
    assert first.exit_code == 0, first.output
    assert json.loads(first.stdout)["status"] == "REGISTERED"
    second = runner.invoke(app, command)
    assert json.loads(second.stdout)["status"] == "UNCHANGED"
    conflict = runner.invoke(app, [*command[:-1], "sha256:" + "4" * 64])
    assert conflict.exit_code == 1
    assert "different content" in conflict.stderr
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"schema_version": 2}')
    rejected = runner.invoke(app, ["register-template", "--file", str(invalid), *command[3:]])
    assert rejected.exit_code == 2
    assert "definition is invalid" in rejected.stderr
    assert len(_audits(migrated_postgres_engine)) == 1
