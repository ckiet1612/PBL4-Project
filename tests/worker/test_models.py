from dataclasses import FrozenInstanceError

import pytest

from nexa.worker.models import (
    AllocationIdentity,
    Authority,
    ContainerIdentity,
    ExecutionContext,
    InputMount,
    ResourceVector,
    StartExecution,
    WorkloadRequirement,
)

UUIDS = {
    "worker": "018f0d60-7b6a-7a21-9d82-1aa39c4f30b7",
    "incarnation": "018f0d60-7b6a-7a22-9d82-1aa39c4f30b7",
    "attempt": "018f0d60-7b6a-7a23-9d82-1aa39c4f30b7",
    "allocation": "018f0d60-7b6a-7a24-9d82-1aa39c4f30b7",
    "lease": "018f0d60-7b6a-7a25-9d82-1aa39c4f30b7",
    "nonce": "018f0d60-7b6a-7a26-9d82-1aa39c4f30b7",
    "tenant": "018f0d60-7b6a-7a27-9d82-1aa39c4f30b7",
    "job": "018f0d60-7b6a-7a28-9d82-1aa39c4f30b7",
    "session": "018f0d60-7b6a-7a29-9d82-1aa39c4f30b7",
}
IMAGE = "sha256:" + "a" * 64
CHECKSUM = "sha256:" + "b" * 64


def authority() -> Authority:
    return Authority(
        worker_id=UUIDS["worker"],
        worker_incarnation_id=UUIDS["incarnation"],
        attempt_id=UUIDS["attempt"],
        allocation_id=UUIDS["allocation"],
        lease_id=UUIDS["lease"],
        job_fence=3,
    )


def context() -> ExecutionContext:
    return ExecutionContext(
        authority=authority(),
        tenant_id=UUIDS["tenant"],
        job_id=UUIDS["job"],
        logical_session_id=UUIDS["session"],
        template_id="cpu-iterative",
        template_version=1,
        image_digest=IMAGE,
        architecture="linux/amd64",
        adapter_id="cpu.iterative",
        adapter_version="1.0.0",
        input_checksum=CHECKSUM,
        startup_nonce=UUIDS["nonce"],
        resources=ResourceVector(cpu_millis=1000, memory_bytes=256 * 1024 * 1024, gpu_count=0),
    )


def test_models_reject_non_v7_ids_and_invalid_checksums() -> None:
    with pytest.raises(ValueError, match="UUIDv7"):
        Authority(
            worker_id="00000000-0000-4000-8000-000000000000",
            worker_incarnation_id=UUIDS["incarnation"],
            attempt_id=UUIDS["attempt"],
            allocation_id=UUIDS["allocation"],
            lease_id=UUIDS["lease"],
            job_fence=1,
        )
    with pytest.raises(ValueError, match="checksum"):
        ContainerIdentity(
            container_id="a" * 64,
            runtime_identity_digest="not-a-checksum",
            attempt_id=UUIDS["attempt"],
            allocation_id=UUIDS["allocation"],
            startup_nonce=UUIDS["nonce"],
        )


def test_resource_vector_is_non_negative_and_bounded() -> None:
    with pytest.raises(ValueError, match="cpu_millis"):
        ResourceVector(cpu_millis=-1, memory_bytes=1, gpu_count=0)
    with pytest.raises(ValueError, match="memory_bytes"):
        ResourceVector(cpu_millis=1, memory_bytes=0, gpu_count=0)
    with pytest.raises(ValueError, match="gpu_count"):
        ResourceVector(cpu_millis=1, memory_bytes=1, gpu_count=2)


def test_execution_context_rejects_mismatched_allocation_identity() -> None:
    start = StartExecution(
        context=context(),
        allocation=AllocationIdentity(
            allocation_id=UUIDS["allocation"],
            attempt_id=UUIDS["attempt"],
            resources=ResourceVector(1000, 256 * 1024 * 1024, 0),
        ),
        startup_nonce=UUIDS["nonce"],
        operation_sequence=1,
        scratch_bytes=64 * 1024 * 1024,
        log_bytes=1024 * 1024,
        runtime_limit_seconds=30,
    )
    assert start.context.authority.allocation_id == start.allocation.allocation_id
    with pytest.raises(ValueError, match="allocation"):
        StartExecution(
            context=context(),
            allocation=AllocationIdentity(
                allocation_id="018f0d60-7b6a-7a27-9d82-1aa39c4f30b7",
                attempt_id=UUIDS["attempt"],
                resources=ResourceVector(1000, 256 * 1024 * 1024, 0),
            ),
            startup_nonce=UUIDS["nonce"],
            operation_sequence=1,
            scratch_bytes=64 * 1024 * 1024,
            log_bytes=1024 * 1024,
            runtime_limit_seconds=30,
        )


def test_container_identity_is_immutable_and_bound_to_attempt() -> None:
    identity = ContainerIdentity(
        container_id="a" * 64,
        runtime_identity_digest=CHECKSUM,
        attempt_id=UUIDS["attempt"],
        allocation_id=UUIDS["allocation"],
        startup_nonce=UUIDS["nonce"],
    )
    with pytest.raises(FrozenInstanceError):
        identity.container_id = "b" * 64  # type: ignore[misc]


def test_workload_requirement_rejects_cuda_without_gpu() -> None:
    with pytest.raises(ValueError, match="gpu_count"):
        WorkloadRequirement(
            architecture="linux/amd64",
            adapter_id="cpu.iterative",
            adapter_version="1.0.0",
            image_digest=IMAGE,
            framework="NEXA_CPU",
            framework_version="1.0.0",
            device="CUDA",
            gpu_count=0,
            resources=ResourceVector(1000, 256 * 1024 * 1024, 0),
        )


def test_input_mount_rejects_relative_source_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        InputMount(
            artifact_id="018f0d60-7b6a-7a27-9d82-1aa39c4f30b7",
            source_path="relative/input.json",
            target_path="/input/passwd",
            content_checksum=CHECKSUM,
            size_bytes=1,
        )
