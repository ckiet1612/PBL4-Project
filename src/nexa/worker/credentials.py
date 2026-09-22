"""One-time worker credential storage; never journal the secret."""

import json
import os
import stat
import tempfile
from pathlib import Path


class CredentialRecoveryRequired(RuntimeError):
    pass


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".worker-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class CredentialStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, *, worker_id: str, installation_id: str) -> str | None:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise CredentialRecoveryRequired("worker credential permissions are invalid")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(4097)
            if len(raw) > 4096:
                raise ValueError("credential storage exceeded bound")
            value = json.loads(raw)
            if (
                set(value) != {"installation_id", "worker_id", "credential"}
                or value["installation_id"] != installation_id
                or value["worker_id"] != worker_id
                or not isinstance(value["credential"], str)
                or not value["credential"]
            ):
                raise ValueError("credential identity mismatch")
            return value["credential"]
        except (OSError, ValueError, TypeError) as exc:
            raise CredentialRecoveryRequired("worker credential storage is invalid") from exc
        finally:
            os.close(fd)

    def save(self, *, worker_id: str, installation_id: str, credential: str) -> None:
        if self.path.exists():
            raise CredentialRecoveryRequired("worker credential already exists; rotation is manual")
        if not credential:
            raise ValueError("empty worker credential")
        _atomic_private_json(
            self.path,
            {
                "installation_id": installation_id,
                "worker_id": worker_id,
                "credential": credential,
            },
        )
