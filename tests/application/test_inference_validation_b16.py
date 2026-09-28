"""B16 batch-inference checkpoints and results: closed JSON, chunk reference and coverage."""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from uuid import UUID

import pytest
import rfc8785

from nexa.application.checkpoint_validation import (
    CheckpointManifestError,
    check_chunk_manifest,
    expected_compatibility,
    validate_checkpoint_manifest,
    validate_inference_state_files,
)
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.workloads import chunk_manifest, inference_state
from nexa.workloads.canonical_json import canonical_json
from tests.worker import b16_claims as c
from tests.workloads import b16_helpers as h

TEMPLATE = {
    "checkpointable": True,
    "restart_safe": True,
    "capability_requirements": dict(c.REQUIREMENT),
}
PARAMETERS = dict(h.INFERENCE_PARAMETERS)
ITEMS = 1000
STATE_NAME = "inference-state.json"


def _sha(raw: bytes) -> str:
    return "sha256:" + sha256(raw).hexdigest()


def _chunks(count: int, *, source=h.INFERENCE_PROVENANCE["attempt_id"], fence=3):
    return [
        chunk_manifest.entry(
            index,
            item_count=ITEMS,
            chunk_size=PARAMETERS["chunk_size"],
            source_attempt_id=source,
            source_job_fence=fence,
            file=h.chunk_file(index, artifact_id=str(new_uuid7()), body=h.chunk_body(index)),
        )
        for index in range(count)
    ]


def _chunk_manifest(count: int, **overrides):
    raw = chunk_manifest.build(
        provenance=overrides.get("provenance", h.INFERENCE_PROVENANCE),
        model_checksum=overrides.get("model_checksum", h.MODEL_CHECKSUM),
        chunks=overrides.get("chunks") or _chunks(count),
    )
    artifact = {
        "artifact_id": str(new_uuid7()),
        "state": "COMMITTED",
        "kind": "CHUNK_OUTPUT_MANIFEST",
        "media_type": "application/json",
        "size_bytes": len(raw),
        "checksum": _sha(raw),
    }
    return raw, artifact


def _state_raw(step: int) -> bytes:
    return canonical_json(h.inference_document(step, item_count=ITEMS))


def _seal(manifest):
    body = {key: value for key, value in manifest.items() if key != "manifest_checksum"}
    manifest["manifest_checksum"] = _sha(rfc8785.dumps(body))
    raw = rfc8785.dumps(manifest)
    artifact = {
        "kind": "CHECKPOINT_MANIFEST",
        "media_type": "application/json",
        "size_bytes": len(raw),
        "checksum": _sha(raw),
    }
    return manifest, raw, artifact


def _checkpoint(step: int = 2):
    state = _state_raw(step)
    _, chunk_artifact = _chunk_manifest(step)
    manifest = {
        "kind": "CHECKPOINT",
        "schema_version": 1,
        "checkpoint_id": str(new_uuid7()),
        "checkpoint_sequence": 1,
        "created_at": "2026-09-28T00:00:00.000Z",
        "provenance": dict(h.INFERENCE_PROVENANCE),
        "compatibility": expected_compatibility(TEMPLATE, c.ARCH),
        "cursor": inference_state.runtime_cursor(h.inference_document(step, item_count=ITEMS)),
        "state_components": ["INFERENCE_CURSOR"],
        "files": [
            {
                "artifact_id": str(new_uuid7()),
                "logical_name": STATE_NAME,
                "media_type": "application/json",
                "size_bytes": len(state),
                "checksum": _sha(state),
            }
        ],
        "chunk_output_manifest": {
            "artifact_id": chunk_artifact["artifact_id"],
            "checksum": chunk_artifact["checksum"],
        },
    }
    return manifest, state


def _validate(manifest, **overrides):
    manifest, raw, artifact = _seal(manifest)
    options = {
        "raw": raw,
        "artifact": artifact,
        "checkpoint_id": UUID(manifest["checkpoint_id"]),
        "sequence": manifest["checkpoint_sequence"],
        "provenance": dict(manifest["provenance"]),
        "compatibility": expected_compatibility(TEMPLATE, c.ARCH),
        "parameters": dict(PARAMETERS),
    }
    options.update(overrides)
    return validate_checkpoint_manifest(manifest, **options)


def _reason(callable_, *args, **kwargs):
    with pytest.raises(CheckpointManifestError) as error:
        callable_(*args, **kwargs)
    assert error.value.status == 422 and error.value.code == "validation_failed"
    return error.value.reason_code


def test_inference_checkpoint_returns_its_state_file():
    manifest, state = _checkpoint(2)
    (entry,) = _validate(manifest)
    assert entry["logical_name"] == STATE_NAME
    assert manifest["cursor"] == {"step": 2, "epoch": 0, "item_cursor": 600}


def test_final_prefix_cursor_counts_the_short_last_chunk():
    manifest, _ = _checkpoint(4)
    assert manifest["cursor"] == {"step": 4, "epoch": 0, "item_cursor": ITEMS}
    _validate(manifest)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda m: m.pop("chunk_output_manifest"), "CHECKPOINT_MANIFEST_INVALID"),
        (lambda m: m.update(chunk_output_manifest=None), "CHECKPOINT_MANIFEST_INVALID"),
        (
            lambda m: m["chunk_output_manifest"].update(extra=1),
            "CHECKPOINT_MANIFEST_INVALID",
        ),
        (
            lambda m: m["chunk_output_manifest"].update(artifact_id="x"),
            "CHECKPOINT_MANIFEST_INVALID",
        ),
        (
            lambda m: m["chunk_output_manifest"].update(checksum="sha256:1"),
            "CHECKPOINT_MANIFEST_INVALID",
        ),
        (lambda m: m["cursor"].update(step=0, item_cursor=0), "CHECKPOINT_CURSOR_INVALID"),
        (lambda m: m["cursor"].update(epoch=1), "CHECKPOINT_CURSOR_INVALID"),
        (lambda m: m["cursor"].update(item_cursor=601), "CHECKPOINT_CURSOR_INVALID"),
        (lambda m: m["cursor"].update(item_cursor=1), "CHECKPOINT_CURSOR_INVALID"),
        (lambda m: m["cursor"].update(accumulator=None), "CHECKPOINT_CURSOR_INVALID"),
        (
            lambda m: m["cursor"].update(step=chunk_manifest.MAX_CHUNKS + 1),
            "CHECKPOINT_CURSOR_INVALID",
        ),
        (lambda m: m.update(state_components=["MODEL"]), "CHECKPOINT_MANIFEST_INVALID"),
        (
            lambda m: m.update(state_components=["INFERENCE_CURSOR", "SAMPLER"]),
            "CHECKPOINT_MANIFEST_INVALID",
        ),
        (lambda m: m["files"][0].update(logical_name="state.json"), "CHECKPOINT_FILES_INVALID"),
        (
            lambda m: m["files"][0].update(media_type="application/octet-stream"),
            "CHECKPOINT_FILES_INVALID",
        ),
        (
            lambda m: m["files"][0].update(size_bytes=inference_state.MAX_DOCUMENT_BYTES + 1),
            "CHECKPOINT_FILES_INVALID",
        ),
        (
            lambda m: m["files"].append(
                {**m["files"][0], "logical_name": "extra.json", "artifact_id": str(new_uuid7())}
            ),
            "CHECKPOINT_FILES_INVALID",
        ),
    ],
)
def test_inference_checkpoint_rejects_each_deviation(mutate, reason):
    manifest, _ = _checkpoint(2)
    manifest = deepcopy(manifest)
    mutate(manifest)
    assert _reason(_validate, manifest) == reason


def test_non_inference_checkpoints_must_omit_the_chunk_reference():
    from tests.application.test_checkpoint_validation_b16 import _case
    from tests.application.test_checkpoint_validation_b16 import _validate as validate_training

    _, manifest, _ = _case(3)
    manifest["chunk_output_manifest"] = {
        "artifact_id": str(new_uuid7()),
        "checksum": "sha256:" + "0" * 64,
    }
    assert _reason(validate_training, manifest) == "CHECKPOINT_MANIFEST_INVALID"


def test_inference_parameters_bound_the_cursor():
    manifest, _ = _checkpoint(2)
    assert _reason(_validate, manifest, parameters={}) == "CHECKPOINT_CURSOR_INVALID"
    assert (
        _reason(_validate, manifest, parameters={**PARAMETERS, "chunk_size": 100})
        == "CHECKPOINT_CURSOR_INVALID"
    )


def test_inference_state_file_matches_the_cursor_and_job():
    manifest, state = _checkpoint(2)
    document = validate_inference_state_files(
        {STATE_NAME: state},
        manifest=manifest,
        parameters=PARAMETERS,
        model_checksum=h.MODEL_CHECKSUM,
    )
    assert document["item_count"] == ITEMS


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(next_chunk=3),
        lambda d: d.update(batch_size=64),
        lambda d: d.update(output_format="PARQUET"),
        lambda d: d.update(model_checksum="sha256:" + "0" * 64),
        lambda d: d.update(input_checksum="sha256:" + "0" * 64),
        lambda d: d.update(spec_checksum="sha256:" + "0" * 64),
        lambda d: d.update(chunk_size=1, item_count=chunk_manifest.MAX_CHUNKS + 1),
    ],
)
def test_inference_state_file_rejects_foreign_state(change):
    manifest, _ = _checkpoint(2)
    document = h.inference_document(2, item_count=ITEMS)
    change(document)
    if document["chunk_size"] == 1:
        manifest["cursor"] = inference_state.runtime_cursor(document)
    counts = [0] * 10
    counts[0] = inference_state.item_cursor(document)
    document["prediction_counts"] = counts
    parameters = {**PARAMETERS, "chunk_size": document["chunk_size"]}
    assert (
        _reason(
            validate_inference_state_files,
            {STATE_NAME: canonical_json(document)},
            manifest=manifest,
            parameters=parameters,
            model_checksum=h.MODEL_CHECKSUM,
        )
        == "CHECKPOINT_STATE_INVALID"
    )


def test_inference_state_file_must_be_present_and_canonical():
    manifest, state = _checkpoint(2)
    for contents in ({}, {STATE_NAME: state + b" "}, {STATE_NAME: b"[]"}):
        assert (
            _reason(
                validate_inference_state_files,
                contents,
                manifest=manifest,
                parameters=PARAMETERS,
                model_checksum=h.MODEL_CHECKSUM,
            )
            == "CHECKPOINT_STATE_INVALID"
        )


def _check(raw, committed, **overrides):
    options = {
        "artifact": committed,
        "reference": {"artifact_id": committed["artifact_id"], "checksum": committed["checksum"]},
        "provenance": dict(h.INFERENCE_PROVENANCE),
        "model_checksum": h.MODEL_CHECKSUM,
        "state": h.inference_document(2, item_count=ITEMS),
        "chunk_total": 2,
    }
    options.update(overrides)
    return check_chunk_manifest(raw, **options)


def test_chunk_manifest_binds_the_exact_committed_artifact():
    raw, artifact = _chunk_manifest(2)
    assert [entry["chunk_id"] for entry in _check(raw, artifact)] == [
        "chunk-00000000",
        "chunk-00000001",
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        lambda a: {"artifact": {**a, "kind": "RESULT_FILE"}},
        lambda a: {"artifact": {**a, "state": "PENDING"}},
        lambda a: {"artifact": {**a, "media_type": "application/x-ndjson"}},
        lambda a: {"artifact": {**a, "size_bytes": a["size_bytes"] + 1}},
        lambda a: {"artifact": {**a, "checksum": "sha256:" + "0" * 64}},
        lambda a: {"reference": {"artifact_id": str(new_uuid7()), "checksum": a["checksum"]}},
        lambda a: {
            "reference": {"artifact_id": a["artifact_id"], "checksum": "sha256:" + "0" * 64}
        },
        lambda a: {"chunk_total": 1},
        lambda a: {"chunk_total": 3},
        lambda a: {"model_checksum": "sha256:" + "0" * 64},
        lambda a: {"provenance": {**h.INFERENCE_PROVENANCE, "job_fence": 4}},
    ],
)
def test_chunk_manifest_rejects_each_mismatch(overrides):
    raw, artifact = _chunk_manifest(2)
    with pytest.raises(chunk_manifest.ChunkManifestError):
        _check(raw, artifact, **overrides(artifact))


def test_chunk_manifest_bytes_are_strict_json():
    raw, artifact = _chunk_manifest(2)
    spaced = json.dumps(json.loads(raw)).encode()
    artifact = {**artifact, "size_bytes": len(spaced), "checksum": _sha(spaced)}
    with pytest.raises(chunk_manifest.ChunkManifestError):
        _check(spaced, artifact)


def _summary_raw() -> bytes:
    final = h.inference_document(4, item_count=ITEMS)
    return canonical_json(inference_state.summary_from_state(final))


def _result(**metric_overrides):
    from nexa.application.result_validation import INFERENCE_METRICS

    summary = _summary_raw()
    _, chunk_artifact = _chunk_manifest(4)
    metrics = {
        "item_count": ITEMS,
        "chunk_count": 4,
        "chunk_size": PARAMETERS["chunk_size"],
        "batch_size": PARAMETERS["batch_size"],
        "output_format": PARAMETERS["output_format"],
        "model_checksum": h.MODEL_CHECKSUM,
    }
    assert set(metrics) == set(INFERENCE_METRICS)
    metrics.update(metric_overrides)
    result_id = new_uuid7()
    document = {
        "kind": "RESULT",
        "schema_version": 1,
        "result_id": str(result_id),
        "created_at": "2026-09-28T01:02:03.000Z",
        "provenance": dict(h.INFERENCE_PROVENANCE),
        "status": "SUCCEEDED",
        "files": [
            {
                "artifact_id": str(new_uuid7()),
                "logical_name": "summary.json",
                "media_type": "application/json",
                "size_bytes": len(summary),
                "checksum": _sha(summary),
            }
        ],
        "metrics": metrics,
        "chunk_output_manifest": {
            "artifact_id": chunk_artifact["artifact_id"],
            "checksum": chunk_artifact["checksum"],
        },
    }
    return document, result_id, summary


def _seal_result(document):
    body = {key: value for key, value in document.items() if key != "manifest_checksum"}
    document["manifest_checksum"] = _sha(rfc8785.dumps(body))
    raw = rfc8785.dumps(document)
    artifact = {
        "kind": "RESULT_MANIFEST",
        "media_type": "application/json",
        "size_bytes": len(raw),
        "checksum": _sha(raw),
    }
    return raw, artifact


def _validate_result(document, result_id, **overrides):
    from nexa.application.result_validation import validate_result_manifest

    raw, artifact = _seal_result(document)
    options = {
        "raw": raw,
        "artifact": artifact,
        "expected_result_id": UUID(str(result_id)),
        "provenance": dict(h.INFERENCE_PROVENANCE),
        "parameters": dict(PARAMETERS),
        "model_checksum": h.MODEL_CHECKSUM,
    }
    options.update(overrides)
    return validate_result_manifest(document, **options)


def _result_rejected(*args, **kwargs):
    from nexa.application.errors import ApplicationError

    with pytest.raises(ApplicationError) as error:
        _validate_result(*args, **kwargs)
    assert error.value.status == 422 and error.value.code == "validation_failed"


def test_inference_result_binds_only_the_summary():
    document, result_id, _ = _result()
    (binding,) = _validate_result(document, result_id)
    assert binding["logical_name"] == "summary.json"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("chunk_output_manifest"),
        lambda d: d["chunk_output_manifest"].update(extra=1),
        lambda d: d["chunk_output_manifest"].update(artifact_id="x"),
        lambda d: d["chunk_output_manifest"].update(checksum="sha256:0"),
        lambda d: d["metrics"].update(extra=1),
        lambda d: d["metrics"].pop("chunk_count"),
        lambda d: d["metrics"].update(chunk_count=3),
        lambda d: d["metrics"].update(item_count=0),
        lambda d: d["metrics"].update(item_count=inference_state.MAX_ITEMS + 1),
        lambda d: d["metrics"].update(item_count=True),
        lambda d: d["metrics"].update(chunk_size=100),
        lambda d: d["metrics"].update(batch_size=64),
        lambda d: d["metrics"].update(output_format="PARQUET"),
        lambda d: d["metrics"].update(model_checksum="sha256:" + "0" * 64),
        lambda d: d["files"][0].update(logical_name="result.json"),
        lambda d: d["files"][0].update(media_type="application/x-ndjson"),
        lambda d: d["files"][0].update(size_bytes=inference_state.MAX_DOCUMENT_BYTES + 1),
        lambda d: d["files"].append({**d["files"][0], "logical_name": "chunk-00000000.jsonl"}),
        lambda d: d["provenance"].update(adapter_id="pytorch.cifar10"),
    ],
)
def test_inference_result_rejects_each_deviation(mutate):
    document, result_id, _ = _result()
    mutate(document)
    _result_rejected(document, result_id)


def test_inference_result_requires_the_job_model_and_parameters():
    document, result_id, _ = _result()
    _result_rejected(document, result_id, model_checksum=None)
    document, result_id, _ = _result()
    _result_rejected(document, result_id, parameters={**PARAMETERS, "batch_size": 64})


def test_other_adapters_reject_a_chunk_reference():
    from tests.application.test_result_validation_b16 import _case, _rejected, _seal

    document, _, _, result_id = _case()
    document = deepcopy(document)
    document["chunk_output_manifest"] = {
        "artifact_id": str(new_uuid7()),
        "checksum": "sha256:" + "0" * 64,
    }
    document, raw, artifact = _seal(document)
    _rejected(document, raw, artifact, result_id)


def test_inference_summary_matches_metrics_and_job():
    from nexa.application.errors import ApplicationError
    from nexa.application.result_validation import validate_inference_summary

    document, _, summary = _result()
    options = {"manifest": document, "parameters": PARAMETERS, "model_checksum": h.MODEL_CHECKSUM}
    assert validate_inference_summary(summary, **options)["item_count"] == ITEMS
    other = json.loads(summary)
    other["batch_size"] = 64
    for raw in (summary + b" ", canonical_json(other)):
        with pytest.raises(ApplicationError):
            validate_inference_summary(raw, **options)
    for change in (
        {"parameters": {**PARAMETERS, "batch_size": 64}},
        {"model_checksum": "sha256:" + "0" * 64},
        {"manifest": {**document, "metrics": {**document["metrics"], "item_count": 999}}},
    ):
        with pytest.raises(ApplicationError):
            validate_inference_summary(summary, **{**options, **change})
