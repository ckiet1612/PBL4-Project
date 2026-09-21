"""Typed worker-side failures used at executor and discovery boundaries."""

from enum import StrEnum


class DiscoveryErrorCode(StrEnum):
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    DOCKER_UNAVAILABLE = "DOCKER_UNAVAILABLE"
    CGROUPS_UNAVAILABLE = "CGROUPS_UNAVAILABLE"
    SECCOMP_UNAVAILABLE = "SECCOMP_UNAVAILABLE"
    INVALID_RESERVE = "INVALID_RESERVE"
    PROBE_FAILED = "PROBE_FAILED"
    INVALID_INVENTORY = "INVALID_INVENTORY"


class ExecutorErrorCode(StrEnum):
    IMAGE_UNAVAILABLE = "IMAGE_UNAVAILABLE"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    START_TIMEOUT = "START_TIMEOUT"
    CREATE_OUTCOME_UNKNOWN = "CREATE_OUTCOME_UNKNOWN"
    START_OUTCOME_UNKNOWN = "START_OUTCOME_UNKNOWN"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    OOM = "OOM"
    STOP_TIMEOUT = "STOP_TIMEOUT"
    INSPECTION_UNAVAILABLE = "INSPECTION_UNAVAILABLE"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    INVALID_STATE = "INVALID_STATE"
    UNSUPPORTED = "UNSUPPORTED"
    INTERNAL = "INTERNAL"


class CompatibilityReason(StrEnum):
    ARCHITECTURE = "ARCHITECTURE"
    IMAGE = "IMAGE"
    ADAPTER = "ADAPTER"
    FRAMEWORK = "FRAMEWORK"
    CUDA = "CUDA"
    DRIVER = "DRIVER"
    GPU_COUNT = "GPU_COUNT"
    RESOURCE = "RESOURCE"
    RUNTIME = "RUNTIME"


class WorkerError(RuntimeError):
    """Base for safe, typed worker errors."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DiscoveryError(WorkerError):
    pass


class ExecutorError(WorkerError):
    pass
