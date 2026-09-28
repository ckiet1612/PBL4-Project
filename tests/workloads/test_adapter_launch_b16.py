"""B16 launch spec v3 of the PyTorch adapters (stdlib, no torch)."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from nexa.workloads import adapter_launch, training_state
from nexa.workloads.canonical_json import canonical_json
from tests.workloads import b16_helpers as h


def test_training_spec_validates_and_builds_the_fixed_argv() -> None:
    spec = adapter_launch.validate_launch_spec(h.training_spec())
    assert adapter_launch.workload_command(spec) == (
        "python",
        "-m",
        "nexa.workloads.pytorch_cifar10",
        "--dataset",
        "/input/dataset.arrow",
        "--output-dir",
        "/output",
        "--epochs",
        "2",
        "--batch-size",
        "64",
        "--learning-rate",
        "0.05",
        "--seed",
        "7",
        "--subset-size",
        "200",
        "--threads",
        "1",
        "--spec-checksum",
        h.SPEC_CHECKSUM,
        "--state-output",
        "/output/state.safetensors",
    )


def test_restore_adds_the_fixed_resume_directory() -> None:
    spec = adapter_launch.validate_launch_spec(h.training_spec(restore=h.restore_block(5)))
    assert adapter_launch.workload_command(spec)[-2:] == ("--resume-dir", "/input/restore")


def test_learning_rate_repr_round_trips_the_binary64() -> None:
    value = 0.1 + 0.2
    spec = h.training_spec(parameters={**h.PARAMETERS, "learning_rate": value})
    argv = adapter_launch.workload_command(adapter_launch.validate_launch_spec(spec))
    assert float(argv[argv.index("--learning-rate") + 1]) == value


def test_threads_derive_from_the_allocation() -> None:
    assert adapter_launch.threads_for(1000) == 1
    assert adapter_launch.threads_for(1999) == 1
    assert adapter_launch.threads_for(8000) == 8
    assert adapter_launch.threads_for(500) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(schema_version=2),
        lambda s: s.update(adapter_id="cpu.iterative"),
        lambda s: s.update(adapter_version="2.0.0"),
        lambda s: s.update(startup_nonce="not-a-uuid"),
        lambda s: s.update(threads=0),
        lambda s: s.update(threads=True),
        lambda s: s["parameters"].update(learning_rate=0.0),
        lambda s: s["parameters"].update(learning_rate=1.5),
        lambda s: s["parameters"].update(learning_rate=float("nan")),
        lambda s: s["parameters"].update(learning_rate=1),
        lambda s: s["parameters"].update(epochs=101),
        lambda s: s["parameters"].update(batch_size=513),
        lambda s: s["parameters"].update(subset_size=99),
        lambda s: s["parameters"].update(seed=2**31),
        lambda s: s["parameters"].update(extra=1),
        lambda s: s.update(spec_checksum="sha256:" + "d" * 64),
        lambda s: s.update(inputs={"dataset": "/input/../etc/passwd"}),
        lambda s: s.update(inputs={"dataset": "/input/dataset.arrow", "model": "/input/m"}),
        lambda s: s.update(output_dir="/tmp"),
        lambda s: s["provenance"].update(template_id="cpu-iterative"),
        lambda s: s["provenance"].update(adapter_id="batch.inference"),
        lambda s: s["provenance"].update(job_fence=0),
        lambda s: s["checkpoint"].update(state_path="/output/state.json"),
        lambda s: s["checkpoint"]["compatibility"].update(framework="PYTHON"),
        lambda s: s["checkpoint"]["compatibility"].update(device_type="CUDA"),
        lambda s: s.update(extra=None),
        lambda s: s.update(checkpoint=None, restore=h.restore_block(3)),
    ],
)
def test_closed_spec_rejects_every_deviation(mutate) -> None:
    spec = copy.deepcopy(h.training_spec())
    mutate(spec)
    with pytest.raises(adapter_launch.LaunchSpecError):
        adapter_launch.validate_launch_spec(spec)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["files"].reverse(),
        lambda r: r["files"].pop(),
        lambda r: r["files"][0].update(path="/input/restore/../model.safetensors"),
        lambda r: r["files"][1].update(size_bytes=0),
        lambda r: r["cursor"].update(item_cursor=1),
        lambda r: r["cursor"].update(step=10_000),
        lambda r: r["cursor"].update(epoch=0),
        lambda r: r.update(checkpoint_sequence=0),
    ],
)
def test_restore_block_is_closed(mutate) -> None:
    restore = h.restore_block(5)
    mutate(restore)
    with pytest.raises(adapter_launch.LaunchSpecError):
        adapter_launch.validate_launch_spec(h.training_spec(restore=restore))


@pytest.mark.parametrize("step", [0, 1, 3, 4, 7, 8])
def test_training_cursor_accepts_every_runtime_cursor(step: int) -> None:
    document = h.training_document(step)
    cursor = training_state.runtime_cursor(document)
    assert adapter_launch.validate_training_cursor(cursor, h.PARAMETERS) == cursor


def test_training_state_must_belong_to_the_job() -> None:
    document = h.training_document(3)
    adapter_launch.check_training_state(
        document,
        parameters=h.PARAMETERS,
        threads=1,
        input_checksum=h.INPUT_CHECKSUM,
        spec_checksum=h.SPEC_CHECKSUM,
    )
    for kwargs in (
        {"threads": 2},
        {"input_checksum": "sha256:" + "0" * 64},
        {"spec_checksum": "sha256:" + "0" * 64},
        {"parameters": {**h.PARAMETERS, "seed": 8}},
        {"parameters": {**h.PARAMETERS, "learning_rate": 0.5}},
    ):
        arguments = {
            "parameters": h.PARAMETERS,
            "threads": 1,
            "input_checksum": h.INPUT_CHECKSUM,
            "spec_checksum": h.SPEC_CHECKSUM,
            **kwargs,
        }
        with pytest.raises(adapter_launch.LaunchSpecError):
            adapter_launch.check_training_state(document, **arguments)


def test_inference_spec_uses_the_model_input_and_resume_state() -> None:
    spec = h.training_spec(
        adapter_id="batch.inference",
        parameters={"chunk_size": 300, "batch_size": 128, "output_format": "JSONL"},
        inputs={"dataset": "/input/dataset.arrow", "model": "/input/model.safetensors"},
        provenance={
            **h.PROVENANCE,
            "template_id": "batch-inference",
            "adapter_id": "batch.inference",
        },
    )
    spec["checkpoint"]["state_path"] = adapter_launch.INFERENCE_STATE_PATH
    argv = adapter_launch.workload_command(adapter_launch.validate_launch_spec(spec))
    assert argv[:3] == ("python", "-m", "nexa.workloads.batch_inference")
    assert argv[argv.index("--model") + 1] == "/input/model.safetensors"
    assert "--resume-state" not in argv


def _parse_metrics(raw: bytes, model: bytes) -> dict:
    return adapter_launch.parse_training_metrics(
        raw,
        parameters=h.PARAMETERS,
        input_checksum=h.INPUT_CHECKSUM,
        spec_checksum=h.SPEC_CHECKSUM,
        model_checksum="sha256:" + hashlib.sha256(model).hexdigest(),
    )


def test_training_metrics_are_parsed_and_projected_to_manifest_scalars() -> None:
    model = h.model_bytes()
    document = h.metrics_document(model)
    metrics = _parse_metrics(canonical_json(document), model)
    projected = adapter_launch.manifest_metrics(metrics)
    assert list(projected) == list(adapter_launch.MANIFEST_METRICS)
    assert all(not isinstance(value, dict | list) for value in projected.values())
    assert projected["model_checksum"] == document["model_checksum"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d.pop("eval_loss"),
        lambda d: d.update(steps=7),
        lambda d: d.update(epochs=3),
        lambda d: d.update(model_checksum="sha256:" + "0" * 64),
        lambda d: d.update(input_checksum="sha256:" + "0" * 64),
        lambda d: d.update(spec_checksum="sha256:" + "0" * 64),
        lambda d: d.update(eval_accuracy=1.5),
        lambda d: d.update(train_loss=-0.1),
        lambda d: d.update(format="other"),
        lambda d: d["epoch_sample_order_digests"].pop(),
        lambda d: d["parameter_l2_norms"].popitem(),
    ],
)
def test_training_metrics_reject_every_deviation(mutate) -> None:
    model = h.model_bytes()
    document = h.metrics_document(model)
    mutate(document)
    with pytest.raises(adapter_launch.LaunchSpecError):
        _parse_metrics(canonical_json(document), model)


@pytest.mark.parametrize(
    "raw",
    [
        lambda d: json.dumps(d, indent=1).encode(),
        lambda d: canonical_json(d).replace(b'"eval_items":1000', b'"eval_items":NaN'),
        lambda d: canonical_json(d)[:-1] + b',"epochs":2}',
        lambda d: b"x" * (adapter_launch.MAX_METRICS_BYTES + 1),
    ],
)
def test_training_metrics_must_be_strict_canonical_json(raw) -> None:
    model = h.model_bytes()
    with pytest.raises(adapter_launch.LaunchSpecError):
        _parse_metrics(raw(h.metrics_document(model)), model)


def _inference_restore(**cursor) -> dict:
    state = canonical_json(h.inference_document(2))
    block = h.inference_restore_block(state, b'{"kind":"CHUNK_OUTPUT"}', next_chunk=2)
    block["cursor"].update(cursor)
    return block


def test_inference_checkpoint_launch_bounds_pending_chunks_and_resumes() -> None:
    plain = adapter_launch.workload_command(
        adapter_launch.validate_launch_spec(h.inference_spec(checkpoint=False))
    )
    assert "--window" not in plain
    spec = adapter_launch.validate_launch_spec(h.inference_spec(restore=_inference_restore()))
    argv = adapter_launch.workload_command(spec)
    assert argv[argv.index("--window") + 1] == str(adapter_launch.INFERENCE_WINDOW)
    assert argv[argv.index("--resume-state") + 1] == "/input/restore/inference-state.json"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["files"].pop(),
        lambda r: r["files"].reverse(),
        lambda r: r["cursor"].update(epoch=1),
        lambda r: r["cursor"].update(step=0, item_cursor=0),
        lambda r: r["cursor"].update(item_cursor=1),
        lambda r: r["cursor"].update(sampler_state_checksum="sha256:" + "0" * 64),
        lambda r: r["files"][1].update(path="/input/restore/other.json"),
    ],
)
def test_inference_restore_block_is_the_state_and_chunk_manifest_pair(mutate) -> None:
    restore = _inference_restore()
    mutate(restore)
    with pytest.raises(adapter_launch.LaunchSpecError):
        adapter_launch.validate_launch_spec(h.inference_spec(restore=restore))


def test_inference_state_must_belong_to_the_job() -> None:
    document = h.inference_document(2)
    arguments = {
        "parameters": h.INFERENCE_PARAMETERS,
        "threads": 1,
        "input_checksum": h.INPUT_CHECKSUM,
        "spec_checksum": h.SPEC_CHECKSUM,
        "model_checksum": h.MODEL_CHECKSUM,
    }
    adapter_launch.check_inference_state(document, **arguments)
    for change in (
        {"threads": 2},
        {"input_checksum": "sha256:" + "0" * 64},
        {"spec_checksum": "sha256:" + "0" * 64},
        {"model_checksum": "sha256:" + "0" * 64},
        {"parameters": {**h.INFERENCE_PARAMETERS, "batch_size": 64}},
        {"parameters": {**h.INFERENCE_PARAMETERS, "output_format": "PARQUET"}},
    ):
        with pytest.raises(adapter_launch.LaunchSpecError):
            adapter_launch.check_inference_state(document, **{**arguments, **change})
    oversized = {**document, "item_count": 60000, "chunk_size": 1}
    with pytest.raises(adapter_launch.LaunchSpecError):
        adapter_launch.check_inference_state(oversized, **arguments)
