"""Remediation B11-H01 on real Docker: the worker dies after the claim commits, before /start.

Reuses the B15 stack (production API, coordinator thread, worker container, CPU workload
image). The claim is held on the Job row lock the test owns; while it waits the worker
container is SIGKILLed, then the lock is released so the claim commits for a dead process.
The next incarnation must stay not READY while the claim is live, prove after the reaper
fenced it that no container with the exact startup identity exists, release the quarantine
with one verified cleanup, become READY and run the recovered Job to the same result.

Evidence rows contain identifiers, states, timestamps and checksums only.
"""

import json
import subprocess
import time
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from nexa.infrastructure.persistence import schema as s
from tests.api.test_http_contract import _client
from tests.docker.test_b11_vertical import _worker_ready_with_new_incarnation
from tests.docker.test_b14_checkpoint_restore import _event_pairs, _ordered, _template, _wait
from tests.docker.test_b15_control_recovery import (
    LEASE_SECONDS,
    _B15Harness,
    _docker_images,
    _start_stack,
    _write_evidence,
)

pytestmark = [pytest.mark.docker, pytest.mark.postgres]


def _blocked_on_job_row(engine, holder_pid):
    with engine.connect() as observer:
        return observer.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE :pid = ANY(pg_blocking_pids(pid))"
                " AND query ILIKE '%FROM jobs%FOR UPDATE%'"
            ),
            {"pid": holder_pid},
        ).scalar_one()


_PENDING_PROBE = (
    "import json,sys\n"
    "state=json.load(open(sys.argv[1]))\n"
    "print(json.dumps(sorted([v['operation'],v['acknowledgment'] is not None]"
    " for v in state['operations'].values())))"
)


def _pending_operations(root, worker_image):
    """Operation names in the dead worker's callback store; no payload leaves the probe.

    The worker container owns the file (mode 0600), so it is read by a throwaway
    container of the worker image with the state root mounted read-only.
    """
    output = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--mount",
            f"type=bind,src={root},dst={root},readonly",
            worker_image,
            "python",
            "-c",
            _PENDING_PROBE,
            str(root / "agent-state.json"),
        ],
        text=True,
        timeout=60,
    )
    return [tuple(value) for value in json.loads(output)]


def test_rem_b11_h01_worker_killed_between_claim_commit_and_journal_on_docker(
    migrated_postgres_engine, tmp_path
):
    image, worker_image, architecture = _docker_images()
    engine = migrated_postgres_engine
    _template(engine, image, architecture)
    evidence = {"image": image, "architecture": architecture, "worker_image": worker_image}
    with _client(engine, tmp_path) as client:
        harness = _B15Harness(engine, tmp_path, client, image, worker_image)
        try:
            evidence["epoch"] = _start_stack(harness, engine)
            baseline = harness.submit("rem-b11-h01-baseline")
            harness.wait_succeeded(baseline)
            r0 = harness.result(baseline)
            harness.settle(baseline, "SUCCEEDED", 1)

            # Freeze the worker so the offer cannot be claimed before the Job row is held.
            subprocess.run(
                ["docker", "pause", harness.worker], check=True, capture_output=True, timeout=15
            )
            job = harness.submit("rem-b11-h01-claim-window")
            (first,) = _wait(lambda: harness.attempts(job), "attempt dispatched", timeout=10)
            assert first["state"] == "CREATED"
            with engine.connect() as holder:
                transaction = holder.begin()
                try:
                    pid = holder.execute(select(func.pg_backend_pid())).scalar_one()
                    holder.execute(
                        select(s.jobs.c.job_id)
                        .where(s.jobs.c.job_id == UUID(job))
                        .with_for_update()
                    )
                    subprocess.run(
                        ["docker", "unpause", harness.worker],
                        check=True,
                        capture_output=True,
                        timeout=15,
                    )
                    _wait(
                        lambda: _blocked_on_job_row(engine, pid),
                        "claim waiting on the Job row",
                        timeout=30,
                        pause=0.01,
                    )
                    prior = harness.kill_worker()
                    killed_at = time.monotonic()
                finally:
                    transaction.rollback()

            def claimed():
                (row,) = harness.attempts(job)
                return row if row["claimed_at"] is not None else None

            first = _wait(claimed, "claim committed after the worker died", timeout=15)
            record = harness.root / "journal" / "records" / f"{first['attempt_id']}.json"
            window = {
                "attempt_state": first["state"],
                "started": first["started_at"] is not None,
                "journal_record": record.exists(),
                "containers": harness.containers(first["attempt_id"]),
                "pending": _pending_operations(harness.root, worker_image),
            }
            assert window == {
                "attempt_state": "CLAIMED",
                "started": False,
                "journal_record": False,
                "containers": [],
                "pending": [("claim", False)],
            }, window

            harness.start_worker()
            # The new incarnation cannot prove anything while the old claim is live.
            live = harness.lease(first["attempt_id"])
            assert live["revoked_at"] is None
            assert not _worker_ready_with_new_incarnation(engine, prior)
            reaped = harness.wait_state(job, "RECOVERING", timeout=LEASE_SECONDS + 45)
            lease = harness.lease(first["attempt_id"])
            assert lease["revoke_reason"] == "LEASE_EXPIRED"
            _wait(
                lambda: _worker_ready_with_new_incarnation(engine, prior),
                "worker READY with a new incarnation",
                timeout=60,
            )
            ready_after = round(time.monotonic() - killed_at, 1)
            harness.wait_succeeded(job)
            r1 = harness.result(job)
            timeline = harness.settle(job, "SUCCEEDED", 2)
            attempt = timeline["attempts"][0]
            assert (attempt["state"], attempt["failure_class"], attempt["failure_reason"]) == (
                "LOST",
                "INFRASTRUCTURE",
                "LEASE_EXPIRED",
            )
            assert timeline["attempts"][0]["worker_incarnation_id"] == str(prior)
            assert timeline["attempts"][1]["worker_incarnation_id"] != str(prior)
            assert reaped["job_fence"] == first["job_fence"] + 1
            assert _ordered(
                _event_pairs(timeline),
                [
                    ("ATTEMPT_LOST", "LEASE_EXPIRED"),
                    ("ALLOCATION_RELEASED", "VERIFIED_CLEANUP"),
                    ("RETRY_READY", "BACKOFF_ELAPSED"),
                ],
            ), _event_pairs(timeline)
            assert r1["checksum"] == r0["checksum"]
            harness.assert_counters()
            evidence["claim_window"] = {
                **window,
                "attempt_id": str(first["attempt_id"]),
                "lease_expires_at": lease["expires_at"].isoformat(timespec="milliseconds"),
                "worker_ready_after_kill_seconds": ready_after,
                "checksum": r1["checksum"],
                "timeline": timeline,
            }
        finally:
            harness.close()
            _write_evidence(evidence, "-rem-b11-h01")
