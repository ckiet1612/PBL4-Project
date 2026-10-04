"""B19-R17: a finished attempt's local input copies are removed only once released."""

import logging
import shutil
from types import SimpleNamespace

import pytest

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.worker import local_cleanup
from nexa.worker.agent import WorkerAgent
from nexa.worker.journal import ExecutionJournal
from nexa.worker.local_cleanup import remove_released_attempt_dirs
from nexa.worker.models import Authority, ContainerIdentity, ResourceVector


def _authority(attempt_id):
    return Authority(
        worker_id=str(new_uuid7()),
        worker_incarnation_id=str(new_uuid7()),
        attempt_id=attempt_id,
        allocation_id=str(new_uuid7()),
        lease_id=str(new_uuid7()),
        job_fence=1,
    )


def _record(journal, *, verified, cleaned=True):
    attempt_id = str(new_uuid7())
    authority = _authority(attempt_id)
    nonce = str(new_uuid7())
    record = journal.prepare(
        attempt_id=attempt_id,
        allocation_id=authority.allocation_id,
        startup_nonce=nonce,
        authority=authority,
        resources=ResourceVector(cpu_millis=1000, memory_bytes=1 << 30, gpu_count=0),
        image_digest="sha256:" + "a" * 64,
        execution_binding={},
    )
    if not cleaned:
        return attempt_id
    record = journal.begin_create(attempt_id, expected_sequence=record.operation_sequence)
    record = journal.bind_created(
        attempt_id,
        ContainerIdentity(
            container_id="c" * 64,
            runtime_identity_digest="sha256:" + "b" * 64,
            attempt_id=attempt_id,
            allocation_id=authority.allocation_id,
            startup_nonce=nonce,
        ),
    )
    record = journal.begin_cleanup(
        attempt_id,
        expected_sequence=record.operation_sequence,
        inspection_checksum="sha256:" + "c" * 64,
        stopped_at="2026-10-04T00:00:00Z",
        exit_code=0,
    )
    journal.finish_cleanup(attempt_id, expected_sequence=record.operation_sequence)
    if verified:
        journal.update_runner_state(
            attempt_id, lambda local: {**local, "cleanup_verified": {"callback_id": "x"}}
        )
    return attempt_id


def _dirs(staging, journal, attempt_id):
    downloads = staging / "downloads" / attempt_id
    materialized = journal.materialized / attempt_id
    for directory in (downloads, materialized):
        directory.mkdir(parents=True)
        (directory / "input.json").write_bytes(b"{}")
    (materialized / "input.json").chmod(0o444)  # as the executor leaves it
    return downloads, materialized


@pytest.fixture
def layout(tmp_path):
    journal = ExecutionJournal(tmp_path / "journal")
    staging = tmp_path / "staging"
    staging.mkdir()
    return journal, staging


def test_only_verified_cleanups_lose_their_directories(layout):
    journal, staging = layout
    verified = _record(journal, verified=True)
    unverified = _record(journal, verified=False)
    running = _record(journal, verified=False, cleaned=False)
    unknown = str(new_uuid7())  # downloaded before prepare: nothing journaled
    kept = [
        *_dirs(staging, journal, unverified),
        *_dirs(staging, journal, running),
        *_dirs(staging, journal, unknown),
    ]
    removed = _dirs(staging, journal, verified)

    assert remove_released_attempt_dirs(journal, staging, adopted=()) == 2
    assert not any(path.exists() for path in removed)
    assert all((path / "input.json").exists() for path in kept)
    assert not journal._lock_path(unknown).exists()  # no lock file for unknown names
    assert remove_released_attempt_dirs(journal, staging, adopted=()) == 0


def test_adopted_attempts_symlinks_and_foreign_names_are_never_touched(layout, tmp_path):
    journal, staging = layout
    adopted = _record(journal, verified=True)
    adopted_dirs = _dirs(staging, journal, adopted)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"keep")
    linked = _record(journal, verified=True)
    (staging / "downloads" / linked).symlink_to(outside, target_is_directory=True)
    inner = _record(journal, verified=True)
    downloads, _materialized = _dirs(staging, journal, inner)
    (downloads / "escape").symlink_to(outside, target_is_directory=True)
    (staging / "downloads" / "not-an-attempt").mkdir()

    assert remove_released_attempt_dirs(journal, staging, adopted=(adopted,)) == 2
    assert all(path.exists() for path in adopted_dirs)
    assert (staging / "downloads" / linked).is_symlink()
    assert (outside / "keep").read_bytes() == b"keep"
    assert not downloads.exists()
    assert (staging / "downloads" / "not-an-attempt").is_dir()


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.mark.parametrize("area", ["downloads", "materialized"])
def test_a_symlinked_area_directory_is_never_followed(layout, tmp_path, area):
    """B19-RV09: downloads/ or materialized/ replaced by a symlink is skipped whole."""
    journal, staging = layout
    attempt_id = _record(journal, verified=True)
    downloads, materialized = _dirs(staging, journal, attempt_id)
    target = downloads if area == "downloads" else materialized
    outside = tmp_path / f"outside-{area}"
    shutil.move(target.parent, outside)  # the released attempt's dir now lives outside
    target.parent.symlink_to(outside, target_is_directory=True)
    other = materialized if area == "downloads" else downloads

    handler = _Records()
    # An earlier Alembic fileConfig in the same run may have disabled the logger.
    disabled, local_cleanup.LOG.disabled = local_cleanup.LOG.disabled, False
    local_cleanup.LOG.addHandler(handler)
    try:
        assert remove_released_attempt_dirs(journal, staging, adopted=()) == 1
    finally:
        local_cleanup.LOG.removeHandler(handler)
        local_cleanup.LOG.disabled = disabled
    assert (outside / attempt_id / "input.json").exists()
    assert target.parent.is_symlink()
    assert not other.exists()  # the other, real area is still cleaned
    (record,) = handler.records
    assert record.nexa_fields == {
        "event": "worker_local_cleanup_failed",
        "area": area,
        "error": "unsafe_area_directory",
    }


def test_each_pass_is_bounded_and_a_failure_skips_only_that_directory(layout, monkeypatch):
    journal, staging = layout
    attempts = [_record(journal, verified=True) for _ in range(3)]
    for attempt_id in attempts:
        _dirs(staging, journal, attempt_id)
    assert remove_released_attempt_dirs(journal, staging, adopted=(), limit=4) == 4
    assert remove_released_attempt_dirs(journal, staging, adopted=(), limit=4) == 2

    failing = _record(journal, verified=True)
    _dirs(staging, journal, failing)
    real = shutil.rmtree

    calls = []

    def rmtree(path, *args, **kwargs):
        calls.append(path)
        if len(calls) == 1:  # downloads/ is scanned before materialized/
            raise PermissionError("busy")
        return real(path, *args, **kwargs)

    rmtree.avoids_symlink_attacks = True
    monkeypatch.setattr(local_cleanup.shutil, "rmtree", rmtree)
    assert remove_released_attempt_dirs(journal, staging, adopted=()) == 1
    assert (staging / "downloads" / failing).exists()
    assert not (journal.materialized / failing).exists()


def test_without_symlink_safe_rmtree_nothing_is_removed(layout, monkeypatch):
    journal, staging = layout
    attempt_id = _record(journal, verified=True)
    removed = _dirs(staging, journal, attempt_id)
    monkeypatch.setattr(local_cleanup.shutil.rmtree, "avoids_symlink_attacks", False)
    assert remove_released_attempt_dirs(journal, staging, adopted=()) == 0
    assert all(path.exists() for path in removed)


class _Client:
    def reconciliation(self, *_args, **_kwargs):
        return {"items": [], "page": {"next_cursor": None}}


def test_the_agent_removes_directories_only_after_a_complete_scan(layout, monkeypatch):
    journal, staging = layout
    attempt_id = _record(journal, verified=True)
    paths = _dirs(staging, journal, attempt_id)
    agent = WorkerAgent.for_test(_Client(), worker_id="worker", incarnation_id="incarnation")
    agent.journal = journal
    agent.executor = SimpleNamespace(staging_root=staging)
    pending = [str(new_uuid7())]
    monkeypatch.setattr(agent, "_blocking_pending_attempts", lambda: list(pending))

    assert agent.reconcile_once().complete is False
    assert all(path.exists() for path in paths)
    pending.clear()
    agent._adopted = {attempt_id: object()}  # an adopted attempt is never touched
    assert agent.reconcile_once().complete is True
    assert all(path.exists() for path in paths)
    agent._adopted = {}
    assert agent.reconcile_once().complete is True
    assert not any(path.exists() for path in paths)
