"""Allowlisted ``batch.inference`` workload; runs only inside its PyTorch image.

Items ``[0, N)`` of the Arrow dataset are split into fixed chunks ``chunk-%08d``. Each chunk
file is written atomically into ``/output``, then ``inference-state.json`` advances its cursor.
The runner uploads confirmed chunks and deletes them; with checkpoints enabled the workload
keeps at most ``window`` chunk files in the bounded ``/output`` tmpfs and waits for the runner
to drain them (B16-R13 flow control). Chunk bytes depend only on the inputs, parameters and
thread count, so a restarted attempt reproduces identical chunk files.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import torch

from . import chunk_manifest, inference_state
from . import pytorch_arch as arch
from .canonical_json import canonical_json
from .torch_common import (
    EXIT_INTERNAL,
    EXIT_INVALID_INPUT,
    InternalWorkloadError,
    NonFiniteError,
    SmallCnn,
    WorkloadInputError,
    atomic_write,
    configure_torch,
    file_checksum,
    images_to_tensor,
    load_dataset,
    load_model_bytes,
)

STATE_FILE = "inference-state.json"
SUMMARY_FILE = "summary.json"
HEADER_KEY = "nexa_chunk"
_WINDOW_POLL_SECONDS = 0.05


def record_lines(
    first_index: int, source_index: np.ndarray, logits: np.ndarray
) -> tuple[list[bytes], list[int]]:
    """Fixed textual records; logits use ``%.6e`` so bytes never depend on float repr."""
    lines = []
    predictions = []
    for offset, row in enumerate(logits):
        prediction = int(np.argmax(row))
        values = ",".join(format(float(value), ".6e") for value in row)
        lines.append(
            (
                f'{{"index":{first_index + offset},"logits":[{values}],'
                f'"prediction":{prediction},"source_index":{int(source_index[offset])}}}\n'
            ).encode("ascii")
        )
        predictions.append(prediction)
    return lines, predictions


def chunk_header(
    *,
    index: int,
    start: int,
    end: int,
    input_checksum: str,
    model_checksum: str,
    spec_checksum: str,
    records_checksum: str,
) -> dict:
    return {
        HEADER_KEY: {
            "chunk_id": inference_state.chunk_id(index),
            "start_index": start,
            "end_index_exclusive": end,
            "input_checksum": input_checksum,
            "model_checksum": model_checksum,
            "spec_checksum": spec_checksum,
            "records_checksum": records_checksum,
        }
    }


def encode_parquet(
    header: dict, first_index: int, source_index: np.ndarray, logits: np.ndarray
) -> bytes:
    count = len(logits)
    table = pa.table(
        {
            "index": pa.array(np.arange(first_index, first_index + count), type=pa.uint32()),
            "source_index": pa.array(source_index, type=pa.uint32()),
            "prediction": pa.array(np.argmax(logits, axis=1), type=pa.uint8()),
            "logits": pa.FixedSizeListArray.from_arrays(
                pa.array(logits.reshape(-1), type=pa.float32()), arch.NUM_CLASSES
            ),
        }
    ).replace_schema_metadata({HEADER_KEY: canonical_json(header).decode("ascii")})
    sink = pa.BufferOutputStream()
    # Fixed writer options: no compression, dictionary, statistics or page index.
    pq.write_table(
        table,
        sink,
        version="2.6",
        compression="NONE",
        use_dictionary=False,
        write_statistics=False,
        write_page_index=False,
        write_page_checksum=False,
        data_page_version="1.0",
    )
    return sink.getvalue().to_pybytes()


class Inference:
    def __init__(
        self,
        *,
        dataset_path: str | Path,
        model_path: str | Path,
        chunk_size: int,
        batch_size: int,
        output_format: str,
        threads: int,
        spec_checksum: str,
    ) -> None:
        if (
            not 1 <= chunk_size <= inference_state.MAX_CHUNK_SIZE
            or not 1 <= batch_size <= inference_state.MAX_BATCH_SIZE
            or output_format not in inference_state.OUTPUT_FORMATS
        ):
            raise WorkloadInputError("inference parameters are out of range")
        configure_torch(threads)
        self.input_checksum = file_checksum(dataset_path)
        self.model_checksum = file_checksum(model_path)
        dataset = load_dataset(dataset_path)
        self.images = dataset.images
        self.source_index = dataset.source_index
        self.model = SmallCnn()
        load_model_bytes(self.model, Path(model_path).read_bytes())
        self.model.eval()
        item_count = int(dataset.metadata["item_count"])
        # Every chunk must fit one manifest before any compute (B16-R24).
        if inference_state.chunk_count(item_count, chunk_size) > chunk_manifest.MAX_CHUNKS:
            raise WorkloadInputError("chunk_size yields more chunks than a manifest can list")
        self.state = inference_state.validate_state(
            {
                "schema_version": 1,
                "format": inference_state.STATE_FORMAT,
                "architecture_id": arch.ARCHITECTURE_ID,
                "input_checksum": self.input_checksum,
                "model_checksum": self.model_checksum,
                "spec_checksum": spec_checksum,
                "threads": threads,
                "chunk_size": chunk_size,
                "batch_size": batch_size,
                "output_format": output_format,
                "item_count": item_count,
                "next_chunk": 0,
                "prediction_counts": [0] * arch.NUM_CLASSES,
            }
        )

    @property
    def total_chunks(self) -> int:
        return inference_state.chunk_count(self.state["item_count"], self.state["chunk_size"])

    def restore(self, state_path: str | Path) -> None:
        try:
            restored = inference_state.parse_state(Path(state_path).read_bytes())
        except (OSError, inference_state.InferenceStateError):
            raise InternalWorkloadError(
                "restore state is unreadable after runner verification"
            ) from None
        for field in inference_state.IDENTITY_FIELDS:
            if restored[field] != self.state[field]:
                raise InternalWorkloadError(f"restore state {field} does not match this run")
        self.state = restored

    def _logits(self, start: int, end: int) -> np.ndarray:
        rows = []
        batch = self.state["batch_size"]
        with torch.no_grad():
            for low in range(start, end, batch):
                high = min(end, low + batch)
                rows.append(self.model(images_to_tensor(self.images[low:high])).numpy())
        logits = np.concatenate(rows).astype(np.float32, copy=False)
        if not np.isfinite(logits).all():
            raise NonFiniteError("model output is not finite")
        return logits

    def chunk_bytes(self, index: int) -> tuple[bytes, list[int]]:
        start, end = inference_state.chunk_extent(
            index, self.state["item_count"], self.state["chunk_size"]
        )
        logits = self._logits(start, end)
        source = self.source_index[start:end]
        lines, predictions = record_lines(start, source, logits)
        header = chunk_header(
            index=index,
            start=start,
            end=end,
            input_checksum=self.input_checksum,
            model_checksum=self.model_checksum,
            spec_checksum=self.state["spec_checksum"],
            records_checksum="sha256:" + hashlib.sha256(b"".join(lines)).hexdigest(),
        )
        if self.state["output_format"] == "JSONL":
            return canonical_json(header) + b"\n" + b"".join(lines), predictions
        return encode_parquet(header, start, source, logits), predictions


def _pending_chunks(directory: Path) -> int:
    return sum(1 for entry in directory.iterdir() if inference_state.CHUNK_FILE.match(entry.name))


def run_inference(
    *,
    inference: Inference,
    output_dir: str | Path,
    window: int = 0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    output = Path(output_dir)
    state = inference.state
    while state["next_chunk"] < inference.total_chunks:
        # Flow control only: the runner removes chunk files once their upload is bound.
        while window and _pending_chunks(output) >= window:
            sleep(_WINDOW_POLL_SECONDS)
        index = state["next_chunk"]
        payload, predictions = inference.chunk_bytes(index)
        # Only the last chunk is shorter, so the first oversized chunk stops the run (B16-R24).
        if len(payload) > chunk_manifest.MAX_CHUNK_FILE_BYTES:
            raise WorkloadInputError("chunk_size yields a chunk file above the upload bound")
        atomic_write(
            output / inference_state.chunk_file_name(index, state["output_format"]), payload
        )
        counts = list(state["prediction_counts"])
        for prediction in predictions:
            counts[prediction] += 1
        state = inference_state.validate_state(
            {**state, "next_chunk": index + 1, "prediction_counts": counts}
        )
        inference.state = state
        atomic_write(output / STATE_FILE, canonical_json(state))
    summary = inference_state.summary_from_state(state)
    atomic_write(output / SUMMARY_FILE, canonical_json(summary))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the allowlisted chunked batch inference")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--chunk-size", type=int, required=True)
    parser.add_argument("--batch-size", type=int, required=True)
    parser.add_argument("--output-format", required=True)
    parser.add_argument("--threads", type=int, required=True)
    parser.add_argument("--spec-checksum", required=True)
    parser.add_argument("--window", type=int, default=0)
    parser.add_argument("--resume-state")
    args = parser.parse_args(argv)
    try:
        inference = Inference(
            dataset_path=args.dataset,
            model_path=args.model,
            chunk_size=args.chunk_size,
            batch_size=args.batch_size,
            output_format=args.output_format,
            threads=args.threads,
            spec_checksum=args.spec_checksum,
        )
        if args.resume_state is not None:
            inference.restore(args.resume_state)
        run_inference(inference=inference, output_dir=args.output_dir, window=args.window)
    except WorkloadInputError as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return EXIT_INVALID_INPUT
    except InternalWorkloadError as exc:
        print(f"internal: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    return 0


__all__ = ["Inference", "chunk_header", "encode_parquet", "main", "record_lines", "run_inference"]


if __name__ == "__main__":
    raise SystemExit(main())
