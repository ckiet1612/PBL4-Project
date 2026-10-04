"""B19-RV11: checkpoint age restarts when a resumed attempt starts, not at the old checkpoint."""

import time

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from nexa.api.collectors import _checkpoint, read_only_transaction
from nexa.infrastructure.persistence import schema as s
from tests.api.test_http_contract import _client
from tests.integration.test_checkpoint_b14 import CheckpointFixture
from tests.integration.test_checkpoint_restore_b14 import _claim, _commit, _next_attempt, _start

pytestmark = pytest.mark.postgres


@pytest.fixture
def checkpoint_job(migrated_postgres_engine, tmp_path):
    with _client(migrated_postgres_engine, tmp_path) as client:
        yield CheckpointFixture(migrated_postgres_engine, client, label="age")


def _age_and_reference(engine, attempt_id):
    """(collector age, now - reference) in one read-only snapshot of the database."""
    with Session(engine) as session, session.begin():
        read_only_transaction(session, 2000)
        (family,) = _checkpoint(session)
        started, newest, now = session.execute(
            select(
                s.attempts.c.started_at,
                select(text("max(created_at)")).select_from(s.checkpoints).scalar_subquery(),
                text("now()"),
            ).where(s.attempts.c.attempt_id == attempt_id)
        ).one()
    (sample,) = family.samples
    return sample.value, started, newest, now


def test_age_counts_from_the_later_of_the_last_checkpoint_and_the_attempt_start(checkpoint_job):
    fixture = checkpoint_job
    _commit(fixture, step=20, accumulator=111)
    age, started, newest, now = _age_and_reference(fixture.engine, fixture.authority["attempt_id"])
    assert newest > started  # the checkpoint is the later reference
    assert age == pytest.approx((now - newest).total_seconds(), abs=0.01)

    time.sleep(1.2)  # the inherited checkpoint gets measurably older than the next start
    _next_attempt(fixture)
    claimed = _claim(fixture)
    assert claimed.status_code == 200, claimed.text
    started_response = _start(fixture, claimed.json()["execution_context"])
    assert started_response.status_code == 200, started_response.text

    age, started, newest, now = _age_and_reference(fixture.engine, fixture.authority["attempt_id"])
    assert age == pytest.approx((now - started).total_seconds(), abs=0.01)
    assert age < (now - newest).total_seconds() - 1.0
