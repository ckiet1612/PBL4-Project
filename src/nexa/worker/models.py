"""Transport-free worker-side value objects.

These objects intentionally model only immutable identity and bounded execution
inputs. They do not import Docker, persistence, API or workload frameworks.
"""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from .errors import CompatibilityReason

Checksum = str
Architecture = Literal["linux/amd64", "linux/arm64"]
Device = Literal["CPU", "CUDA"]
Framework = Literal["NEXA_CPU", "PYTORCH"]

_UUID_V7 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def _uuid(value: str, field: str) -> str:
    if not isinstance(value, str) or not _UUID_V7.fullmatch(value):
        raise ValueError(f"{field} must be a canonical UUIDv7")
    return value


def _checksum(value: str, field: str) -> str:
    if not isinstance(value, str) or not _CHECKSUM.fullmatch(value):
        raise ValueError(f"{field} must be a sha256 checksum")
    return value


def _semver(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SEMVER.fullmatch(value):
        raise ValueError(f"{field} must be semantic version")
    return value


def _positive(value: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class Authority:
    worker_id: str
    worker_incarnation_id: str
    attempt_id: str
    allocation_id: str
    lease_id: str
    job_fence: int

    def __post_init__(self) -> None:
        for name in (
            "worker_id",
            "worker_incarnation_id",
            "attempt_id",
            "allocation_id",
            "lease_id",
        ):
            _uuid(getattr(self, name), name)
        _positive(self.job_fence, "job_fence")


@dataclass(frozen=True, slots=True)
class ResourceVector:
    cpu_millis: int
    memory_bytes: int
    gpu_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.cpu_millis, int) or self.cpu_millis < 0:
            raise ValueError("cpu_millis must be non-negative")
        if not isinstance(self.memory_bytes, int) or self.memory_bytes <= 0:
            raise ValueError("memory_bytes must be positive")
        if not isinstance(self.gpu_count, int) or self.gpu_count < 0 or self.gpu_count > 1:
            raise ValueError("gpu_count must be 0 or 1")


@dataclass(frozen=True, slots=True)
class AllocationIdentity:
    allocation_id: str
    attempt_id: str
    resources: ResourceVector

    def __post_init__(self) -> None:
        _uuid(self.allocation_id, "allocation_id")
        _uuid(self.attempt_id, "attempt_id")


@dataclass(frozen=True, slots=True)
class InputMount:
    artifact_id: str
    source_path: str
    target_path: str
    content_checksum: Checksum
    size_bytes: int
    read_only: bool = True

    def __post_init__(self) -> None:
        _uuid(self.artifact_id, "artifact_id")
        _checksum(self.content_checksum, "content_checksum")
        if (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes < 0
        ):
            raise ValueError("size_bytes must be a non-negative integer")
        source = PurePosixPath(self.source_path)
        if not source.is_absolute() or ".." in source.parts or "\x00" in self.source_path:
            raise ValueError("source_path must be an absolute trusted staging path")
        target = PurePosixPath(self.target_path)
        if (
            not target.is_absolute()
            or target.parts[:2] != ("/", "input")
            or len(target.parts) < 3
            or ".." in target.parts
        ):
            raise ValueError("target_path must be an input mount path")
        if not self.read_only:
            raise ValueError("input mounts must be read-only")


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    authority: Authority
    tenant_id: str
    job_id: str
    logical_session_id: str
    template_id: str
    template_version: int
    image_digest: Checksum
    architecture: Architecture
    adapter_id: str
    adapter_version: str
    input_checksum: Checksum
    startup_nonce: str
    resources: ResourceVector
    device: Device = "CPU"
    framework: Framework = "NEXA_CPU"
    framework_version: str = "1.0.0"

    def __post_init__(self) -> None:
        for name in ("tenant_id", "job_id", "logical_session_id"):
            _uuid(getattr(self, name), name)
        if self.template_id != "cpu-iterative":
            raise ValueError("template_id is not supported by the CPU executor")
        _positive(self.template_version, "template_version")
        _checksum(self.image_digest, "image_digest")
        _checksum(self.input_checksum, "input_checksum")
        _uuid(self.startup_nonce, "startup_nonce")
        _semver(self.adapter_version, "adapter_version")
        _semver(self.framework_version, "framework_version")
        if not self.adapter_id or "/" in self.adapter_id or " " in self.adapter_id:
            raise ValueError("adapter_id must be an allowlisted logical identifier")
        if self.device == "CPU" and self.resources.gpu_count != 0:
            raise ValueError("CPU execution cannot request a GPU")
        if self.device == "CUDA" and self.resources.gpu_count != 1:
            raise ValueError("CUDA execution requires exactly one GPU")


@dataclass(frozen=True, slots=True)
class CpuWorkloadSpec:
    iterations: int
    seed: int
    modulus: int
    spec_checksum: Checksum
    input_target_path: str = "/input/input.json"
    result_logical_name: str = "result.json"

    def __post_init__(self) -> None:
        if not isinstance(self.iterations, int) or isinstance(self.iterations, bool):
            raise ValueError("iterations must be an integer")
        if not 1 <= self.iterations <= 1_000_000_000:
            raise ValueError("iterations is outside the contract range")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ValueError("seed must be an integer")
        if not 0 <= self.seed <= 2_147_483_647:
            raise ValueError("seed is outside the contract range")
        if not isinstance(self.modulus, int) or isinstance(self.modulus, bool):
            raise ValueError("modulus must be an integer")
        if not 2 <= self.modulus <= 2_147_483_647:
            raise ValueError("modulus is outside the contract range")
        _checksum(self.spec_checksum, "spec_checksum")
        if self.input_target_path != "/input/input.json":
            raise ValueError("CPU input target is fixed by the image contract")
        if self.result_logical_name != "result.json":
            raise ValueError("CPU result logical name is fixed by the image contract")


CHECKPOINT_STATE_PATH = "/output/state.json"
RESTORE_STATE_PATH = "/input/restore-state.json"


@dataclass(frozen=True, slots=True)
class CheckpointRestore:
    """Worker-verified restore cursor; bytes are mounted read-only at a fixed path."""

    checkpoint_id: str
    checkpoint_sequence: int
    step: int
    accumulator: int
    state_checksum: Checksum
    path: str = RESTORE_STATE_PATH

    def __post_init__(self) -> None:
        _uuid(self.checkpoint_id, "checkpoint_id")
        _positive(self.checkpoint_sequence, "checkpoint_sequence")
        for name in ("step", "accumulator"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        _checksum(self.state_checksum, "state_checksum")
        if self.path != RESTORE_STATE_PATH:
            raise ValueError("restore path is fixed by the image contract")


@dataclass(frozen=True, slots=True)
class CpuCheckpointLaunch:
    """Checkpoint capability of one CPU launch; absent for non-checkpointable images."""

    framework_version: str
    restart_safe: bool
    interval_seconds: int = 30
    restore: CheckpointRestore | None = None
    state_path: str = CHECKPOINT_STATE_PATH

    def __post_init__(self) -> None:
        _semver(self.framework_version, "framework_version")
        if type(self.restart_safe) is not bool:
            raise ValueError("restart_safe must be a boolean")
        if (
            not isinstance(self.interval_seconds, int)
            or isinstance(self.interval_seconds, bool)
            or not 5 <= self.interval_seconds <= 60
        ):
            raise ValueError("checkpoint interval must be between 5 and 60 seconds")
        if self.state_path != CHECKPOINT_STATE_PATH:
            raise ValueError("checkpoint state path is fixed by the image contract")

    def compatibility(self, architecture: Architecture) -> dict[str, object]:
        # Identical to the server's expected_compatibility for a CPU template.
        return {
            "architecture": architecture,
            "device_type": "CPU",
            "framework": "PYTHON",
            "framework_version": self.framework_version,
            "cuda_version": None,
            "minimum_driver_version": None,
            "gpu_compute_capability": None,
            "checkpointable": True,
            "restart_safe": self.restart_safe,
        }


@dataclass(frozen=True, slots=True)
class StartExecution:
    context: ExecutionContext
    allocation: AllocationIdentity
    startup_nonce: str
    operation_sequence: int
    scratch_bytes: int
    log_bytes: int
    runtime_limit_seconds: int
    startup_limit_seconds: int = 30
    input_mounts: tuple[InputMount, ...] = ()
    cpu_workload: CpuWorkloadSpec | None = None
    checkpoint: CpuCheckpointLaunch | None = None

    def __post_init__(self) -> None:
        _uuid(self.startup_nonce, "startup_nonce")
        _positive(self.operation_sequence, "operation_sequence")
        if self.startup_nonce != self.context.startup_nonce:
            raise ValueError("startup_nonce does not match execution context")
        if self.context.authority.attempt_id != self.allocation.attempt_id:
            raise ValueError("allocation attempt does not match authority")
        if self.context.authority.allocation_id != self.allocation.allocation_id:
            raise ValueError("allocation does not match authority")
        if self.context.resources != self.allocation.resources:
            raise ValueError("allocation resources do not match execution context")
        if self.context.resources.cpu_millis <= 0:
            raise ValueError("execution requires a positive CPU allocation")
        if self.scratch_bytes <= 0 or self.log_bytes <= 0:
            raise ValueError("scratch_bytes/log_bytes must be positive bounded integers")
        if not 1 <= self.runtime_limit_seconds <= 300:
            raise ValueError("runtime_limit_seconds must be between 1 and 300")
        if self.startup_limit_seconds != 30:
            raise ValueError("startup_limit_seconds must be 30")
        if self.cpu_workload is not None:
            if self.context.adapter_id != "cpu.iterative" or self.context.framework != "NEXA_CPU":
                raise ValueError("CPU workload spec requires the CPU iterative adapter")
            targets = {mount.target_path for mount in self.input_mounts}
            if self.cpu_workload.input_target_path not in targets:
                raise ValueError("CPU workload input target is not mounted")
        restore_mounts = [
            mount for mount in self.input_mounts if mount.target_path == RESTORE_STATE_PATH
        ]
        if self.checkpoint is None:
            if restore_mounts:
                raise ValueError("restore state mount requires a checkpoint launch")
            return
        if self.cpu_workload is None:
            raise ValueError("checkpoint launch requires the CPU workload spec")
        restore = self.checkpoint.restore
        if restore is None:
            if restore_mounts:
                raise ValueError("restore state mount requires a restore cursor")
            return
        if len(restore_mounts) != 1 or restore_mounts[0].content_checksum != restore.state_checksum:
            raise ValueError("restore state mount does not match the restore cursor")
        if (
            restore.step > self.cpu_workload.iterations
            or restore.accumulator >= self.cpu_workload.modulus
        ):
            raise ValueError("restore cursor is outside the job bounds")


@dataclass(frozen=True, slots=True)
class ContainerIdentity:
    container_id: str
    runtime_identity_digest: Checksum
    attempt_id: str
    allocation_id: str
    startup_nonce: str
    architecture: Architecture = "linux/amd64"
    image_id: str | None = None

    def __post_init__(self) -> None:
        if not _CONTAINER_ID.fullmatch(self.container_id):
            raise ValueError("container_id must be the full 64-character Docker ID")
        _checksum(self.runtime_identity_digest, "runtime_identity_digest")
        _uuid(self.attempt_id, "attempt_id")
        _uuid(self.allocation_id, "allocation_id")
        _uuid(self.startup_nonce, "startup_nonce")


@dataclass(frozen=True, slots=True)
class WorkloadRequirement:
    architecture: Architecture
    adapter_id: str
    adapter_version: str
    image_digest: Checksum
    framework: Framework
    framework_version: str
    device: Device
    gpu_count: int
    resources: ResourceVector
    cuda_runtime_min: str | None = None
    driver_min: str | None = None

    def __post_init__(self) -> None:
        _semver(self.adapter_version, "adapter_version")
        _semver(self.framework_version, "framework_version")
        _checksum(self.image_digest, "image_digest")
        if self.device == "CPU" and self.gpu_count != 0:
            raise ValueError("CPU requirement must have gpu_count=0")
        if self.device == "CUDA" and self.gpu_count != 1:
            raise ValueError("CUDA requirement must have gpu_count=1")
        if self.resources.gpu_count != self.gpu_count:
            raise ValueError("gpu_count does not match resource vector")
        if self.cuda_runtime_min is not None:
            _semver(self.cuda_runtime_min, "cuda_runtime_min")
        if self.driver_min is not None:
            _semver(self.driver_min, "driver_min")


@dataclass(frozen=True, slots=True)
class ContainerObservation:
    identity: ContainerIdentity
    running: bool
    exit_code: int | None
    oom_killed: bool
    config_checksum: Checksum
    observed_at: str


@dataclass(frozen=True, slots=True)
class CleanupProof:
    proof_type: Literal["NO_CONTAINER", "CONTAINER_STOPPED"]
    startup_nonce: str
    executor_operation_sequence: int
    inspection_checksum: Checksum
    observed_at: str
    tombstone_sequence: int | None = None
    container: ContainerIdentity | None = None
    stopped_at: str | None = None
    exit_code: int | None = None

    def __post_init__(self) -> None:
        _uuid(self.startup_nonce, "startup_nonce")
        _positive(self.executor_operation_sequence, "executor_operation_sequence")
        _checksum(self.inspection_checksum, "inspection_checksum")
        if self.proof_type == "NO_CONTAINER":
            if self.tombstone_sequence is None:
                raise ValueError("NO_CONTAINER proof requires tombstone_sequence")
            if self.container is not None or self.exit_code is not None:
                raise ValueError("NO_CONTAINER proof cannot include container data")
        elif self.proof_type == "CONTAINER_STOPPED":
            if self.container is None or self.stopped_at is None or self.exit_code is None:
                raise ValueError("CONTAINER_STOPPED proof requires container and stop data")
        else:
            raise ValueError("unknown cleanup proof type")


@dataclass(frozen=True, slots=True)
class PreparedExecution:
    request: StartExecution
    config: object


@dataclass(frozen=True, slots=True)
class CompatibilityResult:
    compatible: bool
    reason: CompatibilityReason | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.compatible and self.reason is not None:
            raise ValueError("compatible result cannot carry a failure reason")
        if not self.compatible and self.reason is None:
            raise ValueError("incompatible result requires a reason")
