"""Translate the immutable claimed CPU execution graph into B09 inputs."""

from .models import (
    AllocationIdentity,
    Authority,
    CpuWorkloadSpec,
    ExecutionContext,
    InputMount,
    ResourceVector,
    StartExecution,
)
from .result_flow import checksum


def execution_request(context, source, architecture):
    if context["execution_intent"] != "RUN" or context["restore_checkpoint"] is not None:
        raise ValueError("CPU worker does not implement checkpoint restore")
    spec = context["spec"]
    if len(context["input_artifacts"]) != 1:
        raise ValueError("CPU execution graph requires exactly one input")
    artifact = context["input_artifacts"][0]
    if artifact["artifact_id"] != spec["input_artifact_id"]:
        raise ValueError("CPU primary input graph mismatch")
    if architecture not in context["template_snapshot"]["capability_requirement"]["architectures"]:
        raise ValueError("CPU architecture is incompatible")
    authority = Authority(**context["authority"])
    resources = ResourceVector(**context["allocation"]["resources"])
    execution = ExecutionContext(
        authority=authority,
        tenant_id=artifact["tenant_id"],
        job_id=context["job_id"],
        logical_session_id=context["logical_session_id"],
        template_id=spec["template_id"],
        template_version=spec["template_version"],
        image_digest=context["image_digest"],
        architecture=architecture,
        adapter_id=context["adapter_id"],
        adapter_version=context["adapter_version"],
        input_checksum=artifact["checksum"],
        startup_nonce=context["startup_nonce"],
        resources=resources,
    )
    return StartExecution(
        context=execution,
        allocation=AllocationIdentity(authority.allocation_id, authority.attempt_id, resources),
        startup_nonce=context["startup_nonce"],
        operation_sequence=1,
        scratch_bytes=min(64 * 1024**2, resources.memory_bytes // 4),
        log_bytes=1024**2,
        runtime_limit_seconds=spec["runtime_limit_seconds"],
        input_mounts=(
            InputMount(
                artifact_id=artifact["artifact_id"],
                source_path=str(source),
                target_path="/input/input.json",
                content_checksum=artifact["checksum"],
                size_bytes=artifact["size_bytes"],
            ),
        ),
        cpu_workload=CpuWorkloadSpec(**spec["parameters"], spec_checksum=checksum(spec)),
    )
