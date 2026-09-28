"""B16 opt-in real-Docker evidence: PyTorch CPU training, sweep and chunked inference.

Runs on a Linux Docker host (VPS1) with the frozen amd64 images and the frozen dataset and
model fixtures. Every scenario uses the production path: API + coordinator in process,
the B16 worker image as a container, workload containers from digest-pinned images.
Faults are SIGKILL of a workload container and rewriting or deleting one committed blob
on the test storage root while the coordinator is paused.

Evidence JSON (identifiers, states, checksums, metrics, sizes) is written to
``NEXA_B16_EVIDENCE_OUT`` when set; no credential or content bytes are recorded.
"""

import json
import os
import time
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from nexa.application import sweep_expansion
from nexa.infrastructure.persistence import schema as s
from tests.api.test_http_contract import _client
from tests.docker import b16_support as b16
from tests.docker.test_b11_vertical import _check, _worker_ready
from tests.docker.test_b14_checkpoint_restore import TERMINAL, _event_pairs, _ordered, _wait
from tests.integration.test_sweep_b16 import _admission_state

pytestmark = [pytest.mark.docker, pytest.mark.postgres]

# Peak cgroup memory per workload container fixed with the fixture (fixture.json sizing).
PEAK_LIMIT_BYTES = 768 * 1024**2
FIXTURES = b16.FIXTURES


def _write_evidence(name, evidence):
    out = os.environ.get("NEXA_B16_EVIDENCE_OUT")
    if out:
        path = Path(out) / f"B16-{name}.json"
        path.write_text(json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n")


def _images(env):
    rows = {}
    for key, fixture_name in (
        ("NEXA_B16_PYTORCH_IMAGE_REF", "pytorch-cifar10-v1"),
        ("NEXA_B16_INFERENCE_IMAGE_REF", "batch-inference-v1"),
    ):
        ref = env[key]
        frozen = b16.fixture(fixture_name)["image"]
        # A rebuilt image is a new digest: fixture.json image.history records it first.
        known = [frozen["image_digest"], *(row["image_digest"] for row in frozen["history"])]
        assert ref.rsplit("@", 1)[1] == known[-1], f"{key} is not the fixture's current image"
        rows[key] = {
            "ref": ref,
            "platform": b16.inspect_image(ref, "{{.Os}}/{{.Architecture}}"),
            "labels": json.loads(b16.inspect_image(ref, "{{json .Config.Labels}}")),
        }
        assert rows[key]["platform"] == frozen["platform"]
    rows["NEXA_B09_IMAGE_REF"] = {"ref": env["NEXA_B09_IMAGE_REF"]}
    rows["NEXA_B11_WORKER_IMAGE"] = {
        "ref": env["NEXA_B11_WORKER_IMAGE"],
        "id": b16.inspect_image(env["NEXA_B11_WORKER_IMAGE"], "{{.Id}}"),
    }
    return rows


def _start(harness, engine):
    harness.start_api()
    harness.start_worker()
    try:
        _wait(lambda: _worker_ready(engine), "worker READY", timeout=60)
    except AssertionError as exc:
        raise AssertionError(f"{exc}; worker_log={harness.worker_logs()}") from exc
    harness.coordinator.start()


def _hardened_while_running(harness, container, resources):
    """Inspect once the UID 1001 workload runs beside the runner, then check the oracle.

    A worker ``docker exec`` (control relay, output archive) passes through runc init
    inside the cgroup, still UID 0 and without seccomp until right before execve; such a
    snapshot shows runc, not the container's processes, so the oracle waits for one
    without it. Right after runc executes its cloned binary from ``/proc/self/fd/N`` the
    comm is still ``N`` (seen as ``6`` on VPS1), so runc init is also known by its argv.
    """

    def running():
        row = b16.hardening(container)
        processes = row["processes"]
        steady = not any(
            process["comm"] == "runc"
            or process["comm"].startswith("runc:[")
            or process["argv"] == ["runc", "init"]
            for process in processes
        )
        return steady and {"1001"} <= {process["uid"] for process in processes} and row

    row = _wait(running, "workload process in the container", timeout=60, pause=0.1)
    b16.assert_hardened(row, resources)
    return row


def _peaks(harness, sampler, job_id):
    peaks = {
        str(row["attempt_id"]): sampler.attempt_peak(str(row["attempt_id"]))
        for row in harness.attempts(job_id)
    }
    for attempt_id, peak in peaks.items():
        assert peak is not None, f"no memory sample for attempt {attempt_id}"
        assert peak <= PEAK_LIMIT_BYTES, f"attempt {attempt_id} peaked at {peak} bytes"
    return peaks


def _training_spec(entry, dataset_id, **overrides):
    job = entry["job"]
    return {
        "template_id": entry["template"]["template_id"],
        "template_version": entry["template"]["version"],
        "input_artifact_id": dataset_id,
        "resources": job["resources"],
        "priority": job["priority"],
        "runtime_limit_seconds": job["runtime_limit_seconds"],
        "checkpoint_interval_seconds": job["checkpoint_interval_seconds"],
        "parameters": job["parameters"],
        **overrides,
    }


def _spent(state):
    """What an admission spends: outstanding per scope and the rate buckets.

    The live coordinator may dispatch an accepted child meanwhile; that bumps the
    counter version with active_attempts (coordinator/dispatch.py), not outstanding.
    """
    counters, buckets = state
    return [(scope, outstanding) for scope, outstanding, _version in counters], buckets


def _metrics(harness, job_id):
    result_id, files = harness.result_files(job_id)
    assert sorted(files) == ["metrics.json", "model.safetensors"]
    return result_id, files, json.loads(files["metrics.json"][1])


def test_b16_pytorch_training_crash_resume_corruption_and_sweep(migrated_postgres_engine, tmp_path):
    env = b16.environment()
    entry = b16.fixture("pytorch-cifar10-v1")
    rules, tolerance_checksum = b16.tolerance()
    engine = migrated_postgres_engine
    images = _images(env)
    b16.register_templates(
        engine, env["NEXA_B16_PYTORCH_IMAGE_REF"], env["NEXA_B16_INFERENCE_IMAGE_REF"]
    )
    dataset = b16.data_file(env["NEXA_B16_DATA_DIR"], entry["dataset"])
    expected = entry["expected"]
    evidence = {
        "fixture": entry["fixture"],
        "images": images,
        "tolerance_checksum": tolerance_checksum,
        "dataset_checksum": entry["dataset"]["checksum"],
    }
    sampler = b16.MemorySampler().start()
    with _client(engine, tmp_path) as client:
        harness = b16.B16Harness(engine, tmp_path, client, env)
        try:
            harness.bootstrap()
            dataset_id = harness.upload(entry["dataset"], dataset)
            _start(harness, engine)
            spec = _training_spec(entry, dataset_id)
            resources = spec["resources"]

            # D1. Uninterrupted baseline with periodic checkpoints; hardening inspect.
            started = time.monotonic()
            baseline = harness.submit("b16-docker-train-baseline", spec)
            first, container = harness.running_attempt(baseline, 1)
            hardening_baseline = _hardened_while_running(harness, container, resources)
            harness.wait_succeeded(baseline, timeout=300)
            seconds = round(time.monotonic() - started, 1)
            _r0, files0, m0 = _metrics(harness, baseline)
            timeline = harness.reconcile(baseline, 1)
            assert (
                len(timeline["checkpoints"]) >= expected["min_committed_checkpoints_uninterrupted"]
            )
            assert list(timeline["restores"].values()) == [None]
            assert (m0["steps"], m0["epochs"]) == (expected["final_step"], expected["final_epoch"])
            provenance0 = b16.stable_provenance(harness.result_manifest(baseline)["provenance"])
            identity = harness.execution_identity(baseline)
            assert identity == [
                {
                    "adapter_id": entry["adapter"]["adapter_id"],
                    "adapter_version": entry["adapter"]["adapter_version"],
                    "image_digest": env["NEXA_B16_PYTORCH_IMAGE_REF"].rsplit("@", 1)[1],
                }
            ]
            evidence["D1_baseline"] = {
                "seconds": seconds,
                "metrics": m0,
                "result_files": {name: value[0] for name, value in files0.items()},
                "result_provenance": provenance0,
                "progress_snapshot": harness.progress(baseline),
                "hardening": hardening_baseline,
                "memory_peak_bytes": _peaks(harness, sampler, baseline),
                "timeline": timeline,
            }

            # D2. SIGKILL after the 2nd committed checkpoint: INFRASTRUCTURE retry restores
            # the newest checkpoint; metrics within tolerance of the baseline.
            job = harness.submit("b16-docker-train-crash-resume", spec)
            first, container = harness.running_attempt(job, 1)
            committed = harness.wait_committed(job, first["attempt_id"], 2)
            harness.kill_workload(container)
            harness.wait_retry_released(job, 1)
            second, container = harness.running_attempt(job, 2)
            hardening_restored = _hardened_while_running(harness, container, resources)
            harness.wait_succeeded(job, timeout=300)
            _r1, files1, m1 = _metrics(harness, job)
            timeline = harness.reconcile(job, 2)
            newest = max(harness.checkpoints(job, first["attempt_id"]), key=lambda row: row[1])
            assert newest[1] >= committed[-1][1]
            attempts = timeline["attempts"]
            assert (attempts[0]["failure_class"], attempts[0]["failure_reason"]) == (
                "INFRASTRUCTURE",
                "RUNNER_UNAVAILABLE",
            )
            assert timeline["restores"][attempts[1]["attempt_id"]] == {
                "checkpoint_id": str(newest[0]),
                "sequence": newest[1],
            }
            assert _ordered(
                _event_pairs(timeline),
                [
                    ("CHECKPOINT_COMMITTED", "CHECKPOINT_COMMITTED"),
                    ("CHECKPOINT_COMMITTED", "CHECKPOINT_COMMITTED"),
                    ("ATTEMPT_FAILED", "RUNNER_UNAVAILABLE"),
                    ("ALLOCATION_RELEASED", "VERIFIED_CLEANUP"),
                    ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED"),
                ],
            ), _event_pairs(timeline)
            restored_cursor = harness.checkpoint_manifest(newest[0])["cursor"]
            comparison = b16.compare_training(m0, m1, rules)
            assert comparison["within_tolerance"], comparison
            provenance1 = b16.stable_provenance(harness.result_manifest(job)["provenance"])
            assert provenance1 == provenance0
            evidence["D2_crash_resume"] = {
                "restored_cursor": restored_cursor,
                "metrics": m1,
                "comparison": comparison,
                "bitwise": {name: files1[name][0] == files0[name][0] for name in files0},
                "result_provenance_equal": True,
                "progress_snapshot": harness.progress(job),
                "hardening_restored_attempt": hardening_restored,
                "memory_peak_bytes": _peaks(harness, sampler, job),
                "timeline": timeline,
            }

            # D3. Corrupt the newest checkpoint's model file while the coordinator is
            # paused: CORRUPT + visible fallback to the previous checkpoint.
            job = harness.submit("b16-docker-train-corrupt-newest", spec)
            first, container = harness.running_attempt(job, 1)
            harness.wait_committed(job, first["attempt_id"], 2)
            harness.coordinator.pause()
            harness.kill_workload(container)
            harness.wait_retry_released(job, 1)
            committed = harness.checkpoints(job)
            newest, previous = committed[-1], committed[-2]
            harness.corrupt_blob(harness.checkpoint_file(newest[0], "model.safetensors"))
            harness.coordinator.start()
            harness.wait_succeeded(job, timeout=300)
            _r2, files2, m2 = _metrics(harness, job)
            timeline = harness.reconcile(job, 2)
            second = timeline["attempts"][1]["attempt_id"]
            assert timeline["restores"][second] == {
                "checkpoint_id": str(previous[0]),
                "sequence": previous[1],
            }
            corrupt = {row["checkpoint_id"]: row["corrupt"] for row in timeline["checkpoints"]}
            assert corrupt[str(newest[0])] == "CHECKPOINT_CHECKSUM_MISMATCH"
            assert [value for value in corrupt.values() if value] == [
                "CHECKPOINT_CHECKSUM_MISMATCH"
            ]
            assert _ordered(
                _event_pairs(timeline),
                [
                    ("ATTEMPT_FAILED", "RUNNER_UNAVAILABLE"),
                    ("CHECKPOINT_CORRUPT", "CHECKPOINT_CHECKSUM_MISMATCH"),
                    ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED"),
                ],
            ), _event_pairs(timeline)
            comparison = b16.compare_training(m0, m2, rules)
            assert comparison["within_tolerance"], comparison
            assert b16.stable_provenance(harness.result_manifest(job)["provenance"]) == provenance0
            evidence["D3_corrupt_newest"] = {
                "corrupted_checkpoint": {"checkpoint_id": str(newest[0]), "sequence": newest[1]},
                "restored_cursor": harness.checkpoint_manifest(previous[0])["cursor"],
                "metrics": m2,
                "comparison": comparison,
                "bitwise": {name: files2[name][0] == files0[name][0] for name in files0},
                "memory_peak_bytes": _peaks(harness, sampler, job),
                "timeline": timeline,
            }

            # D7. Sweep of the frozen request: outstanding_limit 2 admits two children and
            # rejects four; children are normal jobs; replay spends nothing.
            request = json.loads(
                (FIXTURES / "hyperparameter-sweep-v1" / "request.json").read_text()
            )
            golden = json.loads(
                (FIXTURES / "hyperparameter-sweep-v1" / "expansion.json").read_text()
            )
            request["base_spec"]["input_artifact_id"] = dataset_id
            with engine.connect() as connection:
                before_jobs = set(
                    str(value) for value in connection.execute(select(s.jobs.c.job_id)).scalars()
                )
            harness.policy(outstanding_limit=2)
            counters_before = _admission_state(engine, harness.tenant_id)
            headers = {**harness.write, "Idempotency-Key": "b16-docker-sweep-0001"}
            first_post = client.post("/v1/sweeps", headers=headers, json=request)
            sweep = _check(first_post, 207)
            children = sweep["children"]
            assert (sweep["child_count"], sweep["accepted_count"], sweep["rejected_count"]) == (
                6,
                2,
                4,
            )
            assert [c["parameter_hash"] for c in children] == [
                c["parameter_hash"] for c in golden["children"]
            ]
            assert [c["status"] for c in children] == ["ACCEPTED"] * 2 + ["REJECTED"] * 4
            assert all(c["job_id"] is None for c in children[2:])
            assert {c["error"]["code"] for c in children[2:]} == {"quota_exceeded"}
            accepted = [c["job_id"] for c in children[:2]]
            for child in children[:2]:
                harness.submitted[child["job_id"]] = sweep_expansion.child_idempotency_key(
                    UUID(sweep["sweep_id"]), child["child_index"], child["parameter_hash"]
                )
            counters_after = _admission_state(engine, harness.tenant_id)
            replay = _check(client.post("/v1/sweeps", headers=headers, json=request), 207)
            assert replay == sweep
            assert _spent(_admission_state(engine, harness.tenant_id)) == _spent(counters_after)
            for job_id in accepted:
                harness.wait_succeeded(job_id, timeout=300)
            # Global counters are checked per child, so both children release first.
            for job_id in accepted:
                _released(harness, job_id)
            for job_id in accepted:
                _settled(harness, job_id)
            child_rows = {}
            for job_id in accepted:
                _result, files, metrics = _metrics(harness, job_id)
                child_rows[job_id] = {
                    "metrics": metrics,
                    "result_files": {name: value[0] for name, value in files.items()},
                    "timeline": harness.reconcile(job_id, 1),
                    "memory_peak_bytes": _peaks(harness, sampler, job_id),
                }
            with engine.connect() as connection:
                jobs_now = set(
                    str(value) for value in connection.execute(select(s.jobs.c.job_id)).scalars()
                )
                allocation_jobs = set(
                    str(value)
                    for value in connection.execute(select(s.allocations.c.job_id)).scalars()
                )
            # The parent is not a job: exactly the two accepted children were created and
            # every allocation belongs to a job.
            assert jobs_now - before_jobs == set(accepted)
            assert allocation_jobs <= jobs_now
            parent = _check(
                client.get(
                    f"/v1/sweeps/{sweep['sweep_id']}",
                    headers={"X-Nexa-Tenant-Id": harness.tenant_id},
                ),
                200,
            )
            evidence["D7_sweep"] = {
                "sweep_id": sweep["sweep_id"],
                "response": sweep,
                "replay_identical": True,
                "counters_before": counters_before,
                "counters_after_admission": counters_after,
                "parent_view": parent,
                "children": child_rows,
            }
            evidence["memory_samples"] = sampler.peaks
        finally:
            sampler.close()
            _write_evidence("training", evidence)
            harness.close()


def _inference_spec(entry, dataset_id, model_id, **overrides):
    job = entry["job"]
    return {
        "template_id": entry["template"]["template_id"],
        "template_version": entry["template"]["version"],
        "input_artifact_id": dataset_id,
        "model_artifact_id": model_id,
        "resources": job["resources"],
        "priority": job["priority"],
        "runtime_limit_seconds": job["runtime_limit_seconds"],
        "checkpoint_interval_seconds": job["checkpoint_interval_seconds"],
        "parameters": job["parameters"],
        **overrides,
    }


def _summary(harness, job_id):
    result_id, files = harness.result_files(job_id)
    assert sorted(files) == ["summary.json"]
    checksum, raw = files["summary.json"]
    return result_id, checksum, json.loads(raw)


def _released(harness, job_id):
    def released():
        rows = harness.timeline(job_id)
        return all(row["state"] == "RELEASED" for row in rows["allocations"]) and rows

    return _wait(released, f"allocation release of {job_id}", timeout=60)


def _settled(harness, job_id):
    """Non-success end: allocations released after proof, no container, counters zero."""
    timeline = _released(harness, job_id)
    for attempt in timeline["attempts"]:
        assert harness.containers(attempt["attempt_id"]) == [], "leaked workload container"
    assert all(row["stopped"] and row["verified"] for row in timeline["containers"])
    assert all(row["revoked"] for row in timeline["leases"])
    assert all(row["ended"] for row in timeline["grants"])
    with harness.engine.connect() as connection:
        counters = set(
            connection.execute(
                select(s.admission_counters.c.outstanding, s.admission_counters.c.active_attempts)
            ).all()
        )
    assert counters == {(0, 0)}
    return timeline


def test_b16_chunked_inference_carry_forward_and_blob_loss(migrated_postgres_engine, tmp_path):
    env = b16.environment()
    entry = b16.fixture("batch-inference-v1")
    engine = migrated_postgres_engine
    images = _images(env)
    b16.register_templates(
        engine, env["NEXA_B16_PYTORCH_IMAGE_REF"], env["NEXA_B16_INFERENCE_IMAGE_REF"]
    )
    dataset = b16.data_file(env["NEXA_B16_DATA_DIR"], entry["dataset"])
    model = b16.data_file(env["NEXA_B16_DATA_DIR"], entry["model"])
    item_count = entry["dataset"]["item_count"]
    chunk_size = entry["job"]["parameters"]["chunk_size"]
    chunk_count = entry["chunks"]["chunk_count"]
    evidence = {
        "fixture": entry["fixture"],
        "images": images,
        "dataset_checksum": entry["dataset"]["checksum"],
        "model_checksum": entry["model"]["checksum"],
    }
    sampler = b16.MemorySampler().start()
    with _client(engine, tmp_path) as client:
        harness = b16.B16Harness(engine, tmp_path, client, env)
        try:
            harness.bootstrap()
            dataset_id = harness.upload(entry["dataset"], dataset)
            model_id = harness.upload(entry["model"], model)
            _start(harness, engine)
            spec = _inference_spec(entry, dataset_id, model_id)
            resources = spec["resources"]

            # D4. Uninterrupted baseline: 40 recognized chunks covering [0, 2000).
            started = time.monotonic()
            baseline = harness.submit("b16-docker-infer-baseline", spec)
            _first, container = harness.running_attempt(baseline, 1)
            hardening_baseline = _hardened_while_running(harness, container, resources)
            harness.wait_succeeded(baseline, timeout=300)
            seconds = round(time.monotonic() - started, 1)
            _r0, summary_checksum0, summary0 = _summary(harness, baseline)
            timeline = harness.reconcile(baseline, 1)
            chunks0 = harness.chunks(baseline)
            reference = {row["chunk_id"]: row["checksum"] for row in chunks0}
            report0 = b16.chunk_report(chunks0, reference, item_count, chunk_size)
            assert report0["recognized"] == report0["distinct_chunk_ids"] == chunk_count
            assert report0["coverage_exact"] and report0["ids_formatted"]
            assert summary0["chunk_count"] == chunk_count
            assert sum(summary0["prediction_counts"]) == item_count
            assert list(timeline["restores"].values()) == [None]
            evidence["D4_baseline"] = {
                "seconds": seconds,
                "checkpoints": len(timeline["checkpoints"]),
                "chunk_report": report0,
                "chunk_checksums": reference,
                "summary_checksum": summary_checksum0,
                "prediction_counts": summary0["prediction_counts"],
                "result_provenance": b16.stable_provenance(
                    harness.result_manifest(baseline)["provenance"]
                ),
                "hardening": hardening_baseline,
                "memory_peak_bytes": _peaks(harness, sampler, baseline),
                "timeline": timeline,
            }

            # D5. SIGKILL after 2 committed checkpoints: the retry restores the newest
            # cursor and carries its recognized chunks forward; no chunk is recognized twice.
            job = harness.submit("b16-docker-infer-crash-resume", spec)
            first, container = harness.running_attempt(job, 1)
            harness.wait_committed(job, first["attempt_id"], 2)
            harness.kill_workload(container)
            harness.wait_retry_released(job, 1)
            recognized_at_kill = harness.chunks(job)
            harness.wait_succeeded(job, timeout=300)
            _r1, summary_checksum1, summary1 = _summary(harness, job)
            timeline = harness.reconcile(job, 2)
            newest = max(harness.checkpoints(job, first["attempt_id"]), key=lambda row: row[1])
            second = timeline["attempts"][1]["attempt_id"]
            assert timeline["restores"][second] == {
                "checkpoint_id": str(newest[0]),
                "sequence": newest[1],
            }
            cursor = harness.checkpoint_manifest(newest[0])["cursor"]
            chunks1 = harness.chunks(job)
            report1 = b16.chunk_report(chunks1, reference, item_count, chunk_size)
            assert report1["recognized"] == report1["distinct_chunk_ids"] == chunk_count
            assert report1["coverage_exact"] and report1["checksums_equal_baseline"], report1
            assert report1["chunks_per_source_attempt"] == {
                str(first["attempt_id"]): cursor["step"],
                second: chunk_count - cursor["step"],
            }
            # Carry-forward: the rows recognized before the kill are unchanged.
            assert [row for row in chunks1 if row["source_attempt_id"] == first["attempt_id"]] == (
                recognized_at_kill[: cursor["step"]]
            )
            assert summary1["prediction_counts"] == summary0["prediction_counts"]
            assert _ordered(
                _event_pairs(timeline),
                [
                    ("ATTEMPT_FAILED", "RUNNER_UNAVAILABLE"),
                    ("ALLOCATION_RELEASED", "VERIFIED_CLEANUP"),
                    ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED"),
                ],
            ), _event_pairs(timeline)
            evidence["D5_crash_resume"] = {
                "restored_cursor": cursor,
                "recognized_at_kill": len(recognized_at_kill),
                "chunk_report": report1,
                "summary_checksum": summary_checksum1,
                "summary_bitwise_equal": summary_checksum1 == summary_checksum0,
                "memory_peak_bytes": _peaks(harness, sampler, job),
                "timeline": timeline,
            }

            # D6. Delete a chunk blob named only by the newest checkpoint (coordinator
            # paused): the newest is CORRUPT CHECKPOINT_BLOB_MISSING and the retry falls back
            # to the previous checkpoint. The recomputed chunks after that cursor were
            # already recognized by attempt 1, so they are refused (open finding B16-R21):
            # the oracle is no changed recognition and no result. How the refused attempt
            # ends is recorded, not asserted; the short runtime limit bounds the run.
            short = _inference_spec(entry, dataset_id, model_id, runtime_limit_seconds=90)
            job = harness.submit("b16-docker-infer-blob-missing", short)
            first, container = harness.running_attempt(job, 1)
            harness.wait_committed(job, first["attempt_id"], 2)
            harness.coordinator.pause()
            harness.kill_workload(container)
            harness.wait_retry_released(job, 1)
            committed = harness.checkpoints(job)
            newest, previous = committed[-1], committed[-2]
            newest_cursor = harness.checkpoint_manifest(newest[0])["cursor"]
            previous_cursor = harness.checkpoint_manifest(previous[0])["cursor"]
            assert newest_cursor["step"] > previous_cursor["step"], (newest_cursor, previous_cursor)
            before = harness.chunks(job)
            victim = before[previous_cursor["step"]]
            harness.delete_blob(victim["artifact_id"])
            harness.coordinator.start()
            ended = harness.wait_state(job, TERMINAL, timeout=600)
            timeline = _settled(harness, job)
            second = timeline["attempts"][1]["attempt_id"]
            corrupt = {row["checkpoint_id"]: row["corrupt"] for row in timeline["checkpoints"]}
            assert corrupt[str(newest[0])] == "CHECKPOINT_BLOB_MISSING"
            assert timeline["restores"][second] == {
                "checkpoint_id": str(previous[0]),
                "sequence": previous[1],
            }
            assert _ordered(
                _event_pairs(timeline),
                [
                    ("ATTEMPT_FAILED", "RUNNER_UNAVAILABLE"),
                    ("CHECKPOINT_CORRUPT", "CHECKPOINT_BLOB_MISSING"),
                    ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED"),
                ],
            ), _event_pairs(timeline)
            after = harness.chunks(job)
            assert ended["state"] != "SUCCEEDED"
            assert timeline["results"] == []
            # No recognition is replaced or re-attributed; later attempts may only add
            # chunks beyond every earlier recognition.
            assert after[: len(before)] == before
            evidence["D6_blob_missing"] = {
                "deleted_chunk": {
                    "chunk_id": victim["chunk_id"],
                    "artifact_id": str(victim["artifact_id"]),
                },
                "newest": {"checkpoint_id": str(newest[0]), "cursor": newest_cursor},
                "previous": {"checkpoint_id": str(previous[0]), "cursor": previous_cursor},
                "recognized_before": len(before),
                "recognized_after": len(after),
                "terminal_state": ended["state"],
                "attempt_failures": [
                    (row["failure_class"], row["failure_reason"]) for row in timeline["attempts"]
                ],
                "memory_peak_bytes": _peaks(harness, sampler, job),
                "timeline": timeline,
            }
            evidence["memory_samples"] = sampler.peaks
        finally:
            sampler.close()
            _write_evidence("inference", evidence)
            harness.close()


def test_b16_workload_oom_is_typed_and_not_retried(migrated_postgres_engine, tmp_path):
    """B16-R26: a workload over its container memory limit fails OOM once, no retry.

    Test-only template version 2 (b16.register_oom_templates) lowers the memory floor to
    256 MiB; the RAM sizing (B16-ram.json) peaks at about 409 MiB for training batch 512
    and about 506 MiB for inference chunk/batch 2000, so both exceed the limit.
    """
    env = b16.environment()
    training = b16.fixture("pytorch-cifar10-v1")
    inference = b16.fixture("batch-inference-v1")
    engine = migrated_postgres_engine
    images = _images(env)
    for register in (b16.register_templates, b16.register_oom_templates):
        register(engine, env["NEXA_B16_PYTORCH_IMAGE_REF"], env["NEXA_B16_INFERENCE_IMAGE_REF"])
    train_data = b16.data_file(env["NEXA_B16_DATA_DIR"], training["dataset"])
    infer_data = b16.data_file(env["NEXA_B16_DATA_DIR"], inference["dataset"])
    model = b16.data_file(env["NEXA_B16_DATA_DIR"], inference["model"])
    evidence = {
        "template_version": b16.OOM_TEMPLATE_VERSION,
        "memory_bytes": b16.OOM_MEMORY_BYTES,
        "images": images,
    }
    sampler = b16.MemorySampler().start()
    with _client(engine, tmp_path) as client:
        harness = b16.B16Harness(engine, tmp_path, client, env)
        try:
            harness.bootstrap()
            train_id = harness.upload(training["dataset"], train_data)
            infer_id = harness.upload(inference["dataset"], infer_data)
            model_id = harness.upload(inference["model"], model)
            _start(harness, engine)
            small = {**training["job"]["resources"], "memory_bytes": b16.OOM_MEMORY_BYTES}
            specs = {
                "training": _training_spec(
                    training,
                    train_id,
                    template_version=b16.OOM_TEMPLATE_VERSION,
                    resources=small,
                    parameters={
                        **training["job"]["parameters"],
                        "batch_size": 512,
                        "subset_size": 5000,
                    },
                ),
                "inference": _inference_spec(
                    inference,
                    infer_id,
                    model_id,
                    template_version=b16.OOM_TEMPLATE_VERSION,
                    resources={
                        **inference["job"]["resources"],
                        "memory_bytes": small["memory_bytes"],
                    },
                    parameters={
                        **inference["job"]["parameters"],
                        "chunk_size": 2000,
                        "batch_size": 2000,
                    },
                ),
            }
            for name, spec in specs.items():
                started = time.monotonic()
                job_id = harness.submit(f"b16-docker-oom-{name}", spec)
                ended = harness.wait_state(job_id, TERMINAL, timeout=300)
                seconds = round(time.monotonic() - started, 1)
                timeline = _settled(harness, job_id)
                job = harness.job(job_id)
                attempts = [
                    (row["attempt_number"], row["failure_class"], row["failure_reason"])
                    for row in harness.attempts(job_id)
                ]
                evidence[name] = {
                    "job_id": job_id,
                    "spec": {key: spec[key] for key in ("template_version", "resources")},
                    "parameters": spec["parameters"],
                    "seconds": seconds,
                    "terminal_state": ended["state"],
                    "retry_count": job["retry_count"],
                    "attempts": attempts,
                    "memory_peak_bytes": {
                        str(row["attempt_id"]): sampler.attempt_peak(str(row["attempt_id"]))
                        for row in harness.attempts(job_id)
                    },
                    "events": _event_pairs(timeline),
                    "timeline": timeline,
                }
                assert ended["state"] == "FAILED", evidence[name]
                assert attempts == [(1, "OOM", "CONTAINER_OOM")], evidence[name]
                assert (job["retry_count"], job["retry_ready_at"]) == (0, None)
                assert timeline["results"] == []
                assert ("ATTEMPT_FAILED", "CONTAINER_OOM") in _event_pairs(timeline)
        finally:
            sampler.close()
            _write_evidence("oom", evidence)
            harness.close()
