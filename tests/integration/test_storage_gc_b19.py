"""B19 GC G1/G2 and counter reconciliation on PostgreSQL (6.E–6.H, 7.B).

Races use barriers on pg_locks (a waiter is visible as an ungranted advisory lock),
never fixed sleeps.
"""

import hashlib
import os
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

import nexa.application.artifact_service as artifact_service
from nexa.api.app import create_app
from nexa.application.errors import ApplicationError
from nexa.application.storage_gc import StorageGc
from nexa.application.storage_identity import bind_artifact_store
from nexa.infrastructure.artifacts.store import FilesystemArtifactStore
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import BLOB_LOCK_CLASS, GC_RUN_LOCK_CLASS
from nexa.infrastructure.persistence.transactions import run_transaction
from nexa.observability import metrics_api
from tests.api.test_http_contract import _client
from tests.integration.identity_support import make_identity_service
from tests.integration.test_artifacts_b07 import _principal
from tests.integration.test_checkpoint_b14 import CheckpointFixture, _worker_headers
from tests.integration.test_checkpoint_restore_b14 import _claim, _commit, _next_attempt
from tests.integration.test_storage_pressure_b19 import (
    Disk,
    _count,
    _reserved,
    _tenant,
    _upload,
    _upload_service,
    _use,
)

pytestmark = pytest.mark.postgres

OLD = 2 * 3600  # older than both TTLs (3600 s in the test settings)


def _age(path, seconds=OLD):
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def _sha(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _service(engine, tmp_path, store_class=FilesystemArtifactStore):
    service, store = _upload_service(engine, tmp_path, store_class)
    assert bind_artifact_store(service.session_factory, store)
    _use(store, Disk(), 50)
    return service, store


def _gc(engine, service, **kwargs):
    return StorageGc(engine, service.settings, service, **kwargs)


def _audits(engine, action):
    with engine.connect() as connection:
        return (
            connection.execute(
                select(s.audit_records)
                .where(s.audit_records.c.action == action)
                .order_by(s.audit_records.c.created_at)
            )
            .mappings()
            .all()
        )


def _metric(counter, *labels):
    target = counter.labels(*labels) if labels else counter
    return target._value.get()


def _active_session(engine, tenant_id, store, *, expires_in, size=4):
    """An upload session and staging file as begin-upload leaves them."""
    upload_id = new_uuid7()
    key = f"staging/{upload_id.hex}"
    with engine.begin() as connection:
        connection.execute(
            s.artifact_storage_counters.insert().values(
                tenant_id=tenant_id, committed_bytes=0, reserved_bytes=size
            )
        )
        connection.execute(
            s.upload_sessions.insert().values(
                upload_id=upload_id,
                tenant_id=tenant_id,
                expected_size_bytes=size,
                expected_checksum="sha256:" + "0" * 64,
                staged_key=key,
                expires_at=datetime.now(UTC) + expires_in,
                state="ACTIVE",
            )
        )
    path = store.root / key
    path.write_bytes(b"part")
    _age(path)
    return upload_id, path


def _wait_for_lock_waiter(engine, lock_class):
    """Barrier: return once a backend waits on an advisory lock of `lock_class`."""
    deadline = time.monotonic() + 10
    with engine.connect() as connection:
        while time.monotonic() < deadline:
            waiting = connection.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND NOT granted AND classid = :lock_class"
                ),
                {"lock_class": lock_class},
            ).scalar_one()
            connection.rollback()
            if waiting:
                return
            time.sleep(0.005)  # polling interval of the barrier, not a fixed wait
    raise AssertionError("no backend waited on the advisory lock")


def test_g1_expires_sessions_and_removes_only_stale_unowned_staging(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    service, store = _service(engine, tmp_path)
    expired_tenant = _tenant(engine, "b19-gc-expired")
    live_tenant = _tenant(engine, "b19-gc-live")
    expired_id, expired_file = _active_session(
        engine, expired_tenant, store, expires_in=-timedelta(seconds=1)
    )
    # Not yet expired: its staging file is old but the session is ACTIVE.
    _live_id, live_file = _active_session(engine, live_tenant, store, expires_in=timedelta(hours=1))
    stray = store.root / "staging" / ("a" * 32)
    stray.write_bytes(b"stray-bytes")
    _age(stray)
    young = store.root / "staging" / ("b" * 32)
    young.write_bytes(b"young")
    handle = store.begin_staging("t", 4, "sha256:" + "1" * 64, "application/octet-stream")
    open_file = store.root / handle.staged_key
    _age(open_file)
    before = (
        _metric(metrics_api.UPLOADS_EXPIRED),
        _metric(metrics_api.GC_DELETED, "staging"),
        _metric(metrics_api.GC_BYTES_DELETED, "staging"),
    )

    result = _gc(engine, service).run_once()

    assert (result.expired, result.staging_deleted, result.failed) == (1, 2, ())
    assert result.staging_bytes_deleted == len(b"part") + len(b"stray-bytes")
    assert not expired_file.exists() and not stray.exists()
    assert live_file.exists() and young.exists() and open_file.exists()
    assert _reserved(engine, expired_tenant) == 0 and _reserved(engine, live_tenant) == 4
    with engine.connect() as connection:
        state = connection.execute(
            select(s.upload_sessions.c.state).where(s.upload_sessions.c.upload_id == expired_id)
        ).scalar_one()
    assert state == "EXPIRED"
    (expire,) = _audits(engine, "artifact.upload.expire")
    assert (expire["actor_type"], expire["actor_id"], expire["tenant_id"]) == (
        "SYSTEM",
        "storage-gc",
        expired_tenant,
    )
    assert expire["target_id"] == str(expired_id)
    assert expire["safe_metadata"] == {"reserved_bytes": 4}
    (summary,) = _audits(engine, "artifact.gc.staging")
    assert (summary["actor_type"], summary["tenant_id"]) == ("SYSTEM", None)
    assert summary["safe_metadata"] == {"files": 2, "bytes": 15}
    assert str(store.root) not in str(dict(summary))
    assert (
        _metric(metrics_api.UPLOADS_EXPIRED),
        _metric(metrics_api.GC_DELETED, "staging"),
        _metric(metrics_api.GC_BYTES_DELETED, "staging"),
    ) == (before[0] + 1, before[1] + 2, before[2] + 15)
    assert _metric(metrics_api.STAGING_FILES) == 3  # live, young and the open handle

    # Nothing left to do: a second pass writes no audit at all.
    again = _gc(engine, service).run_once()
    assert (again.expired, again.staging_deleted) == (0, 0)
    assert len(_audits(engine, "artifact.gc.staging")) == 1
    store.abort_staging(handle)


def test_g2_removes_only_old_unreferenced_blobs_and_never_follows_symlinks(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    service, store = _service(engine, tmp_path)
    tenant_id = _tenant(engine, "b19-gc-orphan")
    kept = _upload(service, _principal(tenant_id), tenant_id, "b19-gc-kept-0001", b"kept")
    with engine.connect() as connection:
        kept_key = connection.execute(
            select(s.artifacts.c.blob_key).where(
                s.artifacts.c.artifact_id == UUID(kept.body["artifact_id"])
            )
        ).scalar_one()
    committed = store.root / "committed"
    kept_path = store._path_for_key(kept_key)
    _age(kept_path)
    # A row in any state protects its blob.
    deleting = committed / ("c" * 32)
    deleting.write_bytes(b"deleting")
    _age(deleting)
    with engine.begin() as connection:
        connection.execute(
            s.artifacts.insert().values(
                artifact_id=new_uuid7(),
                tenant_id=tenant_id,
                kind="INPUT",
                media_type="application/octet-stream",
                size_bytes=8,
                checksum=_sha(deleting),
                blob_key="blobs/" + "c" * 32,
                state="DELETING",
            )
        )
    orphan = committed / ("d" * 32)
    orphan.write_bytes(b"orphan!")
    _age(orphan)
    young = committed / ("e" * 32)
    young.write_bytes(b"young")
    outside = tmp_path / "outside-target"
    outside.write_bytes(b"outside")
    _age(outside)
    link = committed / ("f" * 32)
    link.symlink_to(outside)
    _age(link)
    checksums = {path: _sha(path) for path in (kept_path, deleting, young)}
    before = _metric(metrics_api.GC_DELETED, "orphan")

    result = _gc(engine, service).run_once()

    assert (result.orphans_deleted, result.orphan_bytes_deleted, result.failed) == (1, 7, ())
    assert not orphan.exists()
    assert {path: _sha(path) for path in checksums} == checksums
    assert link.is_symlink() and outside.read_bytes() == b"outside"
    assert _metric(metrics_api.GC_DELETED, "orphan") == before + 1
    assert _metric(metrics_api.ORPHAN_FILES) == 1 and _metric(metrics_api.ORPHAN_BYTES) == 7
    (audit,) = _audits(engine, "artifact.gc.orphan")
    assert (audit["actor_type"], audit["target_type"], audit["target_id"]) == (
        "SYSTEM",
        "BLOB",
        "blobs/" + "d" * 32,
    )
    assert audit["safe_metadata"] == {"bytes": 7}
    assert _count(engine, s.artifacts) == 2


class _AgedStore(FilesystemArtifactStore):
    """Committed blobs look older than the orphan TTL, so G2 considers them at once."""

    def commit_blob(self, handle):
        blob = super().commit_blob(handle)
        _age(self._path_for_key(blob.blob_key))
        return blob


def _remove_in_thread(gc, store, key, outcome, *, pause=None):
    """G2's per-orphan transaction for `key`, optionally paused after the unlink."""
    (file,) = [file for file in store.scan("blobs") if file.key == key]

    def operation(session):
        removed = gc._remove_orphan(session, file)
        if pause is not None:
            pause()
        return removed

    def run():
        outcome.append(run_transaction(gc._session_factory, operation))

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _hook_shared_lock(monkeypatch, hook):
    """Run `hook(blob_key)` in the metadata commit just before its shared blob lock."""
    real = artifact_service.lock_blob_key

    def lock(session, blob_key, *, exclusive):
        if not exclusive:
            hook(blob_key)
        real(session, blob_key, exclusive=exclusive)

    monkeypatch.setattr(artifact_service, "lock_blob_key", lock)


def test_g2_waits_for_a_metadata_commit_and_then_keeps_the_blob(
    migrated_postgres_engine, tmp_path, monkeypatch
):
    engine = migrated_postgres_engine
    service, store = _service(engine, tmp_path, _AgedStore)
    gc = _gc(engine, service)
    tenant_id = _tenant(engine, "b19-gc-row-wins")
    outcome, threads = [], []
    real_has_blob = store.has_blob

    def has_blob(blob_key):
        # The commit holds the shared lock now: G2 must block on the exclusive one.
        threads.append(_remove_in_thread(gc, store, blob_key, outcome))
        _wait_for_lock_waiter(engine, BLOB_LOCK_CLASS)
        return real_has_blob(blob_key)

    monkeypatch.setattr(store, "has_blob", has_blob)
    uploaded = _upload(service, _principal(tenant_id), tenant_id, "b19-gc-row-wins-0001", b"row!")
    threads[0].join(10)
    assert outcome == [False]
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(s.artifacts).where(
                    s.artifacts.c.artifact_id == UUID(uploaded.body["artifact_id"])
                )
            )
            .mappings()
            .one()
        )
    assert store.inspect(row["blob_key"]).checksum == row["checksum"]
    assert _audits(engine, "artifact.gc.orphan") == []


def test_a_metadata_commit_waits_for_g2_and_then_fails_closed(
    migrated_postgres_engine, tmp_path, monkeypatch
):
    engine = migrated_postgres_engine
    service, store = _service(engine, tmp_path, _AgedStore)
    gc = _gc(engine, service)
    tenant_id = _tenant(engine, "b19-gc-gc-wins")
    outcome, threads = [], []
    removed, release = threading.Event(), threading.Event()

    def pause():
        removed.set()
        assert release.wait(10)

    def gc_first(blob_key):
        # G2 unlinks and still holds the exclusive lock when the commit asks for it.
        threads.append(_remove_in_thread(gc, store, blob_key, outcome, pause=pause))
        assert removed.wait(10)
        barrier = threading.Thread(
            target=lambda: (_wait_for_lock_waiter(engine, BLOB_LOCK_CLASS), release.set())
        )
        barrier.start()
        threads.append(barrier)

    _hook_shared_lock(monkeypatch, gc_first)
    with pytest.raises(ApplicationError) as failure:
        _upload(service, _principal(tenant_id), tenant_id, "b19-gc-gc-wins-0001", b"gone")
    for thread in threads:
        thread.join(10)
    assert outcome == [True]
    assert (failure.value.code, failure.value.status) == ("dependency_unavailable", 503)
    assert _count(engine, s.artifacts, s.artifacts.c.tenant_id == tenant_id) == 0
    assert _reserved(engine, tenant_id) == 0
    assert _count(engine, s.upload_sessions, s.upload_sessions.c.state == "ACTIVE") == 0
    assert len(_audits(engine, "artifact.gc.orphan")) == 1


def test_one_pass_at_a_time_and_failures_are_counted_per_step(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    service, store = _service(engine, tmp_path)
    with engine.connect() as holder:
        assert holder.execute(select(func.pg_try_advisory_lock(GC_RUN_LOCK_CLASS, 0))).scalar_one()
        assert _gc(engine, service).run_once() is None
        holder.execute(select(func.pg_advisory_unlock(GC_RUN_LOCK_CLASS, 0)))
        holder.commit()

    orphan = store.root / "committed" / ("d" * 32)
    orphan.write_bytes(b"orphan")
    _age(orphan)
    (store.root / "store-identity").rename(store.root / "moved-identity")
    errors = {kind: _metric(metrics_api.GC_ERRORS, kind) for kind in ("staging", "orphan")}
    last_run = _metric(metrics_api.GC_LAST_RUN)
    result = _gc(engine, service).run_once()
    assert result.failed == ("staging", "orphan")
    assert result.drift_tenants == 0  # reconcile still ran
    assert orphan.exists()
    assert {kind: _metric(metrics_api.GC_ERRORS, kind) for kind in errors} == {
        kind: value + 1 for kind, value in errors.items()
    }
    assert _metric(metrics_api.GC_LAST_RUN) == last_run  # a failing pass is not "completed"

    (store.root / "moved-identity").rename(store.root / "store-identity")
    stopped = _gc(engine, service)
    stopped.stop()
    assert stopped.run_once().orphans_deleted == 0 and orphan.exists()
    assert _gc(engine, service, max_items=0).run_once().orphans_deleted == 0
    assert _gc(engine, service).run_once().orphans_deleted == 1


def test_reconcile_reports_counter_drift_without_repairing_it(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    service, _store = _service(engine, tmp_path)
    tenant_id = _tenant(engine, "b19-gc-drift")
    _upload(service, _principal(tenant_id), tenant_id, "b19-gc-drift-0001", b"data")
    assert _gc(engine, service).run_once().drift_tenants == 0
    assert _metric(metrics_api.COUNTER_DRIFT) == 0
    with engine.begin() as connection:
        connection.execute(
            update(s.artifact_storage_counters)
            .where(s.artifact_storage_counters.c.tenant_id == tenant_id)
            .values(committed_bytes=99)
        )
    result = _gc(engine, service).run_once()
    assert result.drift_tenants == 1 and result.failed == ()
    assert _metric(metrics_api.COUNTER_DRIFT) == 1
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(s.artifact_storage_counters.c.committed_bytes).where(
                    s.artifact_storage_counters.c.tenant_id == tenant_id
                )
            ).scalar_one()
            == 99
        )


def test_published_checkpoints_survive_gc_and_a_restore_reads_during_a_pass(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        fixture = CheckpointFixture(engine, client, label="b19-gc")
        _commit(fixture, step=10, accumulator=7)
        _commit(fixture, step=20, accumulator=14)
        newest = _commit(fixture, step=30, accumulator=21)
        services = client.app.state.services
        store = services.artifact.store
        with engine.connect() as connection:
            # The factory graph's own input row has a placeholder key and no file.
            rows = (
                connection.execute(
                    select(s.artifacts).where(s.artifacts.c.blob_key.like("blobs/%"))
                )
                .mappings()
                .all()
            )
        age = services.artifact.settings.orphan_ttl_seconds + 60
        for row in rows:
            _age(store._path_for_key(row["blob_key"]), age)
        orphan = store.root / "committed" / ("d" * 32)
        orphan.write_bytes(b"orphan")
        _age(orphan, age)
        _next_attempt(fixture)
        reads = []
        real_remove = store.remove_scanned

        def restore_while_removing(file):
            # A restore claim and download in the middle of the pass see intact bytes.
            claimed = _claim(fixture)
            files = claimed.json()["execution_context"]["restore_checkpoint"]["files"]
            download = client.get(
                f"{fixture.base}/execution-artifacts/{files[0]['artifact_id']}/content",
                headers=_worker_headers(fixture.credential, fixture.authority),
            )
            reads.append((claimed.status_code, files, download.status_code, download.content))
            return real_remove(file)

        store.remove_scanned = restore_while_removing
        try:
            result = _gc(engine, services.artifact).run_once()
        finally:
            store.remove_scanned = real_remove

        assert result.orphans_deleted == 1 and result.failed == ()
        assert reads == [(200, [newest["state_artifact"]], 200, fixture.state(30, 21))]
        for row in rows:
            assert store.inspect(row["blob_key"]).checksum == row["checksum"]
        with engine.connect() as connection:
            checkpoints = connection.execute(
                select(s.checkpoints.c.sequence, s.checkpoints.c.state).order_by(
                    s.checkpoints.c.sequence
                )
            ).all()
        assert [tuple(row) for row in checkpoints] == [
            (1, "COMMITTED"),
            (2, "COMMITTED"),
            (3, "COMMITTED"),
        ]
        assert len(rows) >= 6  # three manifests and three states, plus the job input
        assert _count(engine, s.artifacts, s.artifacts.c.blob_key.like("blobs/%")) == len(rows)


def test_the_api_lifespan_runs_gc_and_stops_it_on_shutdown(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    identity = make_identity_service(engine, tmp_path)
    settings = replace(identity.settings, gc_interval_seconds=0.01)
    app = create_app(settings, engine=engine)
    with TestClient(app, base_url="https://nexa.test") as client:
        store = client.app.state.services.artifact.store
        orphan = store.root / "committed" / ("d" * 32)
        orphan.write_bytes(b"orphan")
        _age(orphan, settings.orphan_ttl_seconds + 60)
        deadline = time.monotonic() + 10
        while orphan.exists() and time.monotonic() < deadline:
            time.sleep(0.01)  # polling interval while the loop runs, not a fixed wait
        assert not orphan.exists()
        stopping = time.monotonic()
    assert time.monotonic() - stopping < 5
    assert len(_audits(engine, "artifact.gc.orphan")) == 1
