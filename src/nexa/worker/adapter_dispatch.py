"""Translate a claimed PyTorch adapter execution graph into a v3 launch (B16).

The CPU iterative graph keeps ``dispatch.py``; this module only serves adapters whose
launch spec the trusted runner validates as schema version 3. Nothing here imports an
ML framework: tensors are opaque bytes checked by size, checksum and closed JSON.
"""

import hashlib

import rfc8785

from nexa.config import DEFAULT_RESOURCE_LIMITS, ResourceLimits
from nexa.domain import workload_adapters
from nexa.workloads import adapter_launch, chunk_manifest, inference_state, training_state

from .dispatch import DEFAULT_CHECKPOINT_INTERVAL_SECONDS, RestoreUnavailable
from .docker_config import attempt_bounds
from .models import (
    AdapterLaunch,
    AllocationIdentity,
    Authority,
    ExecutionContext,
    InputMount,
    ResourceVector,
    StartExecution,
)
from .result_flow import checksum

RESTORE_DIRECTORY = "restore"
RECOGNIZED_DIRECTORY = "recognized"
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


def adapter_of(context):
    """The v3 adapter executing this claim, or None for the CPU iterative graph."""
    found = workload_adapters.descriptor(context["adapter_id"], context["adapter_version"])
    if found is None or found is workload_adapters.CPU_ITERATIVE:
        return None
    return found


def _inputs(context, adapter):
    """(dataset view, model view or None) chosen by the spec, never by list order."""
    spec = context["spec"]
    by_id = {view["artifact_id"]: view for view in context["input_artifacts"]}
    wanted = [spec["input_artifact_id"]]
    if adapter.model_target_path is not None:
        wanted.append(spec.get("model_artifact_id"))
    if len(by_id) != len(context["input_artifacts"]) or set(by_id) != set(wanted):
        raise ValueError("adapter input graph mismatch")
    dataset = by_id[spec["input_artifact_id"]]
    if dataset.get("kind") != adapter.input_kind:
        raise ValueError("adapter dataset kind mismatch")
    model = None
    if adapter.model_target_path is not None:
        model = by_id[spec["model_artifact_id"]]
        if model.get("kind") != adapter.model_kind or model["tenant_id"] != dataset["tenant_id"]:
            raise ValueError("adapter model input mismatch")
    return dataset, model


def adapter_checkpoint(context, adapter, *, image_capable, architecture):
    """Return (checkpoint block, interval) or None for a launch without checkpoints."""
    snapshot = context["template_snapshot"]
    requirement = snapshot.get("capability_requirement") or {}
    supported = (
        snapshot.get("checkpointable") is True
        and image_capable
        and requirement.get("framework") == adapter.framework
        and adapter.framework_version_matches(requirement.get("framework_version"))
        and requirement.get("device", "CPU") == "CPU"
        and all(
            requirement.get(field) is None
            for field in ("cuda_runtime_min", "driver_min", "compute_capability_min")
        )
    )
    if not supported:
        if context["restore_checkpoint"] is not None:
            raise RestoreUnavailable("claimed restore needs a checkpoint-capable adapter image")
        return None
    interval = context["spec"].get("checkpoint_interval_seconds")
    if not isinstance(interval, int) or isinstance(interval, bool):
        interval = DEFAULT_CHECKPOINT_INTERVAL_SECONDS
    block = {
        "state_path": adapter_launch.state_path(adapter),
        "compatibility": adapter_launch.compatibility(
            architecture, requirement["framework_version"], snapshot["restart_safe"]
        ),
    }
    return block, min(60, max(5, interval))


def verify_adapter_restore_manifest(context, adapter, checkpoint):
    """Return (restore block, ordered file views) of the manifest the claim froze."""
    restore = context["restore_checkpoint"]
    try:
        record, manifest, files = restore["record"], restore["manifest"], restore["files"]
        raw = rfc8785.dumps(manifest)
        body = {key: value for key, value in manifest.items() if key != "manifest_checksum"}
        inherited = record["job_id"] != context["job_id"]
        if (
            record["state"] != "COMMITTED"
            or "sha256:" + hashlib.sha256(raw).hexdigest() != record["manifest_checksum"]
            or manifest["manifest_checksum"] != checksum(body)
            or manifest["kind"] != "CHECKPOINT"
            or manifest["schema_version"] != 1
            or manifest["checkpoint_id"] != record["checkpoint_id"]
            or manifest["checkpoint_sequence"] != record["sequence"]
            or ("chunk_output_manifest" in manifest) != adapter.chunked
            # B16-R20: chunk carry-forward is job-scoped; the server never offers this.
            or (adapter.chunked and inherited)
        ):
            raise RestoreUnavailable("restore manifest identity mismatch")
        spec = context["spec"]
        dataset, _ = _inputs(context, adapter)
        expected = {
            "tenant_id": dataset["tenant_id"],
            "job_id": record["job_id"] if inherited else context["job_id"],
            "session_id": context["logical_session_id"],
            "input_checksum": dataset["checksum"],
            "spec_checksum": checksum(spec),
            "template_id": spec["template_id"],
            "template_version": spec["template_version"],
            "adapter_id": context["adapter_id"],
            "adapter_version": context["adapter_version"],
            "image_digest": context["image_digest"],
        }
        provenance = manifest["provenance"]
        checked = [
            key for key in _PROVENANCE_FROM_CONTEXT if not (inherited and key == "session_id")
        ]
        if any(provenance[key] != expected[key] for key in checked):
            raise RestoreUnavailable("restore provenance mismatch")
        if manifest["compatibility"] != checkpoint["compatibility"]:
            raise RestoreUnavailable("restore compatibility mismatch")
        if manifest["state_components"] != list(adapter.state_components):
            raise RestoreUnavailable("restore state components mismatch")
        if adapter.chunked:
            cursor = adapter_launch.validate_inference_cursor(manifest["cursor"])
        else:
            cursor = adapter_launch.validate_training_cursor(manifest["cursor"], spec["parameters"])
        entries = manifest["files"]
        views = {view["artifact_id"]: view for view in files}
        rules = adapter.checkpoint_files
        # A chunked checkpoint also restores its chunk-output manifest (B16-R18).
        extra = 1 if adapter.chunked else 0
        if (
            len(entries) != len(rules)
            or len(views) != len(files)
            or len(files) != len(rules) + extra
        ):
            raise RestoreUnavailable("restore file graph mismatch")
        ordered = []
        for entry, rule in zip(entries, rules, strict=True):
            view = views.get(entry["artifact_id"])
            if (
                view is None
                or entry["logical_name"] != rule.logical_name
                or entry["media_type"] != rule.media_type
                or view["kind"] != "CHECKPOINT_FILE"
                or view["state"] != "COMMITTED"
                or view["tenant_id"] != dataset["tenant_id"]
                or any(entry[key] != view[key] for key in ("size_bytes", "checksum", "media_type"))
            ):
                raise RestoreUnavailable("restore file graph mismatch")
            ordered.append((rule.logical_name, view))
        if adapter.chunked:
            reference = manifest["chunk_output_manifest"]
            view = views.get(reference["artifact_id"])
            if (
                view is None
                or view["kind"] != "CHUNK_OUTPUT_MANIFEST"
                or view["media_type"] != chunk_manifest.MEDIA_TYPE
                or view["state"] != "COMMITTED"
                or view["tenant_id"] != dataset["tenant_id"]
                or view["checksum"] != reference["checksum"]
                or not 0 < view["size_bytes"] <= chunk_manifest.MAX_MANIFEST_BYTES
            ):
                raise RestoreUnavailable("restore chunk-output manifest mismatch")
            ordered.append((chunk_manifest.LOGICAL_NAME, view))
        block = {
            "checkpoint_id": record["checkpoint_id"],
            "checkpoint_sequence": record["sequence"],
            "cursor": cursor,
            "files": [
                {
                    "logical_name": name,
                    "path": adapter_launch.restore_path(name),
                    "checksum": view["checksum"],
                    "size_bytes": view["size_bytes"],
                }
                for name, view in ordered
            ],
        }
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise RestoreUnavailable("restore context is malformed") from exc
    except ValueError as exc:
        if isinstance(exc, RestoreUnavailable):
            raise
        raise RestoreUnavailable("restore cursor is invalid") from exc
    return block, ordered


def verify_adapter_restore_files(contents, *, context, restore, threads):
    """The downloaded bytes must be this job's state at the manifest cursor."""
    spec = context["spec"]
    adapter = adapter_of(context)
    dataset, model = _inputs(context, adapter)
    if adapter.chunked:
        _verify_inference_restore(contents, context, restore, threads, dataset, model)
        return
    try:
        document = training_state.validate_checkpoint_files(contents)
        adapter_launch.check_training_state(
            document,
            parameters=spec["parameters"],
            threads=threads,
            input_checksum=dataset["checksum"],
            spec_checksum=checksum(spec),
        )
    except (training_state.TrainingStateError, adapter_launch.LaunchSpecError) as exc:
        raise RestoreUnavailable("restore state is invalid") from exc
    if training_state.runtime_cursor(document) != restore["cursor"]:
        raise RestoreUnavailable("restore state does not match the manifest cursor")


def _verify_inference_restore(contents, context, restore, threads, dataset, model):
    spec = context["spec"]
    parameters = spec["parameters"]
    try:
        document = inference_state.parse_state(contents["inference-state.json"])
        adapter_launch.check_inference_state(
            document,
            parameters=parameters,
            threads=threads,
            input_checksum=dataset["checksum"],
            spec_checksum=checksum(spec),
            model_checksum=model["checksum"],
        )
        if inference_state.runtime_cursor(document) != restore["cursor"]:
            raise RestoreUnavailable("restore state does not match the manifest cursor")
        # The chunk manifest carries the provenance of the attempt that wrote it.
        chunk_manifest.parse(
            contents[chunk_manifest.LOGICAL_NAME],
            provenance=context["restore_checkpoint"]["manifest"]["provenance"],
            model_checksum=model["checksum"],
            item_count=document["item_count"],
            chunk_size=parameters["chunk_size"],
            output_format=parameters["output_format"],
            chunk_total=restore["cursor"]["step"],
        )
    except (KeyError, TypeError) as exc:
        raise RestoreUnavailable("restore state is invalid") from exc
    except ValueError as exc:
        if isinstance(exc, RestoreUnavailable):
            raise
        raise RestoreUnavailable("restore state is invalid") from exc


class ChunkOutputUnavailable(ValueError):
    """A recognized chunk can be neither carried forward nor recomputed (B16-R21)."""


def adapter_recognized(context, adapter, restore):
    """The claim's recognized chunks from the restored cursor, checked before Docker work.

    Absence (a non-chunked adapter, or a claim acknowledged before B16-R21) carries
    nothing. ``null`` means the server could not read a recognized chunk: this Attempt
    may not recompute it under its own source, so it fails before any container.
    """
    if not adapter.chunked or adapter_launch.RECOGNIZED_KEY not in context:
        return []
    value = context[adapter_launch.RECOGNIZED_KEY]
    if value is None:
        raise ChunkOutputUnavailable("recognized chunk output is unavailable")
    try:
        return chunk_manifest.validate_recognized(
            value,
            first=restore["cursor"]["step"] if restore is not None else 0,
            chunk_size=context["spec"]["parameters"]["chunk_size"],
        )
    except chunk_manifest.ChunkManifestError as exc:
        raise ChunkOutputUnavailable("recognized chunks are invalid") from exc


def recognized_downloads(context, directory, recognized, restore):
    """(view, private path) of each recognized chunk file the workload carries (B16-R21)."""
    if not recognized:
        return []
    output_format = context["spec"]["parameters"]["output_format"]
    media_type, _ = workload_adapters.CHUNK_MEDIA_TYPES[output_format]
    first = restore["cursor"]["step"] if restore is not None else 0
    return [
        (
            {
                "artifact_id": item["artifact_id"],
                "kind": "RESULT_FILE",
                "media_type": media_type,
                "size_bytes": item["size_bytes"],
                "checksum": item["checksum"],
            },
            directory
            / RECOGNIZED_DIRECTORY
            / inference_state.chunk_file_name(first + offset, output_format),
        )
        for offset, item in enumerate(recognized)
    ]


def adapter_downloads(context, directory, *, restore_files=()):
    """(view, private path) pairs the worker downloads before prepare."""
    adapter = adapter_of(context)
    dataset, model = _inputs(context, adapter)
    pairs = [(dataset, directory / "dataset")]
    if model is not None:
        pairs.append((model, directory / "model"))
    pairs.extend((view, directory / RESTORE_DIRECTORY / name) for name, view in restore_files)
    return pairs


def adapter_execution_request(
    context,
    directory,
    architecture,
    *,
    checkpoint=None,
    restore=None,
    restore_files=(),
    recognized=(),
    limits: ResourceLimits = DEFAULT_RESOURCE_LIMITS,
):
    if context["execution_intent"] not in {"RUN", "CHECKPOINT_FOR_PAUSE"}:
        raise ValueError("worker executes only RUN or CHECKPOINT_FOR_PAUSE attempts")
    if (context["restore_checkpoint"] is not None) != (restore is not None):
        raise ValueError("claimed restore and launch restore disagree")
    adapter = adapter_of(context)
    if adapter is None:
        raise ValueError("claim is not a v3 adapter graph")
    spec = context["spec"]
    requirement = context["template_snapshot"]["capability_requirement"]
    if architecture not in requirement["architectures"]:
        raise ValueError("adapter architecture is incompatible")
    dataset, model = _inputs(context, adapter)
    authority = Authority(**context["authority"])
    resources = ResourceVector(**context["allocation"]["resources"])
    execution = ExecutionContext(
        authority=authority,
        tenant_id=dataset["tenant_id"],
        job_id=context["job_id"],
        logical_session_id=context["logical_session_id"],
        template_id=spec["template_id"],
        template_version=spec["template_version"],
        image_digest=context["image_digest"],
        architecture=architecture,
        adapter_id=context["adapter_id"],
        adapter_version=context["adapter_version"],
        input_checksum=dataset["checksum"],
        startup_nonce=context["startup_nonce"],
        resources=resources,
        framework=adapter.framework,
        framework_version=requirement["framework_version"],
    )
    block, interval = checkpoint if checkpoint is not None else (None, None)
    spec_checksum = checksum(spec)
    launch_spec = {
        "schema_version": adapter_launch.SCHEMA_VERSION,
        "adapter_id": adapter.adapter_id,
        "adapter_version": adapter.adapter_version,
        "startup_nonce": context["startup_nonce"],
        "parameters": spec["parameters"],
        "threads": adapter_launch.threads_for(resources.cpu_millis),
        "spec_checksum": spec_checksum,
        "inputs": adapter_launch.input_paths(adapter),
        "output_dir": adapter_launch.OUTPUT_DIR,
        "provenance": {
            "tenant_id": dataset["tenant_id"],
            "job_id": context["job_id"],
            "session_id": context["logical_session_id"],
            "attempt_id": authority.attempt_id,
            "job_fence": authority.job_fence,
            "input_checksum": dataset["checksum"],
            "spec_checksum": spec_checksum,
            "template_id": spec["template_id"],
            "template_version": spec["template_version"],
            "adapter_id": context["adapter_id"],
            "adapter_version": context["adapter_version"],
            "image_digest": context["image_digest"],
        },
        "checkpoint": block,
        "restore": restore,
    }
    if recognized:
        # Only an Attempt with recognized chunks to carry forward names them (B16-R21).
        launch_spec[adapter_launch.RECOGNIZED_KEY] = list(recognized)
    sources = {id(view): path for view, path in adapter_downloads(context, directory)}
    mounts = [
        InputMount(
            artifact_id=dataset["artifact_id"],
            source_path=str(sources[id(dataset)]),
            target_path=adapter.input_target_path,
            content_checksum=dataset["checksum"],
            size_bytes=dataset["size_bytes"],
        )
    ]
    if model is not None:
        mounts.append(
            InputMount(
                artifact_id=model["artifact_id"],
                source_path=str(sources[id(model)]),
                target_path=adapter.model_target_path,
                content_checksum=model["checksum"],
                size_bytes=model["size_bytes"],
            )
        )
    for name, view in restore_files:
        mounts.append(
            InputMount(
                artifact_id=view["artifact_id"],
                source_path=str(directory / RESTORE_DIRECTORY / name),
                target_path=adapter_launch.restore_path(name),
                content_checksum=view["checksum"],
                size_bytes=view["size_bytes"],
            )
        )
    downloads = recognized_downloads(context, directory, recognized, restore)
    for (view, source), target in zip(
        downloads, adapter_launch.recognized_paths(launch_spec), strict=True
    ):
        # Read-only recognized chunk files the workload carries forward (B16-R21).
        mounts.append(
            InputMount(
                artifact_id=view["artifact_id"],
                source_path=str(source),
                target_path=target,
                content_checksum=view["checksum"],
                size_bytes=view["size_bytes"],
            )
        )
    scratch_bytes, log_bytes = attempt_bounds(resources.memory_bytes, limits)
    return StartExecution(
        context=execution,
        allocation=AllocationIdentity(authority.allocation_id, authority.attempt_id, resources),
        startup_nonce=context["startup_nonce"],
        operation_sequence=1,
        scratch_bytes=scratch_bytes,
        log_bytes=log_bytes,
        runtime_limit_seconds=spec["runtime_limit_seconds"],
        input_mounts=tuple(mounts),
        adapter_launch=AdapterLaunch(spec=launch_spec, interval_seconds=interval),
    )


__all__ = [
    "ChunkOutputUnavailable",
    "adapter_checkpoint",
    "adapter_downloads",
    "adapter_execution_request",
    "adapter_of",
    "adapter_recognized",
    "recognized_downloads",
    "verify_adapter_restore_files",
    "verify_adapter_restore_manifest",
]
