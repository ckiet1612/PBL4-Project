"""B16 training checkpoints: the server checks closed JSON, sizes and checksums, never tensors."""

import io
import json
import pickle
from copy import deepcopy
from hashlib import sha256
from uuid import UUID

import pytest
import rfc8785

from nexa.application.checkpoint_restore import verify_candidate
from nexa.application.checkpoint_validation import (
    CheckpointManifestError,
    expected_compatibility,
    validate_checkpoint_manifest,
    validate_training_state_files,
)
from nexa.infrastructure.artifacts.store import ArtifactError
from nexa.workloads import adapter_launch, training_state
from tests.worker import b16_claims as c
from tests.workloads import b16_helpers as h

TEMPLATE = {
    "checkpointable": True,
    "restart_safe": True,
    "capability_requirements": dict(c.REQUIREMENT),
}


def _reseal(manifest):
    body = {key: value for key, value in manifest.items() if key != "manifest_checksum"}
    manifest["manifest_checksum"] = "sha256:" + sha256(rfc8785.dumps(body)).hexdigest()
    raw = rfc8785.dumps(manifest)
    artifact = {
        "kind": "CHECKPOINT_MANIFEST",
        "media_type": "application/json",
        "size_bytes": len(raw),
        "checksum": "sha256:" + sha256(raw).hexdigest(),
    }
    return manifest, raw, artifact


def _case(step=3):
    context = c.training_claim()
    sealed, files = c.sealed_training_restore(context, step=step)
    manifest = deepcopy(sealed["manifest"])
    return context, manifest, files


def _validate(manifest, **overrides):
    manifest, raw, artifact = _reseal(manifest)
    options = {
        "raw": raw,
        "artifact": artifact,
        "checkpoint_id": UUID(manifest["checkpoint_id"]),
        "sequence": manifest["checkpoint_sequence"],
        "provenance": dict(manifest["provenance"]),
        "compatibility": expected_compatibility(TEMPLATE, c.ARCH),
        "parameters": dict(h.PARAMETERS),
    }
    options.update(overrides)
    return validate_checkpoint_manifest(manifest, **options)


def _reason(callable_, *args, **kwargs):
    with pytest.raises(CheckpointManifestError) as error:
        callable_(*args, **kwargs)
    assert error.value.status == 422 and error.value.code == "validation_failed"
    return error.value.reason_code


def test_template_compatibility_equals_the_worker_launch_compatibility():
    assert expected_compatibility(TEMPLATE, c.ARCH) == adapter_launch.compatibility(
        c.ARCH, c.FRAMEWORK_VERSION, True
    )


@pytest.mark.parametrize("step", [0, 3, 4, 7])
def test_training_manifest_returns_the_four_files_in_rule_order(step):
    _, manifest, _ = _case(step)
    entries = _validate(manifest)
    assert [e["logical_name"] for e in entries] == [
        "model.safetensors",
        "optimizer.safetensors",
        "rng.safetensors",
        "training-state.json",
    ]


def _files(manifest):
    return manifest["files"]


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda m: m["cursor"].update(accumulator=1), "CHECKPOINT_CURSOR_INVALID"),
        (lambda m: m["cursor"].pop("sampler_state_checksum"), "CHECKPOINT_CURSOR_INVALID"),
        (lambda m: m["cursor"].update(sampler_state_checksum="x"), "CHECKPOINT_CURSOR_INVALID"),
        (lambda m: m["cursor"].update(step=10_000), "CHECKPOINT_CURSOR_INVALID"),
        (lambda m: m["cursor"].update(item_cursor=1), "CHECKPOINT_CURSOR_INVALID"),
        (lambda m: m["cursor"].update(epoch=2), "CHECKPOINT_CURSOR_INVALID"),
        (lambda m: m.update(state_components=["ACCUMULATOR"]), "CHECKPOINT_MANIFEST_INVALID"),
        (lambda m: m["state_components"].reverse(), "CHECKPOINT_MANIFEST_INVALID"),
        (lambda m: m["state_components"].pop(), "CHECKPOINT_MANIFEST_INVALID"),
        (
            lambda m: m["state_components"].append("RNG_TORCH_CUDA"),
            "CHECKPOINT_MANIFEST_INVALID",
        ),
        (lambda m: _files(m).reverse(), "CHECKPOINT_FILES_INVALID"),
        (lambda m: _files(m).pop(), "CHECKPOINT_FILES_INVALID"),
        (
            lambda m: _files(m).append({**_files(m)[0], "logical_name": "extra.safetensors"}),
            "CHECKPOINT_FILES_INVALID",
        ),
        (
            lambda m: _files(m)[0].update(logical_name="weights.safetensors"),
            "CHECKPOINT_FILES_INVALID",
        ),
        (
            lambda m: _files(m)[3].update(media_type="application/octet-stream"),
            "CHECKPOINT_FILES_INVALID",
        ),
        (lambda m: _files(m)[1].update(media_type="application/json"), "CHECKPOINT_FILES_INVALID"),
        (lambda m: _files(m)[0].update(size_bytes=0), "CHECKPOINT_FILES_INVALID"),
        (lambda m: _files(m)[0].update(size_bytes=1024 * 1024 + 1), "CHECKPOINT_FILES_INVALID"),
        (
            lambda m: _files(m)[3].update(size_bytes=training_state.MAX_STATE_BYTES + 1),
            "CHECKPOINT_FILES_INVALID",
        ),
    ],
)
def test_training_manifest_rejects_each_adapter_deviation(mutate, reason):
    _, manifest, _ = _case()
    mutate(manifest)
    assert _reason(_validate, manifest) == reason


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {**h.PARAMETERS, "learning_rate": 0},
        {key: value for key, value in h.PARAMETERS.items() if key != "seed"},
        {**h.PARAMETERS, "iterations": 5},
    ],
)
def test_cursor_bounds_come_from_valid_job_parameters(parameters):
    _, manifest, _ = _case()
    assert _reason(_validate, manifest, parameters=parameters) == "CHECKPOINT_CURSOR_INVALID"


def test_cursor_bounds_follow_the_parameters_of_the_job():
    _, manifest, _ = _case(step=7)
    shorter = {**h.PARAMETERS, "epochs": 1}
    assert _reason(_validate, manifest, parameters=shorter) == "CHECKPOINT_CURSOR_INVALID"


def test_unknown_adapter_version_is_unsupported_not_corrupt():
    _, manifest, _ = _case()
    manifest["provenance"]["adapter_version"] = "2.0.0"
    assert _reason(_validate, manifest) == "CHECKPOINT_TEMPLATE_UNSUPPORTED"


def test_adapter_framework_must_own_the_checkpoint_format():
    _, manifest, _ = _case()
    cpu = {
        **TEMPLATE,
        "capability_requirements": {
            **c.REQUIREMENT,
            "framework": "NEXA_CPU",
            "framework_version": "1.0.0",
        },
    }
    manifest["compatibility"] = expected_compatibility(cpu, c.ARCH)
    reason = _reason(_validate, manifest, compatibility=expected_compatibility(cpu, c.ARCH))
    assert reason == "CHECKPOINT_TEMPLATE_UNSUPPORTED"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("architecture", "linux/arm64"),
        ("framework_version", "2.12.0"),
        ("device_type", "CUDA"),
        ("restart_safe", False),
    ],
)
def test_training_compatibility_is_exact(field, value):
    _, manifest, _ = _case()
    manifest["compatibility"][field] = value
    assert _reason(_validate, manifest) == "CHECKPOINT_COMPATIBILITY_MISMATCH"


def test_image_digest_is_part_of_exact_provenance():
    _, manifest, _ = _case()
    provenance = {**manifest["provenance"], "image_digest": "sha256:" + "e" * 64}
    reason = _reason(_validate, manifest, provenance=provenance)
    assert reason == "CHECKPOINT_PROVENANCE_MISMATCH"


def test_training_state_file_matches_the_manifest_cursor_and_job():
    _, manifest, files = _case()
    document = validate_training_state_files(files, manifest=manifest, parameters=h.PARAMETERS)
    assert training_state.runtime_cursor(document) == manifest["cursor"]


def test_server_never_interprets_tensor_bytes():
    _, manifest, files = _case()
    files = {**files, "model.safetensors": b"not-a-safetensors-file"}
    assert validate_training_state_files(files, manifest=manifest, parameters=h.PARAMETERS)


def _other_state(step):
    context = c.training_claim()
    return c.job_checkpoint_files(context, step)["training-state.json"]


@pytest.mark.parametrize(
    "change",
    [
        lambda f, _m: f.update({"training-state.json": _other_state(4)}),
        lambda f, _m: f.update(
            {
                "training-state.json": json.dumps(
                    json.loads(f["training-state.json"]), indent=1
                ).encode()
            }
        ),
        lambda f, _m: f.update(
            {"training-state.json": pickle.dumps(json.loads(f["training-state.json"]))}
        ),
        lambda f, _m: f.update(
            {"training-state.json": b" " * (training_state.MAX_STATE_BYTES + 1)}
        ),
        lambda f, _m: f.pop("training-state.json"),
        lambda f, m: m["provenance"].update(input_checksum="sha256:" + "0" * 64),
        lambda f, m: m["provenance"].update(spec_checksum="sha256:" + "0" * 64),
        lambda f, m: m["cursor"].update(sampler_state_checksum="sha256:" + "0" * 64),
    ],
)
def test_training_state_file_rejects_foreign_or_malformed_state(change):
    _, manifest, files = _case()
    files = dict(files)
    change(files, manifest)
    reason = _reason(
        validate_training_state_files, files, manifest=manifest, parameters=h.PARAMETERS
    )
    assert reason == "CHECKPOINT_STATE_INVALID"


def test_training_state_file_rejects_other_parameters():
    _, manifest, files = _case()
    parameters = {**h.PARAMETERS, "learning_rate": 0.5}
    reason = _reason(validate_training_state_files, files, manifest=manifest, parameters=parameters)
    assert reason == "CHECKPOINT_STATE_INVALID"


class _Store:
    def __init__(self, blobs):
        self.blobs = blobs
        self.reads = []

    def open(self, key):
        self.reads.append(key)
        if key not in self.blobs:
            raise ArtifactError("not_found", "missing")
        return io.BytesIO(self.blobs[key])


def _candidate():
    context = c.training_claim()
    sealed, files = c.sealed_training_restore(context)
    raw = rfc8785.dumps(sealed["manifest"])
    record = sealed["record"]
    blobs = {"manifest": raw}
    rows = {}
    for view in sealed["files"]:
        name = next(
            e["logical_name"]
            for e in sealed["manifest"]["files"]
            if e["artifact_id"] == view["artifact_id"]
        )
        blobs[name] = files[name]
        rows[view["artifact_id"]] = {**view, "blob_key": name}
    candidate = {
        "row": {
            "checkpoint_id": UUID(record["checkpoint_id"]),
            "sequence": record["sequence"],
            "manifest_checksum": record["manifest_checksum"],
        },
        "manifest": {
            "state": "COMMITTED",
            "kind": "CHECKPOINT_MANIFEST",
            "media_type": "application/json",
            "size_bytes": len(raw),
            "checksum": record["manifest_checksum"],
            "blob_key": "manifest",
        },
        "references": sorted(
            (e["artifact_id"], e["logical_name"]) for e in sealed["manifest"]["files"]
        ),
        "files": rows,
        "provenance": dict(sealed["manifest"]["provenance"]),
    }
    plan = {
        "parameters": dict(h.PARAMETERS),
        "tenant_id": context["input_artifacts"][0]["tenant_id"],
    }
    return _Store(blobs), plan, candidate


def test_restore_selection_verifies_every_training_blob():
    store, plan, candidate = _candidate()
    outcome, (manifest, files) = verify_candidate(
        store, plan, candidate, expected_compatibility(TEMPLATE, c.ARCH)
    )
    assert outcome == "VALID"
    assert [row["blob_key"] for row in files] == [
        "model.safetensors",
        "optimizer.safetensors",
        "rng.safetensors",
        "training-state.json",
    ]
    assert store.reads == ["manifest", *[row["blob_key"] for row in files]]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda b: b.pop("optimizer.safetensors"), "CHECKPOINT_BLOB_MISSING"),
        (
            lambda b: b.update({"rng.safetensors": b"x" + b["rng.safetensors"][1:]}),
            "CHECKPOINT_CHECKSUM_MISMATCH",
        ),
        (
            lambda b: b.update({"model.safetensors": b["model.safetensors"] + b"\0"}),
            "CHECKPOINT_CHECKSUM_MISMATCH",
        ),
    ],
)
def test_restore_selection_marks_missing_or_changed_blobs_corrupt(change, reason):
    store, plan, candidate = _candidate()
    change(store.blobs)
    outcome, value = verify_candidate(
        store, plan, candidate, expected_compatibility(TEMPLATE, c.ARCH)
    )
    assert (outcome, value) == ("CORRUPT", reason)


def test_restore_selection_rejects_state_of_other_parameters_as_corrupt():
    store, plan, candidate = _candidate()
    plan["parameters"] = {**h.PARAMETERS, "learning_rate": 0.5}
    outcome, value = verify_candidate(
        store, plan, candidate, expected_compatibility(TEMPLATE, c.ARCH)
    )
    assert (outcome, value) == ("CORRUPT", "CHECKPOINT_STATE_INVALID")


def test_restore_selection_treats_other_architecture_as_incompatible():
    store, plan, candidate = _candidate()
    arm = expected_compatibility(
        {
            **TEMPLATE,
            "capability_requirements": {
                **c.REQUIREMENT,
                "architectures": ["linux/amd64", "linux/arm64"],
            },
        },
        "linux/arm64",
    )
    outcome, value = verify_candidate(store, plan, candidate, arm)
    assert (outcome, value) == ("INCOMPATIBLE", "CHECKPOINT_COMPATIBILITY_MISMATCH")
    assert store.reads == ["manifest"]
