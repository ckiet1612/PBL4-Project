"""Remediation B16-R21 on the worker: a restored inference Attempt carries chunks forward.

The B14 harness drives the production worker loop and the real trusted runner; only
Docker and HTTP are faked. ``RecognizingApi`` adds the server recognition rules of
``chunk_recognition.recognize``: a current-source entry that differs from its recognized
row is a ``409`` whose safe reason is CHUNK_OUTPUT_CONFLICT, a prior-source entry
without an exact row is a ``422``. Claim names the chunks recognized beyond the restore
cursor in ``recognized_chunks``; the worker downloads their files, the workload takes them
over from those read-only files without recomputing them, and the runner publishes them
with their original source without uploading them again. Output the workload still writes
for a recognized chunk fails the Attempt INTERNAL/CHUNK_OUTPUT_CONFLICT at once; before the
fix the worker re-uploaded it under its own source and replayed the rejected publish
forever.
"""

import json
from dataclasses import asdict

import httpx
import pytest

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.worker.client import WorkerApiClient, WorkerApiError
from nexa.worker.models import Authority
from nexa.worker.state import PendingOperationStore
from nexa.workloads import adapter_launch, chunk_manifest, inference_state
from nexa.workloads.canonical_json import canonical_json
from tests.worker import b16_claims as c
from tests.worker.test_checkpoint_flow_b14 import Backend, Harness, calls
from tests.worker.test_inference_flow_b16 import (
    InferenceApi,
    _checkpoint,
    _chunk_files,
    _started,
    _uploads,
    _write,
)

_ABSENT = object()


def _compact(entry):
    """The claim's ``recognized_chunks`` item of one chunk-output manifest entry."""
    return {
        "chunk_id": entry["chunk_id"],
        "start_index": entry["start_index"],
        "end_index_exclusive": entry["end_index_exclusive"],
        "artifact_id": entry["file"]["artifact_id"],
        "size_bytes": entry["file"]["size_bytes"],
        "checksum": entry["file"]["checksum"],
        "source_attempt_id": entry["source_attempt_id"],
        "source_job_fence": entry["source_job_fence"],
    }


class RecognizingApi(InferenceApi):
    """The B16 API fake that also keeps the job's recognized chunks across publishes."""

    def __init__(self, context, *, blobs=None, recognized=()):
        super().__init__(context, blobs=blobs)
        self.recognized = {item["chunk_id"]: dict(item) for item in recognized}
        self.conflict = None

    def _recognize(self, entries, body):
        current = (body["authority"]["attempt_id"], body["authority"]["job_fence"])
        staged = {}
        for entry in entries:
            item = _compact(entry)
            known = self.recognized.get(item["chunk_id"])
            if (item["source_attempt_id"], item["source_job_fence"]) != current:
                # Carry-forward: only an exact earlier recognition.
                if known != item:
                    raise WorkerApiError(422, "validation_failed")
            elif known is None:
                staged[item["chunk_id"]] = item
            elif known != item:
                raise WorkerApiError(409, "state_conflict", reason="CHUNK_OUTPUT_CONFLICT")
        self.recognized.update(staged)

    def publish_checkpoint(self, attempt, callback, body):
        fresh = callback not in self.published
        if fresh and self.conflict is not None:
            self.calls.append(("publish_checkpoint", callback))
            raise self.conflict
        answer = super().publish_checkpoint(attempt, callback, body)
        if fresh:
            sequence = self.published[callback]["sequence"]
            try:
                self._recognize(self.chunk_entries[sequence], body)
            except WorkerApiError:
                # Nothing commits; the reservation stays RESERVED.
                del self.published[callback], self.chunk_entries[sequence]
                raise
        return answer

    def complete(self, attempt, callback, body):
        if self.conflict is not None:
            self.calls.append(("complete", callback))
            raise self.conflict
        answer = super().complete(attempt, callback, body)
        try:
            self._recognize(self.chunk_entries["result"], body)
        except WorkerApiError:
            self.results.pop()
            self.bindings.pop()
            del self.chunk_entries["result"]
            raise
        return answer


class AttemptBackend(Backend):
    """The B14 Docker fake labelling containers with this harness's own Attempt."""

    def __init__(self, output, attempt_id):
        super().__init__(output, labels=c.INFERENCE_LABELS)
        self.attempt_id = attempt_id

    def run(self, argv, timeout_seconds):
        result = super().run(argv, timeout_seconds)
        if argv[1] == "inspect" and result[0] == 0:
            payload = json.loads(result[1])
            payload[0]["Config"]["Labels"]["nexa.attempt_id"] = self.attempt_id
            return 0, json.dumps(payload).encode(), b""
        return result


def _failures(harness):
    return [(f["failure_class"], f["reason_code"]) for f in harness.api.failures]


def _restore_of(harness, sequence):
    """The claim's frozen restore of one committed checkpoint and its artifact ids."""
    (published,) = [p for p in harness.api.published.values() if p["sequence"] == sequence]
    manifest = published["manifest"]
    record = {key: value for key, value in published.items() if key != "manifest"}
    reference = manifest["chunk_output_manifest"]
    ids = [entry["artifact_id"] for entry in manifest["files"]] + [reference["artifact_id"]]
    files = [harness.api.artifacts[artifact_id] for artifact_id in ids]
    return {"record": record, "manifest": manifest, "files": files}, ids


def _first_attempt(tmp_path):
    """Attempt 1 commits cursor 1 (chunk 0), then cursor 2 (chunk 1); both recognized."""
    first = _started(tmp_path / "first")
    _checkpoint(first, 1, (0,))
    _checkpoint(first, 2, (1,))
    return first, first.api.chunk_entries[2]


def _resumed(tmp_path, first, restore, ids, recognized=_ABSENT):
    """Claim and dispatch attempt 2 (its own attempt id and fence 4) of the same job."""
    context = c.inference_claim(restore=restore)
    if recognized is not _ABSENT:
        context["recognized_chunks"] = recognized
    context["authority"] = {
        **context["authority"],
        "attempt_id": str(new_uuid7()),
        "job_fence": 4,
    }
    # The server keeps every chunk attempt 1 recognized (its newest publish).
    newest = max(key for key in first.api.chunk_entries if isinstance(key, int))
    api = RecognizingApi(
        context,
        blobs=first.api.blobs,
        recognized=[_compact(entry) for entry in first.api.chunk_entries[newest]],
    )
    api.artifacts.update(first.api.artifacts)
    harness = Harness(
        tmp_path,
        context,
        api=api,
        backend=AttemptBackend(tmp_path / "fs" / "output", context["authority"]["attempt_id"]),
    )
    harness.authority = Authority(**context["authority"])
    (harness.fs / "input" / "model.safetensors").write_bytes(c.MODEL)
    if restore is not None:
        directory = harness.fs / adapter_launch.RESTORE_DIR.lstrip("/")
        directory.mkdir(parents=True)
        (directory / "inference-state.json").write_bytes(first.api.blobs[ids[0]])
        (directory / chunk_manifest.LOGICAL_NAME).write_bytes(first.api.blobs[ids[1]])
    harness.dispatch()
    control = harness.journal.root / "control" / harness.attempt_id / "launch-spec.json"
    if control.exists():
        # The executor's read-only mounts of the recognized chunk files the worker fetched.
        spec = harness.launch_spec()
        for item, path in zip(
            spec.get("recognized_chunks") or [], adapter_launch.recognized_paths(spec), strict=True
        ):
            target = harness.fs / path.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(first.api.blobs[item["artifact_id"]])
    return harness


def _running(harness, step):
    assert harness.api.failures == []
    harness.cycle(1)
    if harness.launch_spec()["restore"] is not None:
        assert harness.runner._restore_state_is_valid() is True
    harness.runner.mark_workload_started()
    harness.runner.emit_progress(fraction=step / 4, step=step)
    harness.cycle(2)


def _older_restore(tmp_path, *, legacy=False):
    """Attempt 2 restores the older checkpoint (cursor 1) of attempt 1.

    Its claim names chunk 1, recognized by attempt 1's newest (lost) checkpoint; a
    ``legacy`` claim was acknowledged by a server without ``recognized_chunks``.
    """
    first, entries = _first_attempt(tmp_path)
    restore, ids = _restore_of(first, 1)
    recognized = _ABSENT if legacy else [_compact(entries[1])]
    return first, entries, _resumed(tmp_path / "second", first, restore, ids, recognized)


def _finish(harness, next_chunk, chunks, *, fill=0):
    _, document = _write(harness, next_chunk, chunks=chunks, fill=fill)
    summary = canonical_json(inference_state.summary_from_state(document))
    (harness.fs / "output" / "summary.json").write_bytes(summary)
    harness.runner._finish_adapter_workload(0)


def test_older_restore_adopts_recognized_chunks_and_keeps_their_source(tmp_path):
    first, recognized, second = _older_restore(tmp_path)
    # The claim's recognized chunk beyond the restored cursor reaches the runner.
    assert second.launch_spec()["recognized_chunks"] == [_compact(recognized[1])]
    _running(second, step=1)

    # The worker fetched chunk 1 and the launch hands it to the workload read-only.
    downloads = [artifact for _, artifact in calls(second.api, "download")]
    assert downloads.count(recognized[1]["file"]["artifact_id"]) == 1
    assert second.runner._recognized_files_are_valid() is True
    assert second.runner._launch_command()[-4:] == (
        "--recognized-dir",
        adapter_launch.RECOGNIZED_DIR,
        "--recognized-count",
        "1",
    )

    # REM-R02: a cycle with no new chunk (the cursor is still the restored one) commits.
    _checkpoint(second, 1, ())
    assert second.api.chunk_entries[1] == recognized[:1]

    # The workload carries chunk 1 over without writing it: its entry is published with
    # attempt 1's source and nothing is uploaded (a zero-batch cycle again).
    _checkpoint(second, 2, ())
    assert second.api.chunk_entries[2] == recognized
    assert _uploads(second).count("RESULT_FILE") == 0
    assert _chunk_files(second) == []

    # Chunk 2 is this attempt's own work.
    _checkpoint(second, 3, (2,), fill=1)
    third = second.api.chunk_entries[3]
    assert third[:2] == recognized
    assert (third[2]["source_attempt_id"], third[2]["source_job_fence"]) == (
        second.attempt_id,
        4,
    )

    _finish(second, 4, (3,), fill=2)
    second.cycle(12)
    assert second.api.failures == []
    result = second.api.chunk_entries["result"]
    assert result[:3] == third
    assert len(calls(second.api, "complete")) == 1
    # Chunks 2 and 3, then the summary; chunk 1 was never uploaded twice.
    assert _uploads(second).count("RESULT_FILE") == 3
    assert sorted(second.api.recognized) == [inference_state.chunk_id(i) for i in range(4)]
    assert all(second.api.recognized[entry["chunk_id"]] == _compact(entry) for entry in recognized)


def test_fallback_to_input_adopts_recognized_chunks_from_zero(tmp_path):
    first, recognized = _first_attempt(tmp_path)
    second = _resumed(
        tmp_path / "second", first, None, (), [_compact(entry) for entry in recognized]
    )
    assert second.launch_spec()["recognized_chunks"] == [_compact(e) for e in recognized]
    assert second.runner._recognized_files_are_valid() is True
    _running(second, step=0)
    # The workload's first state already stands past both carried chunks.
    _checkpoint(second, 2, ())
    assert second.api.chunk_entries[1] == recognized
    _checkpoint(second, 3, (2,), fill=0)
    entries = second.api.chunk_entries[2]
    assert entries[:2] == recognized
    assert entries[2]["source_attempt_id"] == second.attempt_id
    assert _uploads(second).count("RESULT_FILE") == 1


def test_result_adopts_recognized_chunks_without_a_checkpoint(tmp_path):
    first, recognized, second = _older_restore(tmp_path)
    _running(second, step=1)
    _finish(second, 4, (2, 3))
    second.cycle(12)
    assert second.api.failures == []
    result = second.api.chunk_entries["result"]
    assert result[:2] == recognized
    assert {e["source_attempt_id"] for e in result[2:]} == {second.attempt_id}
    # Chunks 2 and 3, then the summary.
    assert _uploads(second).count("RESULT_FILE") == 3


def test_zero_batch_checkpoint_cycles_commit_without_a_restore(tmp_path):
    # REM-R02: before the fix a second cycle at an unchanged cursor raised
    # "chunk batches are incomplete" (no upload was opened for its reservation).
    harness = _started(tmp_path)
    _checkpoint(harness, 1, (0,))
    _checkpoint(harness, 1, ())
    assert sorted(p["sequence"] for p in harness.api.published.values()) == [1, 2]
    assert harness.api.chunk_entries[2] == harness.api.chunk_entries[1]
    _checkpoint(harness, 2, (1,))
    assert harness.api.chunk_entries[3][:1] == harness.api.chunk_entries[1]
    assert _uploads(harness).count("RESULT_FILE") == 2


@pytest.mark.parametrize("fill", [0, 1], ids=["identical", "different"])
def test_output_for_a_recognized_chunk_at_a_checkpoint_fails_internal(tmp_path, fill):
    # Even identical bytes: a recognized chunk is carried, never recomputed.
    first, recognized, second = _older_restore(tmp_path)
    before = dict(second.api.recognized)
    _running(second, step=1)
    _write(second, 2, chunks=(1,), fill=fill)
    second.advance(6)
    second.cycle(12)
    assert _failures(second) == [("INTERNAL", "CHUNK_OUTPUT_CONFLICT")]
    assert second.api.published == {}
    assert _uploads(second).count("RESULT_FILE") == 0
    assert second.api.recognized == before


@pytest.mark.parametrize("fill", [0, 1], ids=["identical", "different"])
def test_output_for_a_recognized_chunk_at_the_result_fails_internal(tmp_path, fill):
    first, recognized, second = _older_restore(tmp_path)
    before = dict(second.api.recognized)
    _running(second, step=1)
    _finish(second, 4, (1, 2, 3), fill=fill)
    second.cycle(12)
    assert _failures(second) == [("INTERNAL", "CHUNK_OUTPUT_CONFLICT")]
    assert calls(second.api, "complete") == []
    assert second.api.recognized == before


@pytest.mark.parametrize("stage", ["checkpoint", "result"])
def test_claim_without_recognized_chunks_fails_a_conflict_deterministically(tmp_path, stage):
    # A claim acknowledged before the upgrade has no ``recognized_chunks``: the runner
    # recomputes chunk 1 under its own source and the server answers 409 with the
    # safe reason. The worker fails INTERNAL instead of replaying the publish forever.
    first, recognized, second = _older_restore(tmp_path, legacy=True)
    assert "recognized_chunks" not in second.launch_spec()
    before = dict(second.api.recognized)
    _running(second, step=1)
    if stage == "checkpoint":
        _write(second, 2, chunks=(1,))
        second.advance(6)
    else:
        _finish(second, 4, (1, 2, 3))
    second.cycle(12)
    assert _failures(second) == [("INTERNAL", "CHUNK_OUTPUT_CONFLICT")]
    assert second.api.recognized == before
    assert second.api.published == {}


def test_conflict_without_the_safe_reason_keeps_the_replay(tmp_path):
    first, _ = _first_attempt(tmp_path)
    restore, ids = _restore_of(first, 2)
    second = _resumed(tmp_path / "second", first, restore, ids, [])
    _running(second, step=2)
    second.api.conflict = WorkerApiError(409, "state_conflict")
    _write(second, 3, chunks=(2,))
    second.advance(6)
    second.cycle(6, tolerate=(WorkerApiError,))
    # An unclassified 409 stays a replay (existing behaviour), never a guessed failure.
    assert second.api.failures == []
    assert len(calls(second.api, "publish_checkpoint")) >= 2
    second.api.conflict = WorkerApiError(409, "state_conflict", reason="CHUNK_OUTPUT_CONFLICT")
    second.cycle(6)
    assert _failures(second) == [("INTERNAL", "CHUNK_OUTPUT_CONFLICT")]


def _extent(index):
    start, end = inference_state.chunk_extent(
        index, 1000, c.inference_claim()["spec"]["parameters"]["chunk_size"]
    )
    return {
        "chunk_id": inference_state.chunk_id(index),
        "start_index": start,
        "end_index_exclusive": end,
    }


@pytest.mark.parametrize(
    "defect",
    ["null", "index-gap", "wrong-range", "extra-field", "duplicate-artifact", "not-a-list"],
)
def test_unusable_recognized_chunks_fail_before_any_container(tmp_path, defect):
    first, recognized = _first_attempt(tmp_path)
    restore, ids = _restore_of(first, 1)
    item = _compact(recognized[1])
    value = {
        "null": None,
        "index-gap": [{**item, "chunk_id": inference_state.chunk_id(2)}],
        "wrong-range": [{**item, "start_index": item["start_index"] + 1}],
        "extra-field": [{**item, "session_id": first.context["logical_session_id"]}],
        "duplicate-artifact": [item, {**item, **_extent(2)}],
        "not-a-list": {"chunk-00000001": item},
    }[defect]
    second = _resumed(tmp_path / "second", first, restore, ids, value)
    # Nothing can carry or recompute the recognized chunk: no container, no retry.
    assert _failures(second) == [("INTERNAL", "CHUNK_OUTPUT_UNAVAILABLE")]
    assert second.backend.create_argv is None


def test_recognized_chunk_bytes_that_differ_fail_before_any_container(tmp_path):
    first, entries = _first_attempt(tmp_path)
    restore, ids = _restore_of(first, 1)
    artifact = entries[1]["file"]["artifact_id"]
    first.api.blobs[artifact] = first.api.blobs[artifact] + b"x"
    second = _resumed(tmp_path / "second", first, restore, ids, [_compact(entries[1])])
    # The download does not match the claimed artifact: neither carried nor recomputed.
    assert _failures(second) == [("INTERNAL", "CHUNK_OUTPUT_UNAVAILABLE")]
    assert second.backend.create_argv is None


def test_a_mounted_recognized_chunk_that_differs_fails_before_the_workload(tmp_path):
    first, recognized, second = _older_restore(tmp_path)
    (path,) = adapter_launch.recognized_paths(second.launch_spec())
    target = second.fs / path.lstrip("/")
    target.write_bytes(target.read_bytes() + b"x")
    assert second.runner._recognized_files_are_valid() is False
    target.unlink()
    assert second.runner._recognized_files_are_valid() is False


def test_recognized_mounts_must_match_the_recognized_chunks(tmp_path):
    first, recognized, second = _older_restore(tmp_path)
    binding = second.journal.load(second.attempt_id).execution_binding
    (mount,) = [
        m for m in binding["input_mounts"] if m["target_path"].startswith("/input/recognized/")
    ]
    assert mount["artifact_id"] == recognized[1]["file"]["artifact_id"]
    assert mount["content_checksum"] == recognized[1]["file"]["checksum"]


def test_newest_restore_launches_without_recognized_chunks(tmp_path):
    first, recognized = _first_attempt(tmp_path)
    restore, ids = _restore_of(first, 2)
    second = _resumed(tmp_path / "second", first, restore, ids, [])
    assert "recognized_chunks" not in second.launch_spec()
    _running(second, step=2)
    _checkpoint(second, 3, (2,))
    assert second.api.chunk_entries[1][:2] == recognized


def test_a_maximum_recognized_list_fits_one_pending_claim(tmp_path):
    authority = c.inference_claim()["authority"]
    context = c.inference_claim()
    fence = 2**53 - 1
    context["recognized_chunks"] = [
        {
            "chunk_id": inference_state.chunk_id(index),
            "start_index": index * 4096,
            "end_index_exclusive": (index + 1) * 4096,
            "artifact_id": str(new_uuid7()),
            "size_bytes": chunk_manifest.MAX_CHUNK_FILE_BYTES,
            "checksum": "sha256:" + "f" * 64,
            "source_attempt_id": str(new_uuid7()),
            "source_job_fence": fence,
        }
        for index in range(chunk_manifest.MAX_CHUNKS)
    ]
    store = PendingOperationStore(tmp_path / "pending", boot_id="test")
    callback = str(new_uuid7())
    store.begin(
        callback,
        operation="claim",
        payload={
            "attempt_id": authority["attempt_id"],
            "body": {"authority": authority},
            "offered_checkpoint": True,
        },
    )
    store.first_send(callback)
    store.acknowledge(
        callback, {"accepted": True, "callback_id": callback, "execution_context": context}
    )
    reloaded = PendingOperationStore(tmp_path / "pending", boot_id="test")
    stored = reloaded.operations[callback]["acknowledgment"]["execution_context"]
    assert stored["recognized_chunks"] == context["recognized_chunks"]


def _error_client(body):
    def respond(request):
        return httpx.Response(409, json=body)

    return WorkerApiClient("http://api.local", "secret", transport=httpx.MockTransport(respond))


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"code": "state_conflict", "reason": "CHUNK_OUTPUT_CONFLICT"}, "CHUNK_OUTPUT_CONFLICT"),
        ({"code": "state_conflict"}, None),
        ({"code": "state_conflict", "reason": "chunk output conflict"}, None),
        ({"code": "state_conflict", "reason": "A" * 65}, None),
        ({"code": "state_conflict", "reason": 7}, None),
        ({"code": "state_conflict", "reason": {"x": "SECRET"}}, None),
    ],
)
def test_worker_api_error_keeps_only_a_safe_reason(body, reason):
    client = _error_client(body)
    authority = asdict(Authority(**c.inference_claim()["authority"]))
    with pytest.raises(WorkerApiError) as raised:
        client.renew(authority["attempt_id"], str(new_uuid7()), {"authority": authority})
    client.close()
    assert (raised.value.status, raised.value.code, raised.value.reason) == (
        409,
        "state_conflict",
        reason,
    )
    assert "SECRET" not in str(raised.value)
