"""Bounded Docker executor with exact identity and crash-safe cleanup."""

import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from .docker_client import DockerCli, DockerCommandBackend, DockerContainerNotFound
from .docker_config import build_container_config
from .errors import ExecutorError, ExecutorErrorCode
from .journal import ExecutionJournal, JournalRecord, JournalWriteError
from .models import (
    CleanupProof,
    ContainerIdentity,
    ContainerObservation,
    InputMount,
    PreparedExecution,
    StartExecution,
)

_PROCESS_CLOCK_DOMAIN = f"process:{os.getpid()}:{time.monotonic_ns()}"
CHECKPOINT_RUNNER_LABEL = "io.nexa.runner.checkpoint"
CHECKPOINT_RUNNER_VALUE = "cpu-state-v1"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _pinned_image_ref(image_ref: str, digest: str) -> str:
    if "@" not in image_ref:
        return f"{image_ref}@{digest}"
    if image_ref.count("@") != 1 or image_ref.rsplit("@", 1)[1] != digest:
        raise ValueError("configured image digest does not match execution context")
    return image_ref


class DockerExecutor:
    def __init__(
        self,
        journal: ExecutionJournal,
        backend: DockerCommandBackend | DockerCli,
        *,
        image_ref: str,
        staging_root: str | Path = "/var/lib/nexa/staging",
        monotonic: Callable[[], float] = time.monotonic,
        clock_domain: str | None = None,
        installation_id: str | None = None,
    ) -> None:
        self.journal = journal
        self.docker = backend if isinstance(backend, DockerCli) else DockerCli(backend)
        self.image_ref = image_ref
        self.staging_root = Path(staging_root)
        self.monotonic = monotonic
        self.clock_domain = clock_domain or _detect_clock_domain()
        self.installation_id = installation_id
        self._checkpoint_capability: dict[str, bool] = {}

    def prepare(self, request: StartExecution) -> PreparedExecution:
        attempt_id = request.context.authority.attempt_id
        binding = _execution_binding(request)
        try:
            image = _pinned_image_ref(self.image_ref, request.context.image_digest)
            self._validate_primary_input_binding(request)
            self.journal.prepare(
                attempt_id=attempt_id,
                allocation_id=request.allocation.allocation_id,
                startup_nonce=request.startup_nonce,
                authority=request.context.authority,
                resources=request.context.resources,
                image_digest=request.context.image_digest,
                input_checksum=request.context.input_checksum,
                execution_binding=binding,
            )
            pinned_mounts = tuple(
                self._materialize_input(attempt_id, mount) for mount in request.input_mounts
            )
            prepared_request = replace(request, input_mounts=pinned_mounts)
            control_dir = self._prepare_control_dir(request)
            config = build_container_config(
                prepared_request,
                image=image,
                control_dir=str(control_dir),
                installation_id=self.installation_id,
            )
        except (JournalWriteError, OSError, ValueError) as exc:
            message = str(exc)
            code = (
                ExecutorErrorCode.IDENTITY_MISMATCH
                if "immutable" in message or "identity" in message
                else ExecutorErrorCode.INVALID_STATE
            )
            if request.input_mounts and code == ExecutorErrorCode.INVALID_STATE:
                message = f"input verification failed: {message}"
            raise ExecutorError(code, message) from exc
        return PreparedExecution(request=prepared_request, config=config)

    def start(self, prepared: PreparedExecution) -> ContainerIdentity:
        request = prepared.request
        attempt_id = request.context.authority.attempt_id
        with self.journal.lock(attempt_id):
            self._verify_materialized_inputs(request.input_mounts)
            record = self.journal.load(attempt_id)
            if record.state == "TOMBSTONED":
                raise ExecutorError(ExecutorErrorCode.INVALID_STATE, "attempt is tombstoned")
            if record.state == "CLEANUP_IN_FLIGHT":
                raise ExecutorError(ExecutorErrorCode.INVALID_STATE, "attempt cleanup is in flight")
            if record.state == "PREPARED":
                record = self._create(prepared, record)
            elif record.state == "CREATE_IN_FLIGHT":
                raise ExecutorError(
                    ExecutorErrorCode.CREATE_OUTCOME_UNKNOWN,
                    "Docker create outcome is unknown",
                )
            if record.container is None:
                raise ExecutorError(ExecutorErrorCode.INVALID_STATE, "container identity is absent")
            if record.state == "STARTED":
                self._inspect_identity(record.container, self._remaining(record, request))
                return record.container
            return self._reconcile_and_start(record, request)

    def _create(self, prepared: PreparedExecution, record: JournalRecord) -> JournalRecord:
        request = prepared.request
        attempt_id = request.context.authority.attempt_id
        started_ns = int(self.monotonic() * 1_000_000_000)
        try:
            record = self.journal.begin_create(
                attempt_id,
                expected_sequence=record.operation_sequence,
                started_monotonic_ns=started_ns,
                clock_domain=self.clock_domain,
            )
            result = self.docker.create(
                prepared.config.argv(), timeout_seconds=self._remaining(record, request)
            )
        except TimeoutError as exc:
            raise ExecutorError(
                ExecutorErrorCode.CREATE_OUTCOME_UNKNOWN,
                "Docker create outcome is unknown",
            ) from exc
        except RuntimeError as exc:
            raise ExecutorError(ExecutorErrorCode.RUNTIME_ERROR, "Docker create failed") from exc
        inspection = self._inspect_raw(result.container_id, self._remaining(record, request))
        labels = inspection.get("Config", {}).get("Labels", {})
        if not isinstance(labels, dict):
            raise ExecutorError(ExecutorErrorCode.IDENTITY_MISMATCH, "Docker labels are invalid")
        if any(labels.get(key) != value for key, value in prepared.config.labels.items()):
            raise ExecutorError(
                ExecutorErrorCode.IDENTITY_MISMATCH, "Docker identity labels mismatch"
            )
        identity = ContainerIdentity(
            container_id=result.container_id,
            runtime_identity_digest=_runtime_identity_digest(inspection),
            attempt_id=attempt_id,
            allocation_id=request.allocation.allocation_id,
            startup_nonce=request.startup_nonce,
            architecture=request.context.architecture,
            image_id=str(inspection.get("Image")) if inspection.get("Image") else None,
        )
        try:
            return self.journal.bind_created(attempt_id, identity)
        except JournalWriteError as exc:
            raise ExecutorError(ExecutorErrorCode.IDENTITY_MISMATCH, str(exc)) from exc

    def _reconcile_and_start(
        self, record: JournalRecord, request: StartExecution
    ) -> ContainerIdentity:
        assert record.container is not None
        if (
            record.state in {"CREATED", "START_IN_FLIGHT"}
            and record.startup_started_monotonic_ns is not None
            and record.startup_clock_domain != self.clock_domain
        ):
            raise ExecutorError(
                ExecutorErrorCode.START_OUTCOME_UNKNOWN,
                "clock domain changed; the original startup deadline cannot be reconstructed",
            )
        identity = record.container
        inspection = self._inspect_identity(identity, self._remaining(record, request))
        state = inspection.get("State", {})
        if not isinstance(state, dict):
            raise ExecutorError(
                ExecutorErrorCode.INSPECTION_UNAVAILABLE, "container state is invalid"
            )
        if bool(state.get("Running", False)):
            if record.state == "START_IN_FLIGHT":
                if request.cpu_workload is not None:
                    raise ExecutorError(
                        ExecutorErrorCode.START_OUTCOME_UNKNOWN,
                        "running container cannot prove the supervisor exec outcome",
                    )
                record = self.journal.mark_started(
                    identity.attempt_id, expected_sequence=record.operation_sequence
                )
            elif record.state == "CREATED":
                record = self.journal.begin_start(
                    identity.attempt_id, expected_sequence=record.operation_sequence
                )
                self._start_supervisor(identity, request, self._remaining(record, request))
                record = self.journal.mark_started(
                    identity.attempt_id, expected_sequence=record.operation_sequence
                )
            return identity
        status = str(state.get("Status", "")).lower()
        if status not in {"", "created"}:
            raise ExecutorError(
                ExecutorErrorCode.INVALID_STATE,
                "container already exited and will not be restarted",
            )
        if record.state == "START_IN_FLIGHT" and record.startup_clock_domain != self.clock_domain:
            raise ExecutorError(
                ExecutorErrorCode.START_OUTCOME_UNKNOWN,
                "clock domain changed; inspection cannot prove the pending start outcome",
            )
        if record.state == "CREATED":
            record = self.journal.begin_start(
                identity.attempt_id, expected_sequence=record.operation_sequence
            )
        elif record.state != "START_IN_FLIGHT":
            raise ExecutorError(ExecutorErrorCode.INVALID_STATE, "container is not startable")
        try:
            self.docker.start(
                identity.container_id, timeout_seconds=self._remaining(record, request)
            )
        except TimeoutError as exc:
            raise ExecutorError(
                ExecutorErrorCode.START_OUTCOME_UNKNOWN,
                "Docker start outcome is unknown",
            ) from exc
        except RuntimeError as exc:
            with suppress(JournalWriteError):
                self.journal.reset_start_after_failure(
                    identity.attempt_id, expected_sequence=record.operation_sequence
                )
            raise ExecutorError(ExecutorErrorCode.RUNTIME_ERROR, "Docker start failed") from exc
        self._start_supervisor(identity, request, self._remaining(record, request))
        try:
            self.journal.mark_started(
                identity.attempt_id, expected_sequence=record.operation_sequence
            )
        except JournalWriteError as exc:
            raise ExecutorError(ExecutorErrorCode.INVALID_STATE, str(exc)) from exc
        return identity

    def _start_supervisor(
        self, identity: ContainerIdentity, request: StartExecution, timeout_seconds: float
    ) -> None:
        if request.cpu_workload is None:
            return
        try:
            self.docker.exec_detached(
                identity.container_id,
                _supervisor_command(request),
                user="1001:1000",
                timeout_seconds=timeout_seconds,
            )
        except (RuntimeError, TimeoutError) as exc:
            raise ExecutorError(
                ExecutorErrorCode.START_OUTCOME_UNKNOWN,
                "workload supervisor start outcome is unknown",
            ) from exc

    def _remaining(self, record: JournalRecord, request: StartExecution) -> float:
        started_ns = record.startup_started_monotonic_ns
        if started_ns is None or record.startup_clock_domain != self.clock_domain:
            return float(request.startup_limit_seconds)
        deadline = started_ns / 1_000_000_000 + request.startup_limit_seconds
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise ExecutorError(ExecutorErrorCode.START_TIMEOUT, "startup deadline expired")
        return remaining

    def signal_checkpoint(self, identity: ContainerIdentity, reason: str, deadline: float) -> None:
        del identity, reason, deadline
        # B14 checkpoints travel as REQUEST_CHECKPOINT over the fenced runner
        # control channel after a server reservation; Docker signals never do.
        raise ExecutorError(
            ExecutorErrorCode.UNSUPPORTED, "checkpoint requests use the runner control channel"
        )

    def checkpoint_supported(self, image_digest: str) -> bool:
        """True only when the pinned image declares the B14 CPU state runner label."""
        image = _pinned_image_ref(self.image_ref, image_digest)
        cached = self._checkpoint_capability.get(image)
        if cached is None:
            try:
                labels = self.docker.image_labels(image, timeout_seconds=5.0)
            except RuntimeError as exc:
                raise ExecutorError(
                    ExecutorErrorCode.INSPECTION_UNAVAILABLE, "image inspection failed"
                ) from exc
            cached = labels.get(CHECKPOINT_RUNNER_LABEL) == CHECKPOINT_RUNNER_VALUE
            self._checkpoint_capability[image] = cached
        return cached

    def inspect(self, identity: ContainerIdentity) -> ContainerObservation:
        payload = self._inspect_identity(identity, 5)
        state = payload.get("State", {})
        if not isinstance(state, dict):
            raise ExecutorError(
                ExecutorErrorCode.INSPECTION_UNAVAILABLE, "container state is invalid"
            )
        return ContainerObservation(
            identity=identity,
            running=bool(state.get("Running", False)),
            exit_code=state.get("ExitCode") if isinstance(state.get("ExitCode"), int) else None,
            oom_killed=bool(state.get("OOMKilled", False)),
            config_checksum=_payload_checksum(_immutable_runtime_identity(payload)),
            observed_at=_now(),
        )

    def stop(self, identity: ContainerIdentity, grace_seconds: int = 5) -> ContainerObservation:
        with self.journal.lock(identity.attempt_id):
            record = self.journal.load(identity.attempt_id)
            if record.container != identity:
                raise ExecutorError(
                    ExecutorErrorCode.IDENTITY_MISMATCH,
                    "stop identity is not bound to the attempt",
                )
            observation = self.inspect(identity)
            if observation.running:
                try:
                    self.docker.stop(
                        identity.container_id, timeout_seconds=max(1, grace_seconds + 1)
                    )
                except RuntimeError as exc:
                    raise ExecutorError(
                        ExecutorErrorCode.STOP_TIMEOUT, "container stop failed"
                    ) from exc
                observation = self.inspect(identity)
            return observation

    def cleanup(self, identity: ContainerIdentity | None) -> CleanupProof:
        if identity is None:
            raise ExecutorError(
                ExecutorErrorCode.INSPECTION_UNAVAILABLE,
                "cleanup needs a corroborated identity or journal tombstone",
            )
        with self.journal.lock(identity.attempt_id):
            record = self.journal.load(identity.attempt_id)
            if record.container != identity:
                raise ExecutorError(
                    ExecutorErrorCode.IDENTITY_MISMATCH,
                    "cleanup identity is not bound to the attempt",
                )
            if record.state == "TOMBSTONED":
                return self._stopped_proof(record, identity)
            if record.state == "CREATE_IN_FLIGHT":
                raise ExecutorError(
                    ExecutorErrorCode.CREATE_OUTCOME_UNKNOWN,
                    "cannot cleanup while a Docker outcome is unknown",
                )
            if record.state == "CLEANUP_IN_FLIGHT":
                return self._resume_cleanup(record, identity)
            if record.state not in {"CREATED", "START_IN_FLIGHT", "STARTED"}:
                raise ExecutorError(
                    ExecutorErrorCode.INVALID_STATE,
                    "cleanup requires a created container identity",
                )
            observation = self.stop(identity, grace_seconds=5)
            stopped_at = _now()
            try:
                record = self.journal.begin_cleanup(
                    identity.attempt_id,
                    expected_sequence=record.operation_sequence,
                    inspection_checksum=observation.config_checksum,
                    stopped_at=stopped_at,
                    exit_code=observation.exit_code if observation.exit_code is not None else -1,
                )
            except JournalWriteError as exc:
                raise ExecutorError(ExecutorErrorCode.INVALID_STATE, str(exc)) from exc
            return self._resume_cleanup(record, identity)

    def _resume_cleanup(self, record: JournalRecord, identity: ContainerIdentity) -> CleanupProof:
        try:
            self._inspect_identity(identity, 5)
        except DockerContainerNotFound:
            pass
        except ExecutorError:
            raise
        else:
            try:
                self.docker.remove(identity.container_id, timeout_seconds=5)
            except RuntimeError as exc:
                raise ExecutorError(
                    ExecutorErrorCode.RUNTIME_ERROR, "container removal failed"
                ) from exc
        try:
            cleaned = self.journal.finish_cleanup(
                identity.attempt_id, expected_sequence=record.operation_sequence
            )
        except JournalWriteError as exc:
            raise ExecutorError(ExecutorErrorCode.INVALID_STATE, str(exc)) from exc
        return self._stopped_proof(cleaned, identity)

    def _stopped_proof(self, record: JournalRecord, identity: ContainerIdentity) -> CleanupProof:
        if (
            record.inspection_checksum is None
            or record.stopped_at is None
            or record.exit_code is None
        ):
            raise ExecutorError(ExecutorErrorCode.INVALID_STATE, "cleanup evidence is incomplete")
        return CleanupProof(
            proof_type="CONTAINER_STOPPED",
            startup_nonce=identity.startup_nonce,
            executor_operation_sequence=record.operation_sequence,
            inspection_checksum=record.inspection_checksum,
            observed_at=record.stopped_at,
            container=identity,
            stopped_at=record.stopped_at,
            exit_code=record.exit_code,
        )

    def tombstone_unclaimed(self, request: StartExecution, *, reason: str) -> CleanupProof:
        attempt_id = request.context.authority.attempt_id
        labels = _identity_labels(request)
        if self.installation_id is not None:
            labels["nexa.installation_id"] = self.installation_id
        with self.journal.lock(attempt_id):
            if self.journal.exists(attempt_id):
                existing = self.journal.load(attempt_id)
                if (
                    existing.allocation_id != request.allocation.allocation_id
                    or existing.startup_nonce != request.startup_nonce
                    or existing.authority != request.context.authority
                    or existing.resources != request.context.resources
                    or existing.image_digest != request.context.image_digest
                    or existing.input_checksum != request.context.input_checksum
                    or existing.execution_binding != _execution_binding(request)
                ):
                    raise ExecutorError(
                        ExecutorErrorCode.IDENTITY_MISMATCH,
                        "unclaimed tombstone request does not match the journal identity",
                    )
                if existing.state in {"CREATE_IN_FLIGHT", "START_IN_FLIGHT"}:
                    raise ExecutorError(
                        ExecutorErrorCode.CREATE_OUTCOME_UNKNOWN,
                        "cannot issue no-container proof while an operation is in flight",
                    )
                if existing.container is not None and existing.state != "TOMBSTONED":
                    raise ExecutorError(
                        ExecutorErrorCode.INVALID_STATE,
                        "a bound container requires identity cleanup",
                    )
            try:
                matches = self.docker.find_by_labels(labels, timeout_seconds=5)
            except (RuntimeError, UnicodeDecodeError) as exc:
                raise ExecutorError(
                    ExecutorErrorCode.INSPECTION_UNAVAILABLE,
                    "Docker observation is unavailable",
                ) from exc
            if matches:
                raise ExecutorError(
                    ExecutorErrorCode.IDENTITY_MISMATCH,
                    "Docker still has a container for the startup identity",
                )
            observation = json.dumps(
                {"labels": labels, "container_ids": []}, sort_keys=True, separators=(",", ":")
            ).encode()
            checksum = self.journal.inspection_checksum(observation)
            try:
                tombstone = self.journal.create_unclaimed_tombstone(
                    attempt_id=attempt_id,
                    allocation_id=request.allocation.allocation_id,
                    startup_nonce=request.startup_nonce,
                    authority=request.context.authority,
                    resources=request.context.resources,
                    image_digest=request.context.image_digest,
                    input_checksum=request.context.input_checksum,
                    execution_binding=_execution_binding(request),
                    reason=reason,
                    inspection_checksum=checksum,
                )
            except JournalWriteError as exc:
                raise ExecutorError(ExecutorErrorCode.INVALID_STATE, str(exc)) from exc
        return CleanupProof(
            proof_type="NO_CONTAINER",
            startup_nonce=request.startup_nonce,
            executor_operation_sequence=tombstone.operation_sequence,
            tombstone_sequence=tombstone.tombstone_sequence,
            inspection_checksum=checksum,
            observed_at=_now(),
        )

    def _materialize_input(self, attempt_id: str, mount: InputMount) -> InputMount:
        source = Path(mount.source_path)
        root = self.staging_root.resolve(strict=True)
        target_dir = self.journal.materialized / attempt_id
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = target_dir / mount.artifact_id
        if os.path.lexists(target):
            _verify_file_beneath(self.journal.materialized, target, mount)
        else:
            fd, temporary = tempfile.mkstemp(prefix=f".{mount.artifact_id}.", dir=target_dir)
            source_fd: int | None = None
            try:
                try:
                    source_fd = _open_file_beneath(root, source)
                    _stream_verified_file(source_fd, mount, destination_fd=fd)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                    if source_fd is not None:
                        os.close(source_fd)
                os.chmod(temporary, 0o444)
                os.replace(temporary, target)
                directory_fd = os.open(target_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return replace(mount, source_path=str(target))

    @staticmethod
    def _validate_primary_input_binding(request: StartExecution) -> None:
        for mount in request.input_mounts:
            if (
                mount.target_path == "/input/input.json"
                and mount.content_checksum != request.context.input_checksum
            ):
                raise ValueError("primary CPU input checksum does not match authorized context")

    def _prepare_control_dir(self, request: StartExecution) -> Path:
        attempt_id = request.context.authority.attempt_id
        control_dir = self.journal.root / "control" / attempt_id
        control_dir.mkdir(mode=0o711, parents=True, exist_ok=True)
        # Runner UID 1000 must traverse this read-only bind to read the 0444 spec.
        os.chmod(control_dir, 0o711)
        if request.cpu_workload is not None:
            workload = request.cpu_workload
            launch_spec = {
                "schema_version": 1,
                "adapter_id": "cpu.iterative",
                "startup_nonce": request.startup_nonce,
                "input_path": workload.input_target_path,
                "output_path": "/output/result.json",
                "result_logical_name": workload.result_logical_name,
                "media_type": "application/vnd.nexa.cpu-iterative-result+json",
                "iterations": workload.iterations,
                "seed": workload.seed,
                "modulus": workload.modulus,
                "spec_checksum": workload.spec_checksum,
                "provenance": {
                    "tenant_id": request.context.tenant_id,
                    "job_id": request.context.job_id,
                    "session_id": request.context.logical_session_id,
                    "attempt_id": attempt_id,
                    "job_fence": request.context.authority.job_fence,
                    "input_checksum": request.context.input_checksum,
                    "spec_checksum": workload.spec_checksum,
                    "template_id": request.context.template_id,
                    "template_version": request.context.template_version,
                    "adapter_id": request.context.adapter_id,
                    "adapter_version": request.context.adapter_version,
                    "image_digest": request.context.image_digest,
                },
            }
            checkpoint = request.checkpoint
            if checkpoint is not None:
                # Version 2 is written only for a label-verified checkpoint-capable runner.
                restore = checkpoint.restore
                launch_spec.update(
                    schema_version=2,
                    checkpoint={
                        "state_path": checkpoint.state_path,
                        "compatibility": checkpoint.compatibility(request.context.architecture),
                    },
                    restore=None
                    if restore is None
                    else {
                        "path": restore.path,
                        "checkpoint_id": restore.checkpoint_id,
                        "checkpoint_sequence": restore.checkpoint_sequence,
                        "step": restore.step,
                        "accumulator": restore.accumulator,
                        "state_checksum": restore.state_checksum,
                    },
                )
            _write_immutable_json(control_dir / "launch-spec.json", launch_spec)
        return control_dir

    def _verify_materialized_inputs(self, mounts: tuple[InputMount, ...]) -> None:
        materialized_root = self.journal.materialized.resolve(strict=True)
        for mount in mounts:
            source = Path(mount.source_path)
            try:
                source.relative_to(materialized_root)
            except ValueError as exc:
                raise ExecutorError(
                    ExecutorErrorCode.INVALID_STATE,
                    "input mount is not executor materialized",
                ) from exc
            try:
                _verify_file_beneath(materialized_root, source, mount)
            except (OSError, ValueError) as exc:
                raise ExecutorError(
                    ExecutorErrorCode.INVALID_STATE,
                    f"input verification failed: {exc}",
                ) from exc

    def _inspect_raw(self, container_id: str, timeout: float) -> dict[str, object]:
        try:
            return self.docker.inspect(container_id, timeout_seconds=timeout).payload
        except DockerContainerNotFound:
            raise
        except RuntimeError as exc:
            raise ExecutorError(
                ExecutorErrorCode.INSPECTION_UNAVAILABLE,
                "container inspection failed",
            ) from exc

    def _inspect_identity(self, identity: ContainerIdentity, timeout: float) -> dict[str, object]:
        payload = self._inspect_raw(identity.container_id, timeout)
        if _runtime_identity_digest(payload) != identity.runtime_identity_digest:
            raise ExecutorError(
                ExecutorErrorCode.IDENTITY_MISMATCH,
                "container identity digest mismatch",
            )
        return payload


def _open_file_beneath(root: Path, path: Path) -> int:
    trusted_root = root.resolve(strict=True)
    try:
        relative = path.relative_to(trusted_root)
    except ValueError as exc:
        raise ValueError("input source is outside the trusted staging root") from exc
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("input source path is invalid")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(trusted_root, directory_flags)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(
            parts[-1],
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)


def _verify_file_beneath(root: Path, path: Path, mount: InputMount) -> None:
    descriptor = _open_file_beneath(root, path)
    try:
        _stream_verified_file(descriptor, mount)
    finally:
        os.close(descriptor)


def _stream_verified_file(
    descriptor: int, mount: InputMount, *, destination_fd: int | None = None
) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("input source must be a regular non-symlink file")
    digest = hashlib.sha256()
    total = 0
    remaining = mount.size_bytes + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        total += len(chunk)
        remaining -= len(chunk)
        if total > mount.size_bytes:
            raise ValueError("input size/checksum does not match the descriptor")
        digest.update(chunk)
        if destination_fd is not None:
            _write_all(destination_fd, chunk)
    checksum = "sha256:" + digest.hexdigest()
    if total != mount.size_bytes or checksum != mount.content_checksum:
        raise ValueError("input size/checksum does not match the descriptor")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("input materialization write made no progress")
        view = view[written:]


def _detect_clock_domain() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return _PROCESS_CLOCK_DOMAIN
    return f"linux-boot:{value}" if value else _PROCESS_CLOCK_DOMAIN


def _execution_binding(request: StartExecution) -> dict[str, object]:
    binding: dict[str, object] = {
        "context": asdict(request.context),
        "allocation": asdict(request.allocation),
        "startup_nonce": request.startup_nonce,
        "operation_sequence": request.operation_sequence,
        "scratch_bytes": request.scratch_bytes,
        "log_bytes": request.log_bytes,
        "runtime_limit_seconds": request.runtime_limit_seconds,
        "startup_limit_seconds": request.startup_limit_seconds,
        "input_mounts": [asdict(mount) for mount in request.input_mounts],
        "cpu_workload": (
            asdict(request.cpu_workload) if request.cpu_workload is not None else None
        ),
    }
    if request.checkpoint is not None:
        # Absent for non-checkpoint launches so B09-B13 journal bindings stay byte-identical.
        binding["checkpoint"] = asdict(request.checkpoint)
    return binding


def _supervisor_command(request: StartExecution) -> tuple[str, ...]:
    workload = request.cpu_workload
    if workload is None:
        raise ValueError("CPU workload spec is required")
    return (
        "python",
        "-m",
        "nexa.workloads.workload_supervisor",
        "--registration-socket",
        "/run/nexa/supervisor-register.sock",
        "--log-limit",
        str(request.log_bytes),
        "--",
        "python",
        "-m",
        "nexa.workloads.cpu_entrypoint",
        "--input",
        workload.input_target_path,
        "--output",
        "/output/result.json",
        "--iterations",
        str(workload.iterations),
        "--seed",
        str(workload.seed),
        "--modulus",
        str(workload.modulus),
        "--spec-checksum",
        workload.spec_checksum,
        *_checkpoint_arguments(request),
    )


def _checkpoint_arguments(request: StartExecution) -> tuple[str, ...]:
    checkpoint = request.checkpoint
    if checkpoint is None:
        return ()
    arguments = ("--state-output", checkpoint.state_path)
    if checkpoint.restore is not None:
        arguments += ("--resume-state", checkpoint.restore.path)
    return arguments


def _identity_labels(request: StartExecution) -> dict[str, str]:
    return {
        "nexa.managed": "true",
        "nexa.attempt_id": request.context.authority.attempt_id,
        "nexa.allocation_id": request.context.authority.allocation_id,
        "nexa.startup_nonce": request.startup_nonce,
    }


def _payload_checksum(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _normalize_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(str(item) for item in value)


def _immutable_runtime_identity(payload: dict[str, object]) -> dict[str, object]:
    config = payload.get("Config", {})
    host = payload.get("HostConfig", {})
    if not isinstance(config, dict):
        config = {}
    if not isinstance(host, dict):
        host = {}
    labels = config.get("Labels", {})
    if not isinstance(labels, dict):
        labels = {}
    selected_labels = {
        key: labels.get(key)
        for key in (
            "nexa.managed",
            "nexa.attempt_id",
            "nexa.allocation_id",
            "nexa.startup_nonce",
        )
    }
    if "nexa.installation_id" in labels:
        selected_labels["nexa.installation_id"] = labels["nexa.installation_id"]
    return {
        "container_id": payload.get("Id"),
        "image_id": payload.get("Image"),
        "image_ref": config.get("Image"),
        "user": config.get("User"),
        "labels": selected_labels,
        "readonly_rootfs": bool(host.get("ReadonlyRootfs", False)),
        "network_mode": host.get("NetworkMode"),
        "cap_drop": _normalize_string_list(host.get("CapDrop")),
        "cap_add": _normalize_string_list(host.get("CapAdd")),
        "security_opt": _normalize_string_list(host.get("SecurityOpt")),
        "pids_limit": host.get("PidsLimit"),
        "memory": host.get("Memory"),
        "memory_swap": host.get("MemorySwap"),
        "nano_cpus": host.get("NanoCpus"),
        "cpu_period": host.get("CpuPeriod"),
        "cpu_quota": host.get("CpuQuota"),
        "tmpfs": host.get("Tmpfs") or {},
        "log_config": host.get("LogConfig") or {},
        "restart_policy": host.get("RestartPolicy") or {},
    }


def runtime_identity_digest(payload: dict[str, object]) -> str:
    return _payload_checksum(_immutable_runtime_identity(payload))


_runtime_identity_digest = runtime_identity_digest


def _write_immutable_json(path: Path, value: object) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError("launch spec conflicts with the prepared execution")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o444)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
