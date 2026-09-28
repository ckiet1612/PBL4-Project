"""B16 submitSweep/getSweep: per-child admission, partial acceptance, resumable replay."""

import math
import threading
import time
from decimal import Decimal
from hashlib import sha256

import pytest
from sqlalchemy import func, select, text, update

from nexa.application import sweep_expansion
from nexa.application.sweep_service import SweepService
from nexa.coordinator.retention import expired_records
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration.test_control_b15 import _set_mode
from tests.integration.test_jobs_b08 import _bootstrap_member
from tests.integration.test_templates_b16 import _register_all

pytestmark = pytest.mark.postgres

ORIGIN = "https://nexa.test"
BASE_PARAMETERS = {
    "epochs": 1,
    "batch_size": 64,
    "learning_rate": 0.05,
    "seed": 7,
    "subset_size": 500,
}
DIMENSIONS = [
    {"name": "learning_rate", "values": [0.1, 0.01, 0.001]},
    {"name": "seed", "values": [1, 2]},
]


class Crash(RuntimeError):
    """Injected process loss between two child transactions."""


def _login(client):
    login = client.post(
        "/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"username": "b08-admin@example.test", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200, login.text
    return {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}


def _members(client, engine):
    """One user who is a MEMBER of two tenants that fit the base spec resources."""
    admin_id, csrf = _bootstrap_member(client)
    write = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    tenant_ids = []
    for slug in ("b16-sweep", "b16-sweep-other"):
        tenant = client.post(
            "/v1/admin/tenants",
            headers={**write, "Idempotency-Key": f"b16-create-{slug}"},
            json={"slug": slug, "display_name": slug},
        )
        assert tenant.status_code == 201, tenant.text
        tenant_ids.append(tenant.json()["tenant_id"])
    for tenant_id in tenant_ids:
        membership = client.post(
            f"/v1/admin/tenants/{tenant_id}/memberships",
            headers={**write, "Idempotency-Key": f"b16-member-{tenant_id}", "If-Match": '"v1"'},
            json={"user_id": admin_id, "role": "MEMBER"},
        )
        assert membership.status_code == 200, membership.text
        # A membership change revokes the session; log in again.
        write = _login(client)
        _policy(engine, tenant_id, cpu_limit_millis=8000, memory_limit_bytes=8 * 1024**3)
    return tenant_ids, write


def _dataset(engine, tenant_id):
    artifact_id = new_uuid7()
    with engine.begin() as connection:
        connection.execute(
            s.artifacts.insert().values(
                artifact_id=artifact_id,
                tenant_id=tenant_id,
                kind="DATASET",
                media_type="application/vnd.apache.arrow.file",
                size_bytes=7,
                checksum=f"sha256:{sha256(artifact_id.bytes).hexdigest()}",
                blob_key=f"tenant/{tenant_id}/blob/{artifact_id}",
                state="COMMITTED",
                version=1,
            )
        )
    return str(artifact_id)


def _request(artifact_id, dimensions=DIMENSIONS, **parameters):
    return {
        "base_spec": {
            "template_id": "pytorch-cifar10-cnn",
            "template_version": 1,
            "input_artifact_id": artifact_id,
            "resources": {"cpu_millis": 2000, "memory_bytes": 1024**3, "gpu_count": 0},
            "priority": 1,
            "runtime_limit_seconds": 120,
            "checkpoint_interval_seconds": 10,
            "parameters": {**BASE_PARAMETERS, **parameters},
        },
        "dimensions": dimensions,
    }


def _policy(engine, tenant_id, **values):
    with engine.begin() as connection:
        connection.execute(
            update(s.tenant_policies)
            .where(
                s.tenant_policies.c.tenant_id == tenant_id,
                s.tenant_policies.c.is_current.is_(True),
            )
            .values(**values)
        )


def _count(engine, table, *where):
    with engine.connect() as connection:
        return connection.execute(
            select(func.count()).select_from(table).where(*where)
        ).scalar_one()


def _admission_state(engine, tenant_id):
    """Counters and rate buckets that a child admission spends."""
    with engine.connect() as connection:
        counters = connection.execute(
            text(
                "SELECT scope_type, outstanding, version FROM admission_counters "
                "WHERE scope_type = 'GLOBAL' OR scope_id LIKE :tenant ORDER BY scope_type"
            ),
            {"tenant": f"{tenant_id}%"},
        ).all()
        buckets = connection.execute(
            text(
                "SELECT scope_type, version FROM rate_buckets "
                "WHERE scope_id LIKE :tenant ORDER BY scope_type"
            ),
            {"tenant": f"{tenant_id}%"},
        ).all()
    return [tuple(row) for row in counters], [tuple(row) for row in buckets]


def _post(client, headers, tenant_id, key, body):
    return client.post(
        "/v1/sweeps",
        headers={**headers, "X-Nexa-Tenant-Id": tenant_id, "Idempotency-Key": key},
        json=body,
    )


def _crash_after(monkeypatch, admitted):
    """Fail the child transaction that would follow `admitted` completed children."""
    original = SweepService._admit_child
    calls = {"count": 0}

    def admit_child(self, *args, **kwargs):
        if calls["count"] == admitted:
            raise Crash("injected crash between child transactions")
        calls["count"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SweepService, "_admit_child", admit_child)


def _crashed_post(client, headers, tenant_id, key, body):
    try:
        response = _post(client, headers, tenant_id, key, body)
    except Crash:
        return None
    assert response.status_code == 500, response.text
    return response


def _expected_expansion():
    children = sweep_expansion.expand(
        BASE_PARAMETERS, [(d["name"], d["values"]) for d in DIMENSIONS]
    )
    return [sweep_expansion.parameter_hash(child) for child in children]


def test_sweep_admits_each_child_and_replays_the_same_mapping(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, _other), write = _members(client, engine)
        body = _request(_dataset(engine, tenant_id))
        first = _post(client, write, tenant_id, "b16-sweep-happy-0001", body)
        assert first.status_code == 207, first.text
        sweep = first.json()
        assert first.headers["location"] == f"/v1/sweeps/{sweep['sweep_id']}"
        assert (sweep["child_count"], sweep["accepted_count"], sweep["rejected_count"]) == (6, 6, 0)
        assert sweep["tenant_id"] == tenant_id
        assert sweep["page"] == {"next_cursor": None, "page_size": 100}
        children = sweep["children"]
        assert [c["child_index"] for c in children] == list(range(6))
        assert [c["parameter_hash"] for c in children] == _expected_expansion()
        assert {c["status"] for c in children} == {"ACCEPTED"}
        assert all(c["error"] is None for c in children)
        job_ids = [c["job_id"] for c in children]
        assert len(set(job_ids)) == 6

        # Every child is a normal Job with its own session, spec and admission audit.
        with engine.connect() as connection:
            specs = connection.execute(
                select(s.jobs.c.job_id, s.job_specs.c.canonical_spec)
                .join(s.job_specs, s.job_specs.c.job_id == s.jobs.c.job_id)
                .where(s.jobs.c.tenant_id == tenant_id)
            ).all()
            audits = connection.execute(
                select(s.audit_records.c.safe_metadata).where(
                    s.audit_records.c.action == "job.submit"
                )
            ).scalars()
            audit_children = sorted(
                a["child_index"] for a in audits if a.get("sweep_id") == sweep["sweep_id"]
            )
        by_job = {str(job_id): spec["parameters"] for job_id, spec in specs}
        assert set(by_job) == set(job_ids)
        assert [by_job[j]["learning_rate"] for j in job_ids] == [0.1, 0.1, 0.01, 0.01, 0.001, 0.001]
        assert [by_job[j]["seed"] for j in job_ids] == [1, 2, 1, 2, 1, 2]
        assert audit_children == list(range(6))

        # The parent holds no slot: outstanding counts only the six children and no
        # allocation exists for anything.
        counters, buckets = _admission_state(engine, tenant_id)
        assert [outstanding for scope, outstanding, _v in counters if scope != "GLOBAL"] == [6, 6]
        assert _count(engine, s.allocations) == 0

        replay = _post(client, write, tenant_id, "b16-sweep-happy-0001", body)
        assert replay.status_code == 207, replay.text
        assert replay.json() == sweep
        assert replay.headers["location"] == first.headers["location"]
        assert _admission_state(engine, tenant_id) == (counters, buckets)
        assert _count(engine, s.jobs) == 6
        assert _count(engine, s.sweep_parents) == 1

        changed = _request(body["base_spec"]["input_artifact_id"], dimensions=DIMENSIONS[:1])
        conflict = _post(client, write, tenant_id, "b16-sweep-happy-0001", changed)
        assert (conflict.status_code, conflict.json()["code"]) == (409, "idempotency_conflict")


def test_request_level_violations_persist_nothing(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, _other), write = _members(client, engine)
        artifact_id = _dataset(engine, tenant_id)
        cases = [
            (_request(artifact_id, [{"name": "momentum", "values": [0.9]}]), 422),
            (_request(artifact_id, [{"name": "learning_rate", "values": [0.1, 2]}]), 422),
            (_request(artifact_id, [{"name": "epochs", "values": [1, 1.5]}]), 422),
            (
                _request(
                    artifact_id,
                    [
                        {"name": "seed", "values": list(range(11))},
                        {"name": "batch_size", "values": list(range(1, 11))},
                    ],
                ),
                422,
            ),
            (
                _request(
                    artifact_id,
                    [{"name": "seed", "values": [1]}, {"name": "seed", "values": [2]}],
                ),
                422,
            ),
            (_request(artifact_id, [{"name": "Seed", "values": [1]}]), 422),
            ({**_request(artifact_id), "extra": True}, 400),
        ]
        for index, (body, status) in enumerate(cases):
            response = _post(client, write, tenant_id, f"b16-sweep-invalid-{index:04d}", body)
            assert response.status_code == status, (index, response.text)
        with engine.begin() as connection:
            connection.execute(
                update(s.templates)
                .where(s.templates.c.template_id == "pytorch-cifar10-cnn")
                .values(enabled=False)
            )
        unavailable = _post(
            client, write, tenant_id, "b16-sweep-invalid-disabled", _request(artifact_id)
        )
        assert (unavailable.status_code, unavailable.json()["code"]) == (
            422,
            "infeasible_request",
        )
        assert _count(engine, s.sweep_parents) == 0
        assert _count(engine, s.jobs) == 0
        assert (
            _count(
                engine,
                s.idempotency_records,
                s.idempotency_records.c.operation_id.in_(["submitSweep", "submitJob"]),
            )
            == 0
        )
        counters, _buckets = _admission_state(engine, tenant_id)
        assert all(outstanding == 0 for _scope, outstanding, _v in counters)


def test_quota_and_rate_limits_reject_individual_children(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, other_id), write = _members(client, engine)
        _policy(engine, tenant_id, outstanding_limit=2)
        quota = _post(
            client, write, tenant_id, "b16-sweep-quota-0001", _request(_dataset(engine, tenant_id))
        )
        assert quota.status_code == 207, quota.text
        outcomes = quota.json()
        assert (outcomes["accepted_count"], outcomes["rejected_count"]) == (2, 4)
        assert [c["status"] for c in outcomes["children"]] == ["ACCEPTED"] * 2 + ["REJECTED"] * 4
        for child in outcomes["children"][2:]:
            assert child["job_id"] is None
            assert child["error"]["code"] == "quota_exceeded"
            assert child["error"]["request_id"] == quota.headers["x-request-id"]
        assert _count(engine, s.jobs) == 2

        # The other tenant runs out of user rate tokens after three children.
        _policy(
            engine,
            other_id,
            user_rate_burst=Decimal("3"),
            user_rate_per_second=Decimal("0.0001"),
        )
        body = _request(_dataset(engine, other_id))
        rated = _post(client, write, other_id, "b16-sweep-rate-0001", body)
        assert rated.status_code == 207, rated.text
        codes = [c["error"]["code"] if c["error"] else None for c in rated.json()["children"]]
        assert codes == [None, None, None, "rate_limited", "rate_limited", "rate_limited"]
        # Replaying a partially rejected sweep neither re-admits nor spends rate again.
        before = _admission_state(engine, other_id)
        again = _post(client, write, other_id, "b16-sweep-rate-0001", body)
        assert again.status_code == 207, again.text
        assert again.json() == rated.json()
        assert _admission_state(engine, other_id) == before


def test_a_foreign_input_rejects_every_child_without_a_job(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, other_id), write = _members(client, engine)
        foreign = _post(
            client, write, tenant_id, "b16-sweep-foreign-01", _request(_dataset(engine, other_id))
        )
        assert foreign.status_code == 207, foreign.text
        assert foreign.json()["rejected_count"] == 6
        assert {c["error"]["code"] for c in foreign.json()["children"]} == {"resource_not_found"}
        assert _count(engine, s.jobs) == 0


def test_a_crash_between_children_resumes_without_duplicates(
    migrated_postgres_engine, tmp_path, monkeypatch
):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, _other), write = _members(client, engine)
        body = _request(_dataset(engine, tenant_id))
        _crash_after(monkeypatch, 2)
        _crashed_post(client, write, tenant_id, "b16-sweep-crash-0001", body)
        monkeypatch.undo()
        with engine.connect() as connection:
            parent = connection.execute(select(s.sweep_parents)).mappings().one()
            before = connection.execute(
                select(s.sweep_children.c.child_index, s.sweep_children.c.job_id).order_by(
                    s.sweep_children.c.child_index
                )
            ).all()
        assert (parent["accepted_count"], parent["rejected_count"]) == (2, 0)
        assert [index for index, _job in before] == [0, 1]
        partial = client.get(
            f"/v1/sweeps/{parent['sweep_id']}", headers={"X-Nexa-Tenant-Id": tenant_id}
        )
        assert partial.status_code == 200, partial.text
        assert len(partial.json()["children"]) == 2

        resumed = _post(client, write, tenant_id, "b16-sweep-crash-0001", body)
        assert resumed.status_code == 207, resumed.text
        children = resumed.json()["children"]
        assert [c["child_index"] for c in children] == list(range(6))
        assert [c["job_id"] for c in children[:2]] == [str(job) for _i, job in before]
        assert _count(engine, s.jobs) == 6
        counters, _buckets = _admission_state(engine, tenant_id)
        assert [outstanding for scope, outstanding, _v in counters if scope != "GLOBAL"] == [6, 6]


def test_concurrent_replays_admit_each_child_once(migrated_postgres_engine, tmp_path, monkeypatch):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, _other), write = _members(client, engine)
        body = _request(_dataset(engine, tenant_id))
        _crash_after(monkeypatch, 0)
        _crashed_post(client, write, tenant_id, "b16-sweep-race-0001", body)
        monkeypatch.undo()
        assert _count(engine, s.sweep_children) == 0
        results = []
        barrier = threading.Barrier(3)

        def replay():
            barrier.wait()
            results.append(_post(client, write, tenant_id, "b16-sweep-race-0001", body))

        threads = [threading.Thread(target=replay) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert [r.status_code for r in results] == [207, 207, 207], [r.text for r in results]
        assert results[0].json() == results[1].json() == results[2].json()
        assert _count(engine, s.jobs) == 6
        assert _count(engine, s.sweep_children) == 6


def test_operational_modes_gate_new_sweeps_and_resumed_children(
    migrated_postgres_engine, tmp_path, monkeypatch
):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, _other), write = _members(client, engine)
        body = _request(_dataset(engine, tenant_id))
        _set_mode(engine, "ADMISSION_OFF")
        refused = _post(client, write, tenant_id, "b16-sweep-mode-new-01", body)
        assert (refused.status_code, refused.json()["code"]) == (409, "state_conflict")
        assert _count(engine, s.sweep_parents) == 0
        _set_mode(engine, "NORMAL")

        _crash_after(monkeypatch, 2)
        _crashed_post(client, write, tenant_id, "b16-sweep-mode-0001", body)
        monkeypatch.undo()

        # WRITE_FROZEN aborts the request without writing any child outcome.
        _set_mode(engine, "WRITE_FROZEN")
        frozen = _post(client, write, tenant_id, "b16-sweep-mode-0001", body)
        assert (frozen.status_code, frozen.json()["code"]) == (409, "state_conflict")
        assert _count(engine, s.sweep_children) == 2

        # ADMISSION_OFF is an admission outcome of each remaining child.
        _set_mode(engine, "ADMISSION_OFF")
        resumed = _post(client, write, tenant_id, "b16-sweep-mode-0001", body)
        assert resumed.status_code == 207, resumed.text
        outcome = resumed.json()
        assert (outcome["accepted_count"], outcome["rejected_count"]) == (2, 4)
        assert {c["error"]["code"] for c in outcome["children"][2:]} == {"state_conflict"}
        _set_mode(engine, "NORMAL")
        assert _post(client, write, tenant_id, "b16-sweep-mode-0001", body).json() == outcome


def test_get_sweep_pages_by_child_index_within_the_tenant(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, other_id), write = _members(client, engine)
        created = _post(
            client, write, tenant_id, "b16-sweep-page-0001", _request(_dataset(engine, tenant_id))
        )
        sweep_id = created.json()["sweep_id"]
        tenant = {"X-Nexa-Tenant-Id": tenant_id}
        indexes, cursor = [], None
        while True:
            query = {"page_size": 4, **({"cursor": cursor} if cursor else {})}
            page = client.get(f"/v1/sweeps/{sweep_id}", headers=tenant, params=query)
            assert page.status_code == 200, page.text
            assert page.json()["accepted_count"] == 6
            indexes.extend(c["child_index"] for c in page.json()["children"])
            cursor = page.json()["page"]["next_cursor"]
            if cursor is None:
                break
        assert indexes == list(range(6))
        default = client.get(f"/v1/sweeps/{sweep_id}", headers=tenant).json()
        assert default["page"] == {"next_cursor": None, "page_size": 50}
        assert default["children"] == created.json()["children"]

        first = client.get(f"/v1/sweeps/{sweep_id}", headers=tenant, params={"page_size": 1})
        token = first.json()["page"]["next_cursor"]
        other = _post(
            client,
            write,
            tenant_id,
            "b16-sweep-page-0002",
            _request(_dataset(engine, tenant_id), [{"name": "seed", "values": [3, 4]}]),
        ).json()["sweep_id"]
        for bad in (token, "x" * 20):
            target = other if bad == token else sweep_id
            response = client.get(f"/v1/sweeps/{target}", headers=tenant, params={"cursor": bad})
            assert (response.status_code, response.json()["code"]) == (400, "invalid_cursor")

        foreign = client.get(f"/v1/sweeps/{sweep_id}", headers={"X-Nexa-Tenant-Id": other_id})
        assert (foreign.status_code, foreign.json()["code"]) == (404, "resource_not_found")
        missing = client.get(f"/v1/sweeps/{new_uuid7()}", headers=tenant)
        assert missing.status_code == 404
        too_large = client.get(f"/v1/sweeps/{sweep_id}", headers=tenant, params={"page_size": 101})
        assert too_large.status_code == 422


def test_cli_tokens_need_the_exact_jobs_scope(migrated_postgres_engine, tmp_path):
    from tests.integration.test_templates_b16 import _token

    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, _other), write = _members(client, engine)
        body = _request(_dataset(engine, tenant_id))
        read = _token(client, write, "jobs:read")
        submit = _token(client, write, "jobs:write")
        artifacts = _token(client, write, "artifacts:read")
        with _client(engine, tmp_path) as cli:
            cli.cookies.clear()
            denied = _post(cli, read, tenant_id, "b16-sweep-scope-0001", body)
            assert (denied.status_code, denied.json()["code"]) == (403, "permission_denied")
            created = _post(cli, submit, tenant_id, "b16-sweep-scope-0002", body)
            assert created.status_code == 207, created.text
            path = f"/v1/sweeps/{created.json()['sweep_id']}"
            tenant = {"X-Nexa-Tenant-Id": tenant_id}
            assert cli.get(path, headers={**read, **tenant}).status_code == 200
            assert cli.get(path, headers={**artifacts, **tenant}).status_code == 403
            assert cli.get(path, headers=tenant).status_code == 401
        assert _count(engine, s.sweep_parents) == 1


def test_retention_keeps_a_sweep_record_until_every_child_record_is_gone(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, _other), write = _members(client, engine)
        created = _post(
            client, write, tenant_id, "b16-sweep-keep-0001", _request(_dataset(engine, tenant_id))
        )
        assert created.status_code == 207, created.text
    records = s.idempotency_records
    with engine.begin() as connection:
        connection.execute(update(records).values(expires_at=func.now() - text("interval '1 day'")))
    factory = create_session_factory(engine)

    def due():
        with factory.begin() as session:
            return set(expired_records(session))

    parent_record = select(records.c.idempotency_id).where(records.c.operation_id == "submitSweep")
    with engine.connect() as connection:
        parent_id = connection.execute(parent_record).scalar_one()
    # Queued children are not terminal; neither their records nor the parent are due.
    assert due() == set()
    with engine.begin() as connection:
        connection.execute(records.delete().where(records.c.operation_id == "submitJob"))
    assert due() == {parent_id}


def test_one_hundred_child_sweep_latency(migrated_postgres_engine, tmp_path, capsys):
    """Report-only latency of a 100-child sweep (B16 evidence); asserts correctness only."""
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, _other), write = _members(client, engine)
        _policy(
            engine,
            tenant_id,
            tenant_rate_burst=Decimal("1000"),
            user_rate_burst=Decimal("1000"),
        )
        artifact_id = _dataset(engine, tenant_id)
        dimensions = [
            {"name": "seed", "values": list(range(10))},
            {"name": "batch_size", "values": list(range(16, 26))},
        ]
        durations = []
        for run in range(5):
            started = time.perf_counter()
            response = _post(
                client,
                write,
                tenant_id,
                f"b16-sweep-latency-{run:04d}",
                _request(artifact_id, dimensions),
            )
            durations.append(time.perf_counter() - started)
            assert response.status_code == 207, response.text
            assert response.json()["accepted_count"] == 100
    assert _count(engine, s.jobs) == 500
    ordered = sorted(durations)
    # Nearest-rank percentiles; with five runs p95 is the slowest run.
    p50 = ordered[math.ceil(0.50 * len(ordered)) - 1]
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
    with capsys.disabled():
        print(
            f"\nB16 100-child sweep latency over {len(ordered)} runs (nearest rank): "
            f"p50={p50:.3f}s p95={p95:.3f}s min={ordered[0]:.3f}s"
        )
