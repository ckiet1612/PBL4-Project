"""Worker half of ``batch.inference`` chunk uploads (B16).

A checkpoint cycle or a result handshake of a chunked adapter runs: chunk file batches,
then the state (or summary) file, then the chunk-output manifest, then the final
manifest. Every chunk binding is journaled before its BIND control is sent, because the
runner unlinks bound chunk files: a replayed batch reuses the journal and never re-reads
the container. Chunk bytes are never logged.
"""

import hashlib

from nexa.workloads import adapter_launch, chunk_manifest, inference_state

from .result_flow import checksum, descriptor_key

_DESCRIPTOR_FIELDS = {
    "staging_name",
    "logical_name",
    "kind",
    "media_type",
    "size_bytes",
    "checksum",
}


def adapter_spec(binding):
    """The v3 launch spec of a chunked adapter attempt's execution binding, or None."""
    launch = binding.get("adapter_launch")
    if launch is None or not adapter_launch.adapter_for(launch["spec"]).chunked:
        return None
    return launch["spec"]


def restored_count(spec):
    """Chunks the restored checkpoint already recognized; this attempt continues after them."""
    restore = spec["restore"]
    return restore["cursor"]["step"] if restore is not None else 0


def _chunks(record):
    return (record.runner_state or {}).get("inference_chunks") or []


def _upload_state(record):
    return (record.runner_state or {}).get("inference_upload") or {}


def model_checksum(record, spec):
    for mount in record.execution_binding["input_mounts"]:
        if mount["target_path"] == spec["inputs"]["model"]:
            return mount["content_checksum"]
    raise ValueError("inference model mount is absent")


def check_document(raw, record, spec, *, summary, error):
    """The job's validated inference state (or summary) bytes; ``error`` otherwise."""
    try:
        document = (
            inference_state.parse_summary(raw) if summary else inference_state.parse_state(raw)
        )
        adapter_launch.check_inference_state(
            document,
            parameters=spec["parameters"],
            threads=spec["threads"],
            input_checksum=spec["provenance"]["input_checksum"],
            spec_checksum=spec["spec_checksum"],
            model_checksum=model_checksum(record, spec),
        )
    except ValueError as exc:
        raise error("inference state is invalid") from exc
    return document


def closed_descriptor(descriptor, *, name, logical_name, kind, media_type, limit, error):
    if (
        not isinstance(descriptor, dict)
        or set(descriptor) != _DESCRIPTOR_FIELDS
        or descriptor["staging_name"] != name
        or descriptor["logical_name"] != logical_name
        or descriptor["kind"] != kind
        or descriptor["media_type"] != media_type
        or not isinstance(descriptor["size_bytes"], int)
        or isinstance(descriptor["size_bytes"], bool)
        or not 0 < descriptor["size_bytes"] <= limit
    ):
        raise error("inference descriptor is not closed")
    return descriptor


def open_upload(journal, attempt_id, reserved_id):
    """Chunk upload bookkeeping of one reservation, opened at its first frame."""
    record = journal.load(attempt_id)
    current = _upload_state(record)
    if current.get("reserved_id") == reserved_id:
        return current
    opened = {
        "reserved_id": reserved_id,
        "start": len(_chunks(record)),
        "next_batch": 0,
        "batch_count": None,
    }
    journal.update_runner_state(attempt_id, lambda local: {**local, "inference_upload": opened})
    return opened


def chunk_batch(
    journal, client, read, attempt_id, envelope, *, spec, reserved_id, callback_id, error
):
    """Upload one CHUNK_FILE_BATCH once; return its journaled bindings."""
    payload = envelope["payload"]
    sequence = envelope["message_sequence"]
    record = journal.load(attempt_id)
    done = ((record.runner_state or {}).get("inference_chunk_batches") or {}).get(str(sequence))
    if done is not None:
        # The runner may already have unlinked these files: replay the journal only.
        return _chunks(record)[done[0] : done[1]]
    upload = open_upload(journal, attempt_id, reserved_id)
    batch_count = upload["batch_count"]
    if payload["batch_index"] != upload["next_batch"] or batch_count not in (
        None,
        payload["batch_count"],
    ):
        raise error("chunk batches are out of order")
    record = journal.load(attempt_id)
    known = _chunks(record)
    first = restored_count(spec) + len(known)
    output_format = spec["parameters"]["output_format"]
    descriptors = [
        closed_descriptor(
            item,
            name=inference_state.chunk_file_name(first + offset, output_format),
            logical_name=inference_state.chunk_file_name(first + offset, output_format),
            kind="RESULT_FILE",
            media_type=chunk_manifest.CHUNK_MEDIA_TYPES[output_format],
            limit=chunk_manifest.MAX_CHUNK_FILE_BYTES,
            error=error,
        )
        for offset, item in enumerate(payload["artifacts"])
    ]
    bindings = []
    for descriptor in descriptors:
        content = read(attempt_id, descriptor)
        if (
            len(content) != descriptor["size_bytes"]
            or "sha256:" + hashlib.sha256(content).hexdigest() != descriptor["checksum"]
        ):
            raise error("closed chunk bytes mismatch")
        # The idempotency key returns the same artifact if a crash repeats this upload.
        key = descriptor_key(attempt_id, callback_id, sequence, descriptor, purpose="CHUNK_OUTPUT")
        try:
            artifact = client.upload_artifact(record.authority, descriptor, key, content)
        except ValueError as exc:
            raise error("chunk upload answer is invalid") from exc
        for field in ("kind", "media_type", "size_bytes", "checksum"):
            if artifact[field] != descriptor[field]:
                raise error("committed chunk binding mismatch")
        bindings.append({**descriptor, "artifact_id": artifact["artifact_id"]})

    def append(local):
        chunks = list(local.get("inference_chunks") or [])
        batches = dict(local.get("inference_chunk_batches") or {})
        batches[str(sequence)] = [len(chunks), len(chunks) + len(bindings)]
        return {
            **local,
            "inference_chunks": chunks + bindings,
            "inference_chunk_batches": batches,
            "inference_upload": {
                **local["inference_upload"],
                "next_batch": payload["batch_index"] + 1,
                "batch_count": payload["batch_count"],
            },
        }

    journal.update_runner_state(attempt_id, append)
    return bindings


def require_all_chunks(record, spec, reserved_id, next_chunk, *, error):
    """Every chunk before ``next_chunk`` is bound once the state or summary file arrives."""
    upload = _upload_state(record)
    if upload.get("reserved_id") != reserved_id or upload["batch_count"] not in (
        None,
        upload["next_batch"],
    ):
        raise error("chunk batches are incomplete")
    if restored_count(spec) + len(_chunks(record)) != next_chunk:
        raise error("inference cursor does not match the bound chunks")


def check_chunk_manifest(raw, record, spec, *, total, item_count, error):
    """The runner's manifest lists the restored prefix, then exactly this attempt's chunks."""
    provenance = spec["provenance"]
    parameters = spec["parameters"]
    try:
        entries = chunk_manifest.parse(
            raw,
            provenance=provenance,
            model_checksum=model_checksum(record, spec),
            item_count=item_count,
            chunk_size=parameters["chunk_size"],
            output_format=parameters["output_format"],
            chunk_total=total,
        )
    except ValueError as exc:
        raise error("chunk-output manifest is invalid") from exc
    # The restored prefix is the runner's verified restore file; the server accepts it
    # only as an exact prior recognition of this job (carry-forward).
    restored = restored_count(spec)
    expected = [
        chunk_manifest.entry(
            restored + offset,
            item_count=item_count,
            chunk_size=parameters["chunk_size"],
            source_attempt_id=provenance["attempt_id"],
            source_job_fence=provenance["job_fence"],
            file=binding,
        )
        for offset, binding in enumerate(_chunks(record))
    ]
    if entries[restored:] != expected:
        raise error("chunk-output manifest does not match the bound chunks")


def binding_set_checksum(record, file_bindings, manifest_binding):
    """Checksum of this reservation's bindings in source message order."""
    start = _upload_state(record)["start"]
    return checksum([*_chunks(record)[start:], *file_bindings, manifest_binding])


def manifest_reference(binding):
    return {"artifact_id": binding["artifact_id"], "checksum": binding["checksum"]}


__all__ = [
    "adapter_spec",
    "binding_set_checksum",
    "check_chunk_manifest",
    "check_document",
    "chunk_batch",
    "closed_descriptor",
    "manifest_reference",
    "model_checksum",
    "open_upload",
    "require_all_chunks",
    "restored_count",
]
