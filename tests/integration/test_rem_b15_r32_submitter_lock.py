"""Remediation B15-R32: a request's principal lock never blocks a foreign-key check.

Every request transaction first revalidates its principal, locking the credential and
``users`` rows, and only then takes policy, counter and job locks. The coordinator
holds policy, counter and job locks when a retry promotion (or a dispatch) inserts the
B13 ``queue_submitters`` row, whose foreign key takes ``KEY SHARE`` on the submitter's
``users`` row. With the principal locked ``FOR UPDATE`` that ``KEY SHARE`` waited behind
the request, and the request waited for the job: deadlock, or the coordinator's 1 s
``lock_timeout``. No request changes ``users.user_id``, so the principal lock is
``FOR NO KEY UPDATE``: it still excludes every other request and every user update,
but not a foreign-key check.
"""

import os
from datetime import timedelta

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from nexa.domain.identity import CredentialKind, Principal
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.integration.identity_support import make_identity_service
from tests.integration.test_coordinator_b11 import seed_dispatchable
from tests.integration.test_retry_b14 import _global_counter, _job, _leader, _retry_wait

pytestmark = pytest.mark.postgres


def _credential(engine, user_id, kind):
    """Insert a live credential of `kind` for `user_id`; return its principal."""
    credential_id = new_uuid7()
    with engine.begin() as connection:
        now = connection.execute(select(func.clock_timestamp())).scalar_one()
        if kind is CredentialKind.BROWSER:
            connection.execute(
                insert(s.browser_sessions).values(
                    browser_session_id=credential_id,
                    user_id=user_id,
                    secret_hash=os.urandom(32),
                    csrf_secret_hash=os.urandom(32),
                    expires_at=now + timedelta(hours=1),
                    last_seen_at=now,
                )
            )
        else:
            connection.execute(
                insert(s.cli_tokens).values(
                    token_id=credential_id,
                    user_id=user_id,
                    token_hash=os.urandom(32),
                    name="rem-b15-r32",
                    scopes=["jobs:write"],
                    expires_at=now + timedelta(hours=1),
                )
            )
    return Principal(
        user_id=user_id,
        credential_kind=kind,
        credential_id=str(credential_id),
        scopes=frozenset(),
        memberships=(),
        system_admin=False,
    )


def _request(engine, identity, principal):
    """Open a request transaction that has revalidated its principal, as every route does."""
    session = Session(engine)
    session.begin()
    identity.revalidate_principal(session, principal)
    return session


def _submitters(engine):
    with engine.connect() as connection:
        return connection.execute(
            select(s.queue_submitters.c.tenant_id, s.queue_submitters.c.user_id)
        ).all()


@pytest.mark.parametrize("kind", [CredentialKind.BROWSER, CredentialKind.CLI])
def test_retry_promotion_does_not_wait_for_a_request_holding_its_submitter(
    migrated_postgres_engine, tmp_path, kind
):
    engine = migrated_postgres_engine
    _, _, (job_id,) = seed_dispatchable(engine)
    _retry_wait(engine, job_id, ready_in=timedelta(seconds=-1))
    job = _job(engine, job_id)
    assert _submitters(engine) == []
    identity = make_identity_service(engine, tmp_path)
    principal = _credential(engine, job["submitter_user_id"], kind)
    before = _global_counter(engine)
    service, epoch = _leader(engine)

    request = _request(engine, identity, principal)
    try:
        # The coordinator runs with its 1 s lock_timeout while the request holds the
        # principal lock; before the fix this raised LockNotAvailable.
        assert service.promote_retries(epoch) == 1
        # The request then takes the job lock (as cancel does) without a cycle.
        locked = request.execute(
            select(s.jobs.c.state).where(s.jobs.c.job_id == job_id).with_for_update()
        ).scalar_one()
        assert locked == "QUEUED"
    finally:
        request.rollback()
        request.close()

    promoted = _job(engine, job_id)
    assert (promoted["state"], promoted["retry_count"], promoted["job_fence"]) == ("QUEUED", 1, 2)
    assert _submitters(engine) == [(job["tenant_id"], job["submitter_user_id"])]
    after = _global_counter(engine)
    assert (after["outstanding"], after["active_attempts"]) == (
        before["outstanding"],
        before["active_attempts"],
    )
    with engine.connect() as connection:
        closed = connection.execute(
            select(s.retry_schedules.c.closed_at).where(s.retry_schedules.c.job_id == job_id)
        ).scalar_one()
    assert closed is not None


@pytest.mark.parametrize("kind", [CredentialKind.BROWSER, CredentialKind.CLI])
def test_principal_lock_still_excludes_user_updates_and_other_requests(
    migrated_postgres_engine, tmp_path, kind
):
    engine = migrated_postgres_engine
    _, _, (job_id,) = seed_dispatchable(engine)
    user_id = _job(engine, job_id)["submitter_user_id"]
    identity = make_identity_service(engine, tmp_path)
    principal = _credential(engine, user_id, kind)

    def blocked(statement):
        with Session(engine) as other, other.begin():
            other.execute(text("set local lock_timeout = '200ms'"))
            with pytest.raises(OperationalError) as caught:
                statement(other)
            assert caught.value.orig.sqlstate == "55P03"

    request = _request(engine, identity, principal)
    try:
        # An administrator disabling the user waits for the request, so the request
        # linearizes wholly before the disable.
        blocked(
            lambda other: other.execute(
                update(s.users).where(s.users.c.user_id == user_id).values(enabled=False)
            )
        )
        # A second request of the same principal still serializes behind the first.
        blocked(lambda other: identity.revalidate_principal(other, principal))
    finally:
        request.rollback()
        request.close()
    with Session(engine) as session, session.begin():
        assert identity.revalidate_principal(session, principal).user_id == user_id
