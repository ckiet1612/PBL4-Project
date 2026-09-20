from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat as stat_module
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class ArtifactError(Exception):
    """Safe, stable error raised by the artifact store."""

    def __init__(self, code: str, message: str, *, received_bytes: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.received_bytes = received_bytes


@dataclass(frozen=True, slots=True)
class BlobRange:
    start: int
    end: int | None = None

    def __post_init__(self) -> None:
        if self.start < 0 or (self.end is not None and self.end < self.start):
            raise ValueError("invalid byte range")


@dataclass(frozen=True, slots=True)
class Progress:
    received_bytes: int
    expected_size: int


@dataclass(frozen=True, slots=True)
class DurableBlob:
    blob_key: str
    size_bytes: int
    checksum: str
    media_type: str


@dataclass(frozen=True, slots=True)
class BlobStat:
    blob_key: str
    size_bytes: int
    checksum: str


@dataclass(frozen=True, slots=True)
class GcToken:
    blob_key: str
    checksum: str
    size_bytes: int
    expires_at: datetime
    _capability: str = ""


@dataclass(frozen=True, slots=True)
class StagingHandle:
    """Opaque in-process capability for a single active upload."""

    _capability: str
    owner: str
    upload_id: str
    staged_key: str
    expected_size: int
    expected_checksum: str
    media_type: str


@dataclass(slots=True)
class _StagingState:
    handle: StagingHandle
    path: Path
    file: object
    received_bytes: int
    hasher: hashlib._Hash


class BoundedReader:
    def __init__(self, file: object, remaining: int) -> None:
        self._file = file
        self._remaining = remaining
        self._closed = False

    @property
    def remaining(self) -> int:
        return self._remaining

    def read(self, size: int = -1) -> bytes:
        if self._closed or self._remaining == 0:
            return b""
        if size < 0 or size > self._remaining:
            size = self._remaining
        try:
            value = self._file.read(size)  # type: ignore[attr-defined]
        except OSError as exc:
            raise ArtifactError("storage_unavailable", "Artifact content cannot be read") from exc
        if not isinstance(value, bytes):
            raise ArtifactError("storage_unavailable", "Artifact content cannot be read")
        self._remaining -= len(value)
        return value

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            with suppress(OSError):
                self._file.close()  # type: ignore[attr-defined]

    def __enter__(self) -> BoundedReader:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


_CHECKSUM_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_BLOB_KEY_PATTERN = re.compile(r"^(?:blobs|staging)/[0-9a-f]{32}$")


class FilesystemArtifactStore:
    """Single-node durable store with opaque server-generated keys."""

    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int = 10 * 1024**3,
        fsync_fn: Callable[[int], None] | None = None,
        rename_fn: Callable[[str, str], None] | None = None,
        statvfs_fn: Callable[[str], os.statvfs_result] | None = None,
        mkdir_fn: Callable[..., None] | None = None,
    ) -> None:
        if not root.is_absolute():
            raise ValueError("artifact root must be absolute")
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        self.root = root
        self.max_file_bytes = max_file_bytes
        self._fsync = fsync_fn or os.fsync
        self._rename = rename_fn or os.replace
        self._statvfs = statvfs_fn or os.statvfs
        self._mkdir = mkdir_fn or os.makedirs
        self._staging_root = root / "staging"
        self._committed_root = root / "committed"
        try:
            if root.is_symlink():
                raise ArtifactError(
                    "storage_unavailable", "Artifact storage root cannot be a symlink"
                )
            self._mkdir(self._staging_root, mode=0o700, exist_ok=True)
            self._mkdir(self._committed_root, mode=0o700, exist_ok=True)
            for directory in (root, self._staging_root, self._committed_root):
                directory_stat = os.lstat(directory)
                if stat_module.S_ISLNK(directory_stat.st_mode) or not stat_module.S_ISDIR(
                    directory_stat.st_mode
                ):
                    raise ArtifactError(
                        "storage_unavailable",
                        "Artifact storage directories must be real directories",
                    )
            staging_device = self._staging_root.stat().st_dev
            committed_device = self._committed_root.stat().st_dev
        except ArtifactError:
            raise
        except OSError as exc:
            raise ArtifactError(
                "storage_unavailable", "Artifact storage root cannot be prepared"
            ) from exc
        if staging_device != committed_device:
            raise ArtifactError(
                "storage_unavailable",
                "Artifact staging and committed storage must share a filesystem",
            )
        self._active: dict[str, _StagingState] = {}
        self._gc_tokens: dict[str, GcToken] = {}

    @staticmethod
    def _validate_checksum(checksum: str) -> None:
        if not _CHECKSUM_PATTERN.fullmatch(checksum):
            raise ArtifactError("validation_failed", "Artifact checksum is invalid")

    def _path_for_key(self, key: str) -> Path:
        if not isinstance(key, str) or not _BLOB_KEY_PATTERN.fullmatch(key):
            raise ArtifactError("invalid_blob_key", "Artifact blob key is invalid")
        prefix, name = key.split("/", 1)
        parent = self._committed_root if prefix == "blobs" else self._staging_root
        path = parent / name
        try:
            parent_stat = os.lstat(parent)
            if stat_module.S_ISLNK(parent_stat.st_mode) or not stat_module.S_ISDIR(
                parent_stat.st_mode
            ):
                raise ArtifactError(
                    "storage_unavailable", "Artifact storage directory is unavailable"
                )
            if path.parent.resolve(strict=True) != parent.resolve(strict=True):
                raise ArtifactError("invalid_blob_key", "Artifact blob key is invalid")
        except OSError as exc:
            raise ArtifactError("invalid_blob_key", "Artifact blob key is invalid") from exc
        return path

    @staticmethod
    def _regular_file(path: Path, *, missing_code: str = "not_found") -> os.stat_result:
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                stat = os.fstat(fd)
            finally:
                os.close(fd)
        except FileNotFoundError as exc:
            raise ArtifactError(missing_code, "Artifact blob was not found") from exc
        except OSError as exc:
            raise ArtifactError("storage_unavailable", "Artifact blob cannot be inspected") from exc
        if not stat_module.S_ISREG(stat.st_mode):
            raise ArtifactError("invalid_blob_key", "Artifact blob is not a regular file")
        return stat

    def begin_staging(
        self,
        owner: str,
        expected_size: int,
        expected_checksum: str,
        media_type: str,
        *,
        upload_id: str | None = None,
    ) -> StagingHandle:
        if not owner:
            raise ArtifactError("validation_failed", "Upload owner is required")
        if expected_size < 0 or expected_size > self.max_file_bytes:
            raise ArtifactError("payload_too_large", "Artifact size exceeds the configured limit")
        self._validate_checksum(expected_checksum)
        if not media_type or len(media_type) > 127:
            raise ArtifactError("validation_failed", "Artifact media type is invalid")
        upload_id = upload_id or secrets.token_hex(16)
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
            raise ArtifactError("validation_failed", "Upload identity is invalid")
        staged_key = f"staging/{upload_id}"
        path = self._path_for_key(staged_key)
        try:
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            file = os.fdopen(fd, "wb", buffering=0)
        except FileExistsError as exc:
            raise ArtifactError("state_conflict", "Upload identity is already active") from exc
        except OSError as exc:
            raise ArtifactError(
                "storage_unavailable", "Artifact staging cannot be created"
            ) from exc
        handle = StagingHandle(
            _capability=secrets.token_urlsafe(24),
            owner=owner,
            upload_id=upload_id,
            staged_key=staged_key,
            expected_size=expected_size,
            expected_checksum=expected_checksum,
            media_type=media_type,
        )
        self._active[handle._capability] = _StagingState(
            handle=handle,
            path=path,
            file=file,
            received_bytes=0,
            hasher=hashlib.sha256(),
        )
        return handle

    def _state(self, handle: StagingHandle) -> _StagingState:
        state = self._active.get(handle._capability)
        if state is None or state.handle != handle:
            raise ArtifactError("state_conflict", "Upload staging handle is invalid or expired")
        return state

    def append(self, handle: StagingHandle, value: bytes) -> Progress:
        state = self._state(handle)
        if not isinstance(value, bytes):
            raise ArtifactError("validation_failed", "Artifact chunks must be bytes")
        next_size = state.received_bytes + len(value)
        if next_size > handle.expected_size or next_size > self.max_file_bytes:
            raise ArtifactError("payload_too_large", "Artifact stream exceeds the declared size")
        offset = 0
        while offset < len(value):
            try:
                written = state.file.write(value[offset:])  # type: ignore[attr-defined]
            except OSError as exc:
                raise ArtifactError(
                    "storage_unavailable",
                    "Artifact staging cannot be written",
                    received_bytes=state.received_bytes,
                ) from exc
            if not isinstance(written, int) or written <= 0 or written > len(value) - offset:
                raise ArtifactError(
                    "storage_unavailable",
                    "Artifact staging cannot be written",
                    received_bytes=state.received_bytes,
                )
            state.hasher.update(value[offset : offset + written])
            state.received_bytes += written
            offset += written
        return Progress(state.received_bytes, handle.expected_size)

    def commit_blob(self, handle: StagingHandle) -> DurableBlob:
        state = self._state(handle)
        if state.received_bytes != handle.expected_size:
            raise ArtifactError("size_mismatch", "Received bytes do not match the declared size")
        checksum = f"sha256:{state.hasher.hexdigest()}"
        if checksum != handle.expected_checksum:
            raise ArtifactError(
                "checksum_mismatch", "Artifact checksum does not match the declaration"
            )
        try:
            self._fsync(state.file.fileno())  # type: ignore[attr-defined]
            state.file.close()  # type: ignore[attr-defined]
        except OSError as exc:
            raise ArtifactError(
                "storage_unavailable", "Artifact staging cannot be made durable"
            ) from exc
        blob_key = f"blobs/{secrets.token_hex(16)}"
        destination = self._path_for_key(blob_key)
        try:
            self._rename(str(state.path), str(destination))
        except OSError as exc:
            raise ArtifactError("storage_unavailable", "Artifact blob cannot be committed") from exc
        try:
            directory_fd = os.open(
                self._committed_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                self._fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise ArtifactError(
                "storage_unavailable", "Artifact committed directory cannot be synced"
            ) from exc
        self._active.pop(handle._capability, None)
        return DurableBlob(blob_key, handle.expected_size, checksum, handle.media_type)

    def abort_staging(self, handle: StagingHandle) -> None:
        state = self._active.pop(handle._capability, None)
        if state is None:
            return
        with suppress(OSError):
            state.file.close()  # type: ignore[attr-defined]
        try:
            state.path.unlink(missing_ok=True)
            directory_fd = os.open(self._staging_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                self._fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # A failed cleanup remains an expiring orphan for reconciliation.
            return

    def _open_regular(self, path: Path) -> object:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            stat = os.fstat(fd)
        except FileNotFoundError as exc:
            raise ArtifactError("not_found", "Artifact blob was not found") from exc
        except OSError as exc:
            raise ArtifactError("storage_unavailable", "Artifact blob cannot be opened") from exc
        if not stat_module.S_ISREG(stat.st_mode):
            os.close(fd)
            raise ArtifactError("invalid_blob_key", "Artifact blob is not a regular file")
        return os.fdopen(fd, "rb", buffering=0)

    def open(self, blob_key: str, byte_range: BlobRange | None = None) -> BoundedReader:
        if not blob_key.startswith("blobs/"):
            raise ArtifactError("invalid_blob_key", "Artifact blob key is invalid")
        path = self._path_for_key(blob_key)
        stat = self._regular_file(path)
        start = 0
        length = stat.st_size
        if byte_range is not None:
            if byte_range.start >= stat.st_size:
                raise ArtifactError(
                    "validation_failed", "Requested byte range is outside the artifact"
                )
            start = byte_range.start
            end = byte_range.end if byte_range.end is not None else stat.st_size - 1
            end = min(end, stat.st_size - 1)
            length = end - start + 1
        file = self._open_regular(path)
        try:
            file.seek(start)  # type: ignore[attr-defined]
        except OSError as exc:
            file.close()  # type: ignore[attr-defined]
            raise ArtifactError(
                "storage_unavailable", "Artifact blob cannot be positioned"
            ) from exc
        return BoundedReader(file, length)

    def inspect(self, blob_key: str) -> BlobStat:
        if not blob_key.startswith("blobs/"):
            raise ArtifactError("invalid_blob_key", "Artifact blob key is invalid")
        path = self._path_for_key(blob_key)
        self._regular_file(path)
        file = self._open_regular(path)
        digest = hashlib.sha256()
        size = 0
        try:
            while True:
                chunk = file.read(1024 * 1024)  # type: ignore[attr-defined]
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
        except OSError as exc:
            raise ArtifactError("storage_unavailable", "Artifact blob cannot be inspected") from exc
        finally:
            file.close()  # type: ignore[attr-defined]
        return BlobStat(blob_key, size, f"sha256:{digest.hexdigest()}")

    def disk_usage(self, expected_bytes: int = 0) -> tuple[int, int, float]:
        try:
            stats = self._statvfs(str(self.root))
            total = int(stats.f_frsize * stats.f_blocks)
            free = int(stats.f_frsize * stats.f_bavail)
        except OSError as exc:
            raise ArtifactError(
                "storage_unavailable", "Artifact storage capacity cannot be checked"
            ) from exc
        if total <= 0 or free < 0 or free > total:
            raise ArtifactError("storage_unavailable", "Artifact storage capacity is invalid")
        used_percent = ((total - free + expected_bytes) / total) * 100
        return total, free, used_percent

    def delete_unreferenced(self, blob_key: str, gc_token: GcToken) -> DeleteResult:
        issued = self._gc_tokens.get(gc_token._capability)
        if (
            issued is None
            or issued != gc_token
            or gc_token.blob_key != blob_key
            or gc_token.expires_at.astimezone(UTC) <= datetime.now(UTC)
        ):
            raise ArtifactError("invalid_gc_token", "Garbage-collection claim is invalid")
        stat = self.inspect(blob_key)
        if stat.checksum != gc_token.checksum or stat.size_bytes != gc_token.size_bytes:
            raise ArtifactError(
                "invalid_gc_token", "Garbage-collection claim does not match the blob"
            )
        path = self._path_for_key(blob_key)
        try:
            path.unlink()
            directory_fd = os.open(
                self._committed_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                self._fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except FileNotFoundError as exc:
            raise ArtifactError("not_found", "Artifact blob was not found") from exc
        except OSError as exc:
            raise ArtifactError("storage_unavailable", "Artifact blob cannot be reclaimed") from exc
        self._gc_tokens.pop(gc_token._capability, None)
        return DeleteResult(deleted=True)

    def issue_gc_token(
        self, blob_key: str, checksum: str, size_bytes: int, expires_at: datetime
    ) -> GcToken:
        stat = self.inspect(blob_key)
        if stat.checksum != checksum or stat.size_bytes != size_bytes:
            raise ArtifactError(
                "invalid_gc_token", "Garbage-collection claim does not match the blob"
            )
        capability = secrets.token_urlsafe(24)
        token = GcToken(blob_key, checksum, size_bytes, expires_at, capability)
        self._gc_tokens[capability] = token
        return token


@dataclass(frozen=True, slots=True)
class DeleteResult:
    deleted: bool


FileSystemArtifactStore = FilesystemArtifactStore


__all__ = [
    "ArtifactError",
    "BlobRange",
    "BlobStat",
    "BoundedReader",
    "DeleteResult",
    "DurableBlob",
    "FileSystemArtifactStore",
    "FilesystemArtifactStore",
    "GcToken",
    "Progress",
    "StagingHandle",
]
