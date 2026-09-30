"""B16-R04: losing authorization in the middle of a sweep aborts it, fail closed.

workloads-checkpoints.md, "Partial acceptance and replay": authorization is a
precondition of the request, not a child outcome. A revoked principal writes
nothing more, not even REJECTED, so a temporary credential state is never frozen
into the immutable mapping; ACCEPTED children stay, and a replay of the same key
by a permitted principal resumes the unfinished indexes (step 4).
"""

import pytest
from sqlalchemy import delete, func, insert, select, update

from nexa.application.sweep_service import SweepService
from nexa.infrastructure.persistence import schema as s
from tests.api.test_http_contract import _client
from tests.integration.test_sweep_b16 import _count, _dataset, _login, _members, _post, _request
from tests.integration.test_templates_b16 import _register_all

pytestmark = pytest.mark.postgres

KEY = "rem-r04-sweep-0001"


def _mapping(engine):
    with engine.connect() as connection:
        parent = connection.execute(select(s.sweep_parents)).mappings().one()
        children = connection.execute(
            select(s.sweep_children.c.child_index, s.sweep_children.c.job_id).order_by(
                s.sweep_children.c.child_index
            )
        ).all()
    return parent, children


@pytest.mark.parametrize(
    ("revocation", "status", "code"),
    [("session", 401, "authentication_required"), ("membership", 403, "permission_denied")],
)
def test_authorization_lost_mid_sweep_aborts_without_an_outcome_and_a_permitted_replay_resumes(
    migrated_postgres_engine, tmp_path, monkeypatch, revocation, status, code
):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, _other), write = _members(client, engine)
        body = _request(_dataset(engine, tenant_id))
        with engine.connect() as connection:
            membership = (
                connection.execute(
                    select(s.memberships).where(s.memberships.c.tenant_id == tenant_id)
                )
                .mappings()
                .one()
            )

        def revoke():
            with engine.begin() as connection:
                if revocation == "session":
                    connection.execute(
                        update(s.browser_sessions)
                        .where(s.browser_sessions.c.user_id == membership["user_id"])
                        .values(revoked_at=func.now())
                    )
                else:
                    connection.execute(
                        delete(s.memberships).where(
                            s.memberships.c.tenant_id == tenant_id,
                            s.memberships.c.user_id == membership["user_id"],
                        )
                    )

        original = SweepService._admit_child
        calls = {"count": 0}

        def admit_child(self, *args, **kwargs):
            # Revoked between the second and the third child transaction.
            if calls["count"] == 2:
                revoke()
            calls["count"] += 1
            return original(self, *args, **kwargs)

        monkeypatch.setattr(SweepService, "_admit_child", admit_child)
        aborted = _post(client, write, tenant_id, KEY, body)
        monkeypatch.undo()
        assert (aborted.status_code, aborted.json()["code"]) == (status, code), aborted.text
        parent, before = _mapping(engine)
        # The two ACCEPTED children stay; no REJECTED outcome records the revocation.
        assert (parent["child_count"], parent["accepted_count"], parent["rejected_count"]) == (
            6,
            2,
            0,
        )
        assert [index for index, _job in before] == [0, 1]
        assert all(job is not None for _index, job in before)
        assert _count(engine, s.jobs) == 2

        # The revoked principal cannot write through a replay either.
        denied = _post(client, write, tenant_id, KEY, body)
        assert (denied.status_code, denied.json()["code"]) == (status, code), denied.text
        assert _mapping(engine) == (parent, before)

        if revocation == "session":
            write = _login(client)
        else:
            with engine.begin() as connection:
                connection.execute(insert(s.memberships).values(**membership))
        resumed = _post(client, write, tenant_id, KEY, body)
        assert resumed.status_code == 207, resumed.text
        assert resumed.json()["sweep_id"] == str(parent["sweep_id"])
        children = resumed.json()["children"]
        assert [child["child_index"] for child in children] == list(range(6))
        assert all(child["status"] == "ACCEPTED" for child in children)
        assert [child["job_id"] for child in children[:2]] == [str(job) for _i, job in before]
        assert _count(engine, s.jobs) == 6
