"""B16 training results bind model + metrics by closed JSON, size and checksum only."""

import json
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

import pytest
import rfc8785

from nexa.application.errors import ApplicationError
from nexa.application.json_codec import decode_json_object
from nexa.application.result_validation import validate_result_manifest
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.workloads import adapter_launch
from nexa.workloads.canonical_json import canonical_json
from tests.workloads import b16_helpers as h
from tests.workloads.test_runner import result_provenance


def _file(name, media, body):
    return {
        "artifact_id": str(new_uuid7()),
        "logical_name": name,
        "media_type": media,
        "size_bytes": len(body),
        "checksum": "sha256:" + sha256(body).hexdigest(),
    }


def _seal(document, *, encode=rfc8785.dumps):
    body = {key: value for key, value in document.items() if key != "manifest_checksum"}
    document["manifest_checksum"] = "sha256:" + sha256(encode(body)).hexdigest()
    raw = encode(document)
    artifact = {
        "kind": "RESULT_MANIFEST",
        "media_type": "application/json",
        "size_bytes": len(raw),
        "checksum": "sha256:" + sha256(raw).hexdigest(),
    }
    return document, raw, artifact


def _case(*, encode=rfc8785.dumps):
    result_id = new_uuid7()
    model = h.model_bytes()
    metrics_raw = canonical_json(h.metrics_document(model))
    metrics = adapter_launch.parse_training_metrics(
        metrics_raw,
        parameters=h.PARAMETERS,
        input_checksum=h.INPUT_CHECKSUM,
        spec_checksum=h.SPEC_CHECKSUM,
        model_checksum="sha256:" + sha256(model).hexdigest(),
    )
    document = {
        "kind": "RESULT",
        "schema_version": 1,
        "result_id": str(result_id),
        "created_at": "2026-09-28T01:02:03.000Z",
        "provenance": dict(h.PROVENANCE),
        "status": "SUCCEEDED",
        "files": [
            _file("model.safetensors", "application/octet-stream", model),
            _file("metrics.json", "application/json", metrics_raw),
        ],
        "metrics": adapter_launch.manifest_metrics(metrics),
    }
    document, raw, artifact = _seal(document, encode=encode)
    return document, raw, artifact, result_id


def _validate(document, raw, artifact, result_id, **overrides):
    options = {
        "raw": raw,
        "artifact": artifact,
        "expected_result_id": UUID(str(result_id)),
        "provenance": dict(h.PROVENANCE),
        "parameters": dict(h.PARAMETERS),
    }
    options.update(overrides)
    return validate_result_manifest(document, **options)


def _rejected(*args, **kwargs):
    with pytest.raises(ApplicationError) as error:
        _validate(*args, **kwargs)
    assert error.value.status == 422 and error.value.code == "validation_failed"


def test_runner_canonical_manifest_binds_model_then_metrics():
    document, raw, artifact, result_id = _case(encode=canonical_json)
    assert raw == rfc8785.dumps(document)
    # The worker forwards json.loads(raw): keys arrive in canonical (sorted) order.
    bindings = _validate(json.loads(raw), raw, artifact, result_id)
    assert bindings == document["files"]


def test_http_decoded_manifest_numbers_are_accepted():
    """The HTTP codec decodes JSON numbers as exact Decimal before the service sees them."""
    document, raw, artifact, result_id = _case(encode=canonical_json)
    wire = decode_json_object(json.dumps({"manifest": document}).encode(), max_bytes=1 << 20)
    assert isinstance(wire["manifest"]["metrics"]["train_loss"], Decimal)
    bindings = _validate(wire["manifest"], raw, artifact, result_id)
    assert bindings == document["files"]


def test_cpu_results_keep_the_single_v1_binding():
    from tests.application.test_result_validation_b11 import _case as cpu_case

    document, raw, artifact, result_id, provenance = cpu_case()
    bindings = validate_result_manifest(
        document,
        raw=raw,
        artifact=artifact,
        expected_result_id=result_id,
        provenance=provenance,
        parameters={"iterations": 10, "seed": 1, "modulus": 97},
    )
    assert bindings == document["files"]
    assert result_provenance()["adapter_id"] == "cpu.iterative"


def _metrics(document):
    return document["metrics"]


def _files(document):
    return document["files"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: _metrics(d).update(extra=1),
        lambda d: _metrics(d).pop("eval_loss"),
        lambda d: _metrics(d).update(model_checksum="sha256:" + "0" * 64),
        lambda d: _metrics(d).update(steps=_metrics(d)["steps"] + 1),
        lambda d: _metrics(d).update(epochs=3),
        lambda d: _metrics(d).update(subset_size=300),
        lambda d: _metrics(d).update(batch_size=32),
        lambda d: _metrics(d).update(eval_items=0),
        lambda d: _metrics(d).update(eval_items=1.0),
        lambda d: _metrics(d).update(eval_accuracy=1.5),
        lambda d: _metrics(d).update(train_loss=-0.5),
        lambda d: _metrics(d).update(train_loss=1),
        lambda d: _metrics(d).update(model_l2_norm=True),
        lambda d: _files(d).reverse(),
        lambda d: _files(d).pop(),
        lambda d: _files(d).append({**_files(d)[1], "logical_name": "extra.json"}),
        lambda d: _files(d)[0].update(logical_name="weights.safetensors"),
        lambda d: _files(d)[0].update(media_type="application/json"),
        lambda d: _files(d)[1].update(media_type="application/octet-stream"),
        lambda d: _files(d)[0].update(size_bytes=0),
        lambda d: _files(d)[0].update(size_bytes=1024 * 1024 + 1),
        lambda d: _files(d)[1].update(size_bytes=adapter_launch.MAX_METRICS_BYTES + 1),
        lambda d: _files(d)[1].update(checksum="sha256:" + "Z" * 64),
        lambda d: _files(d)[1].update(artifact_id="not-a-uuid"),
        lambda d: _files(d)[1].pop("artifact_id"),
        lambda d: d.update(status="FAILED"),
        lambda d: d["provenance"].update(image_digest="sha256:" + "e" * 64),
    ],
)
def test_training_result_rejects_each_deviation(mutate):
    document, _, _, result_id = _case()
    document = deepcopy(document)
    mutate(document)
    document, raw, artifact = _seal(document)
    _rejected(document, raw, artifact, result_id)


def test_training_result_metrics_follow_the_job_parameters():
    document, raw, artifact, result_id = _case()
    _rejected(document, raw, artifact, result_id, parameters={**h.PARAMETERS, "epochs": 3})
    _rejected(document, raw, artifact, result_id, parameters={})


def test_training_result_bytes_must_be_the_canonical_manifest():
    document, raw, artifact, result_id = _case()
    _rejected(document, raw + b" ", artifact, result_id)
    _rejected(document, raw, {**artifact, "checksum": "sha256:" + "0" * 64}, result_id)


def test_unknown_adapter_results_are_not_recognized():
    document, _, _, result_id = _case()
    provenance = {**h.PROVENANCE, "adapter_version": "2.0.0"}
    document = deepcopy(document)
    document["provenance"] = dict(provenance)
    document, raw, artifact = _seal(document)
    _rejected(document, raw, artifact, result_id, provenance=provenance)
