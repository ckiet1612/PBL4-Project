"""B14 checkpoint manifests are closed, canonical and bound to exact origin."""

import json
import pickle
from copy import deepcopy
from hashlib import sha256

import pytest
import rfc8785
from hypothesis import given
from hypothesis import strategies as st

from nexa.application.checkpoint_validation import (
    CheckpointManifestError,
    check_file_artifacts,
    expected_compatibility,
    parse_checkpoint_manifest,
    validate_checkpoint_manifest,
    validate_cpu_state,
)
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.workloads.test_runner import result_provenance

PARAMETERS = {"iterations": 100, "seed": 3, "modulus": 1_000_003}
TEMPLATE = {
    "checkpointable": True,
    "restart_safe": True,
    "capability_requirements": {
        "architectures": ["linux/amd64", "linux/arm64"],
        "adapter_id": "cpu.iterative",
        "adapter_version": "1.0.0",
        "image_digest": "sha256:" + "d" * 64,
        "device": "CPU",
        "framework": "NEXA_CPU",
        "framework_version": "1.0.0",
        "cuda_runtime_min": None,
        "driver_min": None,
        "compute_capability_min": None,
    },
}


def _state(step=40, accumulator=12345):
    provenance = result_provenance()
    return rfc8785.dumps(
        {
            "schema_version": 1,
            "step": step,
            "accumulator": accumulator,
            "input_checksum": provenance["input_checksum"],
            "spec_checksum": provenance["spec_checksum"],
        }
    )


def _seal(document):
    body = {key: value for key, value in document.items() if key != "manifest_checksum"}
    document["manifest_checksum"] = "sha256:" + sha256(rfc8785.dumps(body)).hexdigest()
    raw = rfc8785.dumps(document)
    artifact = {
        "kind": "CHECKPOINT_MANIFEST",
        "media_type": "application/json",
        "size_bytes": len(raw),
        "checksum": "sha256:" + sha256(raw).hexdigest(),
    }
    return document, raw, artifact


def _case(**cursor):
    checkpoint_id = new_uuid7()
    state = _state()
    document = {
        "kind": "CHECKPOINT",
        "schema_version": 1,
        "checkpoint_id": str(checkpoint_id),
        "checkpoint_sequence": 2,
        "created_at": "2026-09-26T01:02:03.000Z",
        "provenance": result_provenance(),
        "compatibility": expected_compatibility(TEMPLATE, "linux/arm64"),
        "cursor": {"step": 40, "epoch": 0, "item_cursor": 40, "accumulator": 12345, **cursor},
        "state_components": ["ACCUMULATOR"],
        "files": [
            {
                "artifact_id": str(new_uuid7()),
                "logical_name": "state.json",
                "media_type": "application/json",
                "size_bytes": len(state),
                "checksum": "sha256:" + sha256(state).hexdigest(),
            }
        ],
    }
    document, raw, artifact = _seal(document)
    return document, raw, artifact, checkpoint_id


def _validate(document, raw, artifact, checkpoint_id, **overrides):
    options = {
        "raw": raw,
        "artifact": artifact,
        "checkpoint_id": checkpoint_id,
        "sequence": 2,
        "provenance": result_provenance(),
        "compatibility": expected_compatibility(TEMPLATE, "linux/arm64"),
        "parameters": PARAMETERS,
    }
    options.update(overrides)
    return validate_checkpoint_manifest(document, **options)


def _reason(callable_, *args, **kwargs):
    with pytest.raises(CheckpointManifestError) as error:
        callable_(*args, **kwargs)
    assert error.value.status == 422 and error.value.code == "validation_failed"
    return error.value.reason_code


def test_canonical_cpu_manifest_returns_its_single_state_binding():
    document, raw, artifact, checkpoint_id = _case()
    assert _validate(document, raw, artifact, checkpoint_id) == document["files"]


def test_compatibility_maps_the_template_exactly():
    assert expected_compatibility(TEMPLATE, "linux/amd64") == {
        "architecture": "linux/amd64",
        "device_type": "CPU",
        "framework": "PYTHON",
        "framework_version": "1.0.0",
        "cuda_version": None,
        "minimum_driver_version": None,
        "gpu_compute_capability": None,
        "checkpointable": True,
        "restart_safe": True,
    }
    assert (
        _reason(expected_compatibility, TEMPLATE, "linux/s390x")
        == "CHECKPOINT_COMPATIBILITY_MISMATCH"
    )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"kind":"CHECKPOINT","kind":"CHECKPOINT"}',
        b'{"value":1.5}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":9007199254740993}',
        b"\xff",
        b"[]",
        b"{" * 2048,
    ],
)
def test_strict_parser_rejects_ambiguous_json(raw):
    assert _reason(parse_checkpoint_manifest, raw) == "CHECKPOINT_MANIFEST_INVALID"


def test_manifest_size_is_bounded_before_parsing():
    assert (
        _reason(parse_checkpoint_manifest, b" " * (1024 * 1024 + 1))
        == "CHECKPOINT_MANIFEST_INVALID"
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("extra", "CHECKPOINT_MANIFEST_INVALID"),
        ("missing", "CHECKPOINT_MANIFEST_INVALID"),
        ("kind", "CHECKPOINT_MANIFEST_INVALID"),
        ("schema", "CHECKPOINT_MANIFEST_INVALID"),
        ("schema_bool", "CHECKPOINT_MANIFEST_INVALID"),
        ("uuid4", "CHECKPOINT_MANIFEST_INVALID"),
        ("timestamp", "CHECKPOINT_MANIFEST_INVALID"),
        ("chunk", "CHECKPOINT_MANIFEST_INVALID"),
        ("components", "CHECKPOINT_MANIFEST_INVALID"),
        ("duplicate_components", "CHECKPOINT_MANIFEST_INVALID"),
        ("id", "CHECKPOINT_IDENTITY_MISMATCH"),
        ("sequence", "CHECKPOINT_IDENTITY_MISMATCH"),
        ("step", "CHECKPOINT_CURSOR_INVALID"),
        ("accumulator", "CHECKPOINT_CURSOR_INVALID"),
        ("accumulator_string", "CHECKPOINT_CURSOR_INVALID"),
        ("accumulator_negative", "CHECKPOINT_CURSOR_INVALID"),
        ("epoch", "CHECKPOINT_CURSOR_INVALID"),
        ("item_cursor", "CHECKPOINT_CURSOR_INVALID"),
        ("sampler", "CHECKPOINT_CURSOR_INVALID"),
        ("no_files", "CHECKPOINT_FILES_INVALID"),
        ("too_many_files", "CHECKPOINT_FILES_INVALID"),
        ("duplicate_name", "CHECKPOINT_FILES_INVALID"),
        ("duplicate_artifact", "CHECKPOINT_FILES_INVALID"),
        ("path_name", "CHECKPOINT_FILES_INVALID"),
        ("absolute_name", "CHECKPOINT_FILES_INVALID"),
        ("state_media", "CHECKPOINT_FILES_INVALID"),
        ("template", "CHECKPOINT_PROVENANCE_MISMATCH"),
    ],
)
def test_manifest_rejects_each_closed_schema_violation(change, reason):
    document, _, _, checkpoint_id = _case()
    document = deepcopy(document)
    first = document["files"][0]
    if change == "extra":
        document["extra"] = True
    elif change == "missing":
        del document["created_at"]
    elif change == "kind":
        document["kind"] = "RESULT"
    elif change == "schema":
        document["schema_version"] = 2
    elif change == "schema_bool":
        document["schema_version"] = True
    elif change == "uuid4":
        document["checkpoint_id"] = "7f7f7f7f-7f7f-4f7f-8f7f-7f7f7f7f7f7f"
    elif change == "timestamp":
        document["created_at"] = "2026-09-26T01:02:03Z"
    elif change == "chunk":
        document["chunk_output_manifest"] = {"artifact_id": first["artifact_id"]}
    elif change == "components":
        document["state_components"] = ["MODEL"]
    elif change == "duplicate_components":
        document["state_components"] = ["ACCUMULATOR", "ACCUMULATOR"]
    elif change == "id":
        document["checkpoint_id"] = str(new_uuid7())
    elif change == "sequence":
        document["checkpoint_sequence"] = 3
    elif change == "step":
        document["cursor"].update(step=101, item_cursor=101)
    elif change == "accumulator":
        document["cursor"]["accumulator"] = PARAMETERS["modulus"]
    elif change == "accumulator_string":
        document["cursor"]["accumulator"] = "12345"
    elif change == "accumulator_negative":
        document["cursor"]["accumulator"] = -1
    elif change == "epoch":
        document["cursor"]["epoch"] = 1
    elif change == "item_cursor":
        document["cursor"]["item_cursor"] = 39
    elif change == "sampler":
        document["cursor"]["sampler_state_checksum"] = None
    elif change == "no_files":
        document["files"] = []
    elif change == "too_many_files":
        document["files"] = [
            dict(first, artifact_id=str(new_uuid7()), logical_name=f"part-{index}")
            for index in range(65)
        ]
    elif change == "duplicate_name":
        document["files"].append(dict(first, artifact_id=str(new_uuid7())))
    elif change == "duplicate_artifact":
        document["files"].append(dict(first, logical_name="state2.json"))
    elif change == "path_name":
        first["logical_name"] = "../state.json"
    elif change == "absolute_name":
        first["logical_name"] = "/state.json"
    elif change == "state_media":
        first["media_type"] = "application/octet-stream"
    elif change == "template":
        document["provenance"]["template_id"] = "batch-inference"
    document, raw, artifact = _seal(document)
    assert _reason(_validate, document, raw, artifact, checkpoint_id) == reason


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "job_id",
        "session_id",
        "attempt_id",
        "job_fence",
        "input_checksum",
        "spec_checksum",
        "template_id",
        "template_version",
        "adapter_id",
        "adapter_version",
        "image_digest",
    ],
)
def test_each_provenance_field_must_match_the_database(field):
    document, raw, artifact, checkpoint_id = _case()
    expected = result_provenance()
    original = expected[field]
    if isinstance(original, int):
        expected[field] = original + 1
    elif field.endswith("_id") and field not in {"template_id", "adapter_id"}:
        expected[field] = str(new_uuid7())
    elif field.endswith("checksum") or field == "image_digest":
        expected[field] = "sha256:" + "0" * 64
    elif field == "adapter_version":
        expected[field] = "1.0.1"
    else:
        expected[field] = original + "-other"
    assert (
        _reason(_validate, document, raw, artifact, checkpoint_id, provenance=expected)
        == "CHECKPOINT_PROVENANCE_MISMATCH"
    )


@pytest.mark.parametrize(
    ("dimension", "value"),
    [
        ("architecture", "linux/amd64"),
        ("device_type", "CUDA"),
        ("framework", "PYTORCH"),
        ("framework_version", "1.0.1"),
        ("cuda_version", "12.4"),
        ("minimum_driver_version", "550"),
        ("gpu_compute_capability", "8.0"),
        ("checkpointable", False),
        ("restart_safe", False),
    ],
)
def test_each_compatibility_dimension_must_match_exactly(dimension, value):
    document, raw, artifact, checkpoint_id = _case()
    expected = expected_compatibility(TEMPLATE, "linux/arm64")
    expected[dimension] = value
    assert (
        _reason(_validate, document, raw, artifact, checkpoint_id, compatibility=expected)
        == "CHECKPOINT_COMPATIBILITY_MISMATCH"
    )


@pytest.mark.parametrize("change", ["whitespace", "request", "checksum", "artifact", "kind"])
def test_bytes_request_and_artifact_must_agree(change):
    document, raw, artifact, checkpoint_id = _case()
    document = deepcopy(document)
    artifact = dict(artifact)
    reason = "CHECKPOINT_MANIFEST_NOT_CANONICAL"
    if change == "whitespace":
        raw = raw.replace(b",", b", ", 1)
    elif change == "request":
        document["created_at"] = "2026-09-26T01:02:04.000Z"
    elif change == "checksum":
        document["manifest_checksum"] = "sha256:" + "0" * 64
        raw = rfc8785.dumps(document)
        artifact.update(size_bytes=len(raw), checksum="sha256:" + sha256(raw).hexdigest())
        reason = "CHECKPOINT_MANIFEST_CHECKSUM_MISMATCH"
    elif change == "artifact":
        artifact["checksum"] = "sha256:" + "0" * 64
        reason = "CHECKPOINT_MANIFEST_CHECKSUM_MISMATCH"
    elif change == "kind":
        artifact["kind"] = "RESULT_MANIFEST"
        reason = "CHECKPOINT_MANIFEST_CHECKSUM_MISMATCH"
    assert _reason(_validate, document, raw, artifact, checkpoint_id) == reason


def test_file_rows_must_be_exact_committed_same_tenant_checkpoint_files():
    document, *_ = _case()
    entry = document["files"][0]
    tenant = new_uuid7()
    row = {
        "artifact_id": entry["artifact_id"],
        "tenant_id": tenant,
        "state": "COMMITTED",
        "kind": "CHECKPOINT_FILE",
        "media_type": entry["media_type"],
        "size_bytes": entry["size_bytes"],
        "checksum": entry["checksum"],
    }
    assert check_file_artifacts([entry], {entry["artifact_id"]: row}, tenant_id=tenant) == [row]
    for field, value in (
        ("tenant_id", new_uuid7()),
        ("state", "STAGING"),
        ("kind", "RESULT_FILE"),
        ("media_type", "application/octet-stream"),
        ("size_bytes", entry["size_bytes"] + 1),
        ("checksum", "sha256:" + "0" * 64),
    ):
        changed = dict(row, **{field: value})
        assert (
            _reason(
                check_file_artifacts, [entry], {entry["artifact_id"]: changed}, tenant_id=tenant
            )
            == "CHECKPOINT_FILES_INVALID"
        )
    assert (
        _reason(check_file_artifacts, [entry], {}, tenant_id=tenant) == "CHECKPOINT_FILES_INVALID"
    )


def test_cpu_state_is_closed_canonical_and_matches_cursor():
    document, *_ = _case()
    assert validate_cpu_state(_state(), manifest=document)["accumulator"] == 12345
    for raw in (
        _state(step=41),
        _state(accumulator=1),
        _state().replace(b",", b", ", 1),
        rfc8785.dumps({"schema_version": 1}),
        b"x" * 4097,
        pickle.dumps(json.loads(_state())),
    ):
        assert _reason(validate_cpu_state, raw, manifest=document) == "CHECKPOINT_STATE_INVALID"


@given(
    step=st.integers(min_value=0, max_value=PARAMETERS["iterations"]),
    accumulator=st.integers(min_value=0, max_value=PARAMETERS["modulus"] - 1),
)
def test_any_in_bounds_cpu_cursor_is_accepted(step, accumulator):
    document, _, _, checkpoint_id = _case(step=step, item_cursor=step, accumulator=accumulator)
    document, raw, artifact = _seal(document)
    assert _validate(document, raw, artifact, checkpoint_id)[0]["logical_name"] == "state.json"


def test_checkpoint_format_is_owned_by_the_cpu_adapter_not_the_template_name():
    document, raw, artifact, checkpoint_id = _case()
    provenance = dict(result_provenance(), template_id="cpu.iterative.fixture")
    document["provenance"] = provenance
    document, raw, artifact = _seal(document)
    assert _validate(document, raw, artifact, checkpoint_id, provenance=provenance)

    provenance = dict(result_provenance(), adapter_id="pytorch.cifar10")
    document["provenance"] = provenance
    document, raw, artifact = _seal(document)
    assert (
        _reason(_validate, document, raw, artifact, checkpoint_id, provenance=provenance)
        == "CHECKPOINT_TEMPLATE_UNSUPPORTED"
    )
