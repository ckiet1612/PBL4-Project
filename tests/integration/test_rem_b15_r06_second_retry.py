"""B15-R06 preparation, OD-1 open: a second manual retry of one source Job with a new key.

This records the current behaviour the owner decides on; it does not choose an option.
Option A keeps it: each new key is one more quota-bounded Job with the same
``retry_of_job_id``, and "no second lineage" (concurrency-recovery.md) is about replaying
one request. Option B makes the second request ``409 state_conflict`` naming the existing
retry, with a migration (unique ``retry_of_job_id``) and a concurrent test. The chosen
option turns this file into its regression test.
"""

from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from nexa.infrastructure.persistence import schema as s
from tests.integration.test_control_b15 import (
    _fail_to_terminal,
    _job,
    _retry_body,
    _retry_control,
)

pytestmark = pytest.mark.postgres


def _retries_of(engine, source_id):
    with engine.connect() as connection:
        return connection.execute(
            select(func.count()).select_from(s.jobs).where(s.jobs.c.retry_of_job_id == source_id)
        ).scalar_one()


def test_second_manual_retry_with_a_new_key_is_one_more_quota_bounded_job(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with _retry_control(engine, tmp_path, "rem-r06") as control:
        source_id = control.job.job_id
        _fail_to_terminal(control)
        source = _job(engine, source_id)

        first = control.control("retry", key="rem-r06-key-0001", body=_retry_body())
        assert first.status_code == 202, first.text
        second = control.control("retry", key="rem-r06-key-0002", body=_retry_body())
        assert second.status_code == 202, second.text
        first_id, second_id = UUID(first.json()["job_id"]), UUID(second.json()["job_id"])
        assert len({source_id, first_id, second_id}) == 3
        assert first.json()["retry_of_job_id"] == second.json()["retry_of_job_id"] == str(source_id)
        assert _retries_of(engine, source_id) == 2
        # Each new Job took an admission; the terminal source is untouched.
        assert control.counter_values()["GLOBAL"] == (2, 0)
        assert _job(engine, source_id)["version"] == source["version"]

        # A replay of either request returns its own Job, never a third one.
        for key, response in (("rem-r06-key-0001", first), ("rem-r06-key-0002", second)):
            replay = control.control("retry", key=key, if_match='"v999"', body=_retry_body())
            assert (replay.status_code, replay.json()) == (202, response.json())
        assert _retries_of(engine, source_id) == 2

        # The branches are bounded by the ordinary outstanding quota.
        with engine.begin() as connection:
            connection.execute(
                update(s.tenant_policies)
                .where(s.tenant_policies.c.tenant_id == control.tenant_id)
                .values(user_outstanding_limit=2)
            )
        third = control.control("retry", key="rem-r06-key-0003", body=_retry_body())
        assert third.status_code == 429 and third.json()["code"] == "quota_exceeded", third.text
        assert _retries_of(engine, source_id) == 2
        assert control.counter_values()["GLOBAL"] == (2, 0)
