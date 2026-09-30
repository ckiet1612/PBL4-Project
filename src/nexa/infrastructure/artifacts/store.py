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
from uuid import UUID


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
# B14-OBS-01: the root names the store the deployment bound; see ``_missing``.
_IDENTITY_NAME = "store-identity"
_IDENTITY_PATTERN = re.compile(
    rb"^nexa-artifact-store v1 ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\n$"
)
_IDENTITY_MAX_BYTES = 128


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
        self._identity: UUID | None = None

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
            # The key is well formed; only the storage directory can be at fault.
            raise ArtifactError(
                "storage_unavailable", "Artifact storage directory is unavailable"
            ) from exc
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

    def read_identity(self) -> UUID | None:
        """The store identity recorded at the root, or None when the root records none."""
        try:
            fd = os.open(self.root / _IDENTITY_NAME, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ArtifactError(
                "storage_unavailable", "Artifact storage identity cannot be read"
            ) from exc
        try:
            if not stat_module.S_ISREG(os.fstat(fd).st_mode):
                raise ArtifactError("storage_unavailable", "Artifact storage identity is invalid")
            raw = os.read(fd, _IDENTITY_MAX_BYTES + 1)
        except OSError as exc:
            raise ArtifactError(
                "storage_unavailable", "Artifact storage identity cannot be read"
            ) from exc
        finally:
            os.close(fd)
        match = _IDENTITY_PATTERN.fullmatch(raw)
        if match is None:
            raise ArtifactError("storage_unavailable", "Artifact storage identity is invalid")
        return UUID(match.group(1).decode())

    def create_identity(self, store_id: UUID) -> UUID:
        """Record `store_id` at the root unless an identity exists; return the recorded one."""
        marker = self.root / _IDENTITY_NAME
        temporary = self.root / f".{_IDENTITY_NAME}-{secrets.token_hex(16)}"
        try:
            fd = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.write(fd, f"nexa-artifact-store v1 {store_id}\n".encode())
                self._fsync(fd)
            finally:
                os.close(fd)
            # A hard link never replaces an identity another process recorded first.
            with suppress(FileExistsError):
                os.link(temporary, marker, follow_symlinks=False)
            temporary.unlink()
            directory_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                self._fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise ArtifactError(
                "storage_unavailable", "Artifact storage identity cannot be recorded"
            ) from exc
        finally:
            with suppress(OSError):
                temporary.unlink()
        recorded = self.read_identity()
        if recorded is None:
            raise ArtifactError("storage_unavailable", "Artifact storage identity was not recorded")
        return recorded

    def bind_identity(self, store_id: UUID) -> None:
        """Bind the identity the deployment recorded; only then can a blob be missing."""
        self._identity = store_id

    def has_committed_blobs(self) -> bool:
        try:
            with os.scandir(self._committed_root) as entries:
                return any(True for _ in entries)
        except OSError as exc:
            raise ArtifactError(
                "storage_unavailable", "Artifact committed directory cannot be listed"
            ) from exc

    def _verify_bound(self) -> None:
        """Raise unless the root records the bound identity above a real ``committed``."""
        unverified = ArtifactError(
            "storage_unavailable", "Artifact storage identity is not verified"
        )
        if self._identity is None or self.read_identity() != self._identity:
            raise unverified
        try:
            committed = os.lstat(self._committed_root)
        except OSError as exc:
            raise unverified from exc
        if stat_module.S_ISLNK(committed.st_mode) or not stat_module.S_ISDIR(committed.st_mode):
            raise unverified

    def _unverified_absence(self, path: Path) -> ArtifactError | None:
        """None if the bound store is intact and `path` is absent; else the outage to raise.

        A missing root or ``committed`` directory, a re-created tree, another store's
        volume or an unreadable identity makes an absent file no evidence about the blob:
        callers must treat it as a temporary outage, never as a lost blob (B14-OBS-01).
        """
        try:
            self._verify_bound()
            os.lstat(path)
        except FileNotFoundError:
            return None
        except ArtifactError as exc:
            return exc
        except OSError:
            return ArtifactError("storage_unavailable", "Artifact blob cannot be inspected")
        # The blob reappeared between the two observations; report the race as transient.
        return ArtifactError("storage_unavailable", "Artifact blob changed during inspection")

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
        try:
            stat = self._regular_file(path)
        except ArtifactError as exc:
            unverified = self._unverified_absence(path) if exc.code == "not_found" else None
            if unverified is None:
                raise
            raise unverified from exc
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
        try:
            self._regular_file(path)
        except ArtifactError as exc:
            unverified = self._unverified_absence(path) if exc.code == "not_found" else None
            if unverified is None:
                raise
            raise unverified from exc
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

    def check_readiness(self, *, critical_watermark_percent: int) -> None:
        """Verify current storage durability without retaining a probe artifact."""
        _total, _free, used_percent = self.disk_usage()
        if used_percent >= critical_watermark_percent:
            raise ArtifactError(
                "storage_unavailable", "Artifact storage is at the critical watermark"
            )
        probe = self._staging_root / f".readiness-{secrets.token_hex(16)}"
        directory_fd = None
        try:
            fd = os.open(
                probe,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.write(fd, b"nexa-storage-readiness\n")
                self._fsync(fd)
            finally:
                os.close(fd)
            directory_fd = os.open(self._staging_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            self._fsync(directory_fd)
            probe.unlink()
            self._fsync(directory_fd)
        except OSError as exc:
            raise ArtifactError(
                "storage_unavailable", "Artifact storage readiness probe failed"
            ) from exc
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            with suppress(OSError):
                probe.unlink()
        # A worker is not READY on a store it cannot verify (B14-OBS-01), so nothing
        # dispatches onto an empty or wrong volume.
        self._verify_bound()

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
