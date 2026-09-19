from dataclasses import FrozenInstanceError

import pytest

from benchmarks.simulator.model import (
    Allocation,
    JobSpec,
    ModelError,
    ResourceVector,
    SimulationConfig,
    TenantSpec,
)


def test_resource_vector_rejects_negative_components() -> None:
    with pytest.raises(ModelError, match="non-negative"):
        ResourceVector(cpu_millis=-1, memory_bytes=0, gpu_count=0)


def test_zero_resource_vector_is_valid_and_immutable() -> None:
    resources = ResourceVector(cpu_millis=0, memory_bytes=0, gpu_count=0)

    assert resources == ResourceVector.zero()
    with pytest.raises(FrozenInstanceError):
        resources.cpu_millis = 1  # type: ignore[misc]


def test_resource_arithmetic_rejects_underflow() -> None:
    capacity = ResourceVector(cpu_millis=2_000, memory_bytes=4_096, gpu_count=1)
    request = ResourceVector(cpu_millis=500, memory_bytes=1_024, gpu_count=1)

    assert capacity.subtract(request) == ResourceVector(1_500, 3_072, 0)
    assert request.add(request) == ResourceVector(1_000, 2_048, 2)
    assert request.fits_within(capacity)

    with pytest.raises(ModelError, match="underflow"):
        request.subtract(capacity)


@pytest.mark.parametrize("weight", [0, -1])
def test_tenant_weight_must_be_positive(weight: int) -> None:
    with pytest.raises(ModelError, match="weight"):
        TenantSpec(
            tenant_id="tenant-a",
            weight=weight,
            quota=ResourceVector(1_000, 1_024, 0),
            max_running_jobs=1,
            order=0,
        )


def test_job_duration_must_be_positive() -> None:
    with pytest.raises(ModelError, match="duration"):
        JobSpec(
            job_id="job-1",
            tenant_id="tenant-a",
            ready_sequence=1,
            arrival_ms=0,
            resources=ResourceVector(100, 100, 0),
            duration_ms=0,
        )


def test_simulation_config_rejects_duplicate_gpu_ids() -> None:
    tenant = TenantSpec(
        tenant_id="tenant-a",
        weight=1,
        quota=ResourceVector(2_000, 4_096, 2),
        max_running_jobs=2,
        order=0,
    )

    with pytest.raises(ModelError, match="duplicate GPU UUID"):
        SimulationConfig(
            capacity=ResourceVector(2_000, 4_096, 2),
            gpu_uuids=("gpu-1", "gpu-1"),
            tenants=(tenant,),
        )


def test_allocation_requires_exact_sorted_unique_gpu_ids() -> None:
    with pytest.raises(ModelError, match="sorted"):
        Allocation(
            job_id="job-1",
            tenant_id="tenant-a",
            resources=ResourceVector(100, 100, 2),
            gpu_uuids=("gpu-b", "gpu-a"),
            start_ms=0,
            release_ms=10,
        )


def test_simulation_config_has_stable_unique_tenant_order() -> None:
    tenants = (
        TenantSpec("tenant-b", 1, ResourceVector(1_000, 1_000, 0), 1, 1),
        TenantSpec("tenant-a", 1, ResourceVector(1_000, 1_000, 0), 1, 0),
    )

    config = SimulationConfig(
        capacity=ResourceVector(1_000, 1_000, 0),
        gpu_uuids=(),
        tenants=tenants,
    )

    assert [tenant.tenant_id for tenant in config.tenants] == ["tenant-a", "tenant-b"]
