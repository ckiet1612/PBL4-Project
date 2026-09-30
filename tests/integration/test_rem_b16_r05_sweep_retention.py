"""B16-R05: a sweep's replay record is kept as long as the longer of parent and child retention.

contracts.md "Idempotency" step 8: retention lasts while a resource is active and at
least 30 days after terminal; the sweep parent/child mapping is kept for the longer
time. Time is simulated by moving every remaining record past its `expires_at`, so the
test holds whatever the sweep writes in between.
"""

from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update

from nexa.infrastructure.persistence import schema as s
from tests.api.test_http_contract import _client
from tests.integration.test_retry_b14 import _leader
from tests.integration.test_sweep_b16 import (
    _crash_after,
    _crashed_post,
    _dataset,
    _members,
    _policy,
    _post,
    _request,
)
from tests.integration.test_templates_b16 import _register_all

pytestmark = pytest.mark.postgres

records = s.idempotency_records


def _expire_everything(engine):
    with engine.begin() as connection:
        connection.execute(update(records).values(expires_at=func.now() - text("interval '1 day'")))


def _sweep(service, epoch):
    """Enough ticks for the parent to follow its children (walks are ordered, one batch each)."""
    return sum(service.sweep_idempotency(epoch) for _ in range(3))


def _record_of(engine, operation, resource_id):
    with engine.connect() as connection:
        return connection.execute(
            select(records.c.idempotency_id).where(
                records.c.operation_id == operation, records.c.resource_id == resource_id
            )
        ).scalar_one_or_none()


def _set_terminal(engine, job_ids):
    with engine.begin() as connection:
        connection.execute(
            update(s.jobs)
            .where(s.jobs.c.job_id.in_(job_ids))
            .values(state="CANCELLED", desired_state="CANCELLED", terminal_at=func.now())
        )


def test_sweep_parent_record_outlives_every_child_record_and_an_unfinished_sweep_stays(
    migrated_postgres_engine, tmp_path, monkeypatch
):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, _other), write = _members(client, engine)
        _policy(
            engine,
            tenant_id,
            outstanding_limit=100,
            user_outstanding_limit=100,
            tenant_rate_burst=Decimal("1000"),
            user_rate_burst=Decimal("1000"),
        )
        body = _request(_dataset(engine, tenant_id))
        finished = _post(client, write, tenant_id, "rem-r05-finished-0001", body)
        assert finished.status_code == 207, finished.text
        sweep_id = finished.json()["sweep_id"]
        children = [child["job_id"] for child in finished.json()["children"]]
        assert len(children) == 6
        _crash_after(monkeypatch, 2)
        _crashed_post(client, write, tenant_id, "rem-r05-unfinished-0001", body)
        monkeypatch.undo()
        with engine.connect() as connection:
            unfinished_id, accepted = connection.execute(
                select(s.sweep_parents.c.sweep_id, s.sweep_parents.c.accepted_count).where(
                    s.sweep_parents.c.sweep_id != sweep_id
                )
            ).one()
        assert accepted == 2
        parent = _record_of(engine, "submitSweep", sweep_id)
        unfinished = _record_of(engine, "submitSweep", unfinished_id)
        assert parent is not None and unfinished is not None
        service, epoch = _leader(engine)

        # Children still live: nothing is due, however old the records are.
        _expire_everything(engine)
        assert _sweep(service, epoch) == 0
        assert all(_record_of(engine, "submitJob", child) for child in children)

        # Five children past their terminal retention; one child record remains.
        _set_terminal(engine, children[:5])
        _expire_everything(engine)
        assert _sweep(service, epoch) == 5
        assert [_record_of(engine, "submitJob", child) for child in children[:5]] == [None] * 5
        assert _record_of(engine, "submitJob", children[5]) is not None
        assert _record_of(engine, "submitSweep", sweep_id) == parent

        # The last child record goes, then the parent record; the mapping stays readable.
        _set_terminal(engine, children[5:])
        _expire_everything(engine)
        assert _sweep(service, epoch) == 2
        assert _record_of(engine, "submitSweep", sweep_id) is None
        read = client.get(f"/v1/sweeps/{sweep_id}", headers={"X-Nexa-Tenant-Id": tenant_id})
        assert read.status_code == 200, read.text
        assert [child["job_id"] for child in read.json()["children"]] == children

        # An unfinished sweep is never swept, so the same key still resumes it.
        assert _record_of(engine, "submitSweep", unfinished_id) == unfinished
        resumed = _post(client, write, tenant_id, "rem-r05-unfinished-0001", body)
        assert resumed.status_code == 207, resumed.text
        assert resumed.json()["sweep_id"] == str(unfinished_id)
        assert [c["child_index"] for c in resumed.json()["children"]] == list(range(6))
