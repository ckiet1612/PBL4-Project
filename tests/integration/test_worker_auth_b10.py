from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from nexa.application.errors import ApplicationError
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.schema import worker_credentials
from tests.integration.identity_support import (
    FINGERPRINT,
    INSTALLATION_ID,
    WORKER_ID,
    make_identity_service,
)

pytestmark = pytest.mark.postgres


def _bootstrap_worker(service) -> dict:
    return service.bootstrap_worker(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        installation_id=INSTALLATION_ID,
        credential_public_fingerprint=FINGERPRINT,
        idempotency_key="b10-worker-bootstrap-0001",
        request_hash="sha256:" + "a" * 64,
    )


def test_worker_credential_is_revalidated_inside_the_callers_transaction(
    migrated_postgres_engine, tmp_path
) -> None:
    service = make_identity_service(migrated_postgres_engine, tmp_path)
    created = _bootstrap_worker(service)
    sessions = create_session_factory(migrated_postgres_engine)

    with sessions.begin() as session:
        worker_id = service.revalidate_worker_credential(
            session,
            created["credential"],
            expected_worker_id=WORKER_ID,
        )
        assert worker_id == WORKER_ID

    with migrated_postgres_engine.begin() as connection:
        credential_id = connection.execute(
            select(worker_credentials.c.credential_id).where(
                worker_credentials.c.worker_id == WORKER_ID,
                worker_credentials.c.revoked_at.is_(None),
            )
        ).scalar_one()
        connection.execute(
            update(worker_credentials)
            .where(worker_credentials.c.credential_id == credential_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    with sessions.begin() as session, pytest.raises(ApplicationError) as expired:
        service.revalidate_worker_credential(
            session,
            created["credential"],
            expected_worker_id=WORKER_ID,
        )
    assert expired.value.status == 401


def test_worker_credential_rejects_a_different_worker_path(
    migrated_postgres_engine, tmp_path
) -> None:
    service = make_identity_service(migrated_postgres_engine, tmp_path)
    created = _bootstrap_worker(service)
    sessions = create_session_factory(migrated_postgres_engine)

    with sessions.begin() as session, pytest.raises(ApplicationError) as forged:
        service.revalidate_worker_credential(
            session,
            created["credential"],
            expected_worker_id=INSTALLATION_ID,
        )
    assert forged.value.status == 401
