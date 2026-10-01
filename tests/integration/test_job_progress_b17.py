"""B17 getJobProgress over production HTTP and PostgreSQL."""

from datetime import UTC
from uuid import UUID

import pytest
from sqlalchemy import insert, select

from nexa.api.schemas import ProgressRecord
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration._factories import seed_job, seed_tenant_graph
from tests.integration.test_checkpoint_b14 import CheckpointFixture
from tests.integration.test_checkpoint_restore_b14 import (
    _claim,
    _commit,
    _next_attempt,
    _start,
)

pytestmark = pytest.mark.postgres

_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def progress_client(migrated_postgres_engine, tmp_path):
    with _client(migrated_postgres_engine, tmp_path) as client:
        yield client


@pytest.fixture
def progress_job(migrated_postgres_engine, progress_client):
    return CheckpointFixture(migrated_postgres_engine, progress_client, label="progress")


@pytest.fixture
def reader(migrated_postgres_engine, tmp_path):
    """Browser client kept apart from the worker client: a session cookie would shadow
    the worker bearer on the attempt callbacks."""
    with _client(migrated_postgres_engine, tmp_path) as client:
        yield client


def _member_login(fixture, client, tenant_ids):
    bootstrap = client.post(
        "/v1/internal/admin-bootstrap",
        headers={"X-Nexa-Bootstrap-Secret": "b" * 32, "Idempotency-Key": "b17-progress-admin"},
        json={
            "username": "b17-progress@example.test",
            "display_name": "B17 Progress",
            "password": _PASSWORD,
        },
    )
    assert bootstrap.status_code == 201, bootstrap.text
    user_id = UUID(bootstrap.json()["user_id"])
    with fixture.engine.begin() as connection:
        for tenant_id in tenant_ids:
            connection.execute(
                insert(s.memberships).values(tenant_id=tenant_id, user_id=user_id, role="MEMBER")
            )
    login = client.post(
        "/v1/auth/login",
        headers={"Origin": "https://nexa.test"},
        json={"username": "b17-progress@example.test", "password": _PASSWORD},
    )
    assert login.status_code == 200, login.text
    return {"Origin": "https://nexa.test", "X-CSRF-Token": login.json()["csrf_token"]}


def _renew(fixture, sequence, snapshot):
    response = fixture.post(
        "/renew",
        {"authority": fixture.authority, "progress_sequence": sequence, "progress": snapshot},
    )
    assert response.status_code == 200, response.text


def _wire_time(value):
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _updated_at(fixture, attempt_id):
    with fixture.engine.connect() as connection:
        return connection.execute(
            select(s.attempts.c.updated_at).where(s.attempts.c.attempt_id == UUID(attempt_id))
        ).scalar_one()


def _progress(fixture, client, job_id=None, tenant_id=None):
    tenant = str(tenant_id or fixture.graph["tenant_id"])
    return client.get(
        f"/v1/jobs/{job_id or fixture.job_id}/progress",
        headers={"X-Nexa-Tenant-Id": tenant},
    )


def test_progress_is_absent_until_an_attempt_reports(progress_job, reader):
    fixture = progress_job
    _member_login(fixture, reader, [fixture.graph["tenant_id"]])
    with fixture.engine.begin() as connection:
        queued = seed_job(connection, fixture.graph)["job_id"]

    no_attempt = _progress(fixture, reader, job_id=queued)
    assert no_attempt.status_code == 200, no_attempt.text
    assert no_attempt.json() == {"job_id": str(queued), "available": False}

    # Attempt 1 exists and is RUNNING but has not reported progress yet.
    unreported = _progress(fixture, reader)
    assert unreported.status_code == 200, unreported.text
    assert unreported.json() == {"job_id": str(fixture.job_id), "available": False}
    ProgressRecord.model_validate(unreported.json())


def test_progress_reflects_the_latest_accepted_report(progress_job, reader):
    fixture = progress_job
    _member_login(fixture, reader, [fixture.graph["tenant_id"]])
    attempt_id = fixture.authority["attempt_id"]
    first = {"fraction": 0.25, "step": 2, "epoch": None, "item_cursor": None}
    _renew(fixture, 1, first)

    response = _progress(fixture, reader)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "job_id": str(fixture.job_id),
        "available": True,
        "attempt_id": attempt_id,
        "progress_sequence": 1,
        "snapshot": first,
        "restore_checkpoint_id": None,
        "reported_at": _wire_time(_updated_at(fixture, attempt_id)),
    }
    ProgressRecord.model_validate(body)

    second = {"fraction": 0.5, "step": 4, "epoch": 1, "item_cursor": 7}
    _renew(fixture, 2, second)
    advanced = _progress(fixture, reader).json()
    assert advanced["progress_sequence"] == 2
    assert advanced["snapshot"] == second
    assert advanced["reported_at"] >= body["reported_at"]


def test_progress_after_restore_names_the_new_attempt_and_checkpoint(progress_job, reader):
    fixture = progress_job
    _member_login(fixture, reader, [fixture.graph["tenant_id"]])
    _renew(fixture, 1, {"fraction": 0.4, "step": 40, "epoch": None, "item_cursor": None})
    _commit(fixture, step=20, accumulator=111)
    newest = _commit(fixture, step=40, accumulator=222)
    old_attempt = fixture.authority["attempt_id"]
    _next_attempt(fixture)
    claimed = _claim(fixture)
    assert claimed.status_code == 200, claimed.text
    started = _start(fixture, claimed.json()["execution_context"])
    assert started.status_code == 200, started.text

    # The newest attempt has not reported yet: the old attempt's progress is not reused.
    pending = _progress(fixture, reader)
    assert pending.status_code == 200, pending.text
    assert pending.json() == {"job_id": str(fixture.job_id), "available": False}

    snapshot = {"fraction": 0.2, "step": 40, "epoch": None, "item_cursor": None}
    _renew(fixture, 1, snapshot)
    body = _progress(fixture, reader).json()
    assert body["attempt_id"] == fixture.authority["attempt_id"] != old_attempt
    assert body["progress_sequence"] == 1
    assert body["snapshot"] == snapshot
    assert body["restore_checkpoint_id"] == newest["record"]["checkpoint_id"]
    ProgressRecord.model_validate(body)


def test_progress_is_tenant_scoped_and_needs_read_access(
    progress_job, reader, migrated_postgres_engine, tmp_path
):
    fixture = progress_job
    with fixture.engine.begin() as connection:
        member_tenant = seed_tenant_graph(connection, label="b17-progress-member")["tenant_id"]
        outsider_tenant = seed_tenant_graph(connection, label="b17-progress-outsider")["tenant_id"]
    write = _member_login(fixture, reader, [fixture.graph["tenant_id"], member_tenant])

    other_tenant = _progress(fixture, reader, tenant_id=member_tenant)
    assert other_tenant.status_code == 404, other_tenant.text
    assert other_tenant.json()["code"] == "resource_not_found"
    outsider = _progress(fixture, reader, tenant_id=outsider_tenant)
    assert outsider.status_code == 403, outsider.text
    assert outsider.json()["code"] == "permission_denied"
    missing = _progress(fixture, reader, job_id=new_uuid7())
    assert missing.status_code == 404, missing.text
    malformed = _progress(fixture, reader, job_id="not-a-uuid")
    assert malformed.status_code == 400, malformed.text

    tokens = {}
    for scope in ("jobs:read", "jobs:write"):
        created = reader.post(
            "/v1/tokens",
            headers={**write, "Idempotency-Key": f"b17-progress-token-{scope[5:]}"},
            json={"name": f"b17-{scope[5:]}", "scopes": [scope], "expires_in_seconds": 300},
        )
        assert created.status_code == 201, created.text
        tokens[scope] = created.json()["token"]
    url = f"/v1/jobs/{fixture.job_id}/progress"
    tenant = {"X-Nexa-Tenant-Id": str(fixture.graph["tenant_id"])}
    with _client(migrated_postgres_engine, tmp_path) as cli_client:
        cli_client.cookies.clear()
        read = cli_client.get(
            url, headers={**tenant, "Authorization": f"Bearer {tokens['jobs:read']}"}
        )
        assert read.status_code == 200, read.text
        assert read.json() == {"job_id": str(fixture.job_id), "available": False}
        denied = cli_client.get(
            url, headers={**tenant, "Authorization": f"Bearer {tokens['jobs:write']}"}
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["code"] == "permission_denied"
        assert cli_client.get(url, headers=tenant).status_code == 401
