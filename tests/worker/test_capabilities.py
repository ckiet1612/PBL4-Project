import pytest

from nexa.worker.capabilities import (
    AdapterCapability,
    FrameworkCapability,
    HostInventory,
    ImageCapability,
    ProbeBackend,
    ResourceProvider,
)
from nexa.worker.errors import CompatibilityReason, DiscoveryError
from nexa.worker.models import ResourceVector, WorkloadRequirement

IMAGE = "sha256:" + "a" * 64


def backend(**overrides: object) -> ProbeBackend:
    values: dict[str, object] = {
        "system": "Linux",
        "architecture": "linux/amd64",
        "host_cpu_millis": 8000,
        "host_memory_bytes": 16 * 1024**3,
        "docker_version": "29.5.3",
        "oci_runtime": "runc",
        "oci_runtime_version": "1.2.3",
        "cgroups_version": 2,
        "kernel_release": "6.8.0",
        "seccomp_available": True,
        "images": (ImageCapability(IMAGE, "linux/amd64", True),),
        "adapters": (AdapterCapability("cpu.iterative", "1.0.0"),),
        "frameworks": (FrameworkCapability("NEXA_CPU", "1.0.0", "CPU"),),
        "gpu_devices": (),
    }
    values.update(overrides)
    return ProbeBackend(**values)


def cpu_requirement(**overrides: object) -> WorkloadRequirement:
    values: dict[str, object] = {
        "architecture": "linux/amd64",
        "adapter_id": "cpu.iterative",
        "adapter_version": "1.0.0",
        "image_digest": IMAGE,
        "framework": "NEXA_CPU",
        "framework_version": "1.0.0",
        "device": "CPU",
        "gpu_count": 0,
        "resources": ResourceVector(1000, 64 * 1024**2, 0),
    }
    values.update(overrides)
    return WorkloadRequirement(**values)


def test_discovery_reports_host_and_runtime_capability() -> None:
    inventory = ResourceProvider(backend()).discover()
    assert isinstance(inventory, HostInventory)
    assert inventory.architecture == "linux/amd64"
    assert inventory.host_cpu_millis == 8000
    assert inventory.runtime.cgroups_version == 2
    assert inventory.images[0].verified is True


def test_discovery_fails_closed_for_non_linux_or_missing_runtime() -> None:
    with pytest.raises(DiscoveryError, match="Linux"):
        ResourceProvider(backend(system="Darwin")).discover()
    with pytest.raises(DiscoveryError, match="cgroups"):
        ResourceProvider(backend(cgroups_version=1)).discover()
    with pytest.raises(DiscoveryError, match="seccomp"):
        ResourceProvider(backend(seccomp_available=False)).discover()
    with pytest.raises(DiscoveryError, match="Docker"):
        ResourceProvider(backend(docker_version=None)).discover()


def test_allocatable_applies_floor_and_rounds_up() -> None:
    provider = ResourceProvider(backend(host_cpu_millis=3333, host_memory_bytes=10 * 1024**3))
    inventory = provider.discover()
    snapshot = provider.allocatable(inventory)
    assert snapshot.allocatable.cpu_millis == 2333
    assert snapshot.allocatable.memory_bytes == 8 * 1024**3


def test_allocatable_rejects_host_that_cannot_keep_minimum_workload() -> None:
    provider = ResourceProvider(backend(host_cpu_millis=1000, host_memory_bytes=2 * 1024**3))
    with pytest.raises(DiscoveryError, match="allocatable"):
        provider.allocatable(provider.discover())


def test_compatibility_is_closed_and_returns_structured_reason() -> None:
    provider = ResourceProvider(backend())
    snapshot = provider.allocatable(provider.discover())
    assert provider.compatible(cpu_requirement(), snapshot).compatible is True
    result = provider.compatible(cpu_requirement(image_digest="sha256:" + "b" * 64), snapshot)
    assert result.compatible is False
    assert result.reason is CompatibilityReason.IMAGE
    assert (
        provider.compatible(cpu_requirement(architecture="linux/arm64"), snapshot).reason
        is CompatibilityReason.ARCHITECTURE
    )


def test_compatibility_does_not_fallback_cuda_to_cpu() -> None:
    provider = ResourceProvider(backend())
    snapshot = provider.allocatable(provider.discover())
    requirement = cpu_requirement(
        device="CUDA",
        framework="PYTORCH",
        adapter_id="pytorch.cifar10",
        adapter_version="1.0.0",
        framework_version="2.5.1",
        gpu_count=1,
        resources=ResourceVector(1000, 64 * 1024**2, 1),
    )
    assert provider.compatible(requirement, snapshot).reason is CompatibilityReason.ADAPTER


def test_compatibility_checks_gpu_count_cuda_runtime_and_driver() -> None:
    provider = ResourceProvider(
        backend(
            adapters=(AdapterCapability("pytorch.cifar10", "1.0.0"),),
            frameworks=(FrameworkCapability("PYTORCH", "2.5.1", "CUDA", "12.1", "550.0"),),
            gpu_devices=(),
        )
    )
    snapshot = provider.allocatable(provider.discover())
    requirement = cpu_requirement(
        device="CUDA",
        framework="PYTORCH",
        adapter_id="pytorch.cifar10",
        adapter_version="1.0.0",
        framework_version="2.5.1",
        gpu_count=1,
        cuda_runtime_min="12.0.0",
        driver_min="550.0.0",
        resources=ResourceVector(1000, 64 * 1024**2, 1),
    )
    assert provider.compatible(requirement, snapshot).reason is CompatibilityReason.GPU_COUNT
