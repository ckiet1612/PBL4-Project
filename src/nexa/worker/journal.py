"""Durable local execution bookkeeping for one attempt."""

import fcntl
import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .models import Authority, ContainerIdentity, ResourceVector


class JournalCorruption(RuntimeError):
    pass


class JournalWriteError(RuntimeError):
    pass


JOURNAL_STATES = frozenset(
    {
        "PREPARED",
        "CREATE_IN_FLIGHT",
        "CREATED",
        "START_IN_FLIGHT",
        "STARTED",
        "CLEANUP_IN_FLIGHT",
        "TOMBSTONED",
    }
)


@dataclass(frozen=True, slots=True)
class JournalRecord:
    attempt_id: str
    allocation_id: str
    startup_nonce: str
    authority: Authority
    resources: ResourceVector
    image_digest: str | None
    input_checksum: str | None
    execution_binding: dict[str, object]
    state: str
    operation_sequence: int
    container: ContainerIdentity | None = None
    startup_started_monotonic_ns: int | None = None
    startup_clock_domain: str | None = None
    tombstone_sequence: int | None = None
    reason: str | None = None
    inspection_checksum: str | None = None
    stopped_at: str | None = None
    exit_code: int | None = None
    runner_state: dict[str, object] | None = None


def _json_value(value: object) -> object:
    if isinstance(value, Authority | ResourceVector | ContainerIdentity | JournalRecord):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    return value


def _canonical_mapping(value: Mapping[str, object]) -> dict[str, object]:
    encoded = json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"))
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise JournalWriteError("execution binding must be an object")
    return decoded


class ExecutionJournal:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.records = self.root / "records"
        self.locks = self.root / "locks"
        self.materialized = self.root / "materialized"
        self.records.mkdir(parents=True, exist_ok=True)
        self.locks.mkdir(parents=True, exist_ok=True)
        self.materialized.mkdir(parents=True, exist_ok=True)
        self._thread_lock_guard = threading.Lock()
        self._thread_locks: dict[str, threading.RLock] = {}
        self._thread_depth: dict[str, int] = {}

    def path_for(self, attempt_id: str) -> Path:
        return self.records / f"{attempt_id}.json"

    def exists(self, attempt_id: str) -> bool:
        return self.path_for(attempt_id).exists()

    def _lock_path(self, attempt_id: str) -> Path:
        return self.locks / f"{attempt_id}.lock"

    @contextmanager
    def lock(self, attempt_id: str) -> Iterator[None]:
        with self._thread_lock_guard:
            thread_lock = self._thread_locks.setdefault(attempt_id, threading.RLock())
        with thread_lock:
            depth = self._thread_depth.get(attempt_id, 0)
            self._thread_depth[attempt_id] = depth + 1
            lock_file = None
            if depth == 0:
                lock_file = self._lock_path(attempt_id).open("a+b")
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                self._thread_depth[attempt_id] -= 1
                if lock_file is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    lock_file.close()

    def load(self, attempt_id: str) -> JournalRecord:
        path = self.path_for(attempt_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("record must be an object")
            authority = Authority(**payload.pop("authority"))
            resources = ResourceVector(**payload.pop("resources"))
            container_payload = payload.pop("container", None)
            container = (
                ContainerIdentity(**container_payload) if container_payload is not None else None
            )
            record = JournalRecord(
                authority=authority, resources=resources, container=container, **payload
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise JournalCorruption(f"journal record is invalid for attempt {attempt_id}") from exc
        if (
            record.attempt_id != attempt_id
            or record.operation_sequence < 1
            or record.state not in JOURNAL_STATES
            or not isinstance(record.execution_binding, dict)
        ):
            raise JournalCorruption(f"journal identity is invalid for attempt {attempt_id}")
        return record

    def _write(self, record: JournalRecord) -> JournalRecord:
        path = self.path_for(record.attempt_id)
        payload = json.dumps(_json_value(record), sort_keys=True, separators=(",", ":")).encode()
        try:
            fd, name = tempfile.mkstemp(prefix=f".{record.attempt_id}.", dir=self.records)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, path)
                directory_fd = os.open(self.records, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        except OSError as exc:
            raise JournalWriteError("journal write failed") from exc
        return record

    def prepare(
        self,
        *,
        attempt_id: str,
        allocation_id: str,
        startup_nonce: str,
        authority: Authority,
        resources: ResourceVector,
        image_digest: str,
        execution_binding: Mapping[str, object],
        input_checksum: str | None = None,
    ) -> JournalRecord:
        binding = _canonical_mapping(execution_binding)
        with self.lock(attempt_id):
            if self.exists(attempt_id):
                record = self.load(attempt_id)
                if (
                    record.allocation_id != allocation_id
                    or record.startup_nonce != startup_nonce
                    or record.image_digest != image_digest
                    or record.input_checksum != input_checksum
                    or record.authority != authority
                    or record.resources != resources
                    or record.execution_binding != binding
                ):
                    raise JournalWriteError("immutable execution context mismatch")
                return record
            return self._write(
                JournalRecord(
                    attempt_id=attempt_id,
                    allocation_id=allocation_id,
                    startup_nonce=startup_nonce,
                    authority=authority,
                    resources=resources,
                    image_digest=image_digest,
                    input_checksum=input_checksum,
                    execution_binding=binding,
                    state="PREPARED",
                    operation_sequence=1,
                )
            )

    def begin_create(
        self,
        attempt_id: str,
        *,
        expected_sequence: int,
        started_monotonic_ns: int | None = None,
        clock_domain: str | None = None,
    ) -> JournalRecord:
        with self.lock(attempt_id):
            record = self.load(attempt_id)
            if record.state == "TOMBSTONED":
                raise JournalWriteError("attempt is tombstoned")
            self._expect_sequence(record, expected_sequence)
            if record.state != "PREPARED":
                return record
            return self._write(
                replace(
                    record,
                    state="CREATE_IN_FLIGHT",
                    operation_sequence=record.operation_sequence + 1,
                    startup_started_monotonic_ns=(
                        started_monotonic_ns
                        if started_monotonic_ns is not None
                        else record.startup_started_monotonic_ns
                    ),
                    startup_clock_domain=(
                        clock_domain if clock_domain is not None else record.startup_clock_domain
                    ),
                )
            )

    def bind_created(self, attempt_id: str, identity: ContainerIdentity) -> JournalRecord:
        with self.lock(attempt_id):
            record = self.load(attempt_id)
            if (
                identity.attempt_id != record.attempt_id
                or identity.allocation_id != record.allocation_id
                or identity.startup_nonce != record.startup_nonce
            ):
                raise JournalWriteError("container identity mismatch")
            if record.container is not None and record.container != identity:
                raise JournalWriteError("container identity conflict")
            if record.container == identity and record.state in {
                "CREATED",
                "START_IN_FLIGHT",
                "STARTED",
                "CLEANUP_IN_FLIGHT",
                "TOMBSTONED",
            }:
                return record
            if record.state != "CREATE_IN_FLIGHT":
                raise JournalWriteError("create identity cannot be bound in current state")
            return self._write(
                replace(
                    record,
                    container=identity,
                    state="CREATED",
                    operation_sequence=record.operation_sequence + 1,
                )
            )

    def begin_start(self, attempt_id: str, *, expected_sequence: int) -> JournalRecord:
        with self.lock(attempt_id):
            record = self.load(attempt_id)
            self._expect_sequence(record, expected_sequence)
            if record.state == "START_IN_FLIGHT":
                return record
            if record.state != "CREATED" or record.container is None:
                raise JournalWriteError("container is not ready to start")
            return self._write(
                replace(
                    record,
                    state="START_IN_FLIGHT",
                    operation_sequence=record.operation_sequence + 1,
                )
            )

    def mark_started(self, attempt_id: str, *, expected_sequence: int) -> JournalRecord:
        with self.lock(attempt_id):
            record = self.load(attempt_id)
            self._expect_sequence(record, expected_sequence)
            if record.state == "STARTED":
                return record
            if record.state != "START_IN_FLIGHT" or record.container is None:
                raise JournalWriteError("container start is not in flight")
            return self._write(
                replace(
                    record,
                    state="STARTED",
                    operation_sequence=record.operation_sequence + 1,
                )
            )

    def reset_start_after_failure(
        self, attempt_id: str, *, expected_sequence: int
    ) -> JournalRecord:
        with self.lock(attempt_id):
            record = self.load(attempt_id)
            self._expect_sequence(record, expected_sequence)
            if record.state != "START_IN_FLIGHT":
                raise JournalWriteError("container start is not in flight")
            return self._write(
                replace(
                    record,
                    state="CREATED",
                    operation_sequence=record.operation_sequence + 1,
                )
            )

    def begin_cleanup(
        self,
        attempt_id: str,
        *,
        expected_sequence: int,
        inspection_checksum: str,
        stopped_at: str,
        exit_code: int,
    ) -> JournalRecord:
        with self.lock(attempt_id):
            record = self.load(attempt_id)
            self._expect_sequence(record, expected_sequence)
            if record.state == "CLEANUP_IN_FLIGHT":
                return record
            if record.container is None or record.state not in {
                "CREATED",
                "START_IN_FLIGHT",
                "STARTED",
            }:
                raise JournalWriteError("created identity is required for cleanup")
            return self._write(
                replace(
                    record,
                    state="CLEANUP_IN_FLIGHT",
                    operation_sequence=record.operation_sequence + 1,
                    inspection_checksum=inspection_checksum,
                    stopped_at=stopped_at,
                    exit_code=exit_code,
                )
            )

    def finish_cleanup(self, attempt_id: str, *, expected_sequence: int) -> JournalRecord:
        with self.lock(attempt_id):
            record = self.load(attempt_id)
            self._expect_sequence(record, expected_sequence)
            if record.state == "TOMBSTONED":
                return record
            if record.state != "CLEANUP_IN_FLIGHT" or record.container is None:
                raise JournalWriteError("cleanup is not in flight")
            sequence = record.operation_sequence + 1
            return self._write(
                replace(
                    record,
                    state="TOMBSTONED",
                    operation_sequence=sequence,
                    tombstone_sequence=sequence,
                )
            )

    def tombstone(
        self,
        attempt_id: str,
        *,
        expected_sequence: int,
        reason: str,
        inspection_checksum: str | None = None,
    ) -> JournalRecord:
        with self.lock(attempt_id):
            record = self.load(attempt_id)
            self._expect_sequence(record, expected_sequence)
            if record.state in {"CREATE_IN_FLIGHT", "START_IN_FLIGHT"}:
                raise JournalWriteError("cannot tombstone while operation is in flight")
            if record.container is not None and record.state != "TOMBSTONED":
                raise JournalWriteError("created container requires identity cleanup")
            if record.state == "TOMBSTONED":
                return record
            sequence = record.operation_sequence + 1
            return self._write(
                replace(
                    record,
                    state="TOMBSTONED",
                    operation_sequence=sequence,
                    tombstone_sequence=sequence,
                    reason=reason,
                    inspection_checksum=inspection_checksum,
                )
            )

    def create_unclaimed_tombstone(
        self,
        *,
        attempt_id: str,
        allocation_id: str,
        startup_nonce: str,
        authority: Authority,
        resources: ResourceVector,
        image_digest: str,
        input_checksum: str | None,
        execution_binding: Mapping[str, object],
        reason: str,
        inspection_checksum: str,
    ) -> JournalRecord:
        with self.lock(attempt_id):
            if self.exists(attempt_id):
                record = self.load(attempt_id)
                binding = _canonical_mapping(execution_binding)
                if (
                    record.allocation_id != allocation_id
                    or record.startup_nonce != startup_nonce
                    or record.authority != authority
                    or record.resources != resources
                    or record.image_digest != image_digest
                    or record.input_checksum != input_checksum
                    or record.execution_binding != binding
                ):
                    raise JournalWriteError("unclaimed tombstone identity mismatch")
                return self.tombstone(
                    attempt_id,
                    expected_sequence=record.operation_sequence,
                    reason=reason,
                    inspection_checksum=inspection_checksum,
                )
            return self._write(
                JournalRecord(
                    attempt_id=attempt_id,
                    allocation_id=allocation_id,
                    startup_nonce=startup_nonce,
                    authority=authority,
                    resources=resources,
                    image_digest=image_digest,
                    input_checksum=input_checksum,
                    execution_binding=_canonical_mapping(execution_binding),
                    state="TOMBSTONED",
                    operation_sequence=1,
                    tombstone_sequence=1,
                    reason=reason,
                    inspection_checksum=inspection_checksum,
                )
            )

    def create_reconciliation_tombstone(
        self,
        *,
        attempt_id: str,
        allocation_id: str,
        startup_nonce: str,
        authority: Authority,
        resources: ResourceVector,
        inspection_checksum: str,
        reason: str,
    ) -> JournalRecord:
        """Create the sequence-one tombstone available from reconciliation data.

        A revoked UNCLAIMED row deliberately does not include the immutable
        execution spec. This record is terminal local evidence only and cannot
        be passed back through ``prepare`` or used to start a container.
        """
        with self.lock(attempt_id):
            if self.exists(attempt_id):
                record = self.load(attempt_id)
                if (
                    record.allocation_id != allocation_id
                    or record.startup_nonce != startup_nonce
                    or record.authority != authority
                    or record.resources != resources
                ):
                    raise JournalWriteError("reconciliation tombstone identity mismatch")
                if record.state != "TOMBSTONED":
                    raise JournalWriteError(
                        "existing execution cannot become an unclaimed tombstone"
                    )
                return record
            return self._write(
                JournalRecord(
                    attempt_id=attempt_id,
                    allocation_id=allocation_id,
                    startup_nonce=startup_nonce,
                    authority=authority,
                    resources=resources,
                    image_digest=None,
                    input_checksum=None,
                    execution_binding={"source": "reconciliation", "claim_state": "UNCLAIMED"},
                    state="TOMBSTONED",
                    operation_sequence=1,
                    tombstone_sequence=1,
                    reason=reason,
                    inspection_checksum=inspection_checksum,
                )
            )

    def append_runner_state(self, attempt_id: str, state: dict[str, object]) -> JournalRecord:
        with self.lock(attempt_id):
            record = self.load(attempt_id)
            return self._write(replace(record, runner_state=_canonical_mapping(state)))

    def update_runner_state(
        self,
        attempt_id: str,
        update: Callable[[dict[str, object]], Mapping[str, object]],
    ) -> JournalRecord:
        with self.lock(attempt_id):
            record = self.load(attempt_id)
            current = dict(record.runner_state or {})
            return self._write(replace(record, runner_state=_canonical_mapping(update(current))))

    def rebind_authority(
        self,
        attempt_id: str,
        *,
        prior_authority: Authority,
        current_authority: Authority,
        transferred_reservations: Mapping[str, object] | None = None,
    ) -> JournalRecord:
        """Atomically replace only the authority tuple after server adoption.

        Adoption must not call ``prepare`` again: execution inputs, container
        identity, operation sequence and runner bindings are immutable local
        evidence. Reservation snapshots are retained in runner state solely so
        a restart can verify the server's transfer response before continuing.
        """
        if prior_authority.attempt_id != attempt_id or current_authority.attempt_id != attempt_id:
            raise JournalWriteError("authority attempt identity mismatch")
        if (
            prior_authority.worker_id != current_authority.worker_id
            or prior_authority.allocation_id != current_authority.allocation_id
            or prior_authority.lease_id != current_authority.lease_id
            or prior_authority.job_fence != current_authority.job_fence
            or prior_authority == current_authority
        ):
            raise JournalWriteError("authority rebind identity is invalid")
        if transferred_reservations is not None:
            _canonical_mapping(transferred_reservations)
        with self.lock(attempt_id):
            record = self.load(attempt_id)
            if record.authority == current_authority:
                return record
            if record.authority != prior_authority:
                raise JournalWriteError("journal authority predecessor mismatch")
            return self._write(replace(record, authority=current_authority))

    @staticmethod
    def inspection_checksum(payload: bytes) -> str:
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _expect_sequence(record: JournalRecord, expected_sequence: int) -> None:
        if record.operation_sequence != expected_sequence:
            raise JournalWriteError("operation sequence conflict")
