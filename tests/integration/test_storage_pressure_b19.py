"""B19-R06/R07/R08: watermark, byte quota and ENOSPC on every write path, on PostgreSQL.

The disk is simulated through the store's injected statvfs; every reject must leave no
metadata, no reservation and no ACTIVE upload session behind.
"""

import asyncio
import errno
import time
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from nexa.application.artifact_service import ArtifactService
from nexa.application.errors import ApplicationError
from nexa.coordinator.service import CoordinatorService
from nexa.domain.scheduling import Dispatch, NoDecision
from nexa.infrastructure.artifacts.store import ArtifactError, FilesystemArtifactStore
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.observability import metrics_api, metrics_coordinator
from tests.api.test_http_contract import _client
from tests.integration._factories import IMAGE_DIGEST
from tests.integration.test_artifacts_b07 import _checksum, _Identity, _principal, _settings
from tests.integration.test_control_b15 import _set_mode
from tests.integration.test_coordinator_b11 import seed_dispatchable
from tests.integration.test_execution_closure_b11 import _worker_headers
from tests.integration.test_execution_upload_b16 import _post as _worker_post
from tests.integration.test_execution_upload_b16 import _running
from tests.integration.test_jobs_b08 import _bootstrap_member, _seed_cpu_inputs, _submit_body
from tests.integration.test_sweep_b16 import _dataset, _members, _request
from tests.integration.test_sweep_b16 import _post as _sweep_post
from tests.integration.test_templates_b16 import _register_all
from tests.integration.test_worker_api_b10 import (
    WORKER_ID,
    _bootstrap_worker,
    _create_incarnation,
    _inventory,
)

pytestmark = pytest.mark.postgres

ORIGIN = "https://nexa.test"
MEDIA = "application/vnd.nexa.cpu-iterative-input+json"
MIB = 1024 * 1024


class Disk:
    """A statvfs whose used percentage the test sets; `fail` makes it raise."""

    def __init__(self, percent: float = 50):
        self.percent = percent
        self.fail = False

    def __call__(self, _path):
        if self.fail:
            raise OSError(errno.EIO, "statvfs failed")
        blocks = 1_000_000
        return SimpleNamespace(
            f_frsize=4096, f_blocks=blocks, f_bavail=int(blocks * (100 - self.percent) / 100)
        )


def _use(store, disk: Disk, percent: float | None = None) -> Disk:
    if percent is not None:
        disk.percent = percent
    store._statvfs = disk
    store._capacity = None  # the next reading is fresh, as after the 1 s cache expired
    return disk


def _rejections(reason, operation):
    return metrics_api.STORAGE_REJECTIONS.labels(reason, operation)._value.get()


def _admissions(kind, outcome, reason="none"):
    return metrics_api.ADMISSION.labels(kind, outcome, reason)._value.get()


def _count(engine, table, *where):
    with engine.connect() as connection:
        return connection.execute(
            select(func.count()).select_from(table).where(*where)
        ).scalar_one()


def _reserved(engine, tenant_id):
    with engine.connect() as connection:
        return connection.execute(
            select(s.artifact_storage_counters.c.reserved_bytes).where(
                s.artifact_storage_counters.c.tenant_id == tenant_id
            )
        ).scalar_one_or_none()


def _set_used_bytes(engine, tenant_id, used):
    with engine.begin() as connection:
        connection.execute(
            pg_insert(s.artifact_storage_counters)
            .values(tenant_id=tenant_id, committed_bytes=used, reserved_bytes=0)
            .on_conflict_do_update(
                index_elements=["tenant_id"], set_={"committed_bytes": used, "reserved_bytes": 0}
            )
        )


def _tenant(engine, slug):
    tenant_id = new_uuid7()
    with engine.begin() as connection:
        connection.execute(
            s.tenants.insert(),
            {"tenant_id": tenant_id, "slug": slug, "display_name": slug, "enabled": True},
        )
        connection.execute(s.membership_sets.insert(), {"tenant_id": tenant_id, "version": 1})
    return tenant_id


def _upload_service(engine, tmp_path, store_class=FilesystemArtifactStore, *, max_bytes=1024):
    settings = _settings(tmp_path / "artifacts", quota=8 * MIB)
    settings.artifact_max_file_bytes = max_bytes
    settings.storage_high_watermark_percent = 85
    settings.storage_critical_watermark_percent = 95
    store = store_class(settings.artifact_root, max_file_bytes=max_bytes)
    service = ArtifactService(create_session_factory(engine), settings, _Identity(), store)
    return service, store


def _upload(service, principal, tenant_id, key, payload, chunks=None):
    async def two_chunks():
        yield payload[:1]
        yield payload[1:]

    return asyncio.run(
        service.upload(
            principal,
            tenant_id=tenant_id,
            idempotency_key=key,
            kind="INPUT",
            media_type=MEDIA,
            expected_size=len(payload),
            expected_checksum=_checksum(payload),
            chunks=chunks if chunks is not None else two_chunks(),
            content_length=len(payload),
        )
    )


def _staging_files(store):
    return [path for path in (store.root / "staging").iterdir() if path.is_file()]


def _assert_released(engine, tenant_id, key, *, recorded):
    """No metadata, no reservation, no ACTIVE session; a started upload records its failure."""
    assert _count(engine, s.artifacts, s.artifacts.c.tenant_id == tenant_id) == 0
    assert _count(engine, s.upload_sessions, s.upload_sessions.c.state == "ACTIVE") == 0
    assert _reserved(engine, tenant_id) in (None, 0)
    records = _count(
        engine,
        s.idempotency_records,
        s.idempotency_records.c.idempotency_key == key,
        s.idempotency_records.c.state == "COMPLETED",
    )
    assert records == (1 if recorded else 0)


@pytest.mark.parametrize(
    ("percent", "reason"), [(50, None), (90, "high_watermark"), (96, "critical_watermark")]
)
def test_user_upload_follows_the_watermark_table(
    migrated_postgres_engine, tmp_path, percent, reason
):
    engine = migrated_postgres_engine
    tenant_id = _tenant(engine, f"b19-cells-{percent}")
    service, store = _upload_service(engine, tmp_path)
    _use(store, Disk(), percent)
    key = f"b19-cell-{percent}-0001"
    if reason is None:
        result = _upload(service, _principal(tenant_id), tenant_id, key, b"data")
        assert result.status == 201
        return
    before = _rejections(reason, "user_upload")
    with pytest.raises(ApplicationError) as failure:
        _upload(service, _principal(tenant_id), tenant_id, key, b"data")
    assert (failure.value.code, failure.value.status, failure.value.retry_after) == (
        "storage_pressure",
        503,
        1,
    )
    assert _rejections(reason, "user_upload") == before + 1
    # Rejected before staging and inside the begin transaction: nothing persists.
    _assert_released(engine, tenant_id, key, recorded=False)
    assert _count(engine, s.upload_sessions) == 0
    assert _staging_files(store) == []


def test_a_replay_never_needs_disk_and_a_failed_statvfs_fails_closed(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    tenant_id = _tenant(engine, "b19-replay")
    principal = _principal(tenant_id)
    service, store = _upload_service(engine, tmp_path)
    disk = _use(store, Disk(), 50)
    first = _upload(service, principal, tenant_id, "b19-replay-key-0001", b"data")
    _use(store, disk, 99)
    replay = _upload(service, principal, tenant_id, "b19-replay-key-0001", b"data")
    assert (replay.status, replay.body) == (first.status, first.body)
    disk.fail = True
    store._capacity = None
    assert _upload(service, principal, tenant_id, "b19-replay-key-0001", b"data").body == first.body
    before = _rejections("unavailable", "user_upload")
    with pytest.raises(ApplicationError) as failure:
        _upload(service, principal, tenant_id, "b19-replay-key-0002", b"more")
    assert (failure.value.code, failure.value.status) == ("dependency_unavailable", 503)
    assert _rejections("unavailable", "user_upload") == before + 1
    assert _count(engine, s.upload_sessions) == 1


def test_mid_stream_pressure_aborts_and_releases_the_reservation(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    tenant_id = _tenant(engine, "b19-midstream")
    service, store = _upload_service(engine, tmp_path, max_bytes=4 * MIB)
    disk = _use(store, Disk(), 50)
    payload = b"x" * (2 * MIB)

    async def chunks():
        yield payload[: MIB // 2]
        _use(store, disk, 96)  # the disk fills while the stream is in flight
        yield payload[MIB // 2 : 3 * MIB // 2]
        pytest.fail("the stream must stop at the next re-check")

    before = _rejections("critical_watermark", "user_upload")
    with pytest.raises(ApplicationError) as failure:
        _upload(service, _principal(tenant_id), tenant_id, "b19-midstream-0001", payload, chunks())
    assert (failure.value.code, failure.value.status) == ("storage_pressure", 503)
    assert _rejections("critical_watermark", "user_upload") == before + 1
    _assert_released(engine, tenant_id, "b19-midstream-0001", recorded=True)
    assert _count(engine, s.upload_sessions, s.upload_sessions.c.state == "ABORTED") == 1
    assert _staging_files(store) == []


class _FullFile:
    def __init__(self, inner):
        self._inner = inner

    def write(self, _value):
        raise OSError(errno.ENOSPC, "No space left on device")

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _FullStore(FilesystemArtifactStore):
    def append(self, handle, value):
        state = self._state(handle)
        if not isinstance(state.file, _FullFile):
            state.file = _FullFile(state.file)
        return super().append(handle, value)


def test_enospc_while_staging_is_storage_pressure_and_releases(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    tenant_id = _tenant(engine, "b19-enospc")
    service, store = _upload_service(engine, tmp_path, _FullStore)
    _use(store, Disk(), 50)
    before = _rejections("enospc", "user_upload")
    with pytest.raises(ApplicationError) as failure:
        _upload(service, _principal(tenant_id), tenant_id, "b19-enospc-key-0001", b"data")
    assert (failure.value.code, failure.value.status, failure.value.retry_after) == (
        "storage_pressure",
        503,
        1,
    )
    assert _rejections("enospc", "user_upload") == before + 1
    _assert_released(engine, tenant_id, "b19-enospc-key-0001", recorded=True)
    assert _staging_files(store) == []


class _VanishedBlobStore(FilesystemArtifactStore):
    def has_blob(self, blob_key):
        super().has_blob(blob_key)
        return False  # as if G2 swept it between commit_blob and the metadata commit


def test_a_blob_missing_at_metadata_commit_is_retryable_and_releases(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    tenant_id = _tenant(engine, "b19-vanished")
    service, store = _upload_service(engine, tmp_path, _VanishedBlobStore)
    _use(store, Disk(), 50)
    with pytest.raises(ApplicationError) as failure:
        _upload(service, _principal(tenant_id), tenant_id, "b19-vanished-0001", b"data")
    assert (failure.value.code, failure.value.status) == ("dependency_unavailable", 503)
    _assert_released(engine, tenant_id, "b19-vanished-0001", recorded=True)
    assert (
        _count(engine, s.audit_records, s.audit_records.c.action == "artifact.upload.commit") == 0
    )


class _UninspectableBlobStore(FilesystemArtifactStore):
    def has_blob(self, blob_key):
        raise ArtifactError("storage_unavailable", "Artifact blob cannot be inspected")


def test_a_blob_that_cannot_be_inspected_at_metadata_commit_fails_closed_and_releases(
    migrated_postgres_engine, tmp_path
):
    # The volume fails between commit_blob and the metadata commit: a published 503, the
    # reservation released, no metadata; the blob, if any, is an orphan for GC G2.
    engine = migrated_postgres_engine
    tenant_id = _tenant(engine, "b19-uninspectable")
    service, store = _upload_service(engine, tmp_path, _UninspectableBlobStore)
    _use(store, Disk(), 50)
    with pytest.raises(ApplicationError) as failure:
        _upload(service, _principal(tenant_id), tenant_id, "b19-uninspect-0001", b"data")
    assert (failure.value.code, failure.value.status) == ("dependency_unavailable", 503)
    _assert_released(engine, tenant_id, "b19-uninspect-0001", recorded=True)
    assert _count(engine, s.artifacts, s.artifacts.c.tenant_id == tenant_id) == 0


def test_worker_upload_is_refused_only_at_critical_and_audited_as_the_worker(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential, authority = _running(engine, client, adapter_id="pytorch.cifar10")
        disk = _use(client.app.state.services.artifact.store, Disk(), 90)
        media = "application/octet-stream"
        accepted = _worker_post(client, credential, authority, media, "b19-worker-high-0001")
        assert accepted.status_code == 201, accepted.text
        with engine.connect() as connection:
            audit = (
                connection.execute(
                    select(s.audit_records).where(
                        s.audit_records.c.action == "artifact.upload.commit"
                    )
                )
                .mappings()
                .one()
            )
        assert (audit["actor_type"], audit["actor_id"]) == ("WORKER", WORKER_ID)

        _use(client.app.state.services.artifact.store, disk, 96)
        before = _rejections("critical_watermark", "worker_upload")
        refused = _worker_post(client, credential, authority, media, "b19-worker-crit-0001")
        assert refused.status_code == 503, refused.text
        assert refused.json()["code"] == "storage_pressure"
        assert refused.headers["retry-after"] == "1"
        assert _rejections("critical_watermark", "worker_upload") == before + 1
        assert _count(engine, s.artifacts, s.artifacts.c.kind == "RESULT_FILE") == 1


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({"X-Artifact-Size": str(10 * 1024**4)}, 413, "payload_too_large"),
        ({"Content-Length": "19"}, 422, "validation_failed"),
        ({"Content-Length": "-1"}, 400, "validation_failed"),
        ({"Content-Length": "abc"}, 400, "validation_failed"),
    ],
)
def test_worker_upload_headers_are_checked_before_any_db_work(
    migrated_postgres_engine, tmp_path, headers, status, code
):
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential, authority = _running(engine, client, adapter_id="pytorch.cifar10")
        settings = client.app.state.services.artifact.settings
        if "X-Artifact-Size" in headers:
            headers = {"X-Artifact-Size": str(settings.artifact_max_file_bytes + 1)}
        content = b"\x00safetensors-bytes"
        response = client.post(
            f"/v1/attempts/{authority['attempt_id']}/artifacts",
            headers={
                **_worker_headers(credential, authority),
                "Content-Type": "application/octet-stream",
                "Idempotency-Key": "b19-header-check-0001",
                "X-Artifact-Kind": "RESULT_FILE",
                "X-Artifact-Media-Type": "application/octet-stream",
                "X-Artifact-Checksum": _checksum(content),
                "X-Artifact-Size": str(len(content)),
                **headers,
            },
            content=content,
        )
        assert (response.status_code, response.json()["code"]) == (status, code), response.text
        assert (
            _count(
                engine,
                s.idempotency_records,
                s.idempotency_records.c.idempotency_key == "b19-header-check-0001",
            )
            == 0
        )
        assert _count(engine, s.upload_sessions) == 0


def _job_member(client, engine):
    admin_id, csrf = _bootstrap_member(client)
    write = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    tenant = client.post(
        "/v1/admin/tenants",
        headers={**write, "Idempotency-Key": "b19-create-tenant"},
        json={"slug": "b19-team", "display_name": "B19 Team"},
    )
    assert tenant.status_code == 201, tenant.text
    tenant_id = tenant.json()["tenant_id"]
    membership = client.post(
        f"/v1/admin/tenants/{tenant_id}/memberships",
        headers={**write, "Idempotency-Key": "b19-add-membership", "If-Match": '"v1"'},
        json={"user_id": admin_id, "role": "MEMBER"},
    )
    assert membership.status_code == 200, membership.text
    login = client.post(
        "/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"username": "b08-admin@example.test", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200, login.text
    write = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
    return tenant_id, write, _seed_cpu_inputs(engine, tenant_id)


def test_submit_job_watermark_quota_and_replay(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        tenant_id, write, artifact_id = _job_member(client, engine)
        store = client.app.state.services.jobs.artifact_store
        disk = _use(store, Disk(), 50)
        body = _submit_body(artifact_id)

        def submit(key):
            return client.post(
                "/v1/jobs",
                headers={**write, "X-Nexa-Tenant-Id": tenant_id, "Idempotency-Key": key},
                json=body,
            )

        counts = {
            name: _admissions("job", *labels)
            for name, labels in {
                "accepted": ("accepted",),
                "replayed": ("replayed",),
                "pressure": ("rejected", "storage_pressure"),
                "unavailable": ("rejected", "dependency_unavailable"),
                "quota": ("rejected", "quota_exceeded"),
                "mode": ("rejected", "state_conflict"),
            }.items()
        }
        accepted = submit("b19-submit-key-0001")
        assert accepted.status_code == 202, accepted.text

        for percent in (90, 96):
            _use(store, disk, percent)
            refused = submit(f"b19-submit-{percent}-01")
            assert (refused.status_code, refused.json()["code"]) == (503, "storage_pressure")
            assert refused.headers["retry-after"] == "1"
            replay = submit("b19-submit-key-0001")
            assert (replay.status_code, replay.json()) == (202, accepted.json())

        disk.fail = True
        store._capacity = None
        unavailable = submit("b19-submit-unavail-01")
        assert (unavailable.status_code, unavailable.json()["code"]) == (
            503,
            "dependency_unavailable",
        )

        disk.fail = False
        _use(store, disk, 50)
        quota = client.app.state.services.jobs.settings.tenant_artifact_quota_bytes
        _set_used_bytes(engine, UUID(tenant_id), quota)
        over = submit("b19-submit-quota-01")
        assert (over.status_code, over.json()["code"]) == (429, "quota_exceeded")
        assert over.headers["retry-after"] == "1"

        # The mode is checked before storage: ADMISSION_OFF stays 409 on a full disk.
        _set_mode(engine, "ADMISSION_OFF")
        _use(store, disk, 99)
        off = submit("b19-submit-mode-01")
        assert (off.status_code, off.json()["code"]) == (409, "state_conflict")
        _set_mode(engine, "NORMAL")

        assert _count(engine, s.jobs) == 1
        expected = {
            "accepted": 1,
            "replayed": 2,
            "pressure": 2,
            "unavailable": 1,
            "quota": 1,
            "mode": 1,
        }
        labels = {
            "accepted": ("accepted",),
            "replayed": ("replayed",),
            "pressure": ("rejected", "storage_pressure"),
            "unavailable": ("rejected", "dependency_unavailable"),
            "quota": ("rejected", "quota_exceeded"),
            "mode": ("rejected", "state_conflict"),
        }
        for name, delta in expected.items():
            assert _admissions("job", *labels[name]) == counts[name] + delta, name


def test_oversized_json_body_is_413_also_when_chunked(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        tenant_id, write, _artifact_id = _job_member(client, engine)
        limit = client.app.state.services.jobs.settings.api_json_max_bytes
        headers = {
            **write,
            "X-Nexa-Tenant-Id": tenant_id,
            "Idempotency-Key": "b19-oversized-0001",
            "Content-Type": "application/json",
        }

        def chunked():
            yield b'{"spec": "'
            sent = 10
            while sent <= limit:
                yield b"x" * 4096
                sent += 4096
            yield b'"}'

        for content in (b"{" + b" " * limit + b"}", chunked()):
            response = client.post("/v1/jobs", headers=headers, content=content)
            assert response.status_code == 413, response.text
            assert response.json()["code"] == "payload_too_large"
        assert _count(engine, s.jobs) == 0


def test_create_sweep_parent_is_storage_gated_and_replays_on_a_full_disk(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    _register_all(engine)
    with _client(engine, tmp_path) as client:
        (tenant_id, _other), write = _members(client, engine)
        store = client.app.state.services.jobs.artifact_store
        disk = _use(store, Disk(), 90)
        body = _request(_dataset(engine, tenant_id))
        before = _admissions("sweep", "rejected", "storage_pressure")
        refused = _sweep_post(client, write, tenant_id, "b19-sweep-high-01", body)
        assert (refused.status_code, refused.json()["code"]) == (503, "storage_pressure")
        assert _admissions("sweep", "rejected", "storage_pressure") == before + 1
        assert _count(engine, s.sweep_parents) == 0 and _count(engine, s.jobs) == 0

        quota = client.app.state.services.jobs.settings.tenant_artifact_quota_bytes
        _set_used_bytes(engine, UUID(tenant_id), quota)
        _use(store, disk, 50)
        over = _sweep_post(client, write, tenant_id, "b19-sweep-quota-01", body)
        assert (over.status_code, over.json()["code"]) == (429, "quota_exceeded")
        assert _count(engine, s.sweep_parents) == 0

        _set_used_bytes(engine, UUID(tenant_id), 0)
        first = _sweep_post(client, write, tenant_id, "b19-sweep-ok-0001", body)
        assert first.status_code == 207, first.text
        _use(store, disk, 99)
        replayed = _admissions("sweep", "replayed")
        replay = _sweep_post(client, write, tenant_id, "b19-sweep-ok-0001", body)
        assert (replay.status_code, replay.json()) == (207, first.json())
        assert _admissions("sweep", "replayed") == replayed + 1


def _quota_transitions(transition):
    return metrics_coordinator.QUOTA_BLOCKED.labels(transition)._value.get()


def _queued_reasons(engine):
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                select(s.jobs.c.waiting_reason, s.jobs.c.version)
                .where(s.jobs.c.state == "QUEUED")
                .order_by(s.jobs.c.job_id)
            )
        ]


def test_an_exhausted_tenant_gets_no_dispatch_and_a_derived_quota_reason(
    migrated_postgres_engine,
):
    engine = migrated_postgres_engine
    graph, _worker, _job_ids = seed_dispatchable(engine, count=2)
    quota = 1000
    _set_used_bytes(engine, graph["tenant_id"], quota)
    service = CoordinatorService(create_session_factory(engine), artifact_quota_bytes=quota)
    epoch = service.acquire()
    seeded = _queued_reasons(engine)
    blocked, unblocked = _quota_transitions("blocked"), _quota_transitions("unblocked")
    # The tick's maintenance probe marks the reason; the policy offers nothing.
    assert isinstance(service.tick(epoch), NoDecision)
    assert _count(engine, s.attempts) == 0
    assert _queued_reasons(engine) == [("waiting_for_quota", v + 1) for _r, v in seeded]
    assert _quota_transitions("blocked") == blocked + 1
    # Not rescanned before 30 s; nothing is rewritten.
    assert service.track_artifact_quota(epoch) == 0
    assert _quota_transitions("blocked") == blocked + 1

    _set_used_bytes(engine, graph["tenant_id"], quota - 1)
    events = _count(engine, s.events)
    assert service.track_artifact_quota(epoch) == 2
    assert _count(engine, s.events) == events  # a derived reason writes no event
    assert _queued_reasons(engine) == [(None, v + 2) for _r, v in seeded]
    assert _quota_transitions("unblocked") == unblocked + 1
    assert isinstance(service.tick(epoch), Dispatch)


def test_startup_clears_stale_reasons_and_write_frozen_writes_nothing(migrated_postgres_engine):
    engine = migrated_postgres_engine
    graph, _worker, _job_ids = seed_dispatchable(engine, count=1)
    with engine.begin() as connection:
        connection.execute(update(s.jobs).values(waiting_reason="waiting_for_quota"))
    quota = 1000
    _set_mode(engine, "WRITE_FROZEN")
    service = CoordinatorService(create_session_factory(engine), artifact_quota_bytes=quota)
    epoch = service.acquire()
    stale = _queued_reasons(engine)
    assert service.track_artifact_quota(epoch) == 0
    assert _queued_reasons(engine) == stale

    _set_mode(engine, "NORMAL")
    assert service.track_artifact_quota(epoch) == 1  # the deferred startup clear
    assert _queued_reasons(engine) == [(None, stale[0][1] + 1)]
    assert service.track_artifact_quota(epoch) == 0

    _set_used_bytes(engine, graph["tenant_id"], quota)
    _set_mode(engine, "WRITE_FROZEN")
    assert service.track_artifact_quota(epoch) == 0
    _set_mode(engine, "NORMAL")
    assert service.track_artifact_quota(epoch) == 1  # marking was not recorded as done
    assert _queued_reasons(engine)[0][0] == "waiting_for_quota"


def test_critical_storage_stops_new_offers_at_the_next_heartbeat(
    migrated_postgres_engine, tmp_path
):
    """Dispatch stops through worker readiness, without a storage read in the coordinator."""
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b19-critical-incarnation"
        )
        seed_dispatchable(
            engine,
            count=1,
            existing_worker=(UUID(WORKER_ID), UUID(incarnation["worker_incarnation_id"])),
        )
        disk = _use(client.app.state.services.artifact.store, Disk(), 90)
        reconciled = client.get(
            f"/v1/workers/{WORKER_ID}/reconciliation?page_size=100",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Worker-Incarnation-Id": incarnation["worker_incarnation_id"],
            },
        )
        assert reconciled.status_code == 200, reconciled.text
        inventory = _inventory()  # the seeded arm64 capabilities, so the Job stays compatible
        inventory["architecture"] = "linux/arm64"
        inventory["images"] = [
            {"image_digest": IMAGE_DIGEST, "architecture": "linux/arm64", "verified": True}
        ]

        def heartbeat():
            response = client.post(
                f"/v1/workers/{WORKER_ID}/heartbeat",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "X-Callback-Id": str(new_uuid7()),
                },
                json={
                    "worker_incarnation_id": incarnation["worker_incarnation_id"],
                    "observed_health": "READY",
                    "reconcile_complete": True,
                    "inventory": inventory,
                    "observed_containers": [],
                },
            )
            assert response.status_code == 200, response.text
            with engine.connect() as connection:
                return connection.execute(select(s.workers.c.health)).scalar_one()

        service = CoordinatorService(create_session_factory(engine))
        epoch = service.acquire()
        assert heartbeat() == "READY"  # above high, below critical: still dispatchable

        _use(client.app.state.services.artifact.store, disk, 96)
        crossed = time.monotonic()
        assert heartbeat() != "READY"
        assert isinstance(service.tick(epoch), NoDecision)
        elapsed = time.monotonic() - crossed
        print(f"b19 dispatch stop window {elapsed * 1000:.1f} ms")  # evidence, with -s
        assert _count(engine, s.attempts) == 0
        # The bound is one heartbeat interval (5 s) plus its operation timeout; here the
        # heartbeat is sent at once, so the measured window is only the request itself.
        assert elapsed < 5

        _use(client.app.state.services.artifact.store, disk, 90)
        assert heartbeat() == "READY"
        assert isinstance(service.tick(epoch), Dispatch)
