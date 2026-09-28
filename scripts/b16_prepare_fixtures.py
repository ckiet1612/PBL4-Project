"""Prepare the B16 CIFAR-10 dataset and model fixtures outside the repository (VPS1 only).

``download`` / ``verify-archive`` use only the stdlib (official binary archive, MD5 check,
SHA-256 record; the pickled Python edition is never touched). ``build`` runs inside the
PyTorch image with ``--network none``: it streams 3073-byte records from the read-only
archive, selects deterministic subsets, writes Arrow IPC files with ``nexa.dataset`` schema
metadata and trains the inference model fixture at one thread. Re-running ``build`` yields
byte-identical files; the printed JSON lists their checksums.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import tarfile
import urllib.request
from pathlib import Path

ARCHIVE_URL = "https://www.cs.toronto.edu/~kriz/cifar-10-binary.tar.gz"
ARCHIVE_MD5 = "c32a1d4ab5d03f1284b67883e8d87530"
TRAIN_ITEMS = 50_000
TRAIN_FILE = "cifar10-train-5000-eval-1000.arrow"
INFERENCE_FILE = "cifar10-inference-2000.arrow"
MODEL_FILE = "cifar10-smallcnn-v1.safetensors"
# Model fixture: the training workload itself, fixed parameters, one thread.
MODEL_PARAMS = {
    "epochs": 2,
    "batch_size": 64,
    "learning_rate": 0.05,
    "seed": 16,
    "subset_size": 5000,
}
MODEL_SPEC_CHECKSUM = "sha256:" + hashlib.sha256(b"nexa-b16-model-fixture-v1").hexdigest()


def archive_digests(path: Path) -> dict[str, str]:
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            md5.update(chunk)
            sha256.update(chunk)
    return {"archive_md5": md5.hexdigest(), "archive_sha256": "sha256:" + sha256.hexdigest()}


def verify_archive(path: Path) -> dict[str, str]:
    digests = archive_digests(path)
    if digests["archive_md5"] != ARCHIVE_MD5:
        raise SystemExit("archive MD5 does not match the published value")
    return digests


def download(target: Path) -> dict[str, str]:
    if not target.exists():
        partial = target.with_suffix(target.suffix + ".partial")
        with urllib.request.urlopen(ARCHIVE_URL, timeout=60) as response, partial.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
        partial.replace(target)
    return verify_archive(target)


def _selection(seed: int, train_count: int, eval_count: int, inference_count: int):
    picked = random.Random(f"nexa-b16-fixture:{seed}:train-eval").sample(
        range(TRAIN_ITEMS), train_count + eval_count
    )
    train = sorted(picked[:train_count])
    evaluation = sorted(picked[train_count:])
    inference = sorted(
        TRAIN_ITEMS + index
        for index in random.Random(f"nexa-b16-fixture:{seed}:inference").sample(
            range(10_000), inference_count
        )
    )
    return train, evaluation, inference


def read_records(archive: Path, wanted: set[int]) -> dict[int, tuple[int, bytes]]:
    """Stream the archive once; keep only wanted records as raw uint8 bytes."""
    from nexa.workloads.dataset_format import RECORD_BYTES, RECORDS_PER_MEMBER, SOURCE_MEMBERS

    rows: dict[int, tuple[int, bytes]] = {}
    seen = set()
    with tarfile.open(archive, mode="r|gz") as stream:
        for member in stream:
            if member.name not in SOURCE_MEMBERS:
                continue
            if not member.isreg() or member.size != RECORD_BYTES * RECORDS_PER_MEMBER:
                raise SystemExit(f"archive member {member.name} has an unexpected layout")
            base = SOURCE_MEMBERS.index(member.name) * RECORDS_PER_MEMBER
            handle = stream.extractfile(member)
            for offset in range(RECORDS_PER_MEMBER):
                record = handle.read(RECORD_BYTES)
                if len(record) != RECORD_BYTES or record[0] > 9:
                    raise SystemExit(f"archive member {member.name} has a malformed record")
                if base + offset in wanted:
                    rows[base + offset] = (record[0], record[1:])
            seen.add(member.name)
    if seen != set(SOURCE_MEMBERS):
        raise SystemExit("archive is missing CIFAR-10 binary members")
    return rows


def write_dataset(
    path: Path,
    *,
    splits: list[tuple[str, list[int]]],
    rows: dict[int, tuple[int, bytes]],
    seed: int,
    digests: dict[str, str],
) -> None:
    import pyarrow as pa

    from nexa.workloads.canonical_json import canonical_json
    from nexa.workloads.dataset_format import (
        METADATA_KEY,
        SOURCE_MEMBERS,
        RowDigest,
        parse_metadata,
    )
    from nexa.workloads.pytorch_arch import IMAGE_BYTES, NUM_CLASSES, PREPROCESSING

    order = [index for _, indices in splits for index in indices]
    digest = RowDigest()
    counts = {}
    bounds = {}
    cursor = 0
    for name, indices in splits:
        counts[name] = [0] * NUM_CLASSES
        for index in indices:
            counts[name][rows[index][0]] += 1
        bounds[name] = [cursor, cursor + len(indices)]
        cursor += len(indices)
    for index in order:
        digest.update(index, *rows[index])
    metadata = {
        "schema_version": 1,
        "kind": "cifar10-subset",
        "preprocessing": PREPROCESSING,
        "source": {
            "name": "cifar-10-binary",
            "archive_md5": digests["archive_md5"],
            "archive_sha256": digests["archive_sha256"],
            "members": list(SOURCE_MEMBERS),
        },
        "seed": seed,
        "item_count": len(order),
        "splits": bounds,
        "indices_checksum": digest.indices_checksum,
        "class_counts": counts,
        "content_checksum": digest.content_checksum,
    }
    raw = canonical_json(metadata)
    parse_metadata(raw)
    schema = pa.schema(
        [
            pa.field("source_index", pa.uint32(), nullable=False),
            pa.field("label", pa.uint8(), nullable=False),
            pa.field("image", pa.binary(IMAGE_BYTES), nullable=False),
        ],
        metadata={METADATA_KEY: raw},
    )
    batch = pa.record_batch(
        [
            pa.array(order, type=pa.uint32()),
            pa.array([rows[index][0] for index in order], type=pa.uint8()),
            pa.array([rows[index][1] for index in order], type=pa.binary(IMAGE_BYTES)),
        ],
        schema=schema,
    )
    partial = path.with_suffix(".partial")
    with pa.OSFile(str(partial), "wb") as sink, pa.ipc.new_file(sink, schema) as writer:
        writer.write_batch(batch)
    partial.replace(path)


def build_model(dataset: Path, target: Path) -> None:
    from nexa.workloads.pytorch_cifar10 import Trainer, TrainingParams
    from nexa.workloads.torch_common import atomic_write, encode_model

    trainer = Trainer(
        dataset_path=dataset,
        params=TrainingParams(**MODEL_PARAMS),
        threads=1,
        spec_checksum=MODEL_SPEC_CHECKSUM,
    )
    trainer.train()
    atomic_write(target, encode_model(trainer.model))


def _describe(path: Path) -> dict[str, object]:
    return {
        "file": path.name,
        "size_bytes": path.stat().st_size,
        "checksum": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def build(archive: Path, output: Path, seed: int) -> dict[str, object]:
    from nexa.workloads import pytorch_arch, safetensors_format

    digests = verify_archive(archive)
    train, evaluation, inference = _selection(seed, 5000, 1000, 2000)
    rows = read_records(archive, set(train) | set(evaluation) | set(inference))
    output.mkdir(parents=True, exist_ok=True)
    train_path = output / TRAIN_FILE
    inference_path = output / INFERENCE_FILE
    model_path = output / MODEL_FILE
    write_dataset(
        train_path,
        splits=[("train", train), ("eval", evaluation)],
        rows=rows,
        seed=seed,
        digests=digests,
    )
    write_dataset(
        inference_path, splits=[("inference", inference)], rows=rows, seed=seed, digests=digests
    )
    build_model(train_path, model_path)
    pytorch_arch.validate_model_header(safetensors_format.read_header(model_path))
    for path in (train_path, inference_path, model_path):
        # Fixtures are read-only workload inputs mounted into containers under other uids.
        path.chmod(0o444)
    return {
        "archive": {"file": archive.name, **digests},
        "seed": seed,
        "model_params": MODEL_PARAMS,
        "files": [_describe(path) for path in (train_path, inference_path, model_path)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("download")
    fetch.add_argument("--archive", type=Path, required=True)
    check = commands.add_parser("verify-archive")
    check.add_argument("--archive", type=Path, required=True)
    make = commands.add_parser("build")
    make.add_argument("--archive", type=Path, required=True)
    make.add_argument("--output-dir", type=Path, required=True)
    make.add_argument("--seed", type=int, default=16)
    args = parser.parse_args(argv)
    if args.command == "download":
        report = download(args.archive)
    elif args.command == "verify-archive":
        report = verify_archive(args.archive)
    else:
        report = build(args.archive, args.output_dir, args.seed)
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
