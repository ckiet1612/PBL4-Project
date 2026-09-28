"""B16 inference attempt end to end: production worker loop, real runner, server validators.

Only Docker and HTTP are faked (the B14 harness). Publish runs the server checkpoint
validator, the inference state check and the chunk-output manifest check a restore later
repeats; complete runs the HTTP JSON codec, the server result validator and the summary
check, so what the worker sends is what the API accepts.
"""

import hashlib
import json
from uuid import UUID

import pytest

from nexa.application.checkpoint_validation import (
    check_chunk_manifest,
    validate_checkpoint_manifest,
    validate_inference_state_files,
)
from nexa.application.json_codec import decode_json_object
from nexa.application.result_validation import validate_inference_summary, validate_result_manifest
from nexa.worker.docker_client import DockerCli
from nexa.workloads import adapter_launch, chunk_manifest, inference_state
from nexa.workloads.canonical_json import canonical_json
from tests.worker import b16_claims as c
from tests.worker.test_checkpoint_flow_b14 import (
    Api,
    Backend,
    Harness,
    calls,
    server_compatibility,
    server_provenance,
)
from tests.workloads import b16_helpers as h

STATE = adapter_launch.INFERENCE_STATE_PATH.lstrip("/")


class InferenceApi(Api):
    """The B14 API fake with the B16 server validators on publish and complete."""

    def __init__(self, context, *, blobs=None):
        super().__init__(
            context, blobs={c.DATASET_ID: c.DATASET, c.MODEL_ID: c.MODEL, **(blobs or {})}
        )
        self.bindings = []
        self.chunk_entries = {}
        self.result_reservations = {}
        self.model_checksum = context["input_artifacts"][1]["checksum"]

    def _chunks(self, manifest, state):
        reference = manifest["chunk_output_manifest"]
        artifact = self.artifacts[reference["artifact_id"]]
        entries = check_chunk_manifest(
            self.blobs[reference["artifact_id"]],
            artifact=artifact,
            reference=reference,
            provenance=manifest["provenance"],
            model_checksum=self.model_checksum,
            state=state,
            chunk_total=state.get("next_chunk", state.get("chunk_count")),
        )
        for entry in entries:
            # The committed chunk bytes are exactly what the manifest names.
            body = self.blobs[entry["file"]["artifact_id"]]
            assert (
                self.artifacts[entry["file"]["artifact_id"]]["checksum"]
                == entry["file"]["checksum"]
            )
            assert len(body) == entry["file"]["size_bytes"]
        return entries

    def publish_checkpoint(self, attempt, callback, body):
        if callback not in self.published and self.publish_status is None:
            (reservation,) = [
                item
                for item in self.reservations.values()
                if item["checkpoint_id"] == body["manifest"]["checkpoint_id"]
            ]
            manifest_id = body["manifest_artifact_id"]
            manifest = body["manifest"]
            files = validate_checkpoint_manifest(
                manifest,
                raw=self.blobs[manifest_id],
                artifact=self.artifacts[manifest_id],
                checkpoint_id=UUID(reservation["checkpoint_id"]),
                sequence=reservation["sequence"],
                provenance=server_provenance(self.context, attempt, body["authority"]["job_fence"]),
                compatibility=server_compatibility(self.context),
                parameters=self.context["spec"]["parameters"],
            )
            state = validate_inference_state_files(
                {entry["logical_name"]: self.blobs[entry["artifact_id"]] for entry in files},
                manifest=manifest,
                parameters=self.context["spec"]["parameters"],
                model_checksum=self.model_checksum,
            )
            self.chunk_entries[reservation["sequence"]] = self._chunks(manifest, state)
            self.published[callback] = {
                "checkpoint_id": reservation["checkpoint_id"],
                "job_id": self.context["job_id"],
                "attempt_id": attempt,
                "sequence": reservation["sequence"],
                "manifest_artifact_id": manifest_id,
                "manifest_checksum": self.artifacts[manifest_id]["checksum"],
                "state": "COMMITTED",
                "created_at": "2026-09-28T00:00:01.000Z",
                "manifest": manifest,
            }
        return super().publish_checkpoint(attempt, callback, body)

    def reserve_result(self, attempt, callback, body):
        reserved = super().reserve_result(attempt, callback, body)
        self.result_reservations[attempt] = reserved["result_id"]
        return reserved

    def complete(self, attempt, callback, body):
        wire = decode_json_object(json.dumps(body).encode(), max_bytes=4 * 1024 * 1024)
        manifest_id = wire["result_manifest_artifact_id"]
        manifest = wire["manifest"]
        parameters = self.context["spec"]["parameters"]
        (summary,) = validate_result_manifest(
            manifest,
            raw=self.blobs[manifest_id],
            artifact=self.artifacts[manifest_id],
            expected_result_id=UUID(self.result_reservations[attempt]),
            provenance=server_provenance(self.context, attempt, body["authority"]["job_fence"]),
            parameters=parameters,
            model_checksum=self.model_checksum,
        )
        document = validate_inference_summary(
            self.blobs[summary["artifact_id"]],
            manifest=manifest,
            parameters=parameters,
            model_checksum=self.model_checksum,
        )
        self.chunk_entries["result"] = self._chunks(manifest, document)
        self.bindings.append(summary)
        return super().complete(attempt, callback, body)


def _harness(tmp_path, context):
    backend = Backend(tmp_path / "fs" / "output", labels=c.INFERENCE_LABELS)
    harness = Harness(tmp_path, context, api=InferenceApi(context), backend=backend)
    # FakeDocker does not bind-mount: the runner digests the model where the mount points.
    (harness.fs / "input" / "model.safetensors").write_bytes(c.MODEL)
    return harness


def _started(tmp_path, context=None):
    harness = _harness(tmp_path, context or c.inference_claim())
    harness.dispatch()
    assert harness.api.failures == []
    assert harness.launch_spec()["schema_version"] == 3
    harness.runner.mark_workload_started()
    harness.runner.emit_progress(fraction=0.0, step=0)
    harness.cycle(2)
    return harness


def _write(harness, next_chunk, *, chunks=(), fill=0):
    """The workload wrote these chunk files, then advanced its state cursor."""
    bodies = {}
    for index in chunks:
        body = h.chunk_body(index, fill)
        name = inference_state.chunk_file_name(index, "JSONL")
        (harness.fs / "output" / name).write_bytes(body)
        bodies[name] = body
    document = c.inference_job_document(harness.context, next_chunk)
    (harness.fs / STATE).write_bytes(canonical_json(document))
    return bodies, document


def _chunk_files(harness):
    return sorted(p.name for p in (harness.fs / "output").iterdir() if p.name.startswith("chunk-"))


def _uploads(harness):
    return [kind for name, kind in harness.api.calls if name == "upload"]


def _checkpoint(harness, next_chunk, chunks, *, fill=0):
    bodies, _ = _write(harness, next_chunk, chunks=chunks, fill=fill)
    harness.advance(6)
    harness.cycle(12)
    assert harness.api.failures == []
    return bodies


def test_inference_attempt_publishes_chunked_checkpoints_then_the_result(tmp_path):
    harness = _started(tmp_path)
    first = _checkpoint(harness, 2, (0, 1))
    (published,) = harness.api.published.values()
    manifest = published["manifest"]
    assert manifest["cursor"] == {"step": 2, "epoch": 0, "item_cursor": 600}
    assert [entry["logical_name"] for entry in manifest["files"]] == ["inference-state.json"]
    reference = manifest["chunk_output_manifest"]
    assert harness.api.artifacts[reference["artifact_id"]]["kind"] == "CHUNK_OUTPUT_MANIFEST"
    # Chunks, then the state, then the chunk-output manifest, then the checkpoint manifest.
    assert _uploads(harness) == [
        "RESULT_FILE",
        "RESULT_FILE",
        "CHECKPOINT_FILE",
        "CHUNK_OUTPUT_MANIFEST",
        "CHECKPOINT_MANIFEST",
    ]
    entries = harness.api.chunk_entries[1]
    assert [harness.api.blobs[e["file"]["artifact_id"]] for e in entries] == list(first.values())
    assert {e["source_attempt_id"] for e in entries} == {harness.attempt_id}
    # Bound chunk files leave the bounded scratch; unbound ones stay for the next cycle.
    assert _chunk_files(harness) == []

    _checkpoint(harness, 3, (2,), fill=1)
    assert sorted(p["sequence"] for p in harness.api.published.values()) == [1, 2]
    second = harness.api.chunk_entries[2]
    # The second checkpoint carries the first cycle's chunks forward unchanged.
    assert second[:2] == entries
    assert harness.flow_state()["checkpoint_flow"]["last_outcome"]["outcome"] == "COMMITTED"

    _, document = _write(harness, 4, chunks=(3,), fill=2)
    summary = canonical_json(inference_state.summary_from_state(document))
    (harness.fs / "output" / "summary.json").write_bytes(summary)
    harness.runner._finish_adapter_workload(0)
    harness.cycle(12)
    assert harness.api.failures == []
    (binding,) = harness.api.bindings
    assert binding["logical_name"] == "summary.json"
    assert harness.api.blobs[binding["artifact_id"]] == summary
    result = harness.api.chunk_entries["result"]
    assert [e["chunk_id"] for e in result] == [inference_state.chunk_id(i) for i in range(4)]
    assert result[:3] == second
    assert len(calls(harness.api, "complete")) == 1
    # Every chunk was uploaded once across two checkpoints and the result.
    assert _uploads(harness).count("RESULT_FILE") == 4 + 1


def test_chunk_batches_replay_from_the_journal_after_the_files_are_unlinked(tmp_path):
    harness = _started(tmp_path)
    harness.fail_send.add("BIND_ARTIFACT_BATCH")
    _write(harness, 1, chunks=(0,))
    harness.advance(6)
    for _ in range(12):
        harness.cycle(1, tolerate=(OSError,))
        if "BIND_ARTIFACT_BATCH" not in harness.fail_send:
            break
    # The bind was lost after the chunk binding was journaled; the file is then gone.
    assert len(harness.flow_state()["inference_chunks"]) == 1
    (harness.fs / "output" / "chunk-00000000.jsonl").unlink()
    harness.cycle(12)
    assert harness.api.failures == []
    (published,) = harness.api.published.values()
    assert published["manifest"]["cursor"]["step"] == 1
    assert _uploads(harness).count("RESULT_FILE") == 1


@pytest.mark.parametrize("tamper", ["body", "extra-state-chunk"])
def test_chunk_graph_defects_fail_the_attempt_closed(tmp_path, tamper, monkeypatch):
    harness = _started(tmp_path)
    if tamper == "body":
        # The runner's descriptor names bytes the container no longer holds.
        original = DockerCli.read_output

        def read_output(self, container_id, descriptor):
            content = original(self, container_id, descriptor)
            return content + b"x" if descriptor["kind"] == "RESULT_FILE" else content

        monkeypatch.setattr(DockerCli, "read_output", read_output)
        _write(harness, 1, chunks=(0,))
    else:
        # The state claims two chunks but only one file was written and bound.
        _write(harness, 1, chunks=(0,))
        (harness.fs / STATE).write_bytes(
            canonical_json(c.inference_job_document(harness.context, 2))
        )
    harness.advance(6)
    harness.cycle(12)
    assert harness.api.published == {}
    assert [(f["failure_class"], f["reason_code"]) for f in harness.api.failures] == [
        ("INTERNAL", "CHECKPOINT_PROTOCOL_ERROR")
    ]


def test_published_inference_checkpoint_restores_into_a_verified_launch(tmp_path):
    first = _started(tmp_path / "first")
    _checkpoint(first, 2, (0, 1))
    (published,) = first.api.published.values()
    manifest = published["manifest"]
    record = {key: value for key, value in published.items() if key != "manifest"}
    reference = manifest["chunk_output_manifest"]
    ids = [entry["artifact_id"] for entry in manifest["files"]] + [reference["artifact_id"]]
    restore = {
        "record": record,
        "manifest": manifest,
        "files": [first.api.artifacts[artifact_id] for artifact_id in ids],
    }
    context = c.inference_claim(restore=restore)
    blobs = {artifact_id: first.api.blobs[artifact_id] for artifact_id in ids}
    api = InferenceApi(context, blobs=blobs)
    # Carried-forward chunks stay committed artifacts of the same tenant.
    api.blobs.update(first.api.blobs)
    api.artifacts.update(first.api.artifacts)
    second = Harness(
        tmp_path / "second",
        context,
        api=api,
        backend=Backend(tmp_path / "second" / "fs" / "output", labels=c.INFERENCE_LABELS),
    )
    (second.fs / "input" / "model.safetensors").write_bytes(c.MODEL)
    directory = second.fs / adapter_launch.RESTORE_DIR.lstrip("/")
    directory.mkdir(parents=True)
    (directory / "inference-state.json").write_bytes(blobs[ids[0]])
    (directory / chunk_manifest.LOGICAL_NAME).write_bytes(blobs[ids[1]])
    second.dispatch()
    assert second.api.failures == []
    downloads = [artifact for name, artifact in second.api.calls if name == "download"]
    assert sorted(downloads) == sorted([c.DATASET_ID, c.MODEL_ID, *ids])
    spec = second.launch_spec()
    assert spec["restore"]["cursor"] == manifest["cursor"]
    assert [f["logical_name"] for f in spec["restore"]["files"]] == [
        "inference-state.json",
        chunk_manifest.LOGICAL_NAME,
    ]
    second.cycle(1)
    assert second.runner._restore_state_is_valid() is True
    command = second.runner._launch_command()
    assert command[-2:] == ("--resume-state", "/input/restore/inference-state.json")

    # The resumed attempt uploads only chunk 2 and carries chunks 0-1 forward.
    second.runner.mark_workload_started()
    second.runner.emit_progress(fraction=0.5, step=2)
    second.cycle(2)
    _checkpoint(second, 3, (2,))
    (resumed,) = second.api.chunk_entries.values()
    carried = first.api.chunk_entries[1]
    assert resumed[:2] == carried
    assert resumed[2]["source_attempt_id"] == second.attempt_id
    assert _uploads(second).count("RESULT_FILE") == 1


@pytest.mark.parametrize("defect", ["state", "chunk-manifest", "reference"])
def test_inference_restore_defects_are_unavailable(tmp_path, defect):
    first = _started(tmp_path / "first")
    _checkpoint(first, 2, (0, 1))
    (published,) = first.api.published.values()
    manifest = published["manifest"]
    record = {key: value for key, value in published.items() if key != "manifest"}
    reference = manifest["chunk_output_manifest"]
    ids = [entry["artifact_id"] for entry in manifest["files"]] + [reference["artifact_id"]]
    views = [dict(first.api.artifacts[artifact_id]) for artifact_id in ids]
    blobs = {artifact_id: first.api.blobs[artifact_id] for artifact_id in ids}
    if defect == "reference":
        views[1]["kind"] = "CHECKPOINT_FILE"
    elif defect == "state":
        document = c.inference_job_document(first.context, 1)
        blobs[ids[0]] = canonical_json(document)
    else:
        blobs[ids[1]] = blobs[ids[1]].replace(b'"chunk-00000000', b'"chunk-0000000X')
    restore = {"record": record, "manifest": manifest, "files": views}
    context = c.inference_claim(restore=restore)
    api = InferenceApi(context, blobs=blobs)
    # Downloads verify size/checksum against the view; serve the tampered bytes as-is.
    for view, artifact_id in zip(views, ids, strict=True):
        view["size_bytes"] = len(blobs[artifact_id])
        view["checksum"] = "sha256:" + hashlib.sha256(blobs[artifact_id]).hexdigest()
    second = Harness(
        tmp_path / "second",
        context,
        api=api,
        backend=Backend(tmp_path / "second" / "fs" / "output", labels=c.INFERENCE_LABELS),
    )
    second.dispatch()
    assert [(f["failure_class"], f["reason_code"]) for f in second.api.failures] == [
        ("INCOMPATIBLE", "CHECKPOINT_RESTORE_UNAVAILABLE")
    ]
    assert second.backend.create_argv is None


def test_inference_without_checkpoints_uploads_every_chunk_with_the_result(tmp_path):
    harness = _started(tmp_path, c.inference_claim(checkpointable=False))
    assert harness.launch_spec()["checkpoint"] is None
    assert "--window" not in harness.runner._launch_command()
    bodies, document = _write(harness, 4, chunks=range(4))
    (harness.fs / "output" / "summary.json").write_bytes(
        canonical_json(inference_state.summary_from_state(document))
    )
    harness.runner._finish_adapter_workload(0)
    harness.cycle(12)
    assert harness.api.failures == []
    assert harness.api.published == {}
    assert _uploads(harness) == [
        *["RESULT_FILE"] * 4,
        "RESULT_FILE",
        "CHUNK_OUTPUT_MANIFEST",
        "RESULT_MANIFEST",
    ]
    result = harness.api.chunk_entries["result"]
    assert [harness.api.blobs[e["file"]["artifact_id"]] for e in result] == list(bodies.values())
    assert len(calls(harness.api, "complete")) == 1
