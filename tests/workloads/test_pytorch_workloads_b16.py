"""B16 torch-side workload tests; run only inside the PyTorch image test layer on VPS1.

Skipped where torch is absent (the Mac host never installs it) or where the fixture data
directory ``$NEXA_B16_DATA_DIR`` is missing; docs/evidence B16 records the container runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
np = pytest.importorskip("numpy")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
safetensors_numpy = pytest.importorskip("safetensors.numpy")

from nexa.workloads import (  # noqa: E402
    batch_inference,
    inference_state,
    pytorch_arch,
    pytorch_cifar10,
    safetensors_format,
    training_state,
)
from nexa.workloads.canonical_json import canonical_json  # noqa: E402
from nexa.workloads.chunk_manifest import MAX_CHUNK_FILE_BYTES, MAX_CHUNKS  # noqa: E402
from nexa.workloads.dataset_format import METADATA_KEY, RowDigest  # noqa: E402
from nexa.workloads.torch_common import (  # noqa: E402
    EXIT_INTERNAL,
    EXIT_INVALID_INPUT,
    SmallCnn,
    encode_model,
    load_dataset,
)

DATA_DIR = Path(os.environ.get("NEXA_B16_DATA_DIR", "/nonexistent"))
TRAIN = DATA_DIR / "cifar10-train-5000-eval-1000.arrow"
INFER = DATA_DIR / "cifar10-inference-2000.arrow"
MODEL = DATA_DIR / "cifar10-smallcnn-v1.safetensors"
SPEC = "sha256:" + hashlib.sha256(b"b16-torch-tests").hexdigest()
PARAMS = pytorch_cifar10.TrainingParams(
    epochs=3, batch_size=64, learning_rate=0.05, seed=11, subset_size=500
)

pytestmark = pytest.mark.skipif(
    not (TRAIN.is_file() and INFER.is_file() and MODEL.is_file()),
    reason="B16 fixture data is absent; set NEXA_B16_DATA_DIR (VPS1 container runs only)",
)


def _trainer() -> pytorch_cifar10.Trainer:
    return pytorch_cifar10.Trainer(dataset_path=TRAIN, params=PARAMS, threads=1, spec_checksum=SPEC)


def _write_files(directory: Path, files: dict[str, bytes]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        (directory / name).write_bytes(payload)
    return directory


def test_fixture_datasets_verify_against_their_metadata() -> None:
    train = load_dataset(TRAIN)
    assert train.metadata["splits"] == {"train": [0, 5000], "eval": [5000, 6000]}
    assert train.images.shape == (6000, pytorch_arch.IMAGE_BYTES)
    inference = load_dataset(INFER)
    assert inference.metadata["splits"] == {"inference": [0, 2000]}
    assert int(inference.source_index.min()) >= 50_000


def test_stdlib_safetensors_files_load_with_the_official_library(tmp_path: Path) -> None:
    trainer = _trainer()
    trainer.train()
    snapshot = trainer.snapshot_bytes()
    _, files = training_state.split_snapshot(snapshot)
    for name in ("model.safetensors", "optimizer.safetensors", "rng.safetensors"):
        path = tmp_path / name
        path.write_bytes(files[name])
        tensors = safetensors_numpy.load_file(str(path))
        header, raw = safetensors_format.decode(files[name])
        assert set(tensors) == set(header.tensors)
        for tensor_name, value in tensors.items():
            assert value.tobytes() == raw[tensor_name]
    tensors = safetensors_numpy.load_file(str(MODEL))
    assert set(tensors) == set(pytorch_arch.PARAMETERS)
    pytorch_arch.validate_model_header(safetensors_format.read_header(MODEL))


def test_resume_from_any_boundary_matches_the_uninterrupted_run(tmp_path: Path) -> None:
    captured: dict[int, bytes] = {}
    per_epoch = training_state.batches_per_epoch(PARAMS.subset_size, PARAMS.batch_size)
    targets = {3, per_epoch, per_epoch + 5}

    def capture(current: pytorch_cifar10.Trainer) -> None:
        if current.step in targets:
            captured[current.step] = current.snapshot_bytes()

    baseline = _trainer()
    baseline.train(capture)
    expected_model = encode_model(baseline.model)
    expected_metrics = baseline.metrics("sha256:" + "0" * 64)
    assert sorted(captured) == sorted(targets)
    cursors = []
    for step, snapshot in sorted(captured.items()):
        document, files = training_state.split_snapshot(snapshot)
        cursors.append(training_state.runtime_cursor(document))
        resumed = _trainer()
        resumed.restore(_write_files(tmp_path / f"restore-{step}", files))
        assert resumed.step == step
        # RNG, optimizer and sampler are restored bit for bit before the next batch.
        assert resumed.snapshot_bytes() == snapshot
        resumed.train()
        assert encode_model(resumed.model) == expected_model
        assert resumed.metrics("sha256:" + "0" * 64) == expected_metrics
    steps = [cursor["step"] for cursor in cursors]
    assert steps == sorted(steps) and len(set(steps)) == len(steps)
    assert expected_metrics["epoch_sample_order_digests"] == baseline.completed
    assert len(baseline.completed) == PARAMS.epochs


def test_run_training_writes_result_and_monotonic_snapshots(tmp_path: Path) -> None:
    ticks = iter(range(10_000))
    metrics = pytorch_cifar10.run_training(
        dataset_path=TRAIN,
        output_dir=tmp_path,
        params=PARAMS,
        threads=1,
        spec_checksum=SPEC,
        state_output=tmp_path / "state.safetensors",
        state_interval_seconds=0,
        clock=lambda: float(next(ticks)),
    )
    document, _ = training_state.split_snapshot((tmp_path / "state.safetensors").read_bytes())
    assert document["cursor"]["epoch"] == PARAMS.epochs
    assert training_state.runtime_cursor(document)["item_cursor"] == 0
    raw_metrics = (tmp_path / "metrics.json").read_bytes()
    assert json.loads(raw_metrics) == metrics
    assert canonical_json(metrics) == raw_metrics
    assert metrics["steps"] == training_state.total_steps(document)
    assert 0.0 <= metrics["eval_accuracy"] <= 1.0
    model_bytes = (tmp_path / "model.safetensors").read_bytes()
    assert metrics["model_checksum"] == "sha256:" + hashlib.sha256(model_bytes).hexdigest()


def _main_args(output: Path, *, dataset: Path = TRAIN, subset: int = 500) -> list[str]:
    return [
        "--dataset",
        str(dataset),
        "--output-dir",
        str(output),
        "--epochs",
        "1",
        "--batch-size",
        "64",
        "--learning-rate",
        "0.05",
        "--seed",
        "1",
        "--subset-size",
        str(subset),
        "--threads",
        "1",
        "--spec-checksum",
        SPEC,
    ]


def test_subset_larger_than_the_train_split_is_invalid_input(tmp_path: Path) -> None:
    assert pytorch_cifar10.main(_main_args(tmp_path, subset=5001)) == EXIT_INVALID_INPUT
    assert not (tmp_path / "metrics.json").exists()


def test_corrupted_dataset_is_invalid_input(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.arrow"
    payload = bytearray(TRAIN.read_bytes())
    payload[len(payload) // 2] ^= 0xFF
    corrupt.write_bytes(bytes(payload))
    assert pytorch_cifar10.main(_main_args(tmp_path, dataset=corrupt)) == EXIT_INVALID_INPUT


def test_non_finite_loss_fails_internal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def nan_loss(logits, targets, **kwargs):
        return logits.sum() * float("nan")

    monkeypatch.setattr(pytorch_cifar10.F, "cross_entropy", nan_loss)
    assert pytorch_cifar10.main(_main_args(tmp_path)) == EXIT_INTERNAL
    assert not (tmp_path / "model.safetensors").exists()


def _inference(output_format: str = "JSONL") -> batch_inference.Inference:
    return batch_inference.Inference(
        dataset_path=INFER,
        model_path=MODEL,
        chunk_size=300,
        batch_size=128,
        output_format=output_format,
        threads=1,
        spec_checksum=SPEC,
    )


def test_inference_restart_reproduces_identical_chunks(tmp_path: Path) -> None:
    drained = tmp_path / "drained"
    drained.mkdir()
    first = tmp_path / "first"
    first.mkdir()
    states: dict[int, bytes] = {}

    def drain(_: float) -> None:
        # Stand-in for the runner: record the cursor, then remove confirmed chunk files.
        raw = (first / batch_inference.STATE_FILE).read_bytes()
        states[inference_state.parse_state(raw)["next_chunk"]] = raw
        for entry in first.iterdir():
            if inference_state.CHUNK_FILE.match(entry.name):
                shutil.move(entry, drained / entry.name)

    summary = batch_inference.run_inference(
        inference=_inference(), output_dir=first, window=1, sleep=drain
    )
    drain(0)
    total = inference_state.chunk_count(2000, 300)
    assert summary["chunk_count"] == total == 7
    assert sorted(entry.name for entry in drained.iterdir()) == [
        inference_state.chunk_file_name(index, "JSONL") for index in range(total)
    ]
    assert sorted(states) == list(range(1, total + 1))
    second = tmp_path / "second"
    second.mkdir()
    state_path = tmp_path / "restore.json"
    state_path.write_bytes(states[3])
    resumed = _inference()
    resumed.restore(state_path)
    assert batch_inference.run_inference(inference=resumed, output_dir=second) == summary
    for index in range(3, total):
        name = inference_state.chunk_file_name(index, "JSONL")
        assert (second / name).read_bytes() == (drained / name).read_bytes()
    assert not (second / inference_state.chunk_file_name(2, "JSONL")).exists()


def test_jsonl_and_parquet_chunks_share_records_checksum(tmp_path: Path) -> None:
    jsonl, _ = _inference("JSONL").chunk_bytes(1)
    parquet, _ = _inference("PARQUET").chunk_bytes(1)
    assert parquet == _inference("PARQUET").chunk_bytes(1)[0]
    header_line, records = jsonl.split(b"\n", 1)
    header = json.loads(header_line)[batch_inference.HEADER_KEY]
    assert header["start_index"] == 300 and header["end_index_exclusive"] == 600
    assert header["records_checksum"] == "sha256:" + hashlib.sha256(records).hexdigest()
    path = tmp_path / "chunk.parquet"
    path.write_bytes(parquet)
    table = pq.read_table(path)
    metadata = json.loads(table.schema.metadata[batch_inference.HEADER_KEY.encode()])
    assert metadata == json.loads(header_line)
    lines, _ = batch_inference.record_lines(
        300,
        np.asarray(table.column("source_index").to_pylist()),
        np.asarray(table.column("logits").to_pylist(), dtype=np.float32),
    )
    assert b"".join(lines) == records


def test_model_with_wrong_architecture_is_invalid_input(tmp_path: Path) -> None:
    model = SmallCnn()
    header, tensors = safetensors_format.decode(encode_model(model))
    renamed = {
        ("fc3.bias" if name == "fc2.bias" else name): (info.dtype, info.shape, tensors[name])
        for name, info in header.tensors.items()
    }
    bad = tmp_path / "bad.safetensors"
    bad.write_bytes(safetensors_format.encode(renamed, dict(pytorch_arch.MODEL_METADATA)))
    output = tmp_path / "out"
    output.mkdir()
    code = batch_inference.main(
        [
            "--dataset",
            str(INFER),
            "--model",
            str(bad),
            "--output-dir",
            str(output),
            "--chunk-size",
            "500",
            "--batch-size",
            "64",
            "--output-format",
            "JSONL",
            "--threads",
            "1",
            "--spec-checksum",
            SPEC,
        ]
    )
    assert code == EXIT_INVALID_INPUT
    assert list(output.iterdir()) == []


def _write_arrow(path: Path, raw_metadata: bytes, rows: list[tuple[int, int, bytes]]) -> Path:
    schema = pa.schema(
        [
            pa.field("source_index", pa.uint32(), nullable=False),
            pa.field("label", pa.uint8(), nullable=False),
            pa.field("image", pa.binary(pytorch_arch.IMAGE_BYTES), nullable=False),
        ],
        metadata={METADATA_KEY: raw_metadata},
    )
    batch = pa.record_batch(
        [
            pa.array([row[0] for row in rows], type=pa.uint32()),
            pa.array([row[1] for row in rows], type=pa.uint8()),
            pa.array([row[2] for row in rows], type=pa.binary(pytorch_arch.IMAGE_BYTES)),
        ],
        schema=schema,
    )
    with pa.OSFile(str(path), "wb") as sink, pa.ipc.new_file(sink, schema) as writer:
        writer.write_batch(batch)
    return path


def _inference_dataset(path: Path, count: int) -> Path:
    """A valid inference dataset of ``count`` rows built by cycling the fixture rows."""
    fixture = load_dataset(INFER)
    size = len(fixture.labels)
    rows = [
        (
            int(fixture.source_index[i % size]),
            int(fixture.labels[i % size]),
            fixture.images[i % size].tobytes(),
        )
        for i in range(count)
    ]
    digest = RowDigest()
    counts = [0] * pytorch_arch.NUM_CLASSES
    for row in rows:
        digest.update(*row)
        counts[row[1]] += 1
    metadata = {
        **fixture.metadata,
        "item_count": count,
        "splits": {"inference": [0, count]},
        "class_counts": {"inference": counts},
        "indices_checksum": digest.indices_checksum,
        "content_checksum": digest.content_checksum,
    }
    return _write_arrow(path, canonical_json(metadata), rows)


def _inference_args(output: Path, *, dataset: Path, model: Path = MODEL, chunk_size: int) -> list:
    return [
        "--dataset",
        str(dataset),
        "--model",
        str(model),
        "--output-dir",
        str(output),
        "--chunk-size",
        str(chunk_size),
        "--batch-size",
        "128",
        "--output-format",
        "JSONL",
        "--threads",
        "1",
        "--spec-checksum",
        SPEC,
    ]


def test_more_chunks_than_one_manifest_is_invalid_input_before_any_chunk(tmp_path: Path) -> None:
    """B16-R24: 2049 items with chunk_size 1 exceed MAX_CHUNKS; exit 65, no chunk written."""
    assert MAX_CHUNKS == 2048
    dataset = _inference_dataset(tmp_path / "items-2049.arrow", 2049)
    output = tmp_path / "out"
    output.mkdir()
    code = batch_inference.main(_inference_args(output, dataset=dataset, chunk_size=1))
    assert code == EXIT_INVALID_INPUT
    assert list(output.iterdir()) == []


def test_chunk_file_above_the_upload_bound_is_invalid_input(tmp_path: Path) -> None:
    """B16-R24: one 6000-record JSONL chunk is larger than MAX_CHUNK_FILE_BYTES; exit 65."""
    dataset = _inference_dataset(tmp_path / "items-6000.arrow", 6000)
    probe = batch_inference.Inference(
        dataset_path=dataset,
        model_path=MODEL,
        chunk_size=6000,
        batch_size=128,
        output_format="JSONL",
        threads=1,
        spec_checksum=SPEC,
    )
    assert len(probe.chunk_bytes(0)[0]) > MAX_CHUNK_FILE_BYTES
    output = tmp_path / "out"
    output.mkdir()
    code = batch_inference.main(_inference_args(output, dataset=dataset, chunk_size=6000))
    assert code == EXIT_INVALID_INPUT
    assert list(output.iterdir()) == []


def _bad_metadata(case: str) -> bytes:
    document = dict(load_dataset(TRAIN).metadata)
    text = json.dumps(document, sort_keys=True, separators=(",", ":"))
    if case == "split-type":
        return json.dumps(
            {**document, "splits": {"train": [0, 5], "eval": "x"}}, separators=(",", ":")
        ).encode()
    if case == "count-type":
        counts = {**document["class_counts"], "eval": [None] * pytorch_arch.NUM_CLASSES}
        return json.dumps({**document, "class_counts": counts}, separators=(",", ":")).encode()
    assert case == "huge-integer"
    seed = f'"seed":{document["seed"]}'
    assert text.count(seed) == 1
    return text.replace(seed, '"seed":' + "9" * 5000).encode()


@pytest.mark.parametrize("case", ["split-type", "count-type", "huge-integer"])
def test_training_main_maps_malformed_metadata_to_invalid_input(tmp_path: Path, case: str) -> None:
    """B16-R25: user metadata with wrong JSON types or oversized integers exits 65."""
    with pa.OSFile(str(TRAIN), "rb") as source:
        table = pa.ipc.open_file(source).read_all()
    rows = list(
        zip(
            table.column("source_index").to_pylist(),
            table.column("label").to_pylist(),
            table.column("image").to_pylist(),
            strict=True,
        )
    )
    dataset = _write_arrow(tmp_path / "bad.arrow", _bad_metadata(case), rows)
    output = tmp_path / "out"
    output.mkdir()
    assert pytorch_cifar10.main(_main_args(output, dataset=dataset)) == EXIT_INVALID_INPUT
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(
    "entry",
    [
        '{"dtype":"F32","shape":[10],"data_offsets":[0,' + "9" * 5000 + "]}",
        '{"dtype":["F32"],"shape":[10],"data_offsets":[0,40]}',
        '{"dtype":"F32","shape":[10],"data_offsets":["0",40]}',
    ],
)
def test_inference_main_maps_malformed_model_header_to_invalid_input(
    tmp_path: Path, entry: str
) -> None:
    """B16-R25: a model header with an oversized integer or wrong types exits 65."""
    body = ('{"fc2.bias":' + entry + "}").encode()
    body += b" " * (-(8 + len(body)) % 8)
    bad = tmp_path / "bad.safetensors"
    bad.write_bytes(len(body).to_bytes(8, "little") + body + b"\0" * 40)
    output = tmp_path / "out"
    output.mkdir()
    code = batch_inference.main(_inference_args(output, dataset=INFER, model=bad, chunk_size=500))
    assert code == EXIT_INVALID_INPUT
    assert list(output.iterdir()) == []
