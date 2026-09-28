"""Allowlisted ``pytorch.cifar10`` training workload; runs only inside its PyTorch image.

The trusted runner launches this module through the workload supervisor. It reads the Arrow
dataset, trains ``nexa-cifar10-smallcnn-v1`` with SGD momentum and a self-managed sampler,
atomically replaces ``state.safetensors`` at batch boundaries and finally writes
``model.safetensors`` + ``metrics.json``. No pickle, ``torch.save/load`` or code execution.
"""

from __future__ import annotations

import argparse
import math
import random
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from torch.nn import functional as F  # noqa: N812

from . import pytorch_arch as arch
from . import safetensors_format, training_state
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
    encode_model,
    file_checksum,
    float_tensor,
    images_to_tensor,
    l2_norm,
    load_dataset,
    load_model_bytes,
    tensor_bytes,
)

STATE_FILE = "state.safetensors"
METRICS_FORMAT = "pytorch-cifar10-metrics-v1"
EVAL_BATCH_SIZE = 256
RESTORE_FILES = ("model.safetensors", "optimizer.safetensors", "rng.safetensors")


@dataclass(frozen=True, slots=True)
class TrainingParams:
    epochs: int
    batch_size: int
    learning_rate: float
    seed: int
    subset_size: int

    def __post_init__(self) -> None:
        if not (
            1 <= self.epochs <= training_state.MAX_EPOCHS
            and 1 <= self.batch_size <= training_state.MAX_BATCH_SIZE
            and math.isfinite(self.learning_rate)
            and 0 < self.learning_rate <= 1
            and 0 <= self.seed <= training_state.MAX_SEED
            and training_state.MIN_SUBSET <= self.subset_size <= training_state.MAX_SUBSET
        ):
            raise WorkloadInputError("training parameters are out of range")


def _seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def rng_tensors() -> dict[str, tuple[str, tuple[int, ...], bytes]]:
    version, words, gauss = random.getstate()
    if version != 3 or len(words) != arch.MT19937_WORDS + 1 or gauss is not None:
        raise InternalWorkloadError("python RNG state has an unsupported layout")
    kind, keys, position, has_gauss, _ = np.random.get_state()
    if kind != "MT19937" or len(keys) != arch.MT19937_WORDS or has_gauss:
        raise InternalWorkloadError("numpy RNG state has an unsupported layout")
    torch_state = torch.get_rng_state()
    return {
        "python.mt19937": ("U32", (arch.MT19937_WORDS + 1,), struct.pack("<625I", *words)),
        "numpy.mt19937": (
            "U32",
            (arch.MT19937_WORDS + 1,),
            np.asarray(keys, dtype="<u4").tobytes() + struct.pack("<I", int(position)),
        ),
        "torch.cpu": ("U8", (arch.TORCH_CPU_RNG_BYTES,), tensor_bytes(torch_state)),
    }


def restore_rng(raw: bytes) -> None:
    try:
        header, tensors = safetensors_format.decode(raw)
        arch.validate_rng_header(header)
    except safetensors_format.SafetensorsError as exc:
        raise WorkloadInputError(str(exc)) from exc
    python_words = struct.unpack("<625I", tensors["python.mt19937"])
    if python_words[-1] > arch.MT19937_WORDS:
        raise WorkloadInputError("python RNG position is out of range")
    random.setstate((3, python_words, None))
    numpy_words = struct.unpack("<625I", tensors["numpy.mt19937"])
    if numpy_words[-1] > arch.MT19937_WORDS:
        raise WorkloadInputError("numpy RNG position is out of range")
    np.random.set_state(
        ("MT19937", np.asarray(numpy_words[:-1], dtype=np.uint32), numpy_words[-1], 0, 0.0)
    )
    torch.set_rng_state(torch.frombuffer(bytearray(tensors["torch.cpu"]), dtype=torch.uint8))


class Trainer:
    def __init__(
        self,
        *,
        dataset_path: str | Path,
        params: TrainingParams,
        threads: int,
        spec_checksum: str,
    ) -> None:
        configure_torch(threads)
        self.params = params
        self.threads = threads
        self.spec_checksum = spec_checksum
        self.input_checksum = file_checksum(dataset_path)
        dataset = load_dataset(dataset_path)
        train_low, train_high = dataset.split("train")
        eval_low, eval_high = dataset.split("eval")
        if params.subset_size > train_high - train_low:
            raise WorkloadInputError("subset_size exceeds the dataset train split")
        stop = train_low + params.subset_size
        self.train_images = dataset.images[train_low:stop]
        self.train_labels = torch.from_numpy(dataset.labels[train_low:stop].astype(np.int64))
        self.eval_images = dataset.images[eval_low:eval_high]
        self.eval_labels = torch.from_numpy(dataset.labels[eval_low:eval_high].astype(np.int64))
        _seed_everything(params.seed)
        self.model = SmallCnn()
        self.optimizer = torch.optim.SGD(
            self.model.parameters(), lr=params.learning_rate, momentum=arch.MOMENTUM
        )
        self.per_epoch = training_state.batches_per_epoch(params.subset_size, params.batch_size)
        self.epoch = 0
        self.batch_index = 0
        self.completed: list[str] = []
        self.running = training_state.initial_order_digest(0)
        self._order_epoch = -1
        self._order: list[int] = []

    def _epoch_order(self) -> list[int]:
        if self._order_epoch != self.epoch:
            self._order = training_state.permutation(
                self.params.seed, self.epoch, self.params.subset_size
            )
            self._order_epoch = self.epoch
        return self._order

    @property
    def step(self) -> int:
        return self.epoch * self.per_epoch + self.batch_index

    @property
    def finished(self) -> bool:
        return self.epoch == self.params.epochs

    def state_document(self) -> dict:
        order = None
        if not self.finished:
            order = training_state.permutation_checksum(self._epoch_order())
        return {
            "schema_version": 1,
            "format": training_state.FORMAT,
            "architecture_id": arch.ARCHITECTURE_ID,
            "architecture_version": arch.ARCHITECTURE_VERSION,
            "preprocessing": arch.PREPROCESSING,
            "optimizer": {
                "algorithm": arch.OPTIMIZER,
                "learning_rate": self.params.learning_rate,
                "momentum": arch.MOMENTUM,
            },
            "threads": self.threads,
            "input_checksum": self.input_checksum,
            "spec_checksum": self.spec_checksum,
            "cursor": {"epoch": self.epoch, "batch_index": self.batch_index, "step": self.step},
            "sampler": {
                "algorithm": training_state.SAMPLER_ALGORITHM,
                "seed": self.params.seed,
                "subset_size": self.params.subset_size,
                "batch_size": self.params.batch_size,
                "epochs": self.params.epochs,
                "permutation_checksum": order,
            },
            "sample_order": {
                "completed": list(self.completed),
                "running": None if self.finished else self.running,
            },
        }

    def _momentum_buffers(self) -> dict[str, torch.Tensor]:
        buffers = {}
        for name, parameter in self.model.named_parameters():
            buffer = self.optimizer.state.get(parameter, {}).get("momentum_buffer")
            if buffer is None:
                raise InternalWorkloadError("optimizer state is not initialized")
            buffers[name] = buffer
        return buffers

    def snapshot_bytes(self) -> bytes:
        document = training_state.validate_training_state(self.state_document())
        tensors = {}
        for name, parameter in self.model.named_parameters():
            tensors["model." + name] = ("F32", tuple(parameter.shape), tensor_bytes(parameter))
        for name, buffer in self._momentum_buffers().items():
            tensors["optimizer." + arch.OPTIMIZER_PREFIX + name] = (
                "F32",
                tuple(buffer.shape),
                tensor_bytes(buffer),
            )
        for name, value in rng_tensors().items():
            tensors["rng." + name] = value
        metadata = {training_state.SNAPSHOT_METADATA_KEY: canonical_json(document).decode("utf-8")}
        return safetensors_format.encode(tensors, metadata)

    def restore(self, directory: str | Path) -> None:
        """Resume at the next batch from the four checkpoint files the runner mounted."""
        root = Path(directory)
        try:
            files = {
                name: (root / name).read_bytes()
                for name in (training_state.STATE_FILE, *RESTORE_FILES)
            }
            document = training_state.validate_checkpoint_files(files)
            _, optimizer_tensors = safetensors_format.decode(files["optimizer.safetensors"])
        except (OSError, training_state.TrainingStateError, safetensors_format.SafetensorsError):
            raise InternalWorkloadError(
                "restore state is unreadable after runner verification"
            ) from None
        expected = self.state_document()
        for field in (
            "optimizer",
            "threads",
            "input_checksum",
            "spec_checksum",
        ):
            if document[field] != expected[field]:
                raise InternalWorkloadError(f"restore state {field} does not match this run")
        sampler = dict(document["sampler"])
        sampler.pop("permutation_checksum")
        current = dict(expected["sampler"])
        current.pop("permutation_checksum")
        if sampler != current:
            raise InternalWorkloadError("restore sampler does not match this run")
        try:
            load_model_bytes(self.model, files["model.safetensors"])
            with torch.no_grad():
                for name, parameter in self.model.named_parameters():
                    self.optimizer.state[parameter]["momentum_buffer"] = float_tensor(
                        optimizer_tensors[arch.OPTIMIZER_PREFIX + name], arch.PARAMETERS[name]
                    )
            restore_rng(files["rng.safetensors"])
        except WorkloadInputError as exc:
            raise InternalWorkloadError(f"restore state is not loadable: {exc}") from None
        self.epoch = document["cursor"]["epoch"]
        self.batch_index = document["cursor"]["batch_index"]
        self.completed = list(document["sample_order"]["completed"])
        self.running = document["sample_order"]["running"]

    def _train_batch(self, positions: list[int]) -> None:
        index = np.asarray(positions, dtype=np.int64)
        inputs = images_to_tensor(self.train_images[index])
        targets = self.train_labels[index]
        self.optimizer.zero_grad(set_to_none=True)
        loss = F.cross_entropy(self.model(inputs), targets)
        if not bool(torch.isfinite(loss)):
            raise NonFiniteError("training loss is not finite")
        loss.backward()
        for parameter in self.model.parameters():
            if parameter.grad is None or not bool(torch.isfinite(parameter.grad).all()):
                raise NonFiniteError("gradient is not finite")
        self.optimizer.step()

    def train(self, on_boundary: Callable[[Trainer], None] | None = None) -> None:
        batch = self.params.batch_size
        while not self.finished:
            order = self._epoch_order()
            positions = order[self.batch_index * batch : (self.batch_index + 1) * batch]
            self._train_batch(positions)
            self.running = training_state.chain_order_digest(self.running, positions)
            self.batch_index += 1
            if self.batch_index == self.per_epoch:
                self.completed.append(self.running)
                self.epoch += 1
                self.batch_index = 0
                self.running = training_state.initial_order_digest(self.epoch)
            if on_boundary is not None:
                on_boundary(self)

    def _evaluate(self, images: np.ndarray, labels: torch.Tensor) -> tuple[float, float]:
        total_loss = 0.0
        correct = 0
        self.model.eval()
        with torch.no_grad():
            for start in range(0, len(images), EVAL_BATCH_SIZE):
                logits = self.model(images_to_tensor(images[start : start + EVAL_BATCH_SIZE]))
                targets = labels[start : start + EVAL_BATCH_SIZE]
                loss = F.cross_entropy(logits.double(), targets, reduction="sum")
                total_loss += float(loss)
                correct += int((logits.argmax(dim=1) == targets).sum())
        self.model.train()
        mean = total_loss / len(images)
        if not math.isfinite(mean):
            raise NonFiniteError("evaluation loss is not finite")
        return mean, correct / len(images)

    def metrics(self, model_checksum: str) -> dict:
        train_loss, _ = self._evaluate(self.train_images, self.train_labels)
        eval_loss, eval_accuracy = self._evaluate(self.eval_images, self.eval_labels)
        buffers = self._momentum_buffers()
        return {
            "schema_version": 1,
            "format": METRICS_FORMAT,
            "architecture_id": arch.ARCHITECTURE_ID,
            "input_checksum": self.input_checksum,
            "spec_checksum": self.spec_checksum,
            "model_checksum": model_checksum,
            "epochs": self.params.epochs,
            "steps": self.step,
            "subset_size": self.params.subset_size,
            "batch_size": self.params.batch_size,
            "eval_items": len(self.eval_images),
            "train_loss": train_loss,
            "eval_loss": eval_loss,
            "eval_accuracy": eval_accuracy,
            "model_l2_norm": l2_norm(self.model.parameters()),
            "optimizer_momentum_l2_norm": l2_norm(buffers.values()),
            "parameter_l2_norms": {
                name: l2_norm([parameter]) for name, parameter in self.model.named_parameters()
            },
            "epoch_sample_order_digests": list(self.completed),
        }


def run_training(
    *,
    dataset_path: str | Path,
    output_dir: str | Path,
    params: TrainingParams,
    threads: int,
    spec_checksum: str,
    state_output: str | Path | None = None,
    resume_dir: str | Path | None = None,
    state_interval_seconds: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    trainer = Trainer(
        dataset_path=dataset_path, params=params, threads=threads, spec_checksum=spec_checksum
    )
    if resume_dir is not None:
        trainer.restore(resume_dir)
    observer = None
    if state_output is not None:
        state_path = Path(state_output)
        last_written: list[float] = []

        def observer(current: Trainer) -> None:
            now = clock()
            if (
                last_written
                and not current.finished
                and now - last_written[0] < state_interval_seconds
            ):
                return
            atomic_write(state_path, current.snapshot_bytes())
            last_written[:] = [now]

    trainer.train(observer)
    output = Path(output_dir)
    model_bytes = encode_model(trainer.model)
    atomic_write(output / "model.safetensors", model_bytes)
    metrics = trainer.metrics(file_checksum(output / "model.safetensors"))
    atomic_write(output / "metrics.json", canonical_json(metrics))
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the allowlisted PyTorch CIFAR-10 training")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--epochs", type=int, required=True)
    parser.add_argument("--batch-size", type=int, required=True)
    parser.add_argument("--learning-rate", type=float, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--subset-size", type=int, required=True)
    parser.add_argument("--threads", type=int, required=True)
    parser.add_argument("--spec-checksum", required=True)
    parser.add_argument("--state-output")
    parser.add_argument("--resume-dir")
    args = parser.parse_args(argv)
    try:
        run_training(
            dataset_path=args.dataset,
            output_dir=args.output_dir,
            params=TrainingParams(
                epochs=args.epochs,
                batch_size=args.batch_size,
                learning_rate=args.learning_rate,
                seed=args.seed,
                subset_size=args.subset_size,
            ),
            threads=args.threads,
            spec_checksum=args.spec_checksum,
            state_output=args.state_output,
            resume_dir=args.resume_dir,
        )
    except WorkloadInputError as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return EXIT_INVALID_INPUT
    except InternalWorkloadError as exc:
        print(f"internal: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    return 0


__all__ = ["TrainingParams", "Trainer", "main", "run_training"]


if __name__ == "__main__":
    raise SystemExit(main())
