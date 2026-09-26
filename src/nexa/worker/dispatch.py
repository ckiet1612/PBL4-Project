"""Translate the immutable claimed CPU execution graph into B09 inputs."""

import hashlib

import rfc8785

from nexa.workloads.cpu_state import CpuStateError, decode_state

from .models import (
    RESTORE_STATE_PATH,
    AllocationIdentity,
    Authority,
    CheckpointRestore,
    CpuCheckpointLaunch,
    CpuWorkloadSpec,
    ExecutionContext,
    InputMount,
    ResourceVector,
    StartExecution,
)
from .result_flow import checksum

DEFAULT_CHECKPOINT_INTERVAL_SECONDS = 30
_PROVENANCE_FROM_CONTEXT = (
    "tenant_id",
    "job_id",
    "session_id",
    "input_checksum",
    "spec_checksum",
    "template_id",
    "template_version",
    "adapter_id",
    "adapter_version",
    "image_digest",
)


class RestoreUnavailable(ValueError):
    """A claimed restore cannot run here; the attempt fails INCOMPATIBLE, never from zero."""


def checkpoint_launch(context, *, image_capable):
    """Checkpoint only a checkpointable CPU template on an image whose runner supports it."""
    snapshot = context["template_snapshot"]
    requirement = snapshot.get("capability_requirement") or {}
    supported = (
        snapshot.get("checkpointable") is True
        and image_capable
        and requirement.get("framework") == "NEXA_CPU"
        and requirement.get("device", "CPU") == "CPU"
        and all(
            requirement.get(field) is None
            for field in ("cuda_runtime_min", "driver_min", "compute_capability_min")
        )
    )
    if not supported:
        if context["restore_checkpoint"] is not None:
            raise RestoreUnavailable("claimed restore needs a checkpoint-capable CPU image")
        return None
    interval = context["spec"].get("checkpoint_interval_seconds")
    if not isinstance(interval, int) or isinstance(interval, bool):
        interval = DEFAULT_CHECKPOINT_INTERVAL_SECONDS
    return CpuCheckpointLaunch(
        framework_version=requirement["framework_version"],
        restart_safe=snapshot["restart_safe"],
        interval_seconds=min(60, max(5, interval)),
    )


def verify_restore_manifest(context, architecture, launch):
    """Return (state file view, cursor) after checking the manifest the claim froze."""
    restore = context["restore_checkpoint"]
    try:
        record, manifest, files = restore["record"], restore["manifest"], restore["files"]
        raw = rfc8785.dumps(manifest)
        body = {key: value for key, value in manifest.items() if key != "manifest_checksum"}
        if (
            record["state"] != "COMMITTED"
            or record["job_id"] != context["job_id"]
            or "sha256:" + hashlib.sha256(raw).hexdigest() != record["manifest_checksum"]
            or manifest["manifest_checksum"] != checksum(body)
            or manifest["kind"] != "CHECKPOINT"
            or manifest["schema_version"] != 1
            or manifest["checkpoint_id"] != record["checkpoint_id"]
            or manifest["checkpoint_sequence"] != record["sequence"]
        ):
            raise RestoreUnavailable("restore manifest identity mismatch")
        spec = context["spec"]
        artifact = context["input_artifacts"][0]
        expected = {
            "tenant_id": artifact["tenant_id"],
            "job_id": context["job_id"],
            "session_id": context["logical_session_id"],
            "input_checksum": artifact["checksum"],
            "spec_checksum": checksum(spec),
            "template_id": spec["template_id"],
            "template_version": spec["template_version"],
            "adapter_id": context["adapter_id"],
            "adapter_version": context["adapter_version"],
            "image_digest": context["image_digest"],
        }
        provenance = manifest["provenance"]
        if any(provenance[key] != expected[key] for key in _PROVENANCE_FROM_CONTEXT):
            raise RestoreUnavailable("restore provenance mismatch")
        if manifest["compatibility"] != launch.compatibility(architecture):
            raise RestoreUnavailable("restore compatibility mismatch")
        cursor = manifest["cursor"]
        entries = manifest["files"]
        if (
            len(entries) != 1
            or len(files) != 1
            or entries[0]["logical_name"] != "state.json"
            or entries[0]["artifact_id"] != files[0]["artifact_id"]
            or files[0]["kind"] != "CHECKPOINT_FILE"
            or files[0]["state"] != "COMMITTED"
            or files[0]["tenant_id"] != artifact["tenant_id"]
            or any(entries[0][key] != files[0][key] for key in ("size_bytes", "checksum"))
            or entries[0]["media_type"] != files[0]["media_type"]
        ):
            raise RestoreUnavailable("restore file graph mismatch")
        restore_cursor = CheckpointRestore(
            checkpoint_id=record["checkpoint_id"],
            checkpoint_sequence=record["sequence"],
            step=cursor["step"],
            accumulator=cursor["accumulator"],
            state_checksum=files[0]["checksum"],
        )
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise RestoreUnavailable("restore context is malformed") from exc
    except ValueError as exc:
        if isinstance(exc, RestoreUnavailable):
            raise
        raise RestoreUnavailable("restore cursor is invalid") from exc
    return files[0], restore_cursor


def verify_restore_state(raw, *, context, cursor):
    """The downloaded bytes must be the closed state the manifest cursor names."""
    parameters = context["spec"]["parameters"]
    try:
        state = decode_state(
            raw,
            iterations=parameters["iterations"],
            modulus=parameters["modulus"],
            input_checksum=context["input_artifacts"][0]["checksum"],
            spec_checksum=checksum(context["spec"]),
        )
    except CpuStateError as exc:
        raise RestoreUnavailable("restore state is invalid") from exc
    if (state.step, state.accumulator) != (cursor.step, cursor.accumulator):
        raise RestoreUnavailable("restore state does not match the manifest cursor")


def execution_request(
    context, source, architecture, *, checkpoint=None, restore_file=None, restore_source=None
):
    if context["execution_intent"] != "RUN":
        raise ValueError("CPU worker executes only RUN attempts")
    if (context["restore_checkpoint"] is not None) != (
        checkpoint is not None and checkpoint.restore is not None
    ):
        raise ValueError("claimed restore and launch restore disagree")
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
    mounts = [
        InputMount(
            artifact_id=artifact["artifact_id"],
            source_path=str(source),
            target_path="/input/input.json",
            content_checksum=artifact["checksum"],
            size_bytes=artifact["size_bytes"],
        )
    ]
    if checkpoint is not None and checkpoint.restore is not None:
        mounts.append(
            InputMount(
                artifact_id=restore_file["artifact_id"],
                source_path=str(restore_source),
                target_path=RESTORE_STATE_PATH,
                content_checksum=restore_file["checksum"],
                size_bytes=restore_file["size_bytes"],
            )
        )
    return StartExecution(
        context=execution,
        allocation=AllocationIdentity(authority.allocation_id, authority.attempt_id, resources),
        startup_nonce=context["startup_nonce"],
        operation_sequence=1,
        scratch_bytes=min(64 * 1024**2, resources.memory_bytes // 4),
        log_bytes=1024**2,
        runtime_limit_seconds=spec["runtime_limit_seconds"],
        input_mounts=tuple(mounts),
        cpu_workload=CpuWorkloadSpec(**spec["parameters"], spec_checksum=checksum(spec)),
        checkpoint=checkpoint,
    )
