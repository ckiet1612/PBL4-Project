import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import event, select

from nexa.application.artifact_service import ArtifactService
from nexa.application.errors import ApplicationError
from nexa.domain.identity import CredentialKind, Principal, TenantMembership
from nexa.infrastructure.artifacts.store import ArtifactError, FilesystemArtifactStore
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema import (
    artifact_storage_counters,
    artifacts,
    idempotency_records,
    membership_sets,
    tenants,
    upload_sessions,
)

pytestmark = pytest.mark.postgres


def _checksum(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _settings(root, *, quota: int = 100) -> SimpleNamespace:
    return SimpleNamespace(
        artifact_max_file_bytes=1024,
        tenant_artifact_quota_bytes=quota,
        staging_ttl_seconds=3600,
        orphan_ttl_seconds=3600,
        storage_high_watermark_percent=99,
        storage_critical_watermark_percent=99,
        idempotency_pending_wait_milliseconds=100,
        cursor_ttl_seconds=3600,
        artifact_root=root,
    )


class _Identity:
    _server_secret = b"s" * 32

    def revalidate_principal(self, _session, principal):
        return principal


def _principal(tenant_id: UUID) -> Principal:
    return Principal(
        user_id=new_uuid7(),
        credential_kind=CredentialKind.BROWSER,
        credential_id=str(new_uuid7()),
        scopes=frozenset(),
        memberships=(TenantMembership(tenant_id=tenant_id, role="MEMBER"),),
        system_admin=False,
    )


def _chunks(payload: bytes):
    async def iterator():
        yield payload[:1]
        yield payload[1:]

    return iterator()


def test_b07_upload_dedup_replay_quota_and_tenant_isolation(
    migrated_postgres_engine, tmp_path
) -> None:
    tenant_id = new_uuid7()
    other_tenant_id = new_uuid7()
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            tenants.insert(),
            {"tenant_id": tenant_id, "slug": "b07-a", "display_name": "B07 A", "enabled": True},
        )
        connection.execute(
            tenants.insert(),
            {
                "tenant_id": other_tenant_id,
                "slug": "b07-b",
                "display_name": "B07 B",
                "enabled": True,
            },
        )
        connection.execute(membership_sets.insert(), {"tenant_id": tenant_id, "version": 1})
        connection.execute(membership_sets.insert(), {"tenant_id": other_tenant_id, "version": 1})
    settings = _settings(tmp_path / "artifacts", quota=8)
    store = FilesystemArtifactStore(settings.artifact_root, max_file_bytes=1024)
    service = ArtifactService(
        create_session_factory(migrated_postgres_engine), settings, _Identity(), store
    )
    principal = _principal(tenant_id)
    payload = b"data"
    kwargs = {
        "principal": principal,
        "tenant_id": tenant_id,
        "kind": "INPUT",
        "media_type": "application/vnd.nexa.cpu-iterative-input+json",
        "expected_size": len(payload),
        "expected_checksum": _checksum(payload),
        "content_length": len(payload),
    }
    first = asyncio.run(
        service.upload(idempotency_key="b07-upload-key-1", chunks=_chunks(payload), **kwargs)
    )
    replay = asyncio.run(
        service.upload(idempotency_key="b07-upload-key-1", chunks=_chunks(payload), **kwargs)
    )
    assert first.body == replay.body
    assert first.headers == replay.headers

    deduped = asyncio.run(
        service.upload(idempotency_key="b07-upload-key-2", chunks=_chunks(payload), **kwargs)
    )
    assert deduped.body == first.body
    assert len(list((settings.artifact_root / "committed").iterdir())) == 1

    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(select(artifacts.c.tenant_id)).all() == [(tenant_id,)]
        counter = (
            connection.execute(
                select(artifact_storage_counters).where(
                    artifact_storage_counters.c.tenant_id == tenant_id
                )
            )
            .mappings()
            .one()
        )
        assert counter["committed_bytes"] == len(payload)
        assert counter["reserved_bytes"] == 0

    with pytest.raises(ApplicationError) as denied:
        service.get_artifact(
            principal, tenant_id=other_tenant_id, artifact_id=UUID(first.body["artifact_id"])
        )
    assert denied.value.status == 403

    with pytest.raises(ApplicationError) as quota:
        asyncio.run(
            service.upload(
                idempotency_key="b07-upload-key-3",
                chunks=_chunks(b"x"),
                **{
                    **kwargs,
                    "expected_size": 5,
                    "expected_checksum": _checksum(b"xxxxx"),
                    "content_length": 5,
                },
            )
        )
    assert quota.value.code == "quota_exceeded"


def test_upload_revalidates_ownership_before_creating_staging(
    migrated_postgres_engine, tmp_path
) -> None:
    tenant_id = new_uuid7()
    other_tenant_id = new_uuid7()
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            tenants.insert(),
            {
                "tenant_id": tenant_id,
                "slug": "b07-auth-a",
                "display_name": "B07 Auth A",
                "enabled": True,
            },
        )
        connection.execute(
            tenants.insert(),
            {
                "tenant_id": other_tenant_id,
                "slug": "b07-auth-b",
                "display_name": "B07 Auth B",
                "enabled": True,
            },
        )
        connection.execute(membership_sets.insert(), {"tenant_id": tenant_id, "version": 1})
        connection.execute(membership_sets.insert(), {"tenant_id": other_tenant_id, "version": 1})

    class RecordingStore(FilesystemArtifactStore):
        began = False

        def begin_staging(self, *args, **kwargs):
            self.began = True
            return super().begin_staging(*args, **kwargs)

    settings = _settings(tmp_path / "auth-before-staging", quota=4)
    store = RecordingStore(settings.artifact_root, max_file_bytes=1024)
    service = ArtifactService(
        create_session_factory(migrated_postgres_engine), settings, _Identity(), store
    )

    with pytest.raises(ApplicationError) as denied:
        asyncio.run(
            service.upload(
                _principal(other_tenant_id),
                tenant_id=tenant_id,
                idempotency_key="b07-auth-before-staging",
                kind="INPUT",
                media_type="application/vnd.nexa.cpu-iterative-input+json",
                expected_size=1,
                expected_checksum=_checksum(b"x"),
                chunks=_chunks(b"x"),
                content_length=1,
            )
        )

    assert denied.value.status == 403
    assert store.began is False


def test_upload_maps_storage_write_failure_to_retryable_dependency_error(
    migrated_postgres_engine, tmp_path
) -> None:
    tenant_id = new_uuid7()
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            tenants.insert(),
            {
                "tenant_id": tenant_id,
                "slug": "b07-write-fail",
                "display_name": "B07 Write Fail",
                "enabled": True,
            },
        )
        connection.execute(membership_sets.insert(), {"tenant_id": tenant_id, "version": 1})

    class FailingStore(FilesystemArtifactStore):
        def append(self, handle, value):
            raise ArtifactError("storage_unavailable", "injected disk-full")

    settings = _settings(tmp_path / "write-failure", quota=4)
    store = FailingStore(settings.artifact_root, max_file_bytes=1024)
    service = ArtifactService(
        create_session_factory(migrated_postgres_engine), settings, _Identity(), store
    )

    principal = _principal(tenant_id)
    with pytest.raises(ApplicationError) as failure:
        asyncio.run(
            service.upload(
                principal,
                tenant_id=tenant_id,
                idempotency_key="b07-write-failure",
                kind="INPUT",
                media_type="application/vnd.nexa.cpu-iterative-input+json",
                expected_size=1,
                expected_checksum=_checksum(b"x"),
                chunks=_chunks(b"x"),
                content_length=1,
            )
        )

    assert failure.value.code == "storage_unavailable"
    assert failure.value.status == 503

    with pytest.raises(ApplicationError) as replay:
        asyncio.run(
            service.upload(
                principal,
                tenant_id=tenant_id,
                idempotency_key="b07-write-failure",
                kind="INPUT",
                media_type="application/vnd.nexa.cpu-iterative-input+json",
                expected_size=1,
                expected_checksum=_checksum(b"x"),
                chunks=_chunks(b"x"),
                content_length=1,
            )
        )
    assert replay.value.code == "storage_unavailable"
    assert replay.value.status == 503
    with migrated_postgres_engine.connect() as connection:
        assert (
            connection.execute(
                select(idempotency_records.c.state).where(
                    idempotency_records.c.idempotency_key == "b07-write-failure"
                )
            ).scalar_one()
            == "COMPLETED"
        )


def test_expire_uploads_releases_reservation_and_completes_idempotency(
    migrated_postgres_engine, tmp_path
) -> None:
    tenant_id = new_uuid7()
    upload_id = new_uuid7()
    idempotency_id = new_uuid7()
    now = datetime.now(UTC)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            tenants.insert(),
            {
                "tenant_id": tenant_id,
                "slug": "b07-expire",
                "display_name": "B07 Expire",
                "enabled": True,
            },
        )
        connection.execute(membership_sets.insert(), {"tenant_id": tenant_id, "version": 1})
        connection.execute(
            idempotency_records.insert(),
            {
                "idempotency_id": idempotency_id,
                "context": str(tenant_id),
                "principal_id": str(new_uuid7()),
                "operation_id": "uploadArtifact",
                "idempotency_key": "b07-expire-key-01",
                "request_hash": _checksum(b"request"),
                "state": "PENDING",
                "expires_at": now + timedelta(days=30),
            },
        )
        connection.execute(
            artifact_storage_counters.insert(),
            {"tenant_id": tenant_id, "committed_bytes": 0, "reserved_bytes": 3},
        )
        connection.execute(
            upload_sessions.insert(),
            {
                "upload_id": upload_id,
                "tenant_id": tenant_id,
                "idempotency_id": idempotency_id,
                "expected_size_bytes": 3,
                "expected_checksum": _checksum(b"old"),
                "staged_key": f"staging/{upload_id.hex}",
                "bytes_received": 1,
                "expires_at": now - timedelta(seconds=1),
                "state": "ACTIVE",
            },
        )

    settings = _settings(tmp_path / "expire", quota=10)
    service = ArtifactService(
        create_session_factory(migrated_postgres_engine),
        settings,
        _Identity(),
        FilesystemArtifactStore(settings.artifact_root, max_file_bytes=1024),
    )
    assert service.expire_uploads(limit=10) == 1

    with migrated_postgres_engine.connect() as connection:
        assert (
            connection.execute(
                select(upload_sessions.c.state).where(upload_sessions.c.upload_id == upload_id)
            ).scalar_one()
            == "EXPIRED"
        )
        assert (
            connection.execute(
                select(artifact_storage_counters.c.reserved_bytes).where(
                    artifact_storage_counters.c.tenant_id == tenant_id
                )
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                select(idempotency_records.c.state).where(
                    idempotency_records.c.idempotency_id == idempotency_id
                )
            ).scalar_one()
            == "COMPLETED"
        )


def test_cancelled_stream_aborts_upload_and_releases_reservation(
    migrated_postgres_engine, tmp_path
) -> None:
    tenant_id = new_uuid7()
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            tenants.insert(),
            {
                "tenant_id": tenant_id,
                "slug": "b07-cancelled",
                "display_name": "B07 Cancelled",
                "enabled": True,
            },
        )
        connection.execute(membership_sets.insert(), {"tenant_id": tenant_id, "version": 1})

    settings = _settings(tmp_path / "cancelled", quota=8)
    store = FilesystemArtifactStore(settings.artifact_root, max_file_bytes=1024)
    service = ArtifactService(
        create_session_factory(migrated_postgres_engine), settings, _Identity(), store
    )

    async def cancelled_chunks():
        yield b"x"
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            service.upload(
                _principal(tenant_id),
                tenant_id=tenant_id,
                idempotency_key="b07-cancelled-key-01",
                kind="INPUT",
                media_type="application/vnd.nexa.cpu-iterative-input+json",
                expected_size=8,
                expected_checksum=_checksum(b"xxxxxxxx"),
                chunks=cancelled_chunks(),
                content_length=8,
            )
        )

    with migrated_postgres_engine.connect() as connection:
        upload_state = connection.execute(select(upload_sessions.c.state)).scalar_one()
        reserved_bytes = connection.execute(
            select(artifact_storage_counters.c.reserved_bytes).where(
                artifact_storage_counters.c.tenant_id == tenant_id
            )
        ).scalar_one()
        idempotency_state = connection.execute(
            select(idempotency_records.c.state).where(
                idempotency_records.c.idempotency_key == "b07-cancelled-key-01"
            )
        ).scalar_one()

    assert upload_state == "ABORTED"
    assert reserved_bytes == 0
    assert idempotency_state == "COMPLETED"
    assert not list((settings.artifact_root / "staging").iterdir())
    assert not store._active


def test_expiry_locks_idempotency_then_counter_then_upload(
    migrated_postgres_engine, tmp_path
) -> None:
    tenant_id = new_uuid7()
    upload_id = new_uuid7()
    idempotency_id = new_uuid7()
    now = datetime.now(UTC)
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            tenants.insert(),
            {
                "tenant_id": tenant_id,
                "slug": "b07-lock-order",
                "display_name": "B07 Lock Order",
                "enabled": True,
            },
        )
        connection.execute(membership_sets.insert(), {"tenant_id": tenant_id, "version": 1})
        connection.execute(
            idempotency_records.insert(),
            {
                "idempotency_id": idempotency_id,
                "context": str(tenant_id),
                "principal_id": str(new_uuid7()),
                "operation_id": "uploadArtifact",
                "idempotency_key": "b07-lock-order-key",
                "request_hash": _checksum(b"request"),
                "state": "PENDING",
                "expires_at": now + timedelta(days=1),
            },
        )
        connection.execute(
            artifact_storage_counters.insert(),
            {"tenant_id": tenant_id, "committed_bytes": 0, "reserved_bytes": 3},
        )
        connection.execute(
            upload_sessions.insert(),
            {
                "upload_id": upload_id,
                "tenant_id": tenant_id,
                "idempotency_id": idempotency_id,
                "expected_size_bytes": 3,
                "expected_checksum": _checksum(b"old"),
                "staged_key": f"staging/{upload_id.hex}",
                "bytes_received": 0,
                "expires_at": now - timedelta(seconds=1),
                "state": "ACTIVE",
            },
        )

    settings = _settings(tmp_path / "lock-order", quota=10)
    service = ArtifactService(
        create_session_factory(migrated_postgres_engine),
        settings,
        _Identity(),
        FilesystemArtifactStore(settings.artifact_root, max_file_bytes=1024),
    )
    observed: list[str] = []

    def observe_lock_order(_conn, _cursor, statement, _parameters, _context, _executemany):
        normalized = " ".join(statement.lower().split())
        if "for update" not in normalized:
            return
        if "from idempotency_records" in normalized:
            observed.append("idempotency")
        elif "from artifact_storage_counters" in normalized:
            observed.append("counter")
        elif "from upload_sessions" in normalized:
            observed.append("upload")

    event.listen(migrated_postgres_engine, "before_cursor_execute", observe_lock_order)
    try:
        assert service.expire_uploads(limit=10) == 1
    finally:
        event.remove(migrated_postgres_engine, "before_cursor_execute", observe_lock_order)

    assert observed.index("idempotency") < observed.index("counter") < observed.index("upload")
