import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from nexa.infrastructure.artifacts.store import (
    ArtifactError,
    BlobRange,
    FilesystemArtifactStore,
)


def _checksum(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def test_stream_commit_inspect_and_single_range(tmp_path) -> None:
    payload = b"nexa"
    store = FilesystemArtifactStore(tmp_path, max_file_bytes=1024)

    handle = store.begin_staging(
        owner="tenant",
        expected_size=len(payload),
        expected_checksum=_checksum(payload),
        media_type="application/json",
    )
    assert store.append(handle, payload[:2]).received_bytes == 2
    assert store.append(handle, payload[2:]).received_bytes == len(payload)

    blob = store.commit_blob(handle)
    assert blob.size_bytes == len(payload)
    assert blob.checksum == _checksum(payload)
    assert store.inspect(blob.blob_key).size_bytes == len(payload)

    full = store.open(blob.blob_key)
    assert full.read() == payload
    full.close()
    ranged = store.open(blob.blob_key, BlobRange(1, 2))
    assert ranged.read() == b"ex"
    ranged.close()


def test_append_and_commit_enforce_declared_size_and_checksum(tmp_path) -> None:
    store = FilesystemArtifactStore(tmp_path, max_file_bytes=4)
    handle = store.begin_staging(
        owner="tenant", expected_size=3, expected_checksum=_checksum(b"abc"), media_type="x/y"
    )

    with pytest.raises(ArtifactError, match="declared size") as too_large:
        store.append(handle, b"abcd")
    assert too_large.value.code == "payload_too_large"

    store.append(handle, b"ab")
    with pytest.raises(ArtifactError) as short:
        store.commit_blob(handle)
    assert short.value.code == "size_mismatch"

    store.append(handle, b"c")
    wrong_handle = store.begin_staging(
        owner="tenant", expected_size=3, expected_checksum=_checksum(b"different"), media_type="x/y"
    )
    store.append(wrong_handle, b"abc")
    with pytest.raises(ArtifactError) as wrong_checksum:
        store.commit_blob(wrong_handle)
    assert wrong_checksum.value.code == "checksum_mismatch"


def test_append_completes_short_writes_and_reports_partial_storage_failure(tmp_path) -> None:
    store = FilesystemArtifactStore(tmp_path, max_file_bytes=32)
    handle = store.begin_staging(
        owner="tenant", expected_size=6, expected_checksum=_checksum(b"abcdef"), media_type="x/y"
    )
    state = store._active[handle._capability]

    class ShortWriter:
        def __init__(self) -> None:
            self.value = bytearray()

        def write(self, value: bytes) -> int:
            count = min(2, len(value))
            self.value.extend(value[:count])
            return count

    short_writer = ShortWriter()
    state.file = short_writer
    assert store.append(handle, b"abcdef").received_bytes == 6
    assert bytes(short_writer.value) == b"abcdef"

    class FailingWriter(ShortWriter):
        def write(self, value: bytes) -> int:
            if self.value:
                raise OSError("injected disk-full")
            return super().write(value)

    failing_handle = store.begin_staging(
        owner="tenant", expected_size=4, expected_checksum=_checksum(b"wxyz"), media_type="x/y"
    )
    failing_state = store._active[failing_handle._capability]
    failing_writer = FailingWriter()
    failing_state.file = failing_writer
    with pytest.raises(ArtifactError) as error:
        store.append(failing_handle, b"wxyz")
    assert error.value.code == "storage_unavailable"
    assert error.value.received_bytes == 2


def test_faults_fail_closed_without_turning_staging_into_a_blob(tmp_path) -> None:
    payload = b"fault"
    calls: list[str] = []

    def fail_fsync(_fd: int) -> None:
        calls.append("fsync")
        raise OSError("injected")

    store = FilesystemArtifactStore(tmp_path, fsync_fn=fail_fsync)
    handle = store.begin_staging(
        owner="tenant",
        expected_size=len(payload),
        expected_checksum=_checksum(payload),
        media_type="x/y",
    )
    store.append(handle, payload)
    with pytest.raises(ArtifactError) as error:
        store.commit_blob(handle)
    assert error.value.code == "storage_unavailable"
    assert calls == ["fsync"]
    assert not list((tmp_path / "committed").iterdir())


def test_rename_and_directory_fsync_faults_are_reported(tmp_path) -> None:
    payload = b"fault"
    store = FilesystemArtifactStore(
        tmp_path,
        rename_fn=lambda _source, _target: (_ for _ in ()).throw(OSError("rename")),
    )
    handle = store.begin_staging(
        owner="tenant",
        expected_size=len(payload),
        expected_checksum=_checksum(payload),
        media_type="x/y",
    )
    store.append(handle, payload)
    with pytest.raises(ArtifactError) as error:
        store.commit_blob(handle)
    assert error.value.code == "storage_unavailable"

    fsync_calls = 0

    def fail_directory_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("directory fsync")

    store = FilesystemArtifactStore(tmp_path / "second", fsync_fn=fail_directory_fsync)
    handle = store.begin_staging(
        owner="tenant",
        expected_size=len(payload),
        expected_checksum=_checksum(payload),
        media_type="x/y",
    )
    store.append(handle, payload)
    with pytest.raises(ArtifactError) as error:
        store.commit_blob(handle)
    assert error.value.code == "storage_unavailable"


def test_opaque_keys_reject_traversal_and_symlink_escape(tmp_path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    outside = tmp_path.parent / "outside-artifact"
    outside.write_bytes(b"secret")
    (tmp_path / "committed" / "link").symlink_to(outside)

    for key in ("../outside-artifact", "/etc/passwd", r"..\outside-artifact", "link"):
        with pytest.raises(ArtifactError) as error:
            store.inspect(key)
        assert error.value.code == "invalid_blob_key"


def test_store_rejects_symlinked_storage_parent(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "staging").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "committed").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactError) as error:
        FilesystemArtifactStore(root)
    assert error.value.code == "storage_unavailable"


def test_staging_key_is_never_read_as_a_committed_blob(tmp_path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    handle = store.begin_staging(
        owner="tenant", expected_size=1, expected_checksum=_checksum(b"x"), media_type="x/y"
    )
    store.append(handle, b"x")
    with pytest.raises(ArtifactError) as error:
        store.open(handle.staged_key)
    assert error.value.code == "invalid_blob_key"


def test_delete_requires_unexpired_matching_gc_token(tmp_path) -> None:
    payload = b"delete"
    store = FilesystemArtifactStore(tmp_path)
    handle = store.begin_staging(
        owner="tenant",
        expected_size=len(payload),
        expected_checksum=_checksum(payload),
        media_type="x/y",
    )
    store.append(handle, payload)
    blob = store.commit_blob(handle)

    expired = store.issue_gc_token(
        blob.blob_key,
        blob.checksum,
        blob.size_bytes,
        datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(ArtifactError) as error:
        store.delete_unreferenced(blob.blob_key, expired)
    assert error.value.code == "invalid_gc_token"

    valid = store.issue_gc_token(
        blob.blob_key,
        blob.checksum,
        blob.size_bytes,
        datetime.now(UTC) + timedelta(minutes=1),
    )
    assert store.delete_unreferenced(blob.blob_key, valid).deleted is True
    with pytest.raises(ArtifactError, match="not found"):
        store.inspect(blob.blob_key)
