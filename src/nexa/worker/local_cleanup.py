"""Remove a finished attempt's local input copies (B19-R17).

`downloads/<attempt_id>` (verified inputs) and `materialized/<attempt_id>` (the
read-only copies mounted into the container) are removed only for an attempt whose
journal record is TOMBSTONED with `cleanup_verified`: the API acknowledged the
container's verified cleanup, so no container mounts them and the attempt can never
be adopted again. The agent calls this only after a complete reconciliation scan, so
on start it never runs before reconcile. An attempt without a journal record (inputs
downloaded before prepare) or still adopted is left alone. Directory symlinks and
non-UUID names are never followed or removed. `downloads/` and `materialized/` are
opened with O_NOFOLLOW and every scan and removal is relative to that descriptor, so a
symlink put in their place is skipped whole and logged (B19-RV09); `shutil.rmtree`
removes beneath the directory by file descriptor and does not follow symlinks inside
it. Each pass removes at most `limit` directories; a failure skips only that directory.
"""

import logging
import os
import shutil
from collections.abc import Collection
from pathlib import Path
from uuid import UUID

from .journal import ExecutionJournal, JournalCorruption

LOG = logging.getLogger(__name__)


def _attempt_name(name: str) -> bool:
    try:
        return str(UUID(name)) == name
    except ValueError:
        return False


def _released(journal: ExecutionJournal, attempt_id: str) -> bool:
    try:
        record = journal.load(attempt_id)
    except JournalCorruption:
        return False
    return (
        record.state == "TOMBSTONED"
        and (record.runner_state or {}).get("cleanup_verified") is not None
    )


def _cleanup_failed(area: str, error: str) -> None:
    LOG.warning(
        "worker local directory cleanup failed",
        extra={
            "nexa_fields": {"event": "worker_local_cleanup_failed", "area": area, "error": error}
        },
    )


def _open_area(root: Path) -> int | None:
    """A descriptor for a real `downloads/` or `materialized/` directory, else None."""
    try:
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:  # ELOOP / ENOTDIR: a symlink or a file took the directory's place
        _cleanup_failed(root.name, "unsafe_area_directory")
        return None


def remove_released_attempt_dirs(
    journal: ExecutionJournal,
    staging_root: str | Path,
    *,
    adopted: Collection[str],
    limit: int = 32,
) -> int:
    """Remove released attempts' local directories; returns how many were removed."""
    if not shutil.rmtree.avoids_symlink_attacks:
        return 0  # fail closed on a platform without descriptor-based removal
    removed = 0
    for root in (Path(staging_root) / "downloads", journal.materialized):
        area = _open_area(root)
        if area is None:
            continue
        try:
            removed += _remove_in_area(journal, root.name, area, adopted, limit - removed)
        finally:
            os.close(area)
    return removed


def _remove_in_area(
    journal: ExecutionJournal, area_name: str, area: int, adopted: Collection[str], limit: int
) -> int:
    removed = 0
    with os.scandir(area) as scan:
        entries = sorted(scan, key=lambda entry: entry.name)
    changed = False
    for entry in entries:
        if removed >= limit:
            break
        name = entry.name
        if (
            not _attempt_name(name)
            or name in adopted
            or not entry.is_dir(follow_symlinks=False)
            # Checked before locking, so unknown names never get a lock file.
            or not journal.path_for(name).exists()
        ):
            continue
        with journal.lock(name):
            if not _released(journal, name):
                continue
            try:
                shutil.rmtree(name, dir_fd=area)
            except OSError as exc:
                _cleanup_failed(area_name, type(exc).__name__)
                continue
        removed += 1
        changed = True
    if changed:
        os.fsync(area)
    return removed
