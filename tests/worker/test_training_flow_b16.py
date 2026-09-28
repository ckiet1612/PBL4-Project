"""B16 training attempt end to end: production worker loop, real runner, server validators.

Only Docker and HTTP are faked (the B14 harness). Publish runs the server checkpoint
validator and the restore-time state check; complete runs the HTTP JSON codec and the
server result validator, so what the worker sends is what the API accepts.
"""

import json
from uuid import UUID

from nexa.application.checkpoint_validation import (
    validate_checkpoint_manifest,
    validate_training_state_files,
)
from nexa.application.json_codec import decode_json_object
from nexa.application.result_validation import validate_result_manifest
from nexa.domain import workload_adapters
from nexa.workloads import adapter_launch, training_state
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

ADAPTER = workload_adapters.PYTORCH_CIFAR10
NAMES = [rule.logical_name for rule in ADAPTER.checkpoint_files]


class TrainingApi(Api):
    """The B14 API fake with the B16 server validators on publish and complete."""

    def __init__(self, context, *, blobs=None):
        super().__init__(context, blobs={c.DATASET_ID: c.DATASET, **(blobs or {})})
        self.bindings = []
        self.result_reservations = {}

    def publish_checkpoint(self, attempt, callback, body):
        if callback not in self.published and self.publish_status is None:
            (reservation,) = [
                item
                for item in self.reservations.values()
                if item["checkpoint_id"] == body["manifest"]["checkpoint_id"]
            ]
            manifest_id = body["manifest_artifact_id"]
            files = validate_checkpoint_manifest(
                body["manifest"],
                raw=self.blobs[manifest_id],
                artifact=self.artifacts[manifest_id],
                checkpoint_id=UUID(reservation["checkpoint_id"]),
                sequence=reservation["sequence"],
                provenance=server_provenance(self.context, attempt, body["authority"]["job_fence"]),
                compatibility=server_compatibility(self.context),
                parameters=self.context["spec"]["parameters"],
            )
            # What restore selection later verifies from the same committed bytes.
            validate_training_state_files(
                {entry["logical_name"]: self.blobs[entry["artifact_id"]] for entry in files},
                manifest=body["manifest"],
                parameters=self.context["spec"]["parameters"],
            )
            self.published[callback] = {
                "checkpoint_id": reservation["checkpoint_id"],
                "job_id": self.context["job_id"],
                "attempt_id": attempt,
                "sequence": reservation["sequence"],
                "manifest_artifact_id": manifest_id,
                "manifest_checksum": self.artifacts[manifest_id]["checksum"],
                "state": "COMMITTED",
                "created_at": "2026-09-28T00:00:01.000Z",
                "manifest": body["manifest"],
            }
        return super().publish_checkpoint(attempt, callback, body)

    def reserve_result(self, attempt, callback, body):
        reserved = super().reserve_result(attempt, callback, body)
        self.result_reservations[attempt] = reserved["result_id"]
        return reserved

    def complete(self, attempt, callback, body):
        # The HTTP codec decodes the body before the service sees it.
        wire = decode_json_object(json.dumps(body).encode(), max_bytes=4 * 1024 * 1024)
        manifest_id = wire["result_manifest_artifact_id"]
        self.bindings.append(
            validate_result_manifest(
                wire["manifest"],
                raw=self.blobs[manifest_id],
                artifact=self.artifacts[manifest_id],
                expected_result_id=UUID(self.result_reservations[attempt]),
                provenance=server_provenance(self.context, attempt, body["authority"]["job_fence"]),
                parameters=self.context["spec"]["parameters"],
            )
        )
        return super().complete(attempt, callback, body)


def _harness(tmp_path, context):
    backend = Backend(tmp_path / "fs" / "output", labels=c.TRAINING_LABELS)
    return Harness(tmp_path, context, api=TrainingApi(context), backend=backend)


def _write_snapshot(harness, step, *, fill=0):
    raw = h.snapshot_bytes(c.job_document(harness.context, step), fill=fill)
    path = harness.fs / adapter_launch.TRAINING_STATE_PATH.lstrip("/")
    path.write_bytes(raw)
    return training_state.split_snapshot(raw)[1]


def _write_result(harness):
    model = h.model_bytes(fill=2)
    document = h.metrics_document(model, harness.context["spec"]["parameters"])
    document["input_checksum"] = harness.context["input_artifacts"][0]["checksum"]
    document["spec_checksum"] = c.job_document(harness.context, 0)["spec_checksum"]
    raw = canonical_json(document)
    (harness.fs / "output" / "model.safetensors").write_bytes(model)
    (harness.fs / "output" / "metrics.json").write_bytes(raw)
    return model, raw


def _started(tmp_path, context=None):
    harness = _harness(tmp_path, context or c.training_claim())
    harness.dispatch()
    assert harness.api.failures == []
    assert harness.launch_spec()["schema_version"] == 3
    harness.runner.mark_workload_started()
    harness.runner.emit_progress(fraction=0.0, step=0)
    harness.cycle(2)
    return harness


def test_training_attempt_publishes_server_valid_checkpoints_then_the_result(tmp_path):
    harness = _started(tmp_path)
    first = _write_snapshot(harness, 3)
    harness.advance(6)
    harness.cycle(8)
    (published,) = harness.api.published.values()
    manifest = published["manifest"]
    assert [entry["logical_name"] for entry in manifest["files"]] == NAMES
    assert {
        entry["logical_name"]: harness.api.blobs[entry["artifact_id"]]
        for entry in manifest["files"]
    } == first
    assert manifest["cursor"]["step"] == 3
    assert [kind for name, kind in harness.api.calls if name == "upload"] == [
        *["CHECKPOINT_FILE"] * 4,
        "CHECKPOINT_MANIFEST",
    ]

    _write_snapshot(harness, 7, fill=1)
    harness.advance(6)
    harness.cycle(8)
    assert sorted(p["sequence"] for p in harness.api.published.values()) == [1, 2]
    assert harness.flow_state()["checkpoint_flow"]["last_outcome"]["outcome"] == "COMMITTED"

    model, metrics = _write_result(harness)
    harness.runner._finish_adapter_workload(0)
    harness.cycle(10)
    assert harness.api.failures == []
    (bindings,) = harness.api.bindings
    assert [(b["logical_name"], b["media_type"]) for b in bindings] == [
        (rule.logical_name, rule.media_type) for rule in ADAPTER.result_files
    ]
    assert [harness.api.blobs[b["artifact_id"]] for b in bindings] == [model, metrics]
    assert len(calls(harness.api, "complete")) == 1
    assert [cleanup["proof"]["container"]["container_id"] for cleanup in harness.api.cleanups] == [
        harness.backend.container_id
    ]


def test_published_training_checkpoint_restores_into_a_verified_launch(tmp_path):
    first = _started(tmp_path / "first")
    files = _write_snapshot(first, 5)
    first.advance(6)
    first.cycle(8)
    (published,) = first.api.published.values()
    manifest = published["manifest"]
    record = {key: value for key, value in published.items() if key != "manifest"}
    restore = {
        "record": record,
        "manifest": manifest,
        "files": [first.api.artifacts[entry["artifact_id"]] for entry in manifest["files"]],
    }

    context = c.training_claim(restore=restore)
    blobs = {
        entry["artifact_id"]: first.api.blobs[entry["artifact_id"]] for entry in manifest["files"]
    }
    second = Harness(
        tmp_path / "second",
        context,
        api=TrainingApi(context, blobs=blobs),
        backend=Backend(tmp_path / "second" / "fs" / "output", labels=c.TRAINING_LABELS),
    )
    # FakeDocker does not bind-mount: place the downloaded files where the mounts point.
    directory = second.fs / adapter_launch.RESTORE_DIR.lstrip("/")
    directory.mkdir(parents=True)
    for name, body in files.items():
        (directory / name).write_bytes(body)
    second.dispatch()
    assert second.api.failures == []
    downloads = [artifact for name, artifact in second.api.calls if name == "download"]
    assert sorted(downloads) == sorted(
        [c.DATASET_ID, *(entry["artifact_id"] for entry in manifest["files"])]
    )
    spec = second.launch_spec()
    assert spec["restore"]["cursor"] == manifest["cursor"]
    assert spec["restore"]["checkpoint_id"] == record["checkpoint_id"]
    second.cycle(1)
    assert second.runner._restore_state_is_valid() is True
    command = second.runner._launch_command()
    assert command[-2:] == ("--resume-dir", adapter_launch.RESTORE_DIR)
