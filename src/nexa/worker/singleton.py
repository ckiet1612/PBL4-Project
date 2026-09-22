"""A stable, process-lifetime local worker lock."""

import fcntl
import os
from pathlib import Path


class WorkerAlreadyRunning(RuntimeError):
    pass


class LocalWorkerLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "LocalWorkerLock":
        if self.fd is not None:
            raise RuntimeError("worker lock is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            if not os.path.isfile(self.path) or os.fstat(fd).st_ino != os.stat(self.path).st_ino:
                raise RuntimeError("worker lock inode changed")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise WorkerAlreadyRunning("local worker is already running") from exc
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
