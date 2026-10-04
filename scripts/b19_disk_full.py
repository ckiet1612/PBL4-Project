"""B19 §7.C: a real disk-full at the artifact store level.

Runs inside the worker image (it ships the nexa package) with the artifact root on
a small tmpfs, so the kernel returns a real ENOSPC:

    docker run --rm --network none --read-only --tmpfs /artifacts:rw,size=16m \
        --mount type=bind,src=$PWD/scripts/b19_disk_full.py,dst=/b19_disk_full.py,readonly \
        --entrypoint python nexa/b19-worker:local /b19_disk_full.py /artifacts

Checks, in order: a committed 1 MiB blob; a 64 MiB upload hits ENOSPC and is
reported as `storage_full`; while the disk is full `check_readiness` fails;
`abort_staging` (what the services do on any upload error) removes the staging
file; committed/ holds only the first blob and its checksum is unchanged; the
readiness probe passes again. Prints one JSON summary; exit 0 only if all hold.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from nexa.infrastructure.artifacts.store import ArtifactError, FilesystemArtifactStore

MIB = 1024**2
CRITICAL_PERCENT = 95


def _checksum(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _usage(store: FilesystemArtifactStore) -> dict:
    total, free, percent = store.disk_usage()
    return {"total": total, "free": free, "used_percent": round(percent, 2)}


def _readiness(store: FilesystemArtifactStore) -> str:
    try:
        store.check_readiness(critical_watermark_percent=CRITICAL_PERCENT)
    except ArtifactError as exc:
        return f"{exc.code}: {exc.message}"
    return "ready"


def main(root: Path) -> int:
    store = FilesystemArtifactStore(root, max_file_bytes=1024 * MIB)
    store.bind_identity(store.create_identity(uuid4()))
    report: dict = {"root_fs": _usage(store), "critical_percent": CRITICAL_PERCENT}

    first = os.urandom(MIB)
    handle = store.begin_staging("b19", len(first), _checksum(first), "application/octet-stream")
    store.append(handle, first)
    committed = store.commit_blob(handle)
    report["first_blob"] = {"size": committed.size_bytes, "checksum": committed.checksum}
    report["readiness_before"] = _readiness(store)

    big = 64 * MIB
    chunk = os.urandom(MIB)
    handle = store.begin_staging("b19", big, "sha256:" + "0" * 64, "application/octet-stream")
    error = None
    written = 0
    while written < big:
        try:
            written = store.append(handle, chunk).received_bytes
        except ArtifactError as exc:
            error = {"code": exc.code, "message": exc.message, "received": exc.received_bytes}
            break
    report["enospc"] = error
    report["usage_when_full"] = _usage(store)
    report["staging_when_full"] = sorted(os.listdir(root / "staging"))
    report["readiness_when_full"] = _readiness(store)

    store.abort_staging(handle)
    report["staging_after_abort"] = sorted(os.listdir(root / "staging"))
    committed_files = sorted(os.listdir(root / "committed"))
    report["committed_after"] = committed_files
    report["first_blob_after"] = store.inspect(committed.blob_key).checksum
    report["usage_after"] = _usage(store)
    report["readiness_after"] = _readiness(store)

    checks = {
        "storage_full_code": bool(error) and error["code"] == "storage_full",
        "readiness_fails_when_full": report["readiness_when_full"] != "ready",
        "staging_removed": report["staging_after_abort"] == [],
        "only_first_blob_committed": committed_files == [committed.blob_key.split("/", 1)[1]],
        "first_blob_checksum_kept": report["first_blob_after"] == committed.checksum,
        "readiness_recovers": report["readiness_after"] == "ready",
    }
    report["checks"] = checks
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
