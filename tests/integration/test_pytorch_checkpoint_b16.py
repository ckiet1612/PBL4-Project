"""B16 PyTorch checkpoint publish, restore and result completion over HTTP and PostgreSQL."""

import json
from uuid import UUID

import pytest
import rfc8785
from sqlalchemy import select

from nexa.domain import workload_adapters
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.workloads import adapter_launch, training_state
from nexa.workloads.canonical_json import canonical_json
from tests.api.test_http_contract import _client
from tests.integration.test_checkpoint_b14 import (
    CheckpointFixture,
    _checksum,
    _timestamp,
    _worker_headers,
)
from tests.integration.test_checkpoint_restore_b14 import (
    _blob_path,
    _claim,
    _corruptions,
    _next_attempt,
    _overwrite,
    _restore_events,
    _seed_admission_counters,
)
from tests.workloads import b16_helpers as h

pytestmark = pytest.mark.postgres

ADAPTER = workload_adapters.PYTORCH_CIFAR10
ARCH = "linux/amd64"
FRAMEWORK_VERSION = "2.12.1"
OCTET = "application/octet-stream"
JSON = "application/json"
REQUIREMENTS = {
    "architectures": [ARCH],
    "device": "CPU",
    "framework": "PYTORCH",
    "framework_version": FRAMEWORK_VERSION,
    "cuda_runtime_min": None,
    "driver_min": None,
    "compute_capability_min": None,
}


def _upload(fixture, kind, media_type, content, key, expected=201):
    response = fixture.client.post(
        f"{fixture.base}/artifacts",
        headers={
            **_worker_headers(fixture.credential, fixture.authority),
            "Content-Type": OCTET,
            "Idempotency-Key": key,
            "X-Artifact-Kind": kind,
            "X-Artifact-Media-Type": media_type,
            "X-Artifact-Checksum": _checksum(content),
            "X-Artifact-Size": str(len(content)),
        },
        content=content,
    )
    assert response.status_code == expected, response.text
    return response.json()


class TrainingFixture(CheckpointFixture):
    """One RUNNING attempt of the registered pytorch.cifar10 template on a DATASET input."""

    def __init__(self, engine, client, *, label, restart_safe=True):
        super().__init__(
            engine,
            client,
            label=label,
            restart_safe=restart_safe,
            template_id="pytorch-cifar10-cnn",
            template_values={"adapter_id": ADAPTER.adapter_id, "adapter_version": "1.0.0"},
            input_media_type="application/vnd.apache.arrow.file",
            input_kind="DATASET",
            requirements=REQUIREMENTS,
            parameters=dict(h.PARAMETERS),
        )

    def provenance(self, **changes):
        return super().provenance(adapter_id=ADAPTER.adapter_id, **changes)

    def document(self, step):
        document = h.training_document(step, h.PARAMETERS)
        document["input_checksum"] = self.input_checksum
        document["spec_checksum"] = self.spec_checksum
        return document

    def files(self, step, *, fill=0, document=None):
        raw = h.snapshot_bytes(document or self.document(step), fill=fill)
        return training_state.split_snapshot(raw)[1]

    def training_checkpoint(self, reservation, *, step=3, files=None, mutate=None):
        """Upload the four files and the sealed manifest exactly as the runner would."""
        files = self.files(step) if files is None else files
        key = f"b16-{reservation['checkpoint_id']}"
        names = [name for name, _ in training_state.CHECKPOINT_FILES] + [training_state.STATE_FILE]
        entries, artifacts = [], {}
        for name in names:
            media = JSON if name.endswith(".json") else OCTET
            artifact = _upload(self, "CHECKPOINT_FILE", media, files[name], f"{key}-{name}")
            artifacts[name] = artifact
            entries.append(
                {
                    "artifact_id": artifact["artifact_id"],
                    "logical_name": name,
                    "media_type": media,
                    "size_bytes": len(files[name]),
                    "checksum": _checksum(files[name]),
                }
            )
        manifest = {
            "kind": "CHECKPOINT",
            "schema_version": 1,
            "checkpoint_id": reservation["checkpoint_id"],
            "checkpoint_sequence": reservation["sequence"],
            "created_at": _timestamp(),
            "provenance": self.provenance(),
            "compatibility": adapter_launch.compatibility(
                ARCH, FRAMEWORK_VERSION, self.restart_safe
            ),
            "cursor": training_state.runtime_cursor(self.document(step)),
            "state_components": list(ADAPTER.state_components),
            "files": entries,
        }
        if mutate is not None:
            mutate(manifest)
        manifest["manifest_checksum"] = _checksum(rfc8785.dumps(manifest))
        manifest_artifact = _upload(
            self, "CHECKPOINT_MANIFEST", JSON, rfc8785.dumps(manifest), f"{key}-manifest"
        )
        return manifest, manifest_artifact, artifacts

    def commit(self, *, step, **options):
        reserved = self.reserve()
        assert reserved.status_code == 201, reserved.text
        manifest, manifest_artifact, artifacts = self.training_checkpoint(
            reserved.json(), step=step, **options
        )
        published = self.publish(manifest, manifest_artifact)
        assert published.status_code == 201, published.text
        return {
            "record": published.json(),
            "manifest": manifest,
            "manifest_artifact": manifest_artifact,
            "artifacts": artifacts,
        }


@pytest.fixture
def training_job(migrated_postgres_engine, tmp_path):
    with _client(migrated_postgres_engine, tmp_path) as client:
        yield TrainingFixture(migrated_postgres_engine, client, label="torch")


def test_publish_accepts_the_four_file_training_checkpoint(training_job):
    committed = training_job.commit(step=3)
    rows = training_job.rows()
    assert [r["state"] for r in rows["reservations"]] == ["COMMITTED"]
    assert [c["sequence"] for c in rows["checkpoints"]] == [1]
    references = [(r.purpose, r.logical_name, str(r.artifact_id)) for r in rows["references"]]
    assert sorted(references) == sorted(
        [("CHECKPOINT_MANIFEST", "manifest", committed["manifest_artifact"]["artifact_id"])]
        + [
            ("CHECKPOINT_FILE", name, artifact["artifact_id"])
            for name, artifact in committed["artifacts"].items()
        ]
    )
    assert [row.event_type for row in rows["events"]] == ["CHECKPOINT_COMMITTED"]
    assert rows["attempt"]["state"] == "RUNNING"


def _files(manifest):
    return manifest["files"]


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda m: m["cursor"].update(step=9), "CHECKPOINT_CURSOR_INVALID"),
        (lambda m: m["cursor"].update(item_cursor=0), "CHECKPOINT_CURSOR_INVALID"),
        (lambda m: m["cursor"].update(accumulator=1), "CHECKPOINT_CURSOR_INVALID"),
        (lambda m: m["state_components"].reverse(), "CHECKPOINT_MANIFEST_INVALID"),
        (lambda m: m.update(state_components=["ACCUMULATOR"]), "CHECKPOINT_MANIFEST_INVALID"),
        (lambda m: _files(m).reverse(), "CHECKPOINT_FILES_INVALID"),
        (lambda m: _files(m).pop(1), "CHECKPOINT_FILES_INVALID"),
        (
            lambda m: _files(m)[0].update(logical_name="weights.safetensors"),
            "CHECKPOINT_FILES_INVALID",
        ),
        (lambda m: _files(m)[3].update(checksum="sha256:" + "d" * 64), "CHECKPOINT_FILES_INVALID"),
        (
            lambda m: m["compatibility"].update(framework="PYTHON"),
            "CHECKPOINT_COMPATIBILITY_MISMATCH",
        ),
        (
            lambda m: m["compatibility"].update(architecture="linux/arm64"),
            "CHECKPOINT_COMPATIBILITY_MISMATCH",
        ),
        (
            lambda m: m["compatibility"].update(framework_version="2.12.0"),
            "CHECKPOINT_COMPATIBILITY_MISMATCH",
        ),
        (
            lambda m: m["provenance"].update(image_digest="sha256:" + "e" * 64),
            "CHECKPOINT_PROVENANCE_MISMATCH",
        ),
        (
            lambda m: m["provenance"].update(adapter_id="cpu.iterative"),
            "CHECKPOINT_PROVENANCE_MISMATCH",
        ),
    ],
)
def test_each_training_publish_defect_is_rejected_durably(training_job, mutate, reason):
    reservation = training_job.reserve().json()
    manifest, manifest_artifact, _ = training_job.training_checkpoint(reservation, mutate=mutate)
    response = training_job.publish(manifest, manifest_artifact)
    assert response.status_code == 422, response.text
    rows = training_job.rows()
    assert [r["state"] for r in rows["reservations"]] == ["REJECTED"]
    assert rows["checkpoints"] == [] and rows["references"] == []
    assert rows["events"] == [(1, "CHECKPOINT_REJECTED", reason)]
    assert rows["attempt"]["state"] == "RUNNING"


def test_oversized_tensor_file_is_rejected(training_job):
    files = training_job.files(3)
    files["model.safetensors"] += b"\x00" * (1024 * 1024)
    reservation = training_job.reserve().json()
    manifest, manifest_artifact, _ = training_job.training_checkpoint(reservation, files=files)
    response = training_job.publish(manifest, manifest_artifact)
    assert response.status_code == 422, response.text
    assert training_job.rows()["events"] == [(1, "CHECKPOINT_REJECTED", "CHECKPOINT_FILES_INVALID")]


def _download(fixture, artifact_id):
    response = fixture.client.get(
        f"{fixture.base}/execution-artifacts/{artifact_id}/content",
        headers=_worker_headers(fixture.credential, fixture.authority),
    )
    assert response.status_code == 200, response.text
    return response.content


def test_restore_verifies_every_file_and_replays_the_same_context(training_job):
    fixture = training_job
    fixture.commit(step=3)
    newest = fixture.commit(step=7, files=fixture.files(7, fill=1))
    _next_attempt(fixture)

    callback = str(new_uuid7())
    claimed = _claim(fixture, callback_id=callback)
    assert claimed.status_code == 200, claimed.text
    context = claimed.json()["execution_context"]
    restore = context["restore_checkpoint"]
    assert restore["record"] == newest["record"]
    assert restore["manifest"] == newest["manifest"]
    names = [entry["logical_name"] for entry in restore["manifest"]["files"]]
    assert names == [rule.logical_name for rule in ADAPTER.checkpoint_files]
    assert restore["files"] == [newest["artifacts"][name] for name in names]
    assert _restore_events(fixture) == [("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED")]

    replay = _claim(fixture, callback_id=callback)
    assert replay.status_code == 200 and replay.json() == claimed.json()
    assert _claim(fixture).json()["execution_context"] == context
    assert _restore_events(fixture) == [("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED")]

    # The restored files are the exact committed bytes; the runner checks tensors.
    downloaded = {
        name: _download(fixture, newest["artifacts"][name]["artifact_id"]) for name in names
    }
    assert downloaded == fixture.files(7, fill=1)
    assert training_state.validate_checkpoint_files(downloaded) == fixture.document(7)


@pytest.mark.parametrize(
    ("corrupt", "reason"),
    [
        ("tensor-bytes", "CHECKPOINT_CHECKSUM_MISMATCH"),
        ("tensor-missing", "CHECKPOINT_BLOB_MISSING"),
        ("state-cursor", "CHECKPOINT_STATE_INVALID"),
        ("state-foreign-input", "CHECKPOINT_STATE_INVALID"),
    ],
)
def test_corrupt_newest_training_checkpoint_falls_back_to_older(training_job, corrupt, reason):
    fixture = training_job
    older = fixture.commit(step=3)
    if corrupt == "state-cursor":
        # Publish never reads state files: a disagreeing state is found at restore.
        files = fixture.files(6)
        files[training_state.STATE_FILE] = fixture.files(5)[training_state.STATE_FILE]
        newest = fixture.commit(step=6, files=files)
    elif corrupt == "state-foreign-input":
        document = fixture.document(6)
        document["input_checksum"] = "sha256:" + "f" * 64
        newest = fixture.commit(step=6, files=fixture.files(6, document=document))
    else:
        # Blobs are content-addressed: distinct tensor bytes keep the older one intact.
        newest = fixture.commit(step=6, files=fixture.files(6, fill=1))
        optimizer = _blob_path(fixture, newest["artifacts"]["optimizer.safetensors"]["artifact_id"])
        if corrupt == "tensor-missing":
            optimizer.unlink()
        else:
            original = optimizer.read_bytes()
            _overwrite(optimizer, original[:-1] + bytes([original[-1] ^ 1]))
    _next_attempt(fixture)

    claimed = _claim(fixture)
    assert claimed.status_code == 200, claimed.text
    restore = claimed.json()["execution_context"]["restore_checkpoint"]
    assert restore["record"] == older["record"]
    assert _corruptions(fixture) == {UUID(newest["record"]["checkpoint_id"]): reason}
    assert _restore_events(fixture) == [
        ("CHECKPOINT_CORRUPT", reason),
        ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED"),
    ]
    events = fixture.rows()["events"]
    assert _claim(fixture).json()["execution_context"]["restore_checkpoint"] == restore
    assert fixture.rows()["events"] == events


def _result_rows(fixture):
    with fixture.engine.connect() as connection:
        results = (
            connection.execute(select(s.results).where(s.results.c.job_id == fixture.job_id))
            .mappings()
            .all()
        )
        references = connection.execute(
            select(
                s.artifact_references.c.purpose,
                s.artifact_references.c.logical_name,
                s.artifact_references.c.artifact_id,
            )
            .where(s.artifact_references.c.owner_type == "RESULT")
            .order_by(s.artifact_references.c.purpose, s.artifact_references.c.logical_name)
        ).all()
        job = connection.execute(
            select(s.jobs.c.state).where(s.jobs.c.job_id == fixture.job_id)
        ).scalar_one()
    return results, references, job


def _training_result(fixture, reservation, *, key, mutate=None):
    model = h.model_bytes()
    metrics_document = h.metrics_document(model)
    metrics_document["input_checksum"] = fixture.input_checksum
    metrics_document["spec_checksum"] = fixture.spec_checksum
    metrics_raw = canonical_json(metrics_document)
    metrics = adapter_launch.parse_training_metrics(
        metrics_raw,
        parameters=h.PARAMETERS,
        input_checksum=fixture.input_checksum,
        spec_checksum=fixture.spec_checksum,
        model_checksum=_checksum(model),
    )
    files = []
    for name, media, body in (
        ("model.safetensors", OCTET, model),
        ("metrics.json", JSON, metrics_raw),
    ):
        artifact = _upload(fixture, "RESULT_FILE", media, body, f"{key}-{name}")
        files.append(
            {
                "artifact_id": artifact["artifact_id"],
                "logical_name": name,
                "media_type": media,
                "size_bytes": len(body),
                "checksum": _checksum(body),
            }
        )
    manifest = {
        "kind": "RESULT",
        "schema_version": 1,
        "result_id": reservation["result_id"],
        "created_at": _timestamp(),
        "provenance": fixture.provenance(),
        "status": "SUCCEEDED",
        "files": files,
        "metrics": adapter_launch.manifest_metrics(metrics),
    }
    if mutate is not None:
        mutate(manifest)
    manifest["manifest_checksum"] = _checksum(rfc8785.dumps(manifest))
    raw = canonical_json(manifest)
    manifest_artifact = _upload(fixture, "RESULT_MANIFEST", JSON, raw, f"{key}-manifest")
    return {
        "authority": fixture.authority,
        "result_manifest_artifact_id": manifest_artifact["artifact_id"],
        # The worker forwards json.loads of the canonical bytes (sorted keys).
        "manifest": json.loads(raw),
    }, files


def test_complete_binds_model_and_metrics_once(training_job):
    fixture = training_job
    _seed_admission_counters(fixture)
    reservation = fixture.post("/result-reservations", {"authority": fixture.authority})
    assert reservation.status_code == 201, reservation.text
    reservation = reservation.json()

    for index, mutate in enumerate(
        (
            lambda m: m["metrics"].update(steps=m["metrics"]["steps"] + 1),
            lambda m: m["metrics"].update(model_checksum="sha256:" + "0" * 64),
            lambda m: m["files"].reverse(),
            lambda m: m["files"].pop(),
        )
    ):
        body, _ = _training_result(fixture, reservation, key=f"b16-bad-{index}", mutate=mutate)
        rejected = fixture.post("/complete", body)
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["code"] == "validation_failed"
    results, references, state = _result_rows(fixture)
    assert results == [] and references == [] and state == "RUNNING"

    body, files = _training_result(fixture, reservation, key="b16-good")
    callback = str(new_uuid7())
    completed = fixture.post("/complete", body, callback_id=callback)
    assert completed.status_code == 200, completed.text
    assert completed.json()["accepted"] is True
    replay = fixture.post("/complete", body, callback_id=callback)
    assert replay.status_code == 200 and replay.json() == completed.json()

    results, references, state = _result_rows(fixture)
    assert len(results) == 1 and str(results[0]["result_id"]) == reservation["result_id"]
    assert state == "SUCCEEDED"
    assert [(purpose, name, str(artifact_id)) for purpose, name, artifact_id in references] == [
        ("RESULT_FILE", "metrics.json", files[1]["artifact_id"]),
        ("RESULT_FILE", "model.safetensors", files[0]["artifact_id"]),
        ("RESULT_MANIFEST", "manifest", body["result_manifest_artifact_id"]),
    ]
    # A different callback cannot recognize a second final result for the job.
    second = fixture.post("/complete", body)
    assert second.status_code == 409, second.text
    assert len(_result_rows(fixture)[0]) == 1
