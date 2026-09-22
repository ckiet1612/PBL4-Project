"""Local worker lifecycle, reconciliation and lease orchestration."""

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from contextlib import nullcontext, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from nexa.infrastructure.persistence.ids import new_uuid7

from .capabilities import ResourceProvider, inventory_to_json
from .client import WorkerApiClient, WorkerApiError
from .docker_client import DockerCli, DockerContainerNotFound, DockerControlChannel
from .errors import ExecutorError
from .executor import DockerExecutor, runtime_identity_digest
from .journal import ExecutionJournal, JournalRecord
from .models import Authority, ContainerIdentity, ResourceVector
from .protocol import SequenceState, canonical_envelope_effect
from .runner_control import RunnerControl, RunnerControlError
from .state import PendingOperationStore


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    complete: bool
    items_seen: int
    adopted_attempts: tuple[str, ...] = ()
    unresolved_attempts: tuple[str, ...] = ()


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _authority(value: dict) -> Authority:
    return Authority(**value)


def _identity(value: dict, *, authority: Authority, startup_nonce: str) -> ContainerIdentity:
    return ContainerIdentity(
        container_id=value["container_id"],
        runtime_identity_digest=value["runtime_identity_digest"],
        attempt_id=authority.attempt_id,
        allocation_id=authority.allocation_id,
        startup_nonce=startup_nonce,
    )


class WorkerAgent:
    def __init__(
        self,
        *,
        worker_id: str,
        incarnation_id: str,
        installation_id: str,
        client: WorkerApiClient,
        state: PendingOperationStore,
        journal: ExecutionJournal,
        docker: DockerCli,
        executor: DockerExecutor | None = None,
        provider: ResourceProvider,
        channel_factory: Callable[[str], object] = DockerControlChannel,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        page_restart_limit: int = 3,
        loop_intervals: dict[str, float] | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.incarnation_id = incarnation_id
        self.installation_id = installation_id
        self.client = client
        self.state = state
        self.journal = journal
        self.docker = docker
        self.executor = executor
        self.provider = provider
        self.channel_factory = channel_factory
        self.monotonic_ns = monotonic_ns
        self.page_restart_limit = page_restart_limit
        self.loop_intervals = loop_intervals or {
            "heartbeat": 5.0,
            "renew": 5.0,
            "reconcile": 5.0,
            "poll": 1.0,
            "ipc": 0.25,
        }
        self._adopted: dict[str, Authority] = {}
        self._containers: dict[str, ContainerIdentity] = {}
        self._reconcile_complete = False
        self._readiness_blocked = False
        self._server_ready = False
        self._stop = asyncio.Event()
        self.operation_timeout_seconds = 8.0

    @classmethod
    def for_test(cls, client, *, worker_id: str, incarnation_id: str) -> "WorkerAgent":
        agent = object.__new__(cls)
        agent.worker_id = worker_id
        agent.incarnation_id = incarnation_id
        agent.installation_id = "test"
        agent.client = client
        agent.state = None
        agent.journal = None
        agent.docker = None
        agent.executor = None
        agent.provider = None
        agent.channel_factory = None
        agent.monotonic_ns = time.monotonic_ns
        agent.page_restart_limit = 3
        agent.loop_intervals = {
            "heartbeat": 5.0,
            "renew": 5.0,
            "reconcile": 5.0,
            "poll": 1.0,
            "ipc": 0.25,
        }
        agent._adopted = {}
        agent._containers = {}
        agent._reconcile_complete = False
        agent._readiness_blocked = False
        agent._server_ready = False
        agent._stop = asyncio.Event()
        agent.operation_timeout_seconds = 8.0
        return agent

    def reconcile_once(self) -> ReconciliationResult:
        self._reconcile_complete = False
        self._server_ready = False
        for restart in range(self.page_restart_limit + 1):
            discovered: dict[str, ContainerIdentity] = {}
            cursor = None
            items_seen = 0
            adopted: list[str] = []
            unresolved: list[str] = []
            expected_containers: set[str] = set()
            deferred: list[dict] = []
            try:
                while True:
                    page = self.client.reconciliation(
                        self.worker_id, self.incarnation_id, cursor=cursor
                    )
                    items = page.get("items")
                    page_info = page.get("page")
                    if not isinstance(items, list) or not isinstance(page_info, dict):
                        raise ValueError("worker reconciliation response is invalid")
                    if len(items) > 100:
                        raise ValueError("worker reconciliation page exceeded bound")
                    for item in items:
                        items_seen += 1
                        expected = (
                            item.get("expected_container") if isinstance(item, dict) else None
                        )
                        if isinstance(expected, dict) and isinstance(
                            expected.get("container_id"), str
                        ):
                            expected_containers.add(expected["container_id"])
                        if self.journal is None:
                            continue
                        if item.get("authority_state") != "LIVE" or not isinstance(expected, dict):
                            deferred.append(item)
                            continue
                        # Adopt known live identities before a potentially long
                        # inventory/orphan scan, so renewal can protect them.
                        if isinstance(expected, dict) and expected.get("container_id"):
                            container_id = expected["container_id"]
                            with suppress(DockerContainerNotFound):
                                discovered[container_id] = self._inspect_identity(container_id)
                        attempt_id, resolved, was_adopted = self._reconcile_item(item, discovered)
                        if was_adopted:
                            adopted.append(attempt_id)
                        if not resolved:
                            unresolved.append(attempt_id)
                    cursor = page_info.get("next_cursor")
                    if cursor is None:
                        break
                    if not isinstance(cursor, str) or not cursor:
                        raise ValueError("worker reconciliation cursor is invalid")
                checked = discovered
                discovered = self._discover_containers() if self.docker is not None else {}
                for container_id, identity in checked.items():
                    if identity.attempt_id in adopted and discovered.get(container_id) != identity:
                        raise RuntimeError("adopted container changed during reconciliation")
                for item in deferred:
                    attempt_id, resolved, was_adopted = self._reconcile_item(item, discovered)
                    if was_adopted:
                        adopted.append(attempt_id)
                    if not resolved:
                        unresolved.append(attempt_id)
                for container_id, identity in discovered.items():
                    if (
                        container_id not in expected_containers
                        and identity.attempt_id not in unresolved
                        and not self._stop_orphan(identity)
                    ):
                        unresolved.append(identity.attempt_id)
                if self.state is not None:
                    for callback_id, record in list(self.state.operations.items()):
                        if record["operation"] in {
                            "failure",
                            "cleanup",
                        } and not self._send_resolution(callback_id):
                            attempt_id = record["payload"].get("attempt_id")
                            if isinstance(attempt_id, str) and attempt_id not in unresolved:
                                unresolved.append(attempt_id)
                pending = self._blocking_pending_attempts()
                unresolved.extend(item for item in pending if item not in unresolved)
                result = ReconciliationResult(
                    complete=not unresolved,
                    items_seen=items_seen,
                    adopted_attempts=tuple(adopted),
                    unresolved_attempts=tuple(unresolved),
                )
                self._reconcile_complete = result.complete
                self._containers = discovered
                return result
            except WorkerApiError as exc:
                if exc.status != 409 or restart >= self.page_restart_limit:
                    raise
        raise RuntimeError("reconciliation restart bound exhausted")

    def _discover_containers(self) -> dict[str, ContainerIdentity]:
        identifiers = self.docker.find_by_labels(
            {"nexa.managed": "true", "nexa.installation_id": self.installation_id},
            timeout_seconds=5,
        )
        discovered: dict[str, ContainerIdentity] = {}
        for container_id in identifiers:
            identity = self._inspect_identity(container_id)
            if identity.attempt_id in {item.attempt_id for item in discovered.values()}:
                raise RuntimeError("multiple managed containers claim one attempt")
            discovered[container_id] = identity
        return discovered

    def _inspect_identity(self, container_id: str) -> ContainerIdentity:
        inspection = self.docker.inspect(container_id, timeout_seconds=5).payload
        config = inspection.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        required_labels = {
            "nexa.managed": "true",
            "nexa.installation_id": self.installation_id,
        }
        identity_labels = ("nexa.attempt_id", "nexa.allocation_id", "nexa.startup_nonce")
        if (
            not isinstance(labels, dict)
            or any(labels.get(key) != value for key, value in required_labels.items())
            or any(
                not isinstance(labels.get(key), str) or not labels[key] for key in identity_labels
            )
        ):
            raise RuntimeError("managed container identity labels are invalid")
        return ContainerIdentity(
            container_id=container_id,
            runtime_identity_digest=runtime_identity_digest(inspection),
            attempt_id=labels["nexa.attempt_id"],
            allocation_id=labels["nexa.allocation_id"],
            startup_nonce=labels["nexa.startup_nonce"],
            image_id=str(inspection.get("Image")) if inspection.get("Image") else None,
        )

    def _reconcile_item(
        self, item: dict, discovered: dict[str, ContainerIdentity]
    ) -> tuple[str, bool, bool]:
        with self.journal.lock(item["authority"]["attempt_id"]):
            return self._reconcile_item_locked(item, discovered)

    def _reconcile_item_locked(
        self, item: dict, discovered: dict[str, ContainerIdentity]
    ) -> tuple[str, bool, bool]:
        authority = _authority(item["authority"])
        if authority.worker_id != self.worker_id:
            raise ValueError("reconciliation authority belongs to another worker")
        attempt_id = authority.attempt_id
        expected_payload = item.get("expected_container")
        expected = (
            _identity(expected_payload, authority=authority, startup_nonce=item["startup_nonce"])
            if isinstance(expected_payload, dict)
            else None
        )
        local = self.journal.load(attempt_id) if self.journal.exists(attempt_id) else None
        actual = discovered.get(expected.container_id) if expected is not None else None
        if local is not None and local.state == "CREATE_IN_FLIGHT" and local.container is None:
            return attempt_id, False, False
        exact = (
            expected is not None
            and actual is not None
            and actual.container_id == expected.container_id
            and actual.runtime_identity_digest == expected.runtime_identity_digest
            and actual.attempt_id == expected.attempt_id
            and actual.allocation_id == expected.allocation_id
            and actual.startup_nonce == expected.startup_nonce
            and local is not None
            and local.container is not None
            and local.container.container_id == actual.container_id
            and local.container.runtime_identity_digest == actual.runtime_identity_digest
            and local.container.attempt_id == actual.attempt_id
            and local.container.allocation_id == actual.allocation_id
            and local.container.startup_nonce == actual.startup_nonce
            and local.allocation_id == authority.allocation_id
            and local.startup_nonce == item["startup_nonce"]
        )
        if item["authority_state"] == "LIVE" and exact:
            resumed = self._resume_pending_authority(item, local, actual)
            if resumed is not None:
                return attempt_id, resumed, resumed
            local = self.journal.load(attempt_id)
            if local.authority != authority:
                self._queue_resolution(item, local, actual)
                return attempt_id, False, False
            if authority.worker_incarnation_id == self.incarnation_id:
                runner_state = local.runner_state or {}
                if not isinstance(runner_state.get("authority_deadline_monotonic_ns"), int):
                    return attempt_id, False, False
                self._adopted[attempt_id] = authority
                return attempt_id, True, True
            return attempt_id, self._adopt(item, local, expected), True
        if (
            item["authority_state"] == "REVOKED"
            and item["claim_state"] == "UNCLAIMED"
            and local is None
            and expected is None
            and not any(value.attempt_id == attempt_id for value in discovered.values())
        ):
            return attempt_id, self._tombstone_unclaimed(item, authority), False
        if item["authority_state"] == "REVOKED":
            self._adopted.pop(attempt_id, None)
            # Removal can have committed to the journal before a crash wrote
            # the API callback. A durable cleanup record, not absence alone,
            # lets the executor resume/reconstruct the original stopped proof.
            bound = local.container if local is not None else None
            if (
                expected is None
                or bound is None
                or local.authority != authority
                or local.allocation_id != authority.allocation_id
                or local.startup_nonce != item["startup_nonce"]
                or bound.container_id != expected.container_id
                or bound.runtime_identity_digest != expected.runtime_identity_digest
                or bound.attempt_id != expected.attempt_id
                or bound.allocation_id != expected.allocation_id
                or bound.startup_nonce != expected.startup_nonce
                or (actual is not None and not exact)
                or (actual is None and local.state not in {"CLEANUP_IN_FLIGHT", "TOMBSTONED"})
            ):
                return attempt_id, False, False
            resolved = self._stop_orphan(bound)
            if resolved:
                discovered.pop(bound.container_id, None)
            return attempt_id, resolved, False
        if actual is not None:
            self._stop_orphan(actual)
        self._queue_resolution(item, local, actual)
        return attempt_id, False, False

    def _adopt(self, item: dict, record: JournalRecord, identity: ContainerIdentity) -> bool:
        callback_id = str(new_uuid7())
        body = {
            "prior_authority": asdict(record.authority),
            "current_worker_incarnation_id": self.incarnation_id,
            "container": {
                "container_id": identity.container_id,
                "runtime_identity_digest": identity.runtime_identity_digest,
            },
        }
        payload = {"attempt_id": record.attempt_id, "body": body}
        self.state.begin(callback_id, operation="adopt", payload=payload)
        first_send = self.state.first_send(callback_id)
        acknowledgment = self.client.adopt(record.attempt_id, callback_id, body)
        new_authority = _authority(acknowledgment["authority"])
        if (
            new_authority.worker_incarnation_id != self.incarnation_id
            or new_authority.attempt_id != record.attempt_id
            or new_authority.allocation_id != record.allocation_id
            or new_authority.lease_id != record.authority.lease_id
            or new_authority.job_fence != record.authority.job_fence
        ):
            raise RuntimeError("adoption acknowledgment authority mismatch")
        transferred = {
            "checkpoint": acknowledgment.get("transferred_checkpoint_reservation"),
            "result": acknowledgment.get("transferred_result_reservation"),
        }
        if not self._reservations_match(record, transferred):
            raise RuntimeError("adoption reservation snapshot mismatch")
        sequence = self._next_control_sequence(record)
        self.state.acknowledge(callback_id, acknowledgment, control_sequence=sequence)
        self.journal.rebind_authority(
            record.attempt_id,
            prior_authority=record.authority,
            current_authority=new_authority,
            transferred_reservations=transferred,
        )
        if not self._apply_deadline(
            record.attempt_id,
            identity,
            callback_id,
            first_send,
            acknowledgment,
            sequence,
        ):
            return False
        self.state.finish(callback_id)
        self._adopted[record.attempt_id] = new_authority
        return True

    def _resume_pending_authority(
        self, item: dict, record: JournalRecord, identity: ContainerIdentity
    ) -> bool | None:
        pending = next(
            (
                (callback_id, value)
                for callback_id, value in self.state.operations.items()
                if value["operation"] in {"adopt", "renew"}
                and value["payload"].get("attempt_id") == record.attempt_id
            ),
            None,
        )
        if pending is None:
            return None
        callback_id, operation = pending
        acknowledgment = operation["acknowledgment"]
        target_incarnation = self._operation_incarnation(operation)
        if target_incarnation != self.incarnation_id:
            if acknowledgment is None:
                self.state.discard_superseded_authority(
                    callback_id,
                    current_worker_incarnation_id=self.incarnation_id,
                )
                return None
            if operation["operation"] == "adopt":
                current = _authority(acknowledgment["authority"])
                prior = _authority(operation["payload"]["body"]["prior_authority"])
                self.journal.rebind_authority(
                    record.attempt_id,
                    prior_authority=prior,
                    current_authority=current,
                )
            first_send = operation["first_send_monotonic_ns"]
            sequence = operation["control_sequence"]
            if not isinstance(first_send, int) or not isinstance(sequence, int):
                raise RuntimeError("pending authority deadline state is missing")
            deadline_ack = (
                acknowledgment
                if operation["operation"] == "adopt"
                else {"lease_duration_seconds": 45, **acknowledgment}
            )
            if not self._apply_deadline(
                record.attempt_id,
                identity,
                callback_id,
                first_send,
                deadline_ack,
                sequence,
            ):
                return False
            self.state.finish(callback_id)
            return None
        if acknowledgment is None:
            method = self.client.adopt if operation["operation"] == "adopt" else self.client.renew
            acknowledgment = method(record.attempt_id, callback_id, operation["payload"]["body"])
            sequence = self._next_control_sequence(record)
            self.state.acknowledge(callback_id, acknowledgment, control_sequence=sequence)
            operation = self.state.operations[callback_id]
        sequence = self.state.operations[callback_id]["control_sequence"]
        if not isinstance(sequence, int):
            raise RuntimeError("pending authority control sequence is missing")
        if operation["operation"] == "adopt":
            current = _authority(acknowledgment["authority"])
            prior = _authority(operation["payload"]["body"]["prior_authority"])
            self.journal.rebind_authority(
                record.attempt_id,
                prior_authority=prior,
                current_authority=current,
            )
            self._adopted[record.attempt_id] = current
        first_send = self.state.operations[callback_id]["first_send_monotonic_ns"]
        if not isinstance(first_send, int):
            raise RuntimeError("pending authority first-send is missing")
        deadline_ack = (
            acknowledgment
            if operation["operation"] == "adopt"
            else {"lease_duration_seconds": 45, **acknowledgment}
        )
        if not self._apply_deadline(
            record.attempt_id,
            identity,
            callback_id,
            first_send,
            deadline_ack,
            sequence,
        ):
            return False
        self.state.finish(callback_id)
        return True

    @staticmethod
    def _operation_incarnation(operation: dict) -> str:
        body = operation["payload"].get("body")
        if not isinstance(body, dict):
            raise RuntimeError("pending authority body is invalid")
        if operation["operation"] == "adopt":
            value = body.get("current_worker_incarnation_id")
        else:
            authority = body.get("authority")
            value = authority.get("worker_incarnation_id") if isinstance(authority, dict) else None
        if not isinstance(value, str):
            raise RuntimeError("pending authority incarnation is invalid")
        return value

    @staticmethod
    def _reservations_match(record: JournalRecord, transferred: dict[str, object]) -> bool:
        runner_state = record.runner_state or {}
        expected = runner_state.get("active_reservations")
        if expected is None:
            return transferred == {"checkpoint": None, "result": None}
        return expected == transferred

    @staticmethod
    def _next_control_sequence(record: JournalRecord) -> int:
        state = record.runner_state or {}
        value = state.get("last_control_sequence", 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RuntimeError("journal runner control sequence is invalid")
        return value + 1

    def _apply_deadline(
        self,
        attempt_id: str,
        identity: ContainerIdentity,
        callback_id: str,
        first_send: int,
        acknowledgment: dict,
        sequence: int,
    ) -> bool:
        control = RunnerControl(next_sequence=sequence, monotonic_ns=self.monotonic_ns)
        channel = self.channel_factory(identity.container_id)
        manager = channel if hasattr(channel, "__enter__") else nullcontext(channel)
        try:
            with manager as connection:
                result = control.set_authority_deadline(
                    connection,
                    source_callback_id=callback_id,
                    first_send_monotonic_ns=first_send,
                    lease_duration_seconds=int(acknowledgment["lease_duration_seconds"]),
                    safety_margin_seconds=int(acknowledgment["safety_margin_seconds"]),
                )
        except RunnerControlError:
            return False
        self.journal.update_runner_state(
            attempt_id,
            lambda state: {
                **state,
                "last_control_sequence": result.control_sequence,
                "authority_deadline_monotonic_ns": result.deadline_monotonic_ns,
                "source_callback_id": callback_id,
            },
        )
        return True

    def _tombstone_unclaimed(self, item: dict, authority: Authority) -> bool:
        labels = {
            "nexa.managed": "true",
            "nexa.installation_id": self.installation_id,
            "nexa.attempt_id": authority.attempt_id,
            "nexa.allocation_id": authority.allocation_id,
            "nexa.startup_nonce": item["startup_nonce"],
        }
        raw = json.dumps(
            {"labels": labels, "container_ids": []},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        checksum = "sha256:" + hashlib.sha256(raw).hexdigest()
        allocation = item["allocation"]
        resources = allocation["resources"]
        record = self.journal.create_reconciliation_tombstone(
            attempt_id=authority.attempt_id,
            allocation_id=authority.allocation_id,
            startup_nonce=item["startup_nonce"],
            authority=authority,
            resources=ResourceVector(
                resources["cpu_millis"], resources["memory_bytes"], resources["gpu_count"]
            ),
            inspection_checksum=checksum,
            reason="REVOKED_UNCLAIMED_RECONCILIATION",
        )
        proof = {
            "proof_type": "NO_CONTAINER",
            "startup_nonce": record.startup_nonce,
            "executor_operation_sequence": record.operation_sequence,
            "tombstone_sequence": record.tombstone_sequence,
            "observed_at": _timestamp(),
            "inspection_checksum": checksum,
        }
        body = {
            "worker_id": self.worker_id,
            "worker_incarnation_id": self.incarnation_id,
            "attempt_id": authority.attempt_id,
            "allocation_id": authority.allocation_id,
            "job_fence": authority.job_fence,
            "proof": proof,
        }
        callback_id = str(new_uuid7())
        self.state.begin(
            callback_id,
            operation="cleanup",
            payload={"attempt_id": authority.attempt_id, "body": body},
        )
        return self._send_resolution(callback_id)

    def _queue_resolution(
        self, item: dict, _record: JournalRecord | None, identity: ContainerIdentity | None
    ) -> None:
        if identity is None and not isinstance(item.get("expected_container"), dict):
            return
        authority = item["authority"]
        body = {
            "authority": authority,
            "failure_class": "INFRASTRUCTURE",
            "reason_code": "RECONCILIATION_IDENTITY_MISMATCH",
            "observation": {
                "observation_type": "CONTAINER",
                "container": (
                    {
                        "container_id": identity.container_id,
                        "runtime_identity_digest": identity.runtime_identity_digest,
                    }
                    if identity is not None
                    else item.get("expected_container")
                ),
                "observed_at": _timestamp(),
                "exit_code": None,
                "oom_killed": False,
                "runtime_limit_reached": False,
            },
        }
        callback_id = str(new_uuid7())
        self.state.begin(
            callback_id,
            operation="failure",
            payload={"attempt_id": authority["attempt_id"], "body": body},
        )

    def _send_resolution(self, callback_id: str) -> bool:
        record = self.state.operations[callback_id]
        payload = record["payload"]
        method = self.client.cleanup if record["operation"] == "cleanup" else self.client.fail
        self.state.first_send(callback_id)
        try:
            response = method(payload["attempt_id"], callback_id, payload["body"])
        except WorkerApiError as exc:
            if exc.status == 404:
                return False
            raise
        self.state.acknowledge(callback_id, response)
        if record["operation"] == "cleanup" and not (
            response.get("verified") is True or response.get("allocation_state") == "RELEASED"
        ):
            return False
        self.state.finish(callback_id)
        return True

    def _stop_orphan(self, identity: ContainerIdentity) -> bool:
        if self.executor is None:
            return False
        try:
            record = self.journal.load(identity.attempt_id)
            bound = record.container
            if (
                bound is None
                or bound.container_id != identity.container_id
                or bound.runtime_identity_digest != identity.runtime_identity_digest
                or bound.attempt_id != identity.attempt_id
                or bound.allocation_id != identity.allocation_id
                or bound.startup_nonce != identity.startup_nonce
            ):
                return False
            proof = self.executor.cleanup(bound)
            body = {
                "worker_id": self.worker_id,
                "worker_incarnation_id": self.incarnation_id,
                "attempt_id": identity.attempt_id,
                "allocation_id": identity.allocation_id,
                "job_fence": record.authority.job_fence,
                "proof": {
                    "proof_type": proof.proof_type,
                    "startup_nonce": proof.startup_nonce,
                    "executor_operation_sequence": proof.executor_operation_sequence,
                    "container": {
                        "container_id": identity.container_id,
                        "runtime_identity_digest": identity.runtime_identity_digest,
                    },
                    "stopped_at": proof.stopped_at,
                    "exit_code": proof.exit_code,
                    "inspection_checksum": proof.inspection_checksum,
                },
            }
            for callback_id, pending in self.state.operations.items():
                if (
                    pending["operation"] == "cleanup"
                    and pending["payload"].get("attempt_id") == identity.attempt_id
                ):
                    prior_body = pending["payload"].get("body", {})
                    if any(
                        prior_body.get(key) != body[key]
                        for key in (
                            "worker_id",
                            "attempt_id",
                            "allocation_id",
                            "job_fence",
                            "proof",
                        )
                    ):
                        return False
                    return self._send_resolution(callback_id)
            callback_id = str(new_uuid7())
            self.state.begin(
                callback_id,
                operation="cleanup",
                payload={"attempt_id": identity.attempt_id, "body": body},
            )
            return self._send_resolution(callback_id)
        except (ExecutorError, RuntimeError):
            return False

    def _blocking_pending_attempts(self) -> list[str]:
        if self.state is None:
            return []
        result: list[str] = []
        for record in self.state.operations.values():
            if record["operation"] in {"adopt", "renew", "failure", "cleanup", "runner_deadline"}:
                attempt_id = record["payload"].get("attempt_id")
                if isinstance(attempt_id, str):
                    result.append(attempt_id)
        return result

    def renew_once(self) -> None:
        for attempt_id, authority in tuple(self._adopted.items()):
            with self.journal.lock(attempt_id):
                self._renew_attempt(attempt_id, authority)

    def _renew_attempt(self, attempt_id: str, authority: Authority) -> None:
        if any(
            pending["operation"] in {"adopt", "renew"}
            and pending["payload"].get("attempt_id") == attempt_id
            for pending in self.state.operations.values()
        ):
            return
        record = self.journal.load(attempt_id)
        progress = (record.runner_state or {}).get("latest_progress")
        body = {
            "authority": asdict(authority),
            "progress_sequence": progress["progress_sequence"] if progress else 0,
            "progress": (
                {key: progress[key] for key in ("fraction", "step", "epoch", "item_cursor")}
                if progress
                else None
            ),
        }
        callback_id = str(new_uuid7())
        self.state.begin(
            callback_id,
            operation="renew",
            payload={"attempt_id": attempt_id, "body": body},
        )
        first_send = self.state.first_send(callback_id)
        acknowledgment = self.client.renew(attempt_id, callback_id, body)
        sequence = self._next_control_sequence(record)
        self.state.acknowledge(callback_id, acknowledgment, control_sequence=sequence)
        identity = record.container
        if identity is None or not self._apply_deadline(
            attempt_id,
            identity,
            callback_id,
            first_send,
            {"lease_duration_seconds": 45, **acknowledgment},
            sequence,
        ):
            return
        self.state.finish(callback_id)

    def heartbeat_once(self) -> dict:
        pending = [
            (callback_id, record)
            for callback_id, record in self.state.operations.items()
            if record["operation"] == "heartbeat"
        ]
        if len(pending) > 1:
            raise RuntimeError("multiple pending heartbeat callbacks are unsafe")
        if pending:
            callback_id, record = pending[0]
            response = record["acknowledgment"]
            if response is None:
                self.state.first_send(callback_id)
                response = self.client.heartbeat(
                    self.worker_id,
                    callback_id,
                    record["payload"],
                )
                self.state.acknowledge(callback_id, response)
            self.state.finish(callback_id)
            return response

        discovered = self.provider.discover()
        inventory = inventory_to_json(discovered, self.provider.allocatable(discovered))
        callback_id = str(new_uuid7())
        locally_ready = self._reconcile_complete and not self._readiness_blocked
        body = {
            "worker_incarnation_id": self.incarnation_id,
            "observed_health": "READY" if locally_ready else "STARTING",
            "reconcile_complete": locally_ready,
            "inventory": inventory,
            "observed_containers": [
                {
                    "container_id": item.container_id,
                    "runtime_identity_digest": item.runtime_identity_digest,
                }
                for item in self._containers.values()
            ],
        }
        self.state.begin(callback_id, operation="heartbeat", payload=body)
        self.state.first_send(callback_id)
        response = self.client.heartbeat(self.worker_id, callback_id, body)
        self.state.acknowledge(callback_id, response)
        self.state.finish(callback_id)
        self._server_ready = locally_ready
        return response

    async def run(self) -> None:
        loops = (
            asyncio.create_task(self._loop(self.heartbeat_once, self.loop_intervals["heartbeat"])),
            asyncio.create_task(self._loop(self.renew_once, self.loop_intervals["renew"])),
            asyncio.create_task(self._loop(self.reconcile_once, self.loop_intervals["reconcile"])),
            asyncio.create_task(self._loop(self._poll_once, self.loop_intervals["poll"])),
            asyncio.create_task(self._loop(self._ipc_once, self.loop_intervals["ipc"])),
        )
        try:
            await self._stop.wait()
        finally:
            for task in loops:
                task.cancel()
            await asyncio.gather(*loops, return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()

    async def _loop(self, operation: Callable[[], object], interval: float) -> None:
        in_flight: asyncio.Task[object] | None = None
        timed_out = False
        errors = (
            OSError,
            TimeoutError,
            KeyError,
            WorkerApiError,
            RunnerControlError,
            RuntimeError,
            ValueError,
        )
        try:
            while not self._stop.is_set():
                if in_flight is None:
                    in_flight = asyncio.create_task(asyncio.to_thread(operation))
                try:
                    await asyncio.wait_for(
                        asyncio.shield(in_flight), timeout=self.operation_timeout_seconds
                    )
                except TimeoutError:
                    self._server_ready = False
                    if operation != self._poll_once:
                        self._reconcile_complete = False
                        self._readiness_blocked = True
                    # The operation itself may have raised TimeoutError. Only
                    # retain unfinished threads; a completed failure must retry.
                    if in_flight.done():
                        in_flight = None
                        timed_out = False
                    else:
                        timed_out = True
                except errors:
                    self._server_ready = False
                    if operation != self._poll_once:
                        self._reconcile_complete = False
                        self._readiness_blocked = True
                    in_flight = None
                    timed_out = False
                else:
                    if timed_out:
                        self._reconcile_complete = False
                    elif operation == self.reconcile_once:
                        self._readiness_blocked = False
                    in_flight = None
                    timed_out = False
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=interval)
        finally:
            if in_flight is not None:
                try:
                    await asyncio.shield(in_flight)
                except errors:
                    self._server_ready = False
                    if operation != self._poll_once:
                        self._reconcile_complete = False
                        self._readiness_blocked = True
                if timed_out:
                    self._reconcile_complete = False

    def _poll_once(self) -> None:
        if self._server_ready and self._reconcile_complete and not self._readiness_blocked:
            response = self.client.poll(self.worker_id, self.incarnation_id)
            if response.get("offer") is not None:
                raise RuntimeError("dispatch claim/start belongs to B11")

    def _ipc_once(self) -> None:
        for attempt_id in tuple(self._adopted):
            record = self.journal.load(attempt_id)
            if record.container is None:
                continue
            channel = self.channel_factory(record.container.container_id)
            manager = channel if hasattr(channel, "__enter__") else nullcontext(channel)
            with manager as connection:
                RunnerControl(next_sequence=self._next_control_sequence(record)).receive_messages(
                    connection,
                    lambda frame, current=attempt_id: self._record_runner_message(current, frame),
                )

    def _record_runner_message(self, attempt_id: str, envelope: dict[str, object]) -> str:
        outcome = "OUT_OF_ORDER"

        def update(runner_state: dict[str, object]) -> dict[str, object]:
            nonlocal outcome
            sequence_state = SequenceState.from_snapshot(
                runner_state.get("message_sequences", {"highest": 0, "payload_hashes": {}})
            )
            sequence = int(envelope["message_sequence"])
            effect = canonical_envelope_effect(envelope)
            outcome = sequence_state.classify(sequence, effect)
            if outcome != "ACCEPTED":
                return runner_state
            message_type = envelope["type"]
            if message_type not in {"STARTED", "PROGRESS"}:
                outcome = "OUT_OF_ORDER"
                return runner_state
            sequence_state.commit(sequence, effect)
            runner_state["message_sequences"] = sequence_state.snapshot()
            if message_type == "PROGRESS":
                runner_state["latest_progress"] = dict(envelope["payload"])
            return runner_state

        self.journal.update_runner_state(attempt_id, update)
        return outcome


__all__ = ["ReconciliationResult", "WorkerAgent"]
