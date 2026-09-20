from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from nexa.application.errors import ApplicationError
from nexa.application.idempotency import begin_idempotency, complete_idempotency
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.transactions import run_transaction

pytestmark = pytest.mark.postgres


def test_completed_idempotency_replays_exact_secret_free_response(
    migrated_postgres_engine,
) -> None:
    factory = create_session_factory(migrated_postgres_engine)
    expires_at = datetime.now(UTC) + timedelta(days=30)

    def create(session: Session):
        outcome = begin_idempotency(
            session,
            context="GLOBAL",
            principal_id="user-1",
            operation_id="adminCreateTenant",
            key="0123456789abcdef",
            request_hash="sha256:" + "a" * 64,
            expires_at=expires_at,
            pending_wait_milliseconds=5_000,
        )
        assert outcome.replay is None
        complete_idempotency(
            session,
            outcome.record_id,
            status=201,
            body={"tenant_id": "tenant-1"},
            headers={"Location": "/v1/admin/tenants/tenant-1"},
        )

    run_transaction(factory, create)

    def replay(session: Session):
        return begin_idempotency(
            session,
            context="GLOBAL",
            principal_id="user-1",
            operation_id="adminCreateTenant",
            key="0123456789abcdef",
            request_hash="sha256:" + "a" * 64,
            expires_at=expires_at,
            pending_wait_milliseconds=5_000,
        ).replay

    stored = run_transaction(factory, replay)
    assert stored is not None
    assert stored.status == 201
    assert stored.body == {"tenant_id": "tenant-1"}
    assert stored.headers == {"Location": "/v1/admin/tenants/tenant-1"}


def test_same_key_different_hash_conflicts_without_overwriting_response(
    migrated_postgres_engine,
) -> None:
    factory = create_session_factory(migrated_postgres_engine)
    expires_at = datetime.now(UTC) + timedelta(days=30)

    def create(session: Session):
        outcome = begin_idempotency(
            session,
            context="GLOBAL",
            principal_id="user-1",
            operation_id="adminCreateTenant",
            key="0123456789abcdef",
            request_hash="sha256:" + "a" * 64,
            expires_at=expires_at,
            pending_wait_milliseconds=5_000,
        )
        complete_idempotency(session, outcome.record_id, status=201, body={}, headers={})

    run_transaction(factory, create)

    def conflict(session: Session):
        return begin_idempotency(
            session,
            context="GLOBAL",
            principal_id="user-1",
            operation_id="adminCreateTenant",
            key="0123456789abcdef",
            request_hash="sha256:" + "b" * 64,
            expires_at=expires_at,
            pending_wait_milliseconds=5_000,
        )

    with pytest.raises(ApplicationError) as exc_info:
        run_transaction(factory, conflict)
    assert exc_info.value.code == "idempotency_conflict"
    assert exc_info.value.status == 409


def test_one_time_secret_replay_returns_locator_without_secret(
    migrated_postgres_engine,
) -> None:
    factory = create_session_factory(migrated_postgres_engine)
    expires_at = datetime.now(UTC) + timedelta(days=30)

    def create(session: Session):
        outcome = begin_idempotency(
            session,
            context="GLOBAL",
            principal_id="user-1",
            operation_id="createCliToken",
            key="0123456789abcdef",
            request_hash="sha256:" + "c" * 64,
            expires_at=expires_at,
            pending_wait_milliseconds=5_000,
        )
        complete_idempotency(
            session,
            outcome.record_id,
            status=201,
            body={"token_id": "token-1"},
            headers={"Location": "/v1/tokens/token-1"},
            one_time_secret=True,
        )

    run_transaction(factory, create)

    with pytest.raises(ApplicationError) as exc_info:
        run_transaction(factory, create)
    assert exc_info.value.code == "one_time_secret_unavailable"
    assert exc_info.value.status == 409
    assert exc_info.value.location == "/v1/tokens/token-1"
