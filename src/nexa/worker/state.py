"""Fsync-safe callback identity and first-send monotonic bookkeeping."""

import json
import os
import stat
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from .credentials import _atomic_private_json

_MAX_STATE_BYTES = 1_048_576
_OPERATIONS = {
    "create_incarnation",
    "heartbeat",
    "adopt",
    "renew",
    "failure",
    "cleanup",
    "runner_deadline",
}


def linux_boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


class PendingOperationStore:
    def __init__(self, path: Path, *, boot_id: str | None = None) -> None:
        self.path = path
        self.boot_id = boot_id or linux_boot_id()
        self._lock = threading.RLock()
        self._operations: dict[str, dict[str, Any]] = {}
        if path.exists():
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                    raise ValueError("pending state permissions are too broad")
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    raw = handle.read(_MAX_STATE_BYTES + 1)
                if len(raw) > _MAX_STATE_BYTES:
                    raise ValueError("pending state exceeded bound")
                value = json.loads(raw)
                operations = self._validate_document(value)
                if value["boot_id"] != self.boot_id and operations:
                    raise ValueError("monotonic clock domain changed with pending authority")
                self._operations = operations
            finally:
                os.close(fd)

    @property
    def operations(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return deepcopy(self._operations)

    @staticmethod
    def _validate_record(callback_id: object, record: object) -> dict[str, Any]:
        if not isinstance(callback_id, str) or not 1 <= len(callback_id) <= 128:
            raise ValueError("pending operation identity is invalid")
        if not isinstance(record, dict) or set(record) != {
            "operation",
            "payload",
            "first_send_monotonic_ns",
            "acknowledgment",
            "control_sequence",
        }:
            raise ValueError("pending operation record is invalid")
        operation = record["operation"]
        first_send = record["first_send_monotonic_ns"]
        acknowledgment = record["acknowledgment"]
        control_sequence = record["control_sequence"]
        if operation not in _OPERATIONS or not isinstance(record["payload"], dict):
            raise ValueError("pending operation payload is invalid")
        if first_send is not None and (
            not isinstance(first_send, int) or isinstance(first_send, bool) or first_send < 0
        ):
            raise ValueError("pending operation first-send is invalid")
        if acknowledgment is not None and not isinstance(acknowledgment, dict):
            raise ValueError("pending operation acknowledgment is invalid")
        if acknowledgment is not None and first_send is None:
            raise ValueError("pending operation acknowledgment lacks first-send")
        if control_sequence is not None and (
            not isinstance(control_sequence, int)
            or isinstance(control_sequence, bool)
            or control_sequence < 1
            or acknowledgment is None
        ):
            raise ValueError("pending operation control sequence is invalid")
        return dict(record)

    @classmethod
    def _validate_document(cls, value: object) -> dict[str, dict[str, Any]]:
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "boot_id", "operations"}
            or value["version"] != 1
            or isinstance(value["version"], bool)
            or not isinstance(value["boot_id"], str)
            or not 1 <= len(value["boot_id"]) <= 256
            or not isinstance(value["operations"], dict)
            or len(value["operations"]) > 1024
        ):
            raise ValueError("pending state is invalid")
        return {
            callback_id: cls._validate_record(callback_id, record)
            for callback_id, record in value["operations"].items()
        }

    def _commit(self, operations: dict[str, dict[str, Any]]) -> None:
        document = {
            "version": 1,
            "boot_id": self.boot_id,
            "operations": operations,
        }
        self._validate_document(document)
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_STATE_BYTES:
            raise ValueError("pending state exceeded bound")
        _atomic_private_json(self.path, document)
        self._operations = operations

    def begin(self, callback_id: str, *, operation: str, payload: dict) -> dict:
        self._validate_record(
            callback_id,
            {
                "operation": operation,
                "payload": payload,
                "first_send_monotonic_ns": None,
                "acknowledgment": None,
                "control_sequence": None,
            },
        )
        with self._lock:
            current = self._operations.get(callback_id)
            if current is not None:
                if current["operation"] != operation or current["payload"] != payload:
                    raise ValueError("callback identity cannot be reused with another payload")
                return deepcopy(current)
            if len(self._operations) >= 1024:
                raise ValueError("pending operation bound exceeded")
            current = {
                "operation": operation,
                "payload": payload,
                "first_send_monotonic_ns": None,
                "acknowledgment": None,
                "control_sequence": None,
            }
            operations = dict(self._operations)
            operations[callback_id] = current
            self._commit(operations)
            return deepcopy(self._operations[callback_id])

    def first_send(self, callback_id: str) -> int:
        with self._lock:
            current = self._operations[callback_id]
            if current["first_send_monotonic_ns"] is None:
                replacement = dict(current)
                replacement["first_send_monotonic_ns"] = time.monotonic_ns()
                operations = dict(self._operations)
                operations[callback_id] = replacement
                self._commit(operations)
            return self._operations[callback_id]["first_send_monotonic_ns"]

    def acknowledge(
        self, callback_id: str, response: dict, *, control_sequence: int | None = None
    ) -> None:
        with self._lock:
            current = self._operations[callback_id]
            existing = current["acknowledgment"]
            cleanup_pending = (
                current["operation"] == "cleanup"
                and isinstance(existing, dict)
                and not existing.get("verified", False)
                and existing.get("allocation_state") != "RELEASED"
            )
            if existing is not None and existing != response and not cleanup_pending:
                raise ValueError("callback acknowledgment changed")
            replacement = dict(current)
            replacement["acknowledgment"] = response
            replacement["control_sequence"] = control_sequence
            operations = dict(self._operations)
            operations[callback_id] = replacement
            self._commit(operations)

    def finish(self, callback_id: str) -> None:
        with self._lock:
            current = self._operations[callback_id]
            if current["acknowledgment"] is None:
                raise ValueError("unacknowledged callback cannot be discarded")
            operations = dict(self._operations)
            del operations[callback_id]
            self._commit(operations)

    def discard_superseded_authority(
        self, callback_id: str, *, current_worker_incarnation_id: str
    ) -> None:
        with self._lock:
            current = self._operations[callback_id]
            operation = current["operation"]
            body = current["payload"].get("body")
            if operation == "adopt" and isinstance(body, dict):
                operation_incarnation = body.get("current_worker_incarnation_id")
            elif operation == "renew" and isinstance(body, dict):
                authority = body.get("authority")
                operation_incarnation = (
                    authority.get("worker_incarnation_id") if isinstance(authority, dict) else None
                )
            else:
                raise ValueError("only authority callbacks can be superseded")
            if (
                not isinstance(operation_incarnation, str)
                or operation_incarnation == current_worker_incarnation_id
            ):
                raise ValueError("authority callback is not superseded")
            operations = dict(self._operations)
            del operations[callback_id]
            self._commit(operations)
