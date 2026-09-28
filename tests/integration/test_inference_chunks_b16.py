"""B16 batch-inference chunk recognition on publish, restore and complete over PostgreSQL."""

import json
from uuid import UUID

import pytest
import rfc8785
from sqlalchemy import insert, select

from nexa.domain import workload_adapters
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.workloads import adapter_launch, chunk_manifest, inference_state
from nexa.workloads.canonical_json import canonical_json
from tests.api.test_http_contract import _client
from tests.integration._factories import seed_authority, seed_job
from tests.integration.test_checkpoint_b14 import (
    WORKER_ID,
    CheckpointFixture,
    _checksum,
    _timestamp,
)
from tests.integration.test_checkpoint_restore_b14 import (
    _blob_path,
    _claim,
    _corruptions,
    _next_attempt,
    _overwrite,
    _restore_events,
    _seed_admission_counters,
    _start,
)
from tests.integration.test_pytorch_checkpoint_b16 import (
    ARCH,
    FRAMEWORK_VERSION,
    JSON,
    REQUIREMENTS,
    _result_rows,
    _upload,
)
from tests.workloads import b16_helpers as h

pytestmark = pytest.mark.postgres

ADAPTER = workload_adapters.BATCH_INFERENCE
NDJSON = chunk_manifest.CHUNK_MEDIA_TYPES["JSONL"]
ITEMS = 1000
CHUNK_SIZE = h.INFERENCE_PARAMETERS["chunk_size"]
TOTAL = inference_state.chunk_count(ITEMS, CHUNK_SIZE)


class InferenceFixture(CheckpointFixture):
    """One RUNNING attempt of the registered batch-inference template with a MODEL input."""

    def __init__(self, engine, client, *, label, restart_safe=True):
        super().__init__(
            engine,
            client,
            label=label,
            restart_safe=restart_safe,
            template_id=ADAPTER.template_id,
            template_values={"adapter_id": ADAPTER.adapter_id, "adapter_version": "1.0.0"},
            input_media_type=ADAPTER.input_media_type,
            input_kind="DATASET",
            requirements=REQUIREMENTS,
            parameters=dict(h.INFERENCE_PARAMETERS),
            model_content=b"b16-inference-model-fixture",
        )

    def provenance(self, **changes):
        return super().provenance(adapter_id=ADAPTER.adapter_id, **changes)

    def document(self, next_chunk, *, item_count=ITEMS):
        document = h.inference_document(next_chunk, item_count=item_count)
        document.update(
            input_checksum=self.input_checksum,
            spec_checksum=self.spec_checksum,
            model_checksum=self.model_checksum,
        )
        return document

    def chunk(self, index, *, fill=0, expected=201):
        """Upload one chunk file as the current attempt; return its manifest file entry."""
        body = h.chunk_body(index, fill)
        artifact = _upload(self, "RESULT_FILE", NDJSON, body, f"b16-{new_uuid7()}", expected)
        return h.chunk_file(index, artifact_id=artifact["artifact_id"], body=body)

    def entry(self, index, file, *, source=None, item_count=ITEMS):
        attempt_id, fence = source or (self.authority["attempt_id"], self.authority["job_fence"])
        return chunk_manifest.entry(
            index,
            item_count=item_count,
            chunk_size=CHUNK_SIZE,
            source_attempt_id=attempt_id,
            source_job_fence=fence,
            file=file,
        )

    def chunk_manifest(self, entries):
        raw = chunk_manifest.build(
            provenance=self.provenance(), model_checksum=self.model_checksum, chunks=entries
        )
        artifact = _upload(self, "CHUNK_OUTPUT_MANIFEST", JSON, raw, f"b16-{new_uuid7()}")
        return {"artifact_id": artifact["artifact_id"], "checksum": _checksum(raw)}

    def inference_checkpoint(self, reservation, *, document, reference, mutate=None):
        state = canonical_json(document)
        state_artifact = _upload(
            self, "CHECKPOINT_FILE", JSON, state, f"b16-{reservation['checkpoint_id']}-state"
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
            "cursor": inference_state.runtime_cursor(document),
            "state_components": list(ADAPTER.state_components),
            "files": [
                {
                    "artifact_id": state_artifact["artifact_id"],
                    "logical_name": "inference-state.json",
                    "media_type": JSON,
                    "size_bytes": len(state),
                    "checksum": _checksum(state),
                }
            ],
            "chunk_output_manifest": reference,
        }
        if mutate is not None:
            mutate(manifest)
        manifest["manifest_checksum"] = _checksum(rfc8785.dumps(manifest))
        manifest_artifact = _upload(
            self,
            "CHECKPOINT_MANIFEST",
            JSON,
            rfc8785.dumps(manifest),
            f"b16-{reservation['checkpoint_id']}-manifest",
        )
        return manifest, manifest_artifact, state_artifact

    def attempt_publish(self, entries, *, next_chunk, document=None, mutate=None):
        """Upload the chunk manifest, reserve, upload state and manifest, then publish."""
        reference = self.chunk_manifest(entries)
        reserved = self.reserve()
        assert reserved.status_code == 201, reserved.text
        manifest, manifest_artifact, state_artifact = self.inference_checkpoint(
            reserved.json(),
            document=document or self.document(next_chunk),
            reference=reference,
            mutate=mutate,
        )
        response = self.publish(manifest, manifest_artifact)
        return response, {
            "manifest": manifest,
            "manifest_artifact": manifest_artifact,
            "state": state_artifact,
            "reference": reference,
        }

    def commit(self, entries, *, next_chunk, **options):
        response, published = self.attempt_publish(entries, next_chunk=next_chunk, **options)
        assert response.status_code == 201, response.text
        return {"record": response.json(), **published}

    def recognized(self):
        with self.engine.connect() as connection:
            return {
                row["chunk_id"]: row
                for row in connection.execute(
                    select(s.recognized_chunks).where(s.recognized_chunks.c.job_id == self.job_id)
                ).mappings()
            }

    def extent(self):
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    select(s.inference_extents).where(s.inference_extents.c.job_id == self.job_id)
                )
                .mappings()
                .one_or_none()
            )

    def chunk_references(self):
        with self.engine.connect() as connection:
            return connection.execute(
                select(
                    s.artifact_references.c.owner_type,
                    s.artifact_references.c.owner_id,
                    s.artifact_references.c.purpose,
                    s.artifact_references.c.logical_name,
                    s.artifact_references.c.artifact_id,
                ).where(
                    s.artifact_references.c.tenant_id == self.graph["tenant_id"],
                    s.artifact_references.c.purpose.in_(["CHUNK_OUTPUT", "CHUNK_OUTPUT_MANIFEST"]),
                )
            ).all()


@pytest.fixture
def inference_job(migrated_postgres_engine, tmp_path):
    with _client(migrated_postgres_engine, tmp_path) as client:
        yield InferenceFixture(migrated_postgres_engine, client, label="infer")


def _files(fixture, indices, *, fill=0):
    return {index: fixture.chunk(index, fill=fill) for index in indices}


def _entries(fixture, files, **options):
    return [fixture.entry(index, file, **options) for index, file in sorted(files.items())]


def _key(row):
    return (str(row.artifact_id), row.purpose, row.logical_name)


def test_publish_recognizes_each_chunk_and_binds_the_graph(inference_job):
    fixture = inference_job
    files = _files(fixture, range(2))
    committed = fixture.commit(_entries(fixture, files), next_chunk=2)

    recognized = fixture.recognized()
    assert sorted(recognized) == ["chunk-00000000", "chunk-00000001"]
    for index, file in files.items():
        row = recognized[inference_state.chunk_id(index)]
        start, end = inference_state.chunk_extent(index, ITEMS, CHUNK_SIZE)
        assert (row["range_start"], row["range_end"]) == (start, end)
        assert (str(row["artifact_id"]), row["checksum"]) == (file["artifact_id"], file["checksum"])
        assert (str(row["source_attempt_id"]), row["source_job_fence"]) == (
            fixture.authority["attempt_id"],
            1,
        )
        assert row["session_id"] == fixture.session_id
    extent = fixture.extent()
    assert (extent["item_count"], extent["chunk_size"]) == (ITEMS, CHUNK_SIZE)

    checkpoint_id = UUID(committed["record"]["checkpoint_id"])
    rows = fixture.rows()
    assert sorted(
        (str(r.artifact_id), r.purpose, r.logical_name)
        for r in rows["references"]
        if r.owner_id == checkpoint_id
    ) == sorted(
        [
            (committed["manifest_artifact"]["artifact_id"], "CHECKPOINT_MANIFEST", "manifest"),
            (committed["state"]["artifact_id"], "CHECKPOINT_FILE", "inference-state.json"),
            (
                committed["reference"]["artifact_id"],
                "CHUNK_OUTPUT_MANIFEST",
                chunk_manifest.LOGICAL_NAME,
            ),
            *((f["artifact_id"], "CHUNK_OUTPUT", f["logical_name"]) for f in files.values()),
        ]
    )
    owned = {
        (str(r.artifact_id), r.logical_name)
        for r in fixture.chunk_references()
        if r.owner_type == "RECOGNIZED_CHUNK"
    }
    assert owned == {(f["artifact_id"], f["logical_name"]) for f in files.values()}
    assert [row.event_type for row in rows["events"]] == ["CHECKPOINT_COMMITTED"]


def test_later_checkpoint_verifies_recognized_chunks_and_adds_new_ones(inference_job):
    fixture = inference_job
    files = _files(fixture, range(2))
    fixture.commit(_entries(fixture, files), next_chunk=2)
    first = fixture.recognized()

    files[2] = fixture.chunk(2)
    fixture.commit(_entries(fixture, files), next_chunk=3)
    later = fixture.recognized()
    assert sorted(later) == [inference_state.chunk_id(i) for i in range(3)]
    assert {key: later[key] for key in first} == first
    owned = [r for r in fixture.chunk_references() if r.owner_type == "RECOGNIZED_CHUNK"]
    assert len(owned) == 3


def test_a_chunk_that_differs_from_its_recognition_conflicts_and_commits_nothing(inference_job):
    fixture = inference_job
    files = _files(fixture, range(2))
    fixture.commit(_entries(fixture, files), next_chunk=2)
    before = (fixture.rows(), fixture.recognized(), fixture.extent())

    files[1] = fixture.chunk(1, fill=1)
    files[2] = fixture.chunk(2)
    response, _ = fixture.attempt_publish(_entries(fixture, files), next_chunk=3)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "state_conflict"
    rows = fixture.rows()
    assert [c["sequence"] for c in rows["checkpoints"]] == [1]
    assert [r["state"] for r in rows["reservations"]] == ["COMMITTED", "RESERVED"]
    assert rows["events"] == before[0]["events"]
    assert (fixture.recognized(), fixture.extent()) == before[1:]


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("extra-entry", "CHECKPOINT_FILES_INVALID"),
        ("gap", "CHECKPOINT_FILES_INVALID"),
        ("missing-file", "CHECKPOINT_FILES_INVALID"),
        ("wrong-model", "CHECKPOINT_FILES_INVALID"),
        ("state-model", "CHECKPOINT_STATE_INVALID"),
        ("extent", "CHECKPOINT_STATE_INVALID"),
    ],
)
def test_each_inference_publish_defect_is_rejected_durably(inference_job, case, reason):
    fixture = inference_job
    options = {}
    events = []
    if case == "extent":
        # The first recognition pins the item count; a later state cannot change it.
        files = _files(fixture, range(1))
        fixture.commit(_entries(fixture, files), next_chunk=1)
        events = ["CHECKPOINT_COMMITTED"]
        files[1] = fixture.chunk(1)
        entries = _entries(fixture, files, item_count=900)
        options["document"] = fixture.document(2, item_count=900)
    else:
        files = _files(fixture, range(3 if case == "extra-entry" else 2))
        entries = _entries(fixture, files)
    if case == "gap":
        # Position 1 names chunk 2's extent: coverage is positional and contiguous.
        entries[1] = fixture.entry(2, fixture.chunk(2))
    elif case == "missing-file":
        entries[1]["file"]["artifact_id"] = str(new_uuid7())
    elif case == "wrong-model":
        fixture.model_checksum, real = "sha256:" + "e" * 64, fixture.model_checksum
        reference = fixture.chunk_manifest(entries)
        fixture.model_checksum = real
        reserved = fixture.reserve().json()
        manifest, manifest_artifact, _ = fixture.inference_checkpoint(
            reserved, document=fixture.document(2), reference=reference
        )
        response = fixture.publish(manifest, manifest_artifact)
    elif case == "state-model":
        options["document"] = {**fixture.document(2), "model_checksum": "sha256:" + "e" * 64}
    if case != "wrong-model":
        response, _ = fixture.attempt_publish(entries, next_chunk=2, **options)
    assert response.status_code == 422, response.text
    rows = fixture.rows()
    assert [r["state"] for r in rows["reservations"]][-1] == "REJECTED"
    assert [row.event_type for row in rows["events"]] == [*events, "CHECKPOINT_REJECTED"]
    assert rows["events"][-1].reason == reason
    # The savepoint discards every partial recognition of the rejected publish.
    assert len(fixture.recognized()) == (1 if case == "extent" else 0)
    assert (fixture.extent() is not None) == (case == "extent")


def _restart(fixture):
    """Attempt 2 after an infrastructure failure: claim (restore) then start."""
    _next_attempt(fixture)
    claimed = _claim(fixture)
    assert claimed.status_code == 200, claimed.text
    context = claimed.json()["execution_context"]
    started = _start(fixture, context)
    assert started.status_code == 200, started.text
    return context


def test_restore_carries_the_chunk_manifest_and_attempt_two_carries_chunks_forward(
    inference_job,
):
    fixture = inference_job
    first_attempt = (fixture.authority["attempt_id"], 1)
    files = _files(fixture, range(3))
    older = fixture.commit(_entries(fixture, {i: files[i] for i in range(1)}), next_chunk=1)
    newest = fixture.commit(_entries(fixture, {i: files[i] for i in range(2)}), next_chunk=2)
    # Chunk 2 was uploaded but never recognized by a fenced publish.
    context = _restart(fixture)

    restore = context["restore_checkpoint"]
    assert restore["record"] == newest["record"] != older["record"]
    assert restore["manifest"] == newest["manifest"]
    assert [f["artifact_id"] for f in restore["files"]] == [
        newest["state"]["artifact_id"],
        newest["reference"]["artifact_id"],
    ]
    assert restore["files"][1]["kind"] == "CHUNK_OUTPUT_MANIFEST"
    assert _restore_events(fixture) == [("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED")]

    carried = [fixture.entry(i, files[i], source=first_attempt) for i in range(2)]
    unrecognized = fixture.entry(2, files[2], source=first_attempt)
    response, _ = fixture.attempt_publish([*carried, unrecognized], next_chunk=3)
    assert response.status_code == 422, response.text
    assert fixture.rows()["events"][-1][1:] == ("CHECKPOINT_REJECTED", "CHECKPOINT_FILES_INVALID")

    # A current-lineage claim on an artifact another attempt uploaded is a defect too.
    response, _ = fixture.attempt_publish([*carried, fixture.entry(2, files[2])], next_chunk=3)
    assert response.status_code == 422, response.text

    new = fixture.chunk(2, fill=3)
    fixture.commit([*carried, fixture.entry(2, new)], next_chunk=3)
    recognized = fixture.recognized()
    assert [
        (
            str(recognized[inference_state.chunk_id(i)]["source_attempt_id"]),
            recognized[inference_state.chunk_id(i)]["source_job_fence"],
        )
        for i in range(3)
    ] == [first_attempt, first_attempt, (fixture.authority["attempt_id"], 3)]
    assert str(recognized["chunk-00000002"]["artifact_id"]) == new["artifact_id"]


@pytest.mark.parametrize(
    ("corrupt", "reason"),
    [
        ("chunk-bytes", "CHECKPOINT_CHECKSUM_MISMATCH"),
        ("chunk-missing", "CHECKPOINT_BLOB_MISSING"),
        ("chunk-manifest-missing", "CHECKPOINT_BLOB_MISSING"),
    ],
)
def test_corrupt_referenced_chunk_falls_back_to_the_older_checkpoint(
    inference_job, corrupt, reason
):
    fixture = inference_job
    files = _files(fixture, range(2))
    older = fixture.commit(_entries(fixture, {0: files[0]}), next_chunk=1)
    newest = fixture.commit(_entries(fixture, files), next_chunk=2)
    if corrupt == "chunk-manifest-missing":
        _blob_path(fixture, newest["reference"]["artifact_id"]).unlink()
    else:
        # Only the newest checkpoint references chunk 1; chunk 0 stays intact.
        path = _blob_path(fixture, files[1]["artifact_id"])
        if corrupt == "chunk-missing":
            path.unlink()
        else:
            original = path.read_bytes()
            _overwrite(path, original[:-1] + bytes([original[-1] ^ 1]))
    _next_attempt(fixture)

    claimed = _claim(fixture)
    assert claimed.status_code == 200, claimed.text
    restore = claimed.json()["execution_context"]["restore_checkpoint"]
    assert restore["record"] == older["record"]
    assert restore["files"][1]["artifact_id"] == older["reference"]["artifact_id"]
    assert _corruptions(fixture) == {UUID(newest["record"]["checkpoint_id"]): reason}
    assert _restore_events(fixture) == [
        ("CHECKPOINT_CORRUPT", reason),
        ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED"),
    ]


def test_inherited_inference_checkpoint_falls_back_to_input(inference_job):
    """B16-R20: another job's chunk recognition never carries into this job."""
    fixture = inference_job
    committed = fixture.commit(_entries(fixture, _files(fixture, range(2))), next_chunk=2)
    with fixture.engine.begin() as connection:
        job = seed_job(
            connection,
            fixture.graph,
            state="DISPATCHING",
            retry_of_job_id=fixture.job_id,
            artifact_id=_input_id(connection, fixture),
            canonical_spec=fixture.canonical_spec,
            model_artifact_id=fixture.model_id,
        )
        connection.execute(
            insert(s.checkpoint_references).values(
                tenant_id=fixture.graph["tenant_id"],
                source_checkpoint_id=UUID(committed["record"]["checkpoint_id"]),
                target_job_id=job["job_id"],
                reason="MANUAL_RETRY",
            )
        )
        ids = seed_authority(
            connection,
            fixture.graph,
            job,
            {
                "worker_id": UUID(WORKER_ID),
                "incarnation_id": UUID(fixture.authority["worker_incarnation_id"]),
            },
        )
    fixture.job_id = job["job_id"]
    fixture.authority = {
        **fixture.authority,
        "attempt_id": str(ids["attempt_id"]),
        "allocation_id": str(ids["allocation_id"]),
        "lease_id": str(ids["lease_id"]),
        "job_fence": 1,
    }
    fixture.base = f"/v1/attempts/{ids['attempt_id']}"

    claimed = _claim(fixture)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["execution_context"]["restore_checkpoint"] is None
    assert _restore_events(fixture) == [
        ("CHECKPOINT_INCOMPATIBLE", "CHECKPOINT_COMPATIBILITY_MISMATCH"),
        ("CHECKPOINT_FALLBACK_TO_INPUT", "CHECKPOINT_FALLBACK_TO_INPUT"),
    ]
    assert _corruptions(fixture) == {}


def _input_id(connection, fixture):
    return connection.execute(
        select(s.job_specs.c.input_artifact_id).where(s.job_specs.c.job_id == fixture.job_id)
    ).scalar_one()


def _inference_result(fixture, reservation, entries, *, mutate=None):
    summary = canonical_json(inference_state.summary_from_state(fixture.document(TOTAL)))
    summary_artifact = _upload(fixture, "RESULT_FILE", JSON, summary, f"b16-{new_uuid7()}")
    reference = fixture.chunk_manifest(entries)
    manifest = {
        "kind": "RESULT",
        "schema_version": 1,
        "result_id": reservation["result_id"],
        "created_at": _timestamp(),
        "provenance": fixture.provenance(),
        "status": "SUCCEEDED",
        "files": [
            {
                "artifact_id": summary_artifact["artifact_id"],
                "logical_name": "summary.json",
                "media_type": JSON,
                "size_bytes": len(summary),
                "checksum": _checksum(summary),
            }
        ],
        "metrics": {
            "item_count": ITEMS,
            "chunk_count": TOTAL,
            "chunk_size": CHUNK_SIZE,
            "batch_size": h.INFERENCE_PARAMETERS["batch_size"],
            "output_format": h.INFERENCE_PARAMETERS["output_format"],
            "model_checksum": fixture.model_checksum,
        },
        "chunk_output_manifest": reference,
    }
    if mutate is not None:
        mutate(manifest)
    manifest["manifest_checksum"] = _checksum(rfc8785.dumps(manifest))
    raw = canonical_json(manifest)
    manifest_artifact = _upload(fixture, "RESULT_MANIFEST", JSON, raw, f"b16-{new_uuid7()}")
    body = {
        "authority": fixture.authority,
        "result_manifest_artifact_id": manifest_artifact["artifact_id"],
        "manifest": json.loads(raw),
    }
    return body, summary_artifact, reference


def test_complete_requires_full_coverage_and_binds_the_chunk_graph(inference_job):
    fixture = inference_job
    files = _files(fixture, range(2))
    fixture.commit(_entries(fixture, files), next_chunk=2)
    files.update(_files(fixture, range(2, TOTAL)))
    _seed_admission_counters(fixture)
    reservation = fixture.post("/result-reservations", {"authority": fixture.authority})
    assert reservation.status_code == 201, reservation.text
    reservation = reservation.json()

    # N-1 chunks is a gap at the end; a result never completes with missing coverage.
    partial = {i: files[i] for i in range(TOTAL - 1)}
    body, _, _ = _inference_result(fixture, reservation, _entries(fixture, partial))
    rejected = fixture.post("/complete", body)
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == "validation_failed"
    conflicting = {**files, 0: fixture.chunk(0, fill=5)}
    body, _, _ = _inference_result(fixture, reservation, _entries(fixture, conflicting))
    conflict = fixture.post("/complete", body)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["code"] == "state_conflict"
    results, references, state = _result_rows(fixture)
    assert results == [] and references == [] and state == "RUNNING"
    assert sorted(fixture.recognized()) == ["chunk-00000000", "chunk-00000001"]

    body, summary, reference = _inference_result(fixture, reservation, _entries(fixture, files))
    callback = str(new_uuid7())
    completed = fixture.post("/complete", body, callback_id=callback)
    assert completed.status_code == 200, completed.text
    assert completed.json()["accepted"] is True
    replay = fixture.post("/complete", body, callback_id=callback)
    assert replay.status_code == 200 and replay.json() == completed.json()

    results, references, state = _result_rows(fixture)
    assert len(results) == 1 and state == "SUCCEEDED"
    assert sorted(
        (purpose, name, str(artifact)) for purpose, name, artifact in references
    ) == sorted(
        [
            ("RESULT_MANIFEST", "manifest", body["result_manifest_artifact_id"]),
            ("RESULT_FILE", "summary.json", summary["artifact_id"]),
            ("CHUNK_OUTPUT_MANIFEST", chunk_manifest.LOGICAL_NAME, reference["artifact_id"]),
            *(("CHUNK_OUTPUT", f["logical_name"], f["artifact_id"]) for f in files.values()),
        ]
    )
    assert sorted(fixture.recognized()) == [inference_state.chunk_id(i) for i in range(TOTAL)]
