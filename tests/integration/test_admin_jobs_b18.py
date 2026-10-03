"""B18 adminListJobs/adminGetJob over PostgreSQL: cross-tenant, read-only, audited."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, update

from nexa.api.http import strong_etag
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration._admin_b18 import admin_session, audit_rows, member_session
from tests.integration._factories import seed_job, seed_tenant_graph
from tests.integration.test_jobs_b08 import _submit_body

pytestmark = pytest.mark.postgres

BASE = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)


def _wire(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _seed(engine):
    """Two tenants, six jobs; two share a created_at so job_id breaks the tie."""
    with engine.begin() as connection:
        alpha = seed_tenant_graph(connection, label="b18-alpha", template_id="cpu-iterative")
        beta = seed_tenant_graph(connection, label="b18-beta")
        layout = [
            (alpha, "QUEUED", "waiting_for_capacity", 0),
            (alpha, "RUNNING", None, 1),
            (alpha, "SUCCEEDED", None, 2),
            (beta, "QUEUED", "waiting_for_quota", 3),
            (beta, "QUEUED", "waiting_for_quota", 3),
            (beta, "FAILED", None, 4),
        ]
        jobs = []
        for graph, state, reason, minute in layout:
            spec = _submit_body(graph["artifact_id"])["spec"]
            job_id = seed_job(connection, graph, state=state, canonical_spec=spec)["job_id"]
            created_at = BASE + timedelta(minutes=minute)
            connection.execute(
                update(s.jobs)
                .where(s.jobs.c.job_id == job_id)
                .values(
                    waiting_reason=reason,
                    created_at=created_at,
                    updated_at=created_at,
                    event_sequence=1,
                )
            )
            jobs.append(
                {
                    "job_id": str(job_id),
                    "tenant_id": str(graph["tenant_id"]),
                    "user_id": str(graph["user_id"]),
                    "state": state,
                    "waiting_reason": reason,
                    "created_at": created_at,
                }
            )
    expected = sorted(jobs, key=lambda job: (job["created_at"], job["job_id"]), reverse=True)
    return alpha, beta, expected


def _ids(response):
    assert response.status_code == 200, response.text
    return [item["job_id"] for item in response.json()["items"]]


def _walk(client, params, page_size):
    seen, cursor = [], None
    while True:
        query = {**params, "page_size": page_size}
        if cursor:
            query["cursor"] = cursor
        response = client.get("/v1/admin/jobs", params=query)
        seen.extend(_ids(response))
        cursor = response.json()["page"]["next_cursor"]
        if cursor is None:
            return seen


def test_list_orders_filters_and_pages_across_tenants(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    alpha, beta, expected = _seed(engine)
    every = [job["job_id"] for job in expected]

    def only(predicate):
        return [job["job_id"] for job in expected if predicate(job)]

    with admin_session(engine, tmp_path) as (client, _):
        listing = client.get("/v1/admin/jobs")
        assert _ids(listing) == every
        assert listing.json()["page"] == {"next_cursor": None, "page_size": 50}
        item = listing.json()["items"][0]
        assert item["spec"]["template_id"] == "cpu-iterative"
        assert item["created_at"] == _wire(expected[0]["created_at"])

        filters = [
            (
                {"tenant_id": str(beta["tenant_id"])},
                lambda j: j["tenant_id"] == str(beta["tenant_id"]),
            ),
            ({"user_id": str(alpha["user_id"])}, lambda j: j["user_id"] == str(alpha["user_id"])),
            ({"state": "QUEUED"}, lambda j: j["state"] == "QUEUED"),
            (
                {"waiting_reason": "waiting_for_quota"},
                lambda j: j["waiting_reason"] == "waiting_for_quota",
            ),
            (
                {"created_after": _wire(BASE + timedelta(minutes=3))},
                lambda j: j["created_at"] >= BASE + timedelta(minutes=3),
            ),
            (
                {"tenant_id": str(alpha["tenant_id"]), "state": "RUNNING"},
                lambda j: j["tenant_id"] == str(alpha["tenant_id"]) and j["state"] == "RUNNING",
            ),
        ]
        for params, predicate in filters:
            wanted = only(predicate)
            assert wanted, params
            assert _ids(client.get("/v1/admin/jobs", params=params)) == wanted, params
            assert _walk(client, params, 1) == wanted, params
        assert _walk(client, {}, 2) == every
        assert _ids(client.get("/v1/admin/jobs", params={"tenant_id": str(new_uuid7())})) == []

        first = client.get("/v1/admin/jobs", params={"page_size": 2, "state": "QUEUED"})
        cursor = first.json()["page"]["next_cursor"]
        assert cursor
        for params in (
            {"state": "RUNNING"},
            {"state": "QUEUED", "tenant_id": str(beta["tenant_id"])},
            {"state": "QUEUED", "waiting_reason": "waiting_for_quota"},
        ):
            mismatch = client.get(
                "/v1/admin/jobs", params={**params, "page_size": 2, "cursor": cursor}
            )
            assert mismatch.status_code == 400, mismatch.text
            assert mismatch.json()["code"] == "invalid_cursor"
        tampered = cursor[:-2] + ("AA" if not cursor.endswith("AA") else "BB")
        broken = client.get(
            "/v1/admin/jobs", params={"state": "QUEUED", "page_size": 2, "cursor": tampered}
        )
        assert broken.status_code == 400
        assert broken.json()["code"] == "invalid_cursor"

        for params in (
            {"state": "BOGUS"},
            {"waiting_reason": "waiting_for_godot"},
            {"tenant_id": "nope"},
            {"user_id": "nope"},
            {"created_after": "2026-09-20T08:00:00"},
            {"created_after": "yesterday"},
            {"page_size": 0},
            {"page_size": 101},
            {"cursor": "short"},
        ):
            invalid = client.get("/v1/admin/jobs", params=params)
            assert invalid.status_code == 400, (params, invalid.text)
            assert invalid.json()["code"] in {"validation_failed", "invalid_cursor"}

        successes = audit_rows(engine, "admin.job.list")
        assert all(row["actor_type"] == "ADMIN" for row in successes)
        assert {row["target_id"] for row in successes} >= {"all", str(beta["tenant_id"])}


def test_cursor_is_bound_to_the_admin_and_roles_are_enforced(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    _seed(engine)
    with admin_session(engine, tmp_path) as (client, headers):
        cursor = client.get("/v1/admin/jobs", params={"page_size": 2}).json()["page"]["next_cursor"]
        assert cursor
        with member_session(
            engine,
            tmp_path,
            client,
            headers,
            label="b18-second-admin",
            system_roles=["SYSTEM_ADMIN"],
        ) as second:
            assert len(_ids(second.get("/v1/admin/jobs", params={"page_size": 2}))) == 2
            foreign = second.get("/v1/admin/jobs", params={"page_size": 2, "cursor": cursor})
            assert foreign.status_code == 400
            assert foreign.json()["code"] == "invalid_cursor"
        with member_session(engine, tmp_path, client, headers) as member:
            before = len(audit_rows(engine, "admin.job.list"))
            assert member.get("/v1/admin/jobs").status_code == 403
            assert member.get(f"/v1/admin/jobs/{new_uuid7()}").status_code == 403
            assert len(audit_rows(engine, "admin.job.list")) == before
    with _client(engine, tmp_path) as anonymous:
        assert anonymous.get("/v1/admin/jobs").status_code == 401
        assert anonymous.get(f"/v1/admin/jobs/{new_uuid7()}").status_code == 401


def test_get_is_read_only_and_does_not_grant_job_control(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    alpha, _, expected = _seed(engine)
    target = next(job for job in expected if job["tenant_id"] == str(alpha["tenant_id"]))
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    with admin_session(engine, tmp_path) as (client, headers):
        event.listen(engine, "before_cursor_execute", capture)
        try:
            response = client.get(f"/v1/admin/jobs/{target['job_id']}")
            listing = client.get("/v1/admin/jobs", params={"page_size": 1})
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert response.status_code == 200, response.text
        assert listing.status_code == 200, listing.text
        body = response.json()
        assert body["job_id"] == target["job_id"]
        assert body["tenant_id"] == target["tenant_id"]
        assert response.headers["etag"] == strong_etag(body["version"])
        assert "X-Nexa-Tenant-Id" not in response.request.headers

        assert not any(
            "FOR UPDATE" in sql.upper() or "FOR SHARE" in sql.upper()
            for sql in statements
            if "jobs" in sql
        )
        writes = {
            sql.split()[1 if sql.lstrip().upper().startswith("UPDATE") else 2]
            for sql in statements
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        }
        assert writes <= {"audit_records", "browser_sessions"}, writes

        records = audit_rows(engine, "admin.job.get")
        assert len(records) == 1
        assert records[0]["target_type"] == "JOB"
        assert records[0]["target_id"] == target["job_id"]
        assert str(records[0]["tenant_id"]) == target["tenant_id"]

        missing = client.get(f"/v1/admin/jobs/{new_uuid7()}")
        assert missing.status_code == 404
        assert missing.json()["code"] == "resource_not_found"
        assert client.get("/v1/admin/jobs/not-a-uuid").status_code == 400
        assert len(audit_rows(engine, "admin.job.get")) == 1

        # Reading every tenant's queue grants no control: cancel still needs membership.
        cancel = client.post(
            f"/v1/jobs/{target['job_id']}/cancel",
            headers={
                **headers,
                "X-Nexa-Tenant-Id": target["tenant_id"],
                "Idempotency-Key": "b18-admin-cancel-attempt",
                "If-Match": response.headers["etag"],
            },
            json={"reason": "admin without membership"},
        )
        assert cancel.status_code == 403, cancel.text
    with engine.connect() as connection:
        row = connection.execute(
            select(s.jobs.c.state, s.jobs.c.desired_state, s.jobs.c.version).where(
                s.jobs.c.job_id == target["job_id"]
            )
        ).one()
    assert (row.state, row.version) == (target["state"], body["version"])
    assert row.desired_state == body["desired_state"]
