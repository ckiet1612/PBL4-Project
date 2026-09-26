"""B14 restore selection, one-way corruption marks and fallback in the production claim."""

import os
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import insert, select, update

from nexa.api.schemas import CheckpointRecord
from nexa.infrastructure.artifacts.store import ArtifactError
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration._factories import seed_authority
from tests.integration.test_checkpoint_b14 import (
    CheckpointFixture,
    _worker_headers,
)
from tests.integration.test_worker_api_b10 import WORKER_ID, _inventory

pytestmark = pytest.mark.postgres


def _commit(fixture, *, step, accumulator):
    reserved = fixture.reserve()
    assert reserved.status_code == 201, reserved.text
    manifest, manifest_artifact, state_artifact = fixture.checkpoint(
        reserved.json(), step=step, accumulator=accumulator
    )
    published = fixture.publish(manifest, manifest_artifact)
    assert published.status_code == 201, published.text
    return {
        "record": published.json(),
        "manifest": manifest,
        "manifest_artifact": manifest_artifact,
        "state_artifact": state_artifact,
    }


def _next_attempt(fixture):
    """End attempt 1 as a verified failure would and seed attempt 2 like a retry dispatch."""
    now = datetime.now(UTC)
    old = fixture.authority
    with fixture.engine.begin() as connection:
        connection.execute(
            update(s.attempt_authority_grants)
            .where(s.attempt_authority_grants.c.grant_id == fixture.grant_id)
            .values(ended_at=now)
        )
        connection.execute(
            update(s.attempt_leases)
            .where(s.attempt_leases.c.lease_id == UUID(old["lease_id"]))
            .values(revoked_at=now, revoke_reason="RETRY")
        )
        connection.execute(
            update(s.allocations)
            .where(s.allocations.c.allocation_id == UUID(old["allocation_id"]))
            .values(state="RELEASED", released_at=now, release_reason="VERIFIED_CLEANUP")
        )
        connection.execute(
            update(s.attempts)
            .where(s.attempts.c.attempt_id == UUID(old["attempt_id"]))
            .values(
                state="FAILED",
                failure_class="INFRASTRUCTURE",
                failure_reason="WORKER_LOST",
                ended_at=now,
            )
        )
        connection.execute(
            update(s.jobs).where(s.jobs.c.job_id == fixture.job_id).values(state="DISPATCHING")
        )
        worker = {
            "worker_id": UUID(WORKER_ID),
            "incarnation_id": UUID(old["worker_incarnation_id"]),
        }
        ids = seed_authority(
            connection,
            fixture.graph,
            {"job_id": fixture.job_id},
            worker,
            attempt_number=2,
            job_fence=3,
        )
    fixture.grant_id = ids["grant_id"]
    fixture.authority = {
        **old,
        "attempt_id": str(ids["attempt_id"]),
        "allocation_id": str(ids["allocation_id"]),
        "lease_id": str(ids["lease_id"]),
        "job_fence": 3,
    }
    fixture.base = f"/v1/attempts/{ids['attempt_id']}"
    return fixture.authority


def _claim(fixture, *, callback_id=None):
    return fixture.post("/claim", {"authority": fixture.authority}, callback_id=callback_id)


def _blob_path(fixture, artifact_id):
    with fixture.engine.connect() as connection:
        key = connection.execute(
            select(s.artifacts.c.blob_key).where(s.artifacts.c.artifact_id == UUID(artifact_id))
        ).scalar_one()
    return fixture.client.app.state.services.artifact.store._path_for_key(key)


def _overwrite(path, content):
    os.chmod(path, 0o600)
    path.write_bytes(content)


def _corruptions(fixture):
    with fixture.engine.connect() as connection:
        return dict(
            connection.execute(
                select(
                    s.checkpoint_corruptions.c.checkpoint_id,
                    s.checkpoint_corruptions.c.reason_code,
                )
                .join(
                    s.checkpoints,
                    s.checkpoints.c.checkpoint_id == s.checkpoint_corruptions.c.checkpoint_id,
                )
                .where(s.checkpoints.c.job_id == fixture.job_id)
            ).all()
        )


def _event_types(fixture):
    return [row.event_type for row in fixture.rows()["events"]]


def _restore_events(fixture):
    return [
        (row.event_type, row.reason)
        for row in fixture.rows()["events"]
        if row.event_type.startswith("CHECKPOINT_") and row.event_type != "CHECKPOINT_COMMITTED"
    ]


def _job_versions(fixture):
    job = fixture.rows()["job"]
    return job["version"], job["event_sequence"]


def _assert_claim_kept_version(fixture, claimed, before, events):
    """Claim acknowledgment appends restore events but never changes the Job version."""
    version, sequence = before
    assert _job_versions(fixture) == (version, sequence + events)
    assert claimed.json()["job_version"] == version


def _start(fixture, context):
    return fixture.post(
        "/start",
        {
            "authority": fixture.authority,
            "startup_nonce": context["startup_nonce"],
            "executor_operation_sequence": 1,
            "container": {
                "container_id": "e" * 64,
                "runtime_identity_digest": "sha256:" + "e" * 64,
            },
        },
    )


@pytest.fixture
def restore_client(migrated_postgres_engine, tmp_path):
    with _client(migrated_postgres_engine, tmp_path) as client:
        yield client


@pytest.fixture
def checkpoint_job(migrated_postgres_engine, restore_client):
    return CheckpointFixture(migrated_postgres_engine, restore_client, label="restore")


def test_claim_selects_the_newest_valid_checkpoint_immutably(checkpoint_job):
    fixture = checkpoint_job
    _commit(fixture, step=20, accumulator=111)
    newest = _commit(fixture, step=40, accumulator=222)
    _next_attempt(fixture)
    before = _job_versions(fixture)

    callback = str(new_uuid7())
    claimed = _claim(fixture, callback_id=callback)
    assert claimed.status_code == 200, claimed.text
    _assert_claim_kept_version(fixture, claimed, before, 1)
    context = claimed.json()["execution_context"]
    restore = context["restore_checkpoint"]
    assert set(restore) == {"record", "manifest", "files"}
    assert CheckpointRecord.model_validate(restore["record"]).state == "COMMITTED"
    assert restore["record"] == newest["record"]
    assert restore["manifest"] == newest["manifest"]
    assert restore["files"] == [newest["state_artifact"]]

    replay = _claim(fixture, callback_id=callback)
    assert replay.status_code == 200 and replay.json() == claimed.json()
    again = _claim(fixture)
    assert again.status_code == 200 and again.json()["execution_context"] == context

    rows = fixture.rows()
    assert _restore_events(fixture) == [("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED")]
    assert rows["job"]["retry_count"] == 0
    assert rows["attempt"]["execution_context"] == context
    assert _corruptions(fixture) == {}

    state_id = newest["state_artifact"]["artifact_id"]
    download = fixture.client.get(
        f"{fixture.base}/execution-artifacts/{state_id}/content",
        headers=_worker_headers(fixture.credential, fixture.authority),
    )
    assert download.status_code == 200, download.text
    assert download.content == fixture.state(40, 222)
    assert _start(fixture, context).status_code == 200


def test_corrupt_checkpoints_are_marked_once_and_older_is_restored(checkpoint_job):
    fixture = checkpoint_job
    oldest = _commit(fixture, step=10, accumulator=7)
    middle = _commit(fixture, step=20, accumulator=8)
    newest = _commit(fixture, step=30, accumulator=9)
    state = _blob_path(fixture, newest["state_artifact"]["artifact_id"])
    original = state.read_bytes()
    _overwrite(state, original.replace(b"30", b"31"))
    _blob_path(fixture, middle["manifest_artifact"]["artifact_id"]).unlink()
    _next_attempt(fixture)
    before = _job_versions(fixture)

    claimed = _claim(fixture)
    assert claimed.status_code == 200, claimed.text
    _assert_claim_kept_version(fixture, claimed, before, 3)
    restore = claimed.json()["execution_context"]["restore_checkpoint"]
    assert restore["record"] == oldest["record"]
    assert _corruptions(fixture) == {
        UUID(newest["record"]["checkpoint_id"]): "CHECKPOINT_CHECKSUM_MISMATCH",
        UUID(middle["record"]["checkpoint_id"]): "CHECKPOINT_BLOB_MISSING",
    }
    assert _restore_events(fixture) == [
        ("CHECKPOINT_CORRUPT", "CHECKPOINT_CHECKSUM_MISMATCH"),
        ("CHECKPOINT_CORRUPT", "CHECKPOINT_BLOB_MISSING"),
        ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED"),
    ]
    events = _event_types(fixture)
    assert _claim(fixture).json()["execution_context"]["restore_checkpoint"] == restore
    assert _event_types(fixture) == events
    assert fixture.rows()["job"]["retry_count"] == 0


def test_no_valid_checkpoint_falls_back_to_input_only_when_restart_safe(checkpoint_job):
    fixture = checkpoint_job
    only = _commit(fixture, step=10, accumulator=5)
    manifest = _blob_path(fixture, only["manifest_artifact"]["artifact_id"])
    _overwrite(manifest, manifest.read_bytes()[:-1] + b" ")
    _next_attempt(fixture)
    before = _job_versions(fixture)

    claimed = _claim(fixture)
    assert claimed.status_code == 200, claimed.text
    _assert_claim_kept_version(fixture, claimed, before, 2)
    context = claimed.json()["execution_context"]
    assert context["restore_checkpoint"] is None
    assert _restore_events(fixture) == [
        ("CHECKPOINT_CORRUPT", "CHECKPOINT_CHECKSUM_MISMATCH"),
        ("CHECKPOINT_FALLBACK_TO_INPUT", "CHECKPOINT_FALLBACK_TO_INPUT"),
    ]
    assert _start(fixture, context).status_code == 200


def test_incompatible_architecture_stops_at_identical_compatibility(
    migrated_postgres_engine, tmp_path
):
    architectures = ["linux/amd64", "linux/arm64"]
    with _client(migrated_postgres_engine, tmp_path) as client:
        fixture = CheckpointFixture(
            migrated_postgres_engine, client, label="arch", architectures=architectures
        )
        _commit(fixture, step=10, accumulator=5)
        _commit(fixture, step=20, accumulator=6)
        inventory = _inventory()
        inventory["architecture"] = "linux/arm64"
        inventory["images"][0]["architecture"] = "linux/arm64"
        heartbeat = client.post(
            f"/v1/workers/{WORKER_ID}/heartbeat",
            headers={
                "Authorization": f"Bearer {fixture.credential}",
                "X-Callback-Id": str(new_uuid7()),
            },
            json={
                "worker_incarnation_id": fixture.authority["worker_incarnation_id"],
                "observed_health": "READY",
                "reconcile_complete": True,
                "inventory": inventory,
                "observed_containers": [],
            },
        )
        assert heartbeat.status_code == 200, heartbeat.text
        _next_attempt(fixture)
        before = _job_versions(fixture)

        claimed = _claim(fixture)
        assert claimed.status_code == 200, claimed.text
        _assert_claim_kept_version(fixture, claimed, before, 2)
        assert claimed.json()["execution_context"]["restore_checkpoint"] is None
        assert _restore_events(fixture) == [
            ("CHECKPOINT_INCOMPATIBLE", "CHECKPOINT_COMPATIBILITY_MISMATCH"),
            ("CHECKPOINT_FALLBACK_TO_INPUT", "CHECKPOINT_FALLBACK_TO_INPUT"),
        ]
        assert _corruptions(fixture) == {}


def _seed_admission_counters(fixture):
    with fixture.engine.begin() as connection:
        job = (
            connection.execute(select(s.jobs).where(s.jobs.c.job_id == fixture.job_id))
            .mappings()
            .one()
        )
        for scope, scope_id in (
            ("GLOBAL", "global"),
            ("TENANT", str(job["tenant_id"])),
            ("USER", f"{job['tenant_id']}:{job['submitter_user_id']}"),
        ):
            connection.execute(
                insert(s.admission_counters).values(
                    scope_type=scope, scope_id=scope_id, outstanding=1, active_attempts=1
                )
            )


def test_non_restart_safe_recovery_without_valid_checkpoint_fails_before_create(
    migrated_postgres_engine, tmp_path
):
    """Production worker + API: no container, no input replay, terminal INCOMPATIBLE."""
    from types import SimpleNamespace

    from nexa.worker.agent import WorkerAgent
    from nexa.worker.client import WorkerApiClient
    from nexa.worker.docker_client import DockerCli
    from nexa.worker.executor import DockerExecutor
    from nexa.worker.journal import ExecutionJournal
    from nexa.worker.state import PendingOperationStore
    from tests.worker.test_executor import FakeDocker

    with _client(migrated_postgres_engine, tmp_path) as client:
        # A catalog template id so the real poll offer validates its JobSpec.
        fixture = CheckpointFixture(
            migrated_postgres_engine,
            client,
            label="unsafe",
            restart_safe=False,
            template_id="cpu-iterative",
        )
        only = _commit(fixture, step=10, accumulator=5)
        _blob_path(fixture, only["state_artifact"]["artifact_id"]).unlink()
        authority = _next_attempt(fixture)
        _seed_admission_counters(fixture)
        with fixture.engine.begin() as connection:
            connection.execute(
                update(s.attempts)
                .where(s.attempts.c.attempt_id == UUID(authority["attempt_id"]))
                .values(dispatch_coordinator_epoch=1)
            )

        api = WorkerApiClient("http://testserver", fixture.credential, transport=client._transport)
        downloads = []
        download = api.download_execution
        api.download_execution = lambda *args: downloads.append(args) or download(*args)
        journal = ExecutionJournal(tmp_path / "journal")
        backend = FakeDocker()
        agent = WorkerAgent(
            worker_id=WORKER_ID,
            incarnation_id=authority["worker_incarnation_id"],
            installation_id="test",
            client=api,
            journal=journal,
            state=PendingOperationStore(tmp_path / "pending.json", boot_id="test"),
            docker=DockerCli(backend),
            executor=DockerExecutor(
                journal,
                backend,
                image_ref="registry.invalid/cpu",
                staging_root=tmp_path / "staging",
            ),
            provider=SimpleNamespace(discover=lambda: SimpleNamespace(architecture="linux/amd64")),
        )

        offer = api.poll(WORKER_ID, authority["worker_incarnation_id"])["offer"]
        assert offer["authority"] == authority
        # The newest committed checkpoint is still unmarked when the offer is made.
        assert offer["checkpoint"] == only["record"]
        agent._dispatch_offer(offer)

        rows = fixture.rows()
        assert rows["job"]["state"] == "FAILED"
        assert rows["job"]["retry_count"] == 0
        assert (rows["attempt"]["failure_class"], rows["attempt"]["failure_reason"]) == (
            "INCOMPATIBLE",
            "CHECKPOINT_RESTORE_UNAVAILABLE",
        )
        assert rows["attempt"]["started_at"] is None
        assert rows["attempt"]["execution_context"]["restore_checkpoint"] is None
        assert _restore_events(fixture) == [
            ("CHECKPOINT_CORRUPT", "CHECKPOINT_BLOB_MISSING"),
            ("CHECKPOINT_RESTORE_UNAVAILABLE", "CHECKPOINT_RESTORE_UNAVAILABLE"),
        ]
        failures = [
            (row.event_type, row.reason)
            for row in rows["events"]
            if row.event_type == "ATTEMPT_FAILED"
        ]
        assert failures == [("ATTEMPT_FAILED", "CHECKPOINT_RESTORE_UNAVAILABLE")]
        assert "STARTUP_TIMEOUT" not in {row.reason for row in rows["events"]}
        # No Docker create, no container identity and no input or restore download.
        assert backend.created == 0 and backend.started == []
        assert downloads == []
        assert not (
            tmp_path / "staging" / "downloads" / authority["attempt_id"] / "input.json"
        ).exists()
        with fixture.engine.connect() as connection:
            assert (
                connection.execute(
                    select(s.container_identities).where(
                        s.container_identities.c.attempt_id == UUID(authority["attempt_id"])
                    )
                ).first()
                is None
            )
            allocation = connection.execute(
                select(s.allocations.c.state).where(
                    s.allocations.c.allocation_id == UUID(authority["allocation_id"])
                )
            ).scalar_one()
        assert allocation == "RELEASED"
        assert journal.load(authority["attempt_id"]).state == "TOMBSTONED"
        assert agent.state.operations == {}


def test_non_restart_safe_recovery_refuses_start_without_restore(
    migrated_postgres_engine, tmp_path
):
    """Defense in depth: a worker that skips the local check still cannot start."""
    with _client(migrated_postgres_engine, tmp_path) as client:
        fixture = CheckpointFixture(
            migrated_postgres_engine, client, label="unsafe-start", restart_safe=False
        )
        only = _commit(fixture, step=10, accumulator=5)
        _blob_path(fixture, only["state_artifact"]["artifact_id"]).unlink()
        _next_attempt(fixture)
        before = _job_versions(fixture)

        claimed = _claim(fixture)
        assert claimed.status_code == 200, claimed.text
        _assert_claim_kept_version(fixture, claimed, before, 2)
        context = claimed.json()["execution_context"]
        assert context["restore_checkpoint"] is None
        started = _start(fixture, context)
        assert started.status_code == 409, started.text
        assert fixture.rows()["attempt"]["state"] == "CLAIMED"


def test_first_attempt_claim_has_no_restore_decision(checkpoint_job):
    fixture = checkpoint_job
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
    assert claimed.json()["execution_context"]["restore_checkpoint"] is None
    assert _restore_events(fixture) == []


def test_storage_outage_fails_closed_without_marks_or_context(checkpoint_job):
    fixture = checkpoint_job
    _commit(fixture, step=10, accumulator=5)
    _next_attempt(fixture)
    service = fixture.client.app.state.services.worker
    store = service.artifact_store
    original = store.open

    def unavailable(*_args, **_kwargs):
        raise ArtifactError("storage_unavailable", "Artifact storage is unavailable")

    store.open = unavailable
    try:
        offline = _claim(fixture)
    finally:
        store.open = original
    assert offline.status_code == 503, offline.text
    rows = fixture.rows()
    assert rows["attempt"]["execution_context"] is None
    assert rows["attempt"]["state"] == "CREATED"
    assert _corruptions(fixture) == {}
    assert _restore_events(fixture) == []
    recovered = _claim(fixture)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["execution_context"]["restore_checkpoint"] is not None


def test_unreported_worker_inventory_is_transient_not_incompatible(checkpoint_job):
    fixture = checkpoint_job
    newest = _commit(fixture, step=10, accumulator=5)
    _next_attempt(fixture)
    with fixture.engine.begin() as connection:
        version = connection.execute(
            select(s.workers.c.current_inventory_version).where(
                s.workers.c.worker_id == UUID(WORKER_ID)
            )
        ).scalar_one()
        # A restarted incarnation has not reported its inventory yet.
        connection.execute(
            update(s.workers)
            .where(s.workers.c.worker_id == UUID(WORKER_ID))
            .values(current_inventory_version=None)
        )
    unknown = _claim(fixture)
    assert unknown.status_code == 503, unknown.text
    assert unknown.headers["retry-after"] == "1"
    rows = fixture.rows()
    assert rows["attempt"]["execution_context"] is None
    assert rows["attempt"]["state"] == "CREATED"
    assert _corruptions(fixture) == {}
    assert _restore_events(fixture) == []
    with fixture.engine.begin() as connection:
        connection.execute(
            update(s.workers)
            .where(s.workers.c.worker_id == UUID(WORKER_ID))
            .values(current_inventory_version=version)
        )
    recovered = _claim(fixture)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["execution_context"]["restore_checkpoint"]["record"] == newest["record"]
