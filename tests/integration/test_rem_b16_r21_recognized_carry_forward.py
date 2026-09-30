"""Remediation B16-R21: a restored inference Attempt carries recognized chunks forward.

Recognition commits with the checkpoint, so the recognized set is the prefix up to the
newest committed cursor. When claim restores an older checkpoint, or falls back to
input, the chunks recognized beyond the restored cursor stay authoritative
(workloads-checkpoints :130). Claim therefore hands the Attempt exactly those rows,
with their original source Attempt and fence, as ``recognized_chunks``; a publish that
carries them forward unchanged is accepted, while a recomputed chunk under the current
source is a ``409`` whose safe ``reason`` names the conflict.
"""

from uuid import UUID

import pytest
from sqlalchemy import update

from nexa.infrastructure.persistence import schema as s
from nexa.worker.adapter_dispatch import adapter_of, adapter_recognized
from nexa.workloads import inference_state
from tests.api.test_http_contract import _client
from tests.integration.test_checkpoint_b14 import CheckpointFixture
from tests.integration.test_checkpoint_restore_b14 import (
    _blob_path,
    _claim,
    _commit,
    _corruptions,
    _next_attempt,
    _overwrite,
    _restore_events,
    _seed_admission_counters,
    _start,
    _worker_headers,
)
from tests.integration.test_inference_chunks_b16 import (
    CHUNK_SIZE,
    ITEMS,
    TOTAL,
    InferenceFixture,
    _entries,
    _files,
    _inference_result,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def inference_job(migrated_postgres_engine, tmp_path):
    with _client(migrated_postgres_engine, tmp_path) as client:
        yield InferenceFixture(migrated_postgres_engine, client, label="rem-r21")


@pytest.fixture
def checkpoint_job(migrated_postgres_engine, tmp_path):
    with _client(migrated_postgres_engine, tmp_path) as client:
        yield CheckpointFixture(migrated_postgres_engine, client, label="rem-r21-cpu")


def _context_entry(index, file, source):
    """The compact ``recognized_chunks`` item claim derives from one recognized row."""
    start, end = inference_state.chunk_extent(index, ITEMS, CHUNK_SIZE)
    return {
        "chunk_id": inference_state.chunk_id(index),
        "start_index": start,
        "end_index_exclusive": end,
        "artifact_id": file["artifact_id"],
        "size_bytes": file["size_bytes"],
        "checksum": file["checksum"],
        "source_attempt_id": source[0],
        "source_job_fence": source[1],
    }


def _two_checkpoints(fixture):
    """Older covers chunk 0, newest covers chunks 0-1; both recognized by attempt 1."""
    first = (fixture.authority["attempt_id"], 1)
    files = _files(fixture, range(2))
    older = fixture.commit(_entries(fixture, {0: files[0]}), next_chunk=1)
    newest = fixture.commit(_entries(fixture, files), next_chunk=2)
    return first, files, older, newest


def _claimed_context(fixture):
    _next_attempt(fixture)
    claimed = _claim(fixture)
    assert claimed.status_code == 200, claimed.text
    return claimed.json()["execution_context"]


def test_older_restore_names_the_recognized_chunks_beyond_its_cursor(inference_job):
    fixture = inference_job
    first, files, older, newest = _two_checkpoints(fixture)
    # Only the newest checkpoint's chunk-output manifest is lost; chunk 1 stays intact.
    _blob_path(fixture, newest["reference"]["artifact_id"]).unlink()
    before = fixture.recognized()

    context = _claimed_context(fixture)
    assert context["restore_checkpoint"]["record"] == older["record"]
    assert context["restore_checkpoint"]["manifest"]["cursor"]["step"] == 1
    # Chunk 1 was recognized by the lost checkpoint's publish and stays authoritative.
    assert context["recognized_chunks"] == [_context_entry(1, files[1], first)]
    assert _restore_events(fixture) == [
        ("CHECKPOINT_CORRUPT", "CHECKPOINT_BLOB_MISSING"),
        ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED"),
    ]
    # A claim replay returns the frozen context unchanged.
    assert _claim(fixture).json()["execution_context"] == context
    assert _start(fixture, context).status_code == 200
    current = (fixture.authority["attempt_id"], fixture.authority["job_fence"])
    assert current[1] > first[1]

    # A cycle that only carries recognized chunks forward (no new chunk) commits.
    carried = [fixture.entry(i, files[i], source=first) for i in range(2)]
    fixture.commit(carried, next_chunk=2)
    # Then chunk 2 is new work of this attempt and is recognized once.
    chunk_2 = fixture.chunk(2)
    fixture.commit([*carried, fixture.entry(2, chunk_2)], next_chunk=3)
    recognized = fixture.recognized()
    assert {key: recognized[key] for key in before} == before
    assert (
        str(recognized["chunk-00000002"]["source_attempt_id"]),
        recognized["chunk-00000002"]["source_job_fence"],
    ) == current

    # The job completes with the carried entries and no duplicate recognition.
    rest = {i: fixture.chunk(i) for i in range(3, TOTAL)}
    entries = [*carried, fixture.entry(2, chunk_2), *_entries(fixture, rest)]
    _seed_admission_counters(fixture)
    reservation = fixture.post("/result-reservations", {"authority": fixture.authority})
    assert reservation.status_code == 201, reservation.text
    body, _, _ = _inference_result(fixture, reservation.json(), entries)
    completed = fixture.post("/complete", body)
    assert completed.status_code == 200, completed.text
    final = fixture.recognized()
    assert sorted(final) == [inference_state.chunk_id(i) for i in range(TOTAL)]
    assert {key: final[key] for key in before} == before


def _download(fixture, artifact_id):
    return fixture.client.get(
        f"{fixture.base}/execution-artifacts/{artifact_id}/content",
        headers=_worker_headers(fixture.credential, fixture.authority),
    )


def test_lost_newest_state_blob_hands_the_worker_a_valid_recognized_run(inference_job):
    # Docker L D6b: only the newest checkpoint's state file is lost. The worker must
    # accept the claim's recognized chunks for the older cursor, not fail the attempt.
    fixture = inference_job
    first, files, older, newest = _two_checkpoints(fixture)
    _blob_path(fixture, newest["state"]["artifact_id"]).unlink()

    context = _claimed_context(fixture)
    assert context["restore_checkpoint"]["record"] == older["record"]
    assert context["recognized_chunks"] == [_context_entry(1, files[1], first)]
    adapter = adapter_of(context)
    restore = {"cursor": context["restore_checkpoint"]["manifest"]["cursor"]}
    assert adapter_recognized(context, adapter, restore) == context["recognized_chunks"]


def test_the_attempt_downloads_the_recognized_chunks_it_carries(inference_job):
    # RV03: the worker fetches each recognized chunk file so the workload takes it over
    # instead of recomputing it; the execution graph widens by exactly those files.
    fixture = inference_job
    _, files, _, newest = _two_checkpoints(fixture)
    _blob_path(fixture, newest["reference"]["artifact_id"]).unlink()
    context = _claimed_context(fixture)
    assert [item["artifact_id"] for item in context["recognized_chunks"]] == [
        files[1]["artifact_id"]
    ]

    carried = _download(fixture, files[1]["artifact_id"])
    assert carried.status_code == 200, carried.text
    assert carried.content == _blob_path(fixture, files[1]["artifact_id"]).read_bytes()
    # Chunk 0 is covered by the restored checkpoint and is not part of the graph.
    outside = _download(fixture, files[0]["artifact_id"])
    assert outside.status_code == 404, outside.text


def test_a_recognized_chunk_blob_changed_after_claim_is_refused(inference_job):
    fixture = inference_job
    _, files, _, newest = _two_checkpoints(fixture)
    _blob_path(fixture, newest["reference"]["artifact_id"]).unlink()
    _claimed_context(fixture)
    path = _blob_path(fixture, files[1]["artifact_id"])
    original = path.read_bytes()
    _overwrite(path, original[:-1] + bytes([original[-1] ^ 1]))
    refused = _download(fixture, files[1]["artifact_id"])
    assert refused.status_code == 503, refused.text


def test_recomputed_chunk_after_an_older_restore_conflicts_with_a_safe_reason(inference_job):
    fixture = inference_job
    first, files, _, newest = _two_checkpoints(fixture)
    _blob_path(fixture, newest["reference"]["artifact_id"]).unlink()
    before = (fixture.recognized(), fixture.extent())
    context = _claimed_context(fixture)
    assert _start(fixture, context).status_code == 200

    # Before the fix the attempt recomputed chunk 1 under its own source: that
    # conflicts with the recognition, now with a safe reason, and commits nothing.
    recomputed = fixture.chunk(1, fill=4)
    conflicting = [fixture.entry(0, files[0], source=first), fixture.entry(1, recomputed)]
    response, _ = fixture.attempt_publish(conflicting, next_chunk=2)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "state_conflict"
    assert response.json()["reason"] == "CHUNK_OUTPUT_CONFLICT"
    assert set(response.json()) == {"code", "message", "request_id", "reason"}
    assert (fixture.recognized(), fixture.extent()) == before
    assert [r["state"] for r in fixture.rows()["reservations"]][-1] == "RESERVED"


def test_fallback_to_input_names_every_recognized_chunk_from_zero(inference_job):
    fixture = inference_job
    first, files, older, newest = _two_checkpoints(fixture)
    for committed in (older, newest):
        _blob_path(fixture, committed["reference"]["artifact_id"]).unlink()

    context = _claimed_context(fixture)
    assert context["restore_checkpoint"] is None
    assert _restore_events(fixture) == [
        ("CHECKPOINT_CORRUPT", "CHECKPOINT_BLOB_MISSING"),
        ("CHECKPOINT_CORRUPT", "CHECKPOINT_BLOB_MISSING"),
        ("CHECKPOINT_FALLBACK_TO_INPUT", "CHECKPOINT_FALLBACK_TO_INPUT"),
    ]
    assert context["recognized_chunks"] == [_context_entry(i, files[i], first) for i in range(2)]
    assert _start(fixture, context).status_code == 200
    carried = [fixture.entry(i, files[i], source=first) for i in range(2)]
    fixture.commit([*carried, fixture.entry(2, fixture.chunk(2))], next_chunk=3)
    assert len(fixture.recognized()) == 3


@pytest.mark.parametrize(
    ("corrupt", "reason"),
    [
        ("chunk-bytes", "CHECKPOINT_CHECKSUM_MISMATCH"),
        ("chunk-missing", "CHECKPOINT_BLOB_MISSING"),
    ],
)
def test_unreadable_recognized_chunk_beyond_the_cursor_is_unavailable(
    inference_job, corrupt, reason
):
    fixture = inference_job
    _, files, older, newest = _two_checkpoints(fixture)
    path = _blob_path(fixture, files[1]["artifact_id"])
    if corrupt == "chunk-missing":
        path.unlink()
    else:
        original = path.read_bytes()
        _overwrite(path, original[:-1] + bytes([original[-1] ^ 1]))

    context = _claimed_context(fixture)
    # Restore selection is unchanged (B16-R18): the newest checkpoint falls back.
    assert context["restore_checkpoint"]["record"] == older["record"]
    assert _corruptions(fixture) == {UUID(newest["record"]["checkpoint_id"]): reason}
    # The recognized chunk 1 cannot be carried and must not be recomputed: null
    # makes the worker fail the attempt before any container exists.
    assert context["recognized_chunks"] is None


def test_newest_restore_carries_nothing(inference_job):
    fixture = inference_job
    _two_checkpoints(fixture)
    context = _claimed_context(fixture)
    assert context["restore_checkpoint"]["manifest"]["cursor"]["step"] == 2
    assert context["recognized_chunks"] == []


def test_first_inference_attempt_carries_nothing(inference_job):
    fixture = inference_job
    with fixture.engine.begin() as connection:
        connection.execute(
            update(s.attempts)
            .where(s.attempts.c.attempt_id == UUID(fixture.authority["attempt_id"]))
            .values(state="CREATED", started_at=None)
        )
        connection.execute(
            update(s.jobs).where(s.jobs.c.job_id == fixture.job_id).values(state="DISPATCHING")
        )
    claimed = _claim(fixture)
    assert claimed.status_code == 200, claimed.text
    context = claimed.json()["execution_context"]
    assert (context["restore_checkpoint"], context["recognized_chunks"]) == (None, [])


def test_non_chunked_claim_has_no_recognized_chunks(checkpoint_job):
    fixture = checkpoint_job
    _commit(fixture, step=10, accumulator=5)
    context = _claimed_context(fixture)
    assert context["restore_checkpoint"] is not None
    assert "recognized_chunks" not in context


def test_complete_conflict_names_the_safe_reason(inference_job):
    fixture = inference_job
    files = _files(fixture, range(2))
    fixture.commit(_entries(fixture, files), next_chunk=2)
    files.update(_files(fixture, range(2, TOTAL)))
    _seed_admission_counters(fixture)
    reservation = fixture.post("/result-reservations", {"authority": fixture.authority})
    assert reservation.status_code == 201, reservation.text
    conflicting = {**files, 0: fixture.chunk(0, fill=5)}
    body, _, _ = _inference_result(fixture, reservation.json(), _entries(fixture, conflicting))
    conflict = fixture.post("/complete", body)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["code"] == "state_conflict"
    assert conflict.json()["reason"] == "CHUNK_OUTPUT_CONFLICT"
    # Other errors keep the three-field body.
    missing = fixture.post("/complete", {**body, "result_manifest_artifact_id": "x"})
    assert missing.status_code == 422
    assert "reason" not in missing.json()
