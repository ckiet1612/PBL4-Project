"""Closed host capability discovery and compatibility evaluation."""

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from .errors import CompatibilityReason, DiscoveryError, DiscoveryErrorCode
from .models import CompatibilityResult, ResourceVector, WorkloadRequirement

GIB = 1024**3


@dataclass(frozen=True, slots=True)
class RuntimeCapability:
    docker_version: str
    oci_runtime: str
    oci_runtime_version: str
    cgroups_version: int
    kernel_release: str
    seccomp_available: bool


@dataclass(frozen=True, slots=True)
class AdapterCapability:
    adapter_id: str
    adapter_version: str


@dataclass(frozen=True, slots=True)
class ImageCapability:
    image_digest: str
    architecture: str
    verified: bool


@dataclass(frozen=True, slots=True)
class FrameworkCapability:
    framework: str
    framework_version: str
    device: str
    cuda_runtime_version: str | None = None
    driver_min: str | None = None


@dataclass(frozen=True, slots=True)
class GpuCapability:
    uuid: str
    model: str
    memory_bytes: int
    compute_capability: str
    driver_version: str
    healthy: bool = True


@dataclass(frozen=True, slots=True)
class ProbeBackend:
    """Pure probe snapshot used by tests and the real system probe."""

    system: str
    architecture: str
    host_cpu_millis: int
    host_memory_bytes: int
    docker_version: str | None
    oci_runtime: str
    oci_runtime_version: str
    cgroups_version: int
    kernel_release: str
    seccomp_available: bool
    images: tuple[ImageCapability, ...]
    adapters: tuple[AdapterCapability, ...]
    frameworks: tuple[FrameworkCapability, ...]
    gpu_devices: tuple[GpuCapability, ...]


@dataclass(frozen=True, slots=True)
class HostInventory:
    architecture: str
    host_cpu_millis: int
    host_memory_bytes: int
    runtime: RuntimeCapability
    adapters: tuple[AdapterCapability, ...]
    images: tuple[ImageCapability, ...]
    frameworks: tuple[FrameworkCapability, ...]
    gpu_devices: tuple[GpuCapability, ...]
    discovered_at: str


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    inventory: HostInventory
    allocatable: ResourceVector


@dataclass(frozen=True, slots=True)
class ReservePolicy:
    cpu_millis: int | None = None
    memory_bytes: int | None = None


class ResourceProvider:
    def __init__(self, backend: ProbeBackend | None = None) -> None:
        self._backend = backend

    def _probe(self) -> ProbeBackend:
        if self._backend is not None:
            return self._backend
        from .probes import DockerProbeBackend

        try:
            return DockerProbeBackend().snapshot()
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            raise DiscoveryError(
                DiscoveryErrorCode.PROBE_FAILED, "live Docker runtime probe failed"
            ) from exc

    def discover(self) -> HostInventory:
        probe = self._probe()
        if probe.system != "Linux":
            raise DiscoveryError(
                DiscoveryErrorCode.UNSUPPORTED_PLATFORM, "Docker workload discovery requires Linux"
            )
        if probe.docker_version is None:
            raise DiscoveryError(
                DiscoveryErrorCode.DOCKER_UNAVAILABLE, "Docker runtime probe failed"
            )
        if probe.architecture not in {"linux/amd64", "linux/arm64"}:
            raise DiscoveryError(
                DiscoveryErrorCode.INVALID_INVENTORY, "workload architecture is unknown"
            )
        if probe.cgroups_version != 2:
            raise DiscoveryError(DiscoveryErrorCode.CGROUPS_UNAVAILABLE, "cgroups v2 is required")
        if not probe.seccomp_available:
            raise DiscoveryError(DiscoveryErrorCode.SECCOMP_UNAVAILABLE, "seccomp is required")
        if probe.host_cpu_millis < 1000 or probe.host_memory_bytes < 2 * GIB:
            raise DiscoveryError(
                DiscoveryErrorCode.INVALID_INVENTORY, "host inventory is below the minimum bounds"
            )
        if any(not image.verified for image in probe.images):
            raise DiscoveryError(
                DiscoveryErrorCode.INVALID_INVENTORY, "all declared images must be verified"
            )
        gpu_uuids = [device.uuid for device in probe.gpu_devices]
        if len(gpu_uuids) != len(set(gpu_uuids)):
            raise DiscoveryError(DiscoveryErrorCode.INVALID_INVENTORY, "GPU UUIDs must be unique")
        return HostInventory(
            architecture=probe.architecture,
            host_cpu_millis=probe.host_cpu_millis,
            host_memory_bytes=probe.host_memory_bytes,
            runtime=RuntimeCapability(
                docker_version=probe.docker_version,
                oci_runtime=probe.oci_runtime,
                oci_runtime_version=probe.oci_runtime_version,
                cgroups_version=probe.cgroups_version,
                kernel_release=probe.kernel_release,
                seccomp_available=probe.seccomp_available,
            ),
            adapters=probe.adapters,
            images=probe.images,
            frameworks=probe.frameworks,
            gpu_devices=probe.gpu_devices,
            discovered_at=datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        )

    def allocatable(
        self, inventory: HostInventory, reserve: ReservePolicy | None = None
    ) -> CapabilitySnapshot:
        reserve = reserve or ReservePolicy()
        if reserve.cpu_millis is not None and reserve.cpu_millis < 0:
            raise DiscoveryError(
                DiscoveryErrorCode.INVALID_RESERVE, "CPU reserve cannot be negative"
            )
        if reserve.memory_bytes is not None and reserve.memory_bytes < 0:
            raise DiscoveryError(
                DiscoveryErrorCode.INVALID_RESERVE, "RAM reserve cannot be negative"
            )
        floor_cpu = max(1000, math.ceil(inventory.host_cpu_millis * 0.20))
        floor_memory = max(2 * GIB, math.ceil(inventory.host_memory_bytes * 0.20))
        reserved_cpu = max(floor_cpu, reserve.cpu_millis or 0)
        reserved_memory = max(floor_memory, reserve.memory_bytes or 0)
        cpu = inventory.host_cpu_millis - reserved_cpu
        memory = inventory.host_memory_bytes - reserved_memory
        if cpu < 1000 or memory < 64 * 1024**2:
            raise DiscoveryError(
                DiscoveryErrorCode.INVALID_INVENTORY,
                "allocatable capacity is below the minimum bounds",
            )
        return CapabilitySnapshot(
            inventory=inventory,
            allocatable=ResourceVector(
                cpu_millis=cpu,
                memory_bytes=memory,
                gpu_count=sum(device.healthy for device in inventory.gpu_devices),
            ),
        )

    def compatible(
        self, requirement: WorkloadRequirement, capability: CapabilitySnapshot
    ) -> CompatibilityResult:
        inventory = capability.inventory
        if requirement.architecture != inventory.architecture:
            return CompatibilityResult(
                False, CompatibilityReason.ARCHITECTURE, "architecture mismatch"
            )
        image = next(
            (item for item in inventory.images if item.image_digest == requirement.image_digest),
            None,
        )
        if image is None or not image.verified or image.architecture != requirement.architecture:
            return CompatibilityResult(
                False, CompatibilityReason.IMAGE, "exact verified image is unavailable"
            )
        adapter = next(
            (
                item
                for item in inventory.adapters
                if item.adapter_id == requirement.adapter_id
                and item.adapter_version == requirement.adapter_version
            ),
            None,
        )
        if adapter is None:
            return CompatibilityResult(
                False, CompatibilityReason.ADAPTER, "exact adapter is unavailable"
            )
        framework = next(
            (
                item
                for item in inventory.frameworks
                if item.framework == requirement.framework
                and item.framework_version == requirement.framework_version
                and item.device == requirement.device
            ),
            None,
        )
        if framework is None:
            return CompatibilityResult(
                False, CompatibilityReason.FRAMEWORK, "exact framework/device is unavailable"
            )
        healthy_gpus = [device for device in inventory.gpu_devices if device.healthy]
        if len(healthy_gpus) < requirement.gpu_count:
            return CompatibilityResult(
                False,
                CompatibilityReason.GPU_COUNT,
                "the requested number of healthy GPUs is unavailable",
            )
        if (
            requirement.device == "CUDA"
            and requirement.cuda_runtime_min is not None
            and (
                framework.cuda_runtime_version is None
                or not _version_at_least(
                    framework.cuda_runtime_version, requirement.cuda_runtime_min
                )
            )
        ):
            return CompatibilityResult(
                False, CompatibilityReason.CUDA, "CUDA runtime is below the requirement"
            )
        if (
            requirement.device == "CUDA"
            and requirement.driver_min is not None
            and not any(
                _version_at_least(device.driver_version, requirement.driver_min)
                for device in healthy_gpus
            )
        ):
            return CompatibilityResult(
                False, CompatibilityReason.DRIVER, "GPU driver is below the requirement"
            )
        if (
            requirement.resources.cpu_millis > capability.allocatable.cpu_millis
            or requirement.resources.memory_bytes > capability.allocatable.memory_bytes
        ):
            return CompatibilityResult(
                False,
                CompatibilityReason.RESOURCE,
                "requested resources exceed allocatable capacity",
            )
        return CompatibilityResult(True)


def _version_at_least(actual: str, minimum: str) -> bool:
    try:
        actual_parts = tuple(int(part) for part in actual.split("."))
        minimum_parts = tuple(int(part) for part in minimum.split("."))
    except ValueError:
        return False
    width = max(len(actual_parts), len(minimum_parts))
    return actual_parts + (0,) * (width - len(actual_parts)) >= minimum_parts + (0,) * (
        width - len(minimum_parts)
    )


def inventory_to_json(
    inventory: HostInventory, snapshot: CapabilitySnapshot | None = None
) -> dict[str, Any]:
    payload = {
        "architecture": inventory.architecture,
        "host_cpu_millis": inventory.host_cpu_millis,
        "host_memory_bytes": inventory.host_memory_bytes,
        "runtime": {
            "docker_version": inventory.runtime.docker_version,
            "oci_runtime": inventory.runtime.oci_runtime,
            "oci_runtime_version": inventory.runtime.oci_runtime_version,
            "cgroups_version": inventory.runtime.cgroups_version,
            "kernel_release": inventory.runtime.kernel_release,
            "seccomp_available": inventory.runtime.seccomp_available,
        },
        "adapters": [asdict(item) for item in inventory.adapters],
        "images": [asdict(item) for item in inventory.images],
        "frameworks": [
            {
                "framework": item.framework,
                "framework_version": item.framework_version,
                "device": item.device,
                "cuda_runtime_version": item.cuda_runtime_version,
            }
            for item in inventory.frameworks
        ],
        "gpu_devices": [asdict(item) for item in inventory.gpu_devices],
        "discovered_at": inventory.discovered_at,
    }
    if snapshot is not None:
        payload["allocatable"] = {
            "cpu_millis": snapshot.allocatable.cpu_millis,
            "memory_bytes": snapshot.allocatable.memory_bytes,
            "gpu_count": snapshot.allocatable.gpu_count,
        }
    return payload
