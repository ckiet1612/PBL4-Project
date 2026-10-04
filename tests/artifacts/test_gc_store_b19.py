"""B19 GC G1/G2 filesystem decisions on a temporary store, without a DB."""

import hashlib
import os
import time
from uuid import uuid4

import pytest

from nexa.infrastructure.artifacts.store import ArtifactError, FilesystemArtifactStore

NAME = "0123456789abcdef0123456789abcdef"


def _checksum(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _store(tmp_path, **kwargs) -> FilesystemArtifactStore:
    store = FilesystemArtifactStore(tmp_path / "artifacts", **kwargs)
    store.bind_identity(store.create_identity(uuid4()))
    return store


def _age(path, seconds):
    old = time.time() - seconds
    os.utime(path, (old, old), follow_symlinks=False)


def test_scan_yields_only_regular_files_with_key_names(tmp_path):
    store = _store(tmp_path)
    blobs = store.root / "committed"
    (blobs / NAME).write_bytes(b"orphan")
    (blobs / "not-a-key").write_bytes(b"x")
    (blobs / ("f" * 32)).mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    (blobs / ("e" * 32)).symlink_to(outside)
    (store.root / "staging" / ("a" * 32)).write_bytes(b"staged")

    assert [(f.key, f.size_bytes) for f in store.scan("blobs")] == [(f"blobs/{NAME}", 6)]
    assert [f.key for f in store.scan("staging")] == ["staging/" + "a" * 32]
    with pytest.raises(ArtifactError):
        list(store.scan("../"))


def test_remove_scanned_unlinks_the_same_file_only_and_never_follows_symlinks(tmp_path):
    synced = []
    store = _store(tmp_path, fsync_fn=lambda fd: synced.append(fd) or os.fsync(fd))
    blob = store.root / "committed" / NAME
    blob.write_bytes(b"orphan")
    _age(blob, 7200)
    (scanned,) = store.scan("blobs")
    assert scanned.age_seconds(time.time()) > 7000

    # Rewritten after the scan: a different mtime/size is a different file.
    blob.write_bytes(b"orphan-rewritten")
    assert store.remove_scanned(scanned) is False and blob.exists()

    (scanned,) = store.scan("blobs")
    blob.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    blob.symlink_to(outside)
    assert store.remove_scanned(scanned) is False
    assert blob.is_symlink() and outside.read_bytes() == b"keep"

    blob.unlink()
    blob.write_bytes(b"orphan")
    (scanned,) = store.scan("blobs")
    synced.clear()
    assert store.remove_scanned(scanned) is True
    assert not blob.exists() and len(synced) == 1  # the directory fsync
    assert store.remove_scanned(scanned) is False  # already gone


def test_an_open_staging_handle_is_never_removed(tmp_path):
    store = _store(tmp_path)
    handle = store.begin_staging("tenant", 4, _checksum(b"data"), "application/octet-stream")
    assert store.active_staging_keys() == frozenset({handle.staged_key})
    (scanned,) = store.scan("staging")
    assert store.remove_scanned(scanned) is False
    store.append(handle, b"data")
    blob = store.commit_blob(handle)
    assert store.active_staging_keys() == frozenset()
    assert [f.key for f in store.scan("blobs")] == [blob.blob_key]


def test_an_unbound_or_replaced_store_is_never_swept(tmp_path):
    unbound = FilesystemArtifactStore(tmp_path / "artifacts")
    (unbound.root / "committed" / NAME).write_bytes(b"x")
    with pytest.raises(ArtifactError) as failure:
        list(unbound.scan("blobs"))
    assert failure.value.code == "storage_unavailable"

    store = _store(tmp_path / "bound")
    (store.root / "committed" / NAME).write_bytes(b"x")
    (scanned,) = store.scan("blobs")
    (store.root / "store-identity").unlink()
    with pytest.raises(ArtifactError):
        store.remove_scanned(scanned)
    assert (store.root / "committed" / NAME).exists()
