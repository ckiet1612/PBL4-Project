"""B14 fenced checkpoint reservation and publish over production HTTP and PostgreSQL."""

import hashlib
from datetime import UTC, datetime
from uuid import UUID

import pytest
import rfc8785
from sqlalchemy import func, insert, select, update

from nexa.application.errors import ApplicationError
from nexa.infrastructure.artifacts.store import ArtifactError
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration._factories import seed_authority, seed_job, seed_tenant_graph
from tests.integration.test_worker_api_b10 import (
    WORKER_ID,
    _bootstrap_worker,
    _create_incarnation,
    _inventory,
)
from tests.integration.test_worker_authority_b10 import CONTAINER_ID, DIGEST

pytestmark = pytest.mark.postgres

PARAMETERS = {"iterations": 100, "seed": 3, "modulus": 1_000_003}
REQUIREMENTS = {
    "architectures": ["linux/amd64"],
    "adapter_id": "cpu.iterative",
    "adapter_version": "1.0.0",
    "device": "CPU",
    "framework": "NEXA_CPU",
    "framework_version": "1.0.0",
    "cuda_runtime_min": None,
    "driver_min": None,
    "compute_capability_min": None,
}


def _checksum(content):
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _timestamp():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _worker_headers(credential, authority):
    return {
        "Authorization": f"Bearer {credential}",
        "X-Worker-Id": authority["worker_id"],
        "X-Worker-Incarnation-Id": authority["worker_incarnation_id"],
        "X-Allocation-Id": authority["allocation_id"],
        "X-Lease-Id": authority["lease_id"],
        "X-Job-Fence": str(authority["job_fence"]),
    }


def _upload(client, credential, authority, kind, content, key, expected=201):
    response = client.post(
        f"/v1/attempts/{authority['attempt_id']}/artifacts",
        headers={
            **_worker_headers(credential, authority),
            "Content-Type": "application/octet-stream",
            "Idempotency-Key": key,
            "X-Artifact-Kind": kind,
            "X-Artifact-Media-Type": "application/json",
            "X-Artifact-Checksum": _checksum(content),
            "X-Artifact-Size": str(len(content)),
        },
        content=content,
    )
    assert response.status_code == expected, response.text
    return response.json()


class CheckpointFixture:
    """One RUNNING attempt of a checkpointable CPU job with a ready worker inventory."""

    def __init__(
        self,
        engine,
        client,
        *,
        label,
        checkpointable=True,
        restart_safe=True,
        architectures=("linux/amd64",),
        template_id=None,
    ):
        self.engine = engine
        self.client = client
        self.credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, self.credential, nonce=str(new_uuid7()), key=f"b14-{label}-incarnation"
        )
        reconciliation = client.get(
            f"/v1/workers/{WORKER_ID}/reconciliation?page_size=100",
            headers={
                "Authorization": f"Bearer {self.credential}",
                "X-Worker-Incarnation-Id": incarnation["worker_incarnation_id"],
            },
        )
        assert reconciliation.status_code == 200, reconciliation.text
        heartbeat = client.post(
            f"/v1/workers/{WORKER_ID}/heartbeat",
            headers={
                "Authorization": f"Bearer {self.credential}",
                "X-Callback-Id": str(new_uuid7()),
            },
            json={
                "worker_incarnation_id": incarnation["worker_incarnation_id"],
                "observed_health": "READY",
                "reconcile_complete": True,
                "inventory": _inventory(),
                "observed_containers": [],
            },
        )
        assert heartbeat.status_code == 200, heartbeat.text
        content = rfc8785.dumps({"schema_version": 1, "values": [1, 2, 3]})
        store = client.app.state.services.artifact.store
        staged = store.begin_staging(
            f"b14-{label}-input", len(content), _checksum(content), "application/json"
        )
        store.append(staged, content)
        blob = store.commit_blob(staged)
        input_id = new_uuid7()
        with engine.begin() as connection:
            graph = seed_tenant_graph(connection, label=f"b14-{label}", template_id=template_id)
            connection.execute(
                insert(s.artifacts).values(
                    artifact_id=input_id,
                    tenant_id=graph["tenant_id"],
                    kind="INPUT",
                    media_type="application/json",
                    size_bytes=len(content),
                    checksum=blob.checksum,
                    blob_key=blob.blob_key,
                    state="COMMITTED",
                    version=1,
                )
            )
            template = (
                connection.execute(
                    select(s.template_versions).where(
                        s.template_versions.c.template_id == graph["template_id"]
                    )
                )
                .mappings()
                .one()
            )
            connection.execute(
                update(s.template_versions)
                .where(s.template_versions.c.template_id == graph["template_id"])
                .values(
                    capability_requirements={
                        **REQUIREMENTS,
                        "architectures": list(architectures),
                        "image_digest": template["image_digest"],
                    },
                    checkpointable=checkpointable,
                    restart_safe=restart_safe,
                )
            )
            canonical_spec = {"template_id": graph["template_id"], "parameters": PARAMETERS}
            if template_id is not None:
                # The closed wire JobSpec, so a real poll offer validates.
                canonical_spec = {
                    "template_id": template_id,
                    "template_version": 1,
                    "input_artifact_id": str(input_id),
                    "resources": {
                        "cpu_millis": 1000,
                        "memory_bytes": 1_073_741_824,
                        "gpu_count": 0,
                    },
                    "priority": 1,
                    "runtime_limit_seconds": 300,
                    "checkpoint_interval_seconds": 30,
                    "parameters": PARAMETERS,
                }
            job = seed_job(
                connection,
                graph,
                state="RUNNING",
                artifact_id=input_id,
                canonical_spec=canonical_spec,
            )
            worker = {
                "worker_id": UUID(WORKER_ID),
                "incarnation_id": UUID(incarnation["worker_incarnation_id"]),
            }
            ids = seed_authority(connection, graph, job, worker)
            connection.execute(
                update(s.attempts)
                .where(s.attempts.c.attempt_id == ids["attempt_id"])
                .values(state="RUNNING", started_at=datetime.now(UTC))
            )
            nonce = connection.execute(
                select(s.attempts.c.startup_nonce).where(
                    s.attempts.c.attempt_id == ids["attempt_id"]
                )
            ).scalar_one()
            connection.execute(
                insert(s.container_identities).values(
                    tenant_id=graph["tenant_id"],
                    job_id=job["job_id"],
                    attempt_id=ids["attempt_id"],
                    allocation_id=ids["allocation_id"],
                    startup_nonce=nonce,
                    executor_create_sequence=1,
                    container_id=CONTAINER_ID,
                    runtime_identity_digest=DIGEST,
                    created_at=datetime.now(UTC),
                )
            )
            spec = (
                connection.execute(select(s.job_specs).where(s.job_specs.c.job_id == job["job_id"]))
                .mappings()
                .one()
            )
        self.graph = graph
        self.job_id = job["job_id"]
        self.session_id = job["session_id"]
        self.input_checksum = blob.checksum
        self.spec_checksum = spec["spec_checksum"]
        self.template_id = graph["template_id"]
        self.image_digest = template["image_digest"]
        self.checkpointable = checkpointable
        self.restart_safe = restart_safe
        self.grant_id = ids["grant_id"]
        self.authority = {
            "worker_id": WORKER_ID,
            "worker_incarnation_id": incarnation["worker_incarnation_id"],
            "attempt_id": str(ids["attempt_id"]),
            "allocation_id": str(ids["allocation_id"]),
            "lease_id": str(ids["lease_id"]),
            "job_fence": 1,
        }
        self.base = f"/v1/attempts/{ids['attempt_id']}"

    def post(self, path, body, *, callback_id=None):
        return self.client.post(
            self.base + path,
            headers={
                "Authorization": f"Bearer {self.credential}",
                "X-Callback-Id": callback_id or str(new_uuid7()),
            },
            json=body,
        )

    def reserve(self, *, callback_id=None, authority=None):
        return self.post(
            "/checkpoint-reservations",
            {"authority": authority or self.authority},
            callback_id=callback_id,
        )

    def provenance(self, **changes):
        return {
            "tenant_id": str(self.graph["tenant_id"]),
            "job_id": str(self.job_id),
            "session_id": str(self.session_id),
            "attempt_id": self.authority["attempt_id"],
            "job_fence": self.authority["job_fence"],
            "input_checksum": self.input_checksum,
            "spec_checksum": self.spec_checksum,
            "template_id": self.template_id,
            "template_version": 1,
            "adapter_id": "cpu.iterative",
            "adapter_version": "1.0.0",
            "image_digest": self.image_digest,
            **changes,
        }

    def compatibility(self, **changes):
        return {
            "architecture": "linux/amd64",
            "device_type": "CPU",
            "framework": "PYTHON",
            "framework_version": "1.0.0",
            "cuda_version": None,
            "minimum_driver_version": None,
            "gpu_compute_capability": None,
            "checkpointable": self.checkpointable,
            "restart_safe": self.restart_safe,
            **changes,
        }

    def state(self, step, accumulator):
        return rfc8785.dumps(
            {
                "schema_version": 1,
                "step": step,
                "accumulator": accumulator,
                "input_checksum": self.input_checksum,
                "spec_checksum": self.spec_checksum,
            }
        )

    def checkpoint(
        self, reservation, *, step=40, accumulator=12345, key=None, file_changes=None, **changes
    ):
        """Upload state.json and the sealed manifest exactly as the runner would."""
        key = key or f"b14-{reservation['checkpoint_id']}"
        state = self.state(step, accumulator)
        state_artifact = _upload(
            self.client, self.credential, self.authority, "CHECKPOINT_FILE", state, key + "-state"
        )
        manifest = {
            "kind": "CHECKPOINT",
            "schema_version": 1,
            "checkpoint_id": reservation["checkpoint_id"],
            "checkpoint_sequence": reservation["sequence"],
            "created_at": _timestamp(),
            "provenance": self.provenance(),
            "compatibility": self.compatibility(),
            "cursor": {"step": step, "epoch": 0, "item_cursor": step, "accumulator": accumulator},
            "state_components": ["ACCUMULATOR"],
            "files": [
                {
                    "artifact_id": state_artifact["artifact_id"],
                    "logical_name": "state.json",
                    "media_type": "application/json",
                    "size_bytes": len(state),
                    "checksum": _checksum(state),
                    **(file_changes or {}),
                }
            ],
            **changes,
        }
        manifest.pop("manifest_checksum", None)
        manifest["manifest_checksum"] = _checksum(rfc8785.dumps(manifest))
        raw = rfc8785.dumps(manifest)
        manifest_artifact = _upload(
            self.client,
            self.credential,
            self.authority,
            "CHECKPOINT_MANIFEST",
            raw,
            key + "-manifest",
        )
        return manifest, manifest_artifact, state_artifact

    def publish(self, manifest, manifest_artifact, *, callback_id=None, authority=None):
        return self.post(
            "/checkpoints",
            {
                "authority": authority or self.authority,
                "manifest_artifact_id": manifest_artifact["artifact_id"],
                "manifest": manifest,
            },
            callback_id=callback_id,
        )

    def rows(self):
        with self.engine.connect() as connection:
            return {
                "job": connection.execute(select(s.jobs).where(s.jobs.c.job_id == self.job_id))
                .mappings()
                .one(),
                "attempt": connection.execute(
                    select(s.attempts).where(
                        s.attempts.c.attempt_id == UUID(self.authority["attempt_id"])
                    )
                )
                .mappings()
                .one(),
                "reservations": connection.execute(
                    select(s.checkpoint_reservations)
                    .where(s.checkpoint_reservations.c.job_id == self.job_id)
                    .order_by(s.checkpoint_reservations.c.sequence)
                )
                .mappings()
                .all(),
                "checkpoints": connection.execute(
                    select(s.checkpoints)
                    .where(s.checkpoints.c.job_id == self.job_id)
                    .order_by(s.checkpoints.c.sequence)
                )
                .mappings()
                .all(),
                "events": connection.execute(
                    select(s.events.c.sequence, s.events.c.event_type, s.events.c.reason)
                    .where(s.events.c.job_id == self.job_id)
                    .order_by(s.events.c.sequence)
                ).all(),
                "references": connection.execute(
                    select(
                        s.artifact_references.c.owner_id,
                        s.artifact_references.c.purpose,
                        s.artifact_references.c.logical_name,
                        s.artifact_references.c.artifact_id,
                    )
                    .where(s.artifact_references.c.owner_type == "CHECKPOINT")
                    .order_by(s.artifact_references.c.purpose)
                ).all(),
                "receipts": connection.execute(
                    select(func.count())
                    .select_from(s.callback_receipts)
                    .where(
                        s.callback_receipts.c.operation_id.in_(
                            ["workerReserveCheckpoint", "workerPublishCheckpoint"]
                        )
                    )
                ).scalar_one(),
            }


@pytest.fixture
def checkpoint_job(migrated_postgres_engine, tmp_path):
    with _client(migrated_postgres_engine, tmp_path) as client:
        yield CheckpointFixture(migrated_postgres_engine, client, label="main")


def test_reserve_allocates_a_job_locked_sequence_and_replays_exactly(checkpoint_job):
    callback = str(new_uuid7())
    first = checkpoint_job.reserve(callback_id=callback)
    assert first.status_code == 201, first.text
    body = first.json()
    assert set(body) == {
        "callback_id",
        "checkpoint_id",
        "job_id",
        "attempt_id",
        "sequence",
        "reserved_at",
    }
    assert body["callback_id"] == callback and body["sequence"] == 1
    assert UUID(body["checkpoint_id"]).version == 7
    assert body["attempt_id"] == checkpoint_job.authority["attempt_id"]

    replay = checkpoint_job.reserve(callback_id=callback)
    assert replay.status_code == 201 and replay.json() == body

    changed = dict(checkpoint_job.authority, job_fence=2)
    conflict = checkpoint_job.reserve(callback_id=callback, authority=changed)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"

    second = checkpoint_job.reserve()
    assert second.status_code == 409, second.text
    rows = checkpoint_job.rows()
    assert rows["job"]["checkpoint_sequence"] == 1
    assert rows["attempt"]["state"] == "CHECKPOINTING"
    assert [(r["state"], r["sequence"]) for r in rows["reservations"]] == [("RESERVED", 1)]
    assert rows["reservations"][0]["authority_grant_id"] == checkpoint_job.grant_id
    assert rows["reservations"][0]["callback_id"] == UUID(callback)
    # A second reserve never silently abandons the first reservation.
    assert rows["events"] == []


def test_reserve_requires_live_exact_authority_and_a_checkpointable_template(
    migrated_postgres_engine, tmp_path
):
    with _client(migrated_postgres_engine, tmp_path) as client:
        plain = CheckpointFixture(
            migrated_postgres_engine, client, label="plain", checkpointable=False
        )
        refused = plain.reserve()
        assert refused.status_code == 409, refused.text
        stale = plain.reserve(authority=dict(plain.authority, job_fence=2))
        assert stale.status_code == 409, stale.text
        with migrated_postgres_engine.begin() as connection:
            connection.execute(
                update(s.attempt_leases)
                .where(s.attempt_leases.c.lease_id == UUID(plain.authority["lease_id"]))
                .values(expires_at=func.clock_timestamp())
            )
        rows = plain.rows()
        assert rows["reservations"] == [] and rows["job"]["checkpoint_sequence"] == 0
        assert rows["attempt"]["state"] == "RUNNING"


def test_reserve_rejects_an_expired_lease(checkpoint_job):
    with checkpoint_job.engine.begin() as connection:
        connection.execute(
            update(s.attempt_leases)
            .where(s.attempt_leases.c.lease_id == UUID(checkpoint_job.authority["lease_id"]))
            .values(expires_at=func.clock_timestamp())
        )
    response = checkpoint_job.reserve()
    assert response.status_code == 409, response.text
    assert checkpoint_job.rows()["reservations"] == []


def test_publish_commits_the_complete_graph_once_and_replays(checkpoint_job):
    reservation = checkpoint_job.reserve().json()
    manifest, manifest_artifact, state_artifact = checkpoint_job.checkpoint(reservation)
    callback = str(new_uuid7())
    published = checkpoint_job.publish(manifest, manifest_artifact, callback_id=callback)
    assert published.status_code == 201, published.text
    record = published.json()
    assert record == {
        "checkpoint_id": reservation["checkpoint_id"],
        "job_id": str(checkpoint_job.job_id),
        "attempt_id": checkpoint_job.authority["attempt_id"],
        "sequence": 1,
        "manifest_artifact_id": manifest_artifact["artifact_id"],
        "manifest_checksum": manifest_artifact["checksum"],
        "state": "COMMITTED",
        "created_at": record["created_at"],
    }
    replay = checkpoint_job.publish(manifest, manifest_artifact, callback_id=callback)
    assert replay.status_code == 201 and replay.json() == record

    rows = checkpoint_job.rows()
    assert rows["attempt"]["state"] == "RUNNING"
    assert [(r["state"], r["ended_at"] is not None) for r in rows["reservations"]] == [
        ("COMMITTED", True)
    ]
    (checkpoint,) = rows["checkpoints"]
    assert checkpoint["provenance"] == manifest["provenance"]
    assert checkpoint["compatibility"] == manifest["compatibility"]
    assert checkpoint["manifest_checksum"] == manifest_artifact["checksum"]
    assert rows["references"] == [
        (
            UUID(reservation["checkpoint_id"]),
            "CHECKPOINT_FILE",
            "state.json",
            UUID(state_artifact["artifact_id"]),
        ),
        (
            UUID(reservation["checkpoint_id"]),
            "CHECKPOINT_MANIFEST",
            "manifest",
            UUID(manifest_artifact["artifact_id"]),
        ),
    ]
    assert rows["events"] == [(1, "CHECKPOINT_COMMITTED", "CHECKPOINT_COMMITTED")]
    assert rows["job"]["state"] == "RUNNING" and rows["job"]["version"] == 2

    stale_publish = checkpoint_job.publish(manifest, manifest_artifact)
    assert stale_publish.status_code == 409, stale_publish.text
    following = checkpoint_job.reserve()
    assert following.status_code == 201 and following.json()["sequence"] == 2


def test_deterministic_manifest_defect_rejects_reservation_and_never_reuses_sequence(
    checkpoint_job,
):
    reservation = checkpoint_job.reserve().json()
    manifest, manifest_artifact, _ = checkpoint_job.checkpoint(
        reservation, provenance=checkpoint_job.provenance(job_fence=7)
    )
    callback = str(new_uuid7())
    rejected = checkpoint_job.publish(manifest, manifest_artifact, callback_id=callback)
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == "validation_failed"
    replay = checkpoint_job.publish(manifest, manifest_artifact, callback_id=callback)
    assert replay.status_code == 422
    assert {k: replay.json()[k] for k in ("code", "message")} == {
        k: rejected.json()[k] for k in ("code", "message")
    }

    rows = checkpoint_job.rows()
    assert [(r["state"], r["ended_at"] is not None) for r in rows["reservations"]] == [
        ("REJECTED", True)
    ]
    assert rows["checkpoints"] == [] and rows["references"] == []
    assert rows["attempt"]["state"] == "RUNNING"
    assert rows["events"] == [(1, "CHECKPOINT_REJECTED", "CHECKPOINT_PROVENANCE_MISMATCH")]

    again = checkpoint_job.publish(manifest, manifest_artifact)
    assert again.status_code == 409, again.text
    following = checkpoint_job.reserve()
    assert following.status_code == 201 and following.json()["sequence"] == 2


@pytest.mark.parametrize(
    ("defect", "reason"),
    [
        ("identity", "CHECKPOINT_IDENTITY_MISMATCH"),
        ("compatibility", "CHECKPOINT_COMPATIBILITY_MISMATCH"),
        ("cursor", "CHECKPOINT_CURSOR_INVALID"),
        ("file-checksum", "CHECKPOINT_FILES_INVALID"),
        ("wrong-kind", "CHECKPOINT_MANIFEST_INVALID"),
        ("foreign-manifest", "CHECKPOINT_MANIFEST_INVALID"),
        ("request-differs", "CHECKPOINT_MANIFEST_NOT_CANONICAL"),
    ],
)
def test_each_publish_defect_is_rejected_durably(checkpoint_job, defect, reason):
    reservation = checkpoint_job.reserve().json()
    changes = {
        "identity": {"checkpoint_sequence": 9},
        "compatibility": {
            "compatibility": checkpoint_job.compatibility(architecture="linux/arm64")
        },
        "cursor": {"cursor": {"step": 101, "epoch": 0, "item_cursor": 101, "accumulator": 1}},
        "file-checksum": {"file_changes": {"checksum": "sha256:" + "d" * 64}},
    }.get(defect, {})
    manifest, manifest_artifact, state_artifact = checkpoint_job.checkpoint(reservation, **changes)
    if defect == "wrong-kind":
        manifest_artifact = state_artifact
    if defect == "foreign-manifest":
        with checkpoint_job.engine.begin() as connection:
            other = seed_tenant_graph(connection, label="b14-foreign")
        manifest_artifact = {"artifact_id": str(other["artifact_id"])}
    if defect == "request-differs":
        manifest = dict(manifest, created_at="2026-01-01T00:00:00.000Z")
    response = checkpoint_job.publish(manifest, manifest_artifact)
    assert response.status_code == 422, response.text
    rows = checkpoint_job.rows()
    assert [r["state"] for r in rows["reservations"]] == ["REJECTED"]
    assert rows["checkpoints"] == [] and rows["references"] == []
    assert rows["events"] == [(1, "CHECKPOINT_REJECTED", reason)]
    assert rows["attempt"]["state"] == "RUNNING"


def test_wrong_authority_or_unavailable_storage_commits_nothing(checkpoint_job):
    reservation = checkpoint_job.reserve().json()
    manifest, manifest_artifact, _ = checkpoint_job.checkpoint(reservation)
    before = checkpoint_job.rows()

    stale = checkpoint_job.publish(
        manifest, manifest_artifact, authority=dict(checkpoint_job.authority, job_fence=2)
    )
    assert stale.status_code == 409, stale.text

    store = checkpoint_job.client.app.state.services.worker.artifact_store
    original = store.open

    def unavailable(*_args, **_kwargs):
        raise ArtifactError("storage_unavailable", "offline")

    store.open = unavailable
    try:
        offline = checkpoint_job.publish(manifest, manifest_artifact)
    finally:
        store.open = original
    assert offline.status_code == 503, offline.text

    after = checkpoint_job.rows()
    assert after["reservations"] == before["reservations"]
    assert after["events"] == before["events"] == []
    assert after["job"]["version"] == before["job"]["version"]
    assert checkpoint_job.publish(manifest, manifest_artifact).status_code == 201


def test_publish_after_failure_is_stale_and_reservation_is_abandoned(checkpoint_job):
    reservation = checkpoint_job.reserve().json()
    manifest, manifest_artifact, _ = checkpoint_job.checkpoint(reservation)
    with checkpoint_job.engine.begin() as connection:
        job = (
            connection.execute(select(s.jobs).where(s.jobs.c.job_id == checkpoint_job.job_id))
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
    failed = checkpoint_job.post(
        "/fail",
        {
            "authority": checkpoint_job.authority,
            "failure_class": "INTERNAL",
            "reason_code": "WORKLOAD_EXIT_NONZERO",
            "observation": {
                "observation_type": "CONTAINER",
                "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
                "exit_code": 1,
                "oom_killed": False,
                "runtime_limit_reached": False,
                "observed_at": _timestamp(),
            },
        },
    )
    assert failed.status_code == 200, failed.text
    response = checkpoint_job.publish(manifest, manifest_artifact)
    assert response.status_code == 409, response.text
    rows = checkpoint_job.rows()
    assert [r["state"] for r in rows["reservations"]] == ["ABANDONED"]
    assert rows["checkpoints"] == []


def test_checkpoint_uploads_are_gated_by_attempt_phase(checkpoint_job):
    early = _upload(
        checkpoint_job.client,
        checkpoint_job.credential,
        checkpoint_job.authority,
        "CHECKPOINT_FILE",
        checkpoint_job.state(1, 2),
        "b14-gate-early-state",
        expected=409,
    )
    assert early["code"] == "state_conflict"
    checkpoint_job.reserve()
    result_during_checkpoint = checkpoint_job.client.post(
        f"{checkpoint_job.base}/artifacts",
        headers={
            **_worker_headers(checkpoint_job.credential, checkpoint_job.authority),
            "Content-Type": "application/octet-stream",
            "Idempotency-Key": "b14-gate-result-file",
            "X-Artifact-Kind": "RESULT_FILE",
            "X-Artifact-Media-Type": "application/json",
            "X-Artifact-Checksum": _checksum(b"{}"),
            "X-Artifact-Size": "2",
        },
        content=b"{}",
    )
    assert result_during_checkpoint.status_code == 409, result_during_checkpoint.text
    reserve_result = checkpoint_job.post(
        "/result-reservations", {"authority": checkpoint_job.authority}
    )
    assert reserve_result.status_code == 409, reserve_result.text


def _publish_series(fixture, count):
    published = []
    for step in range(1, count + 1):
        reservation = fixture.reserve().json()
        manifest, manifest_artifact, state_artifact = fixture.checkpoint(
            reservation, step=step * 10, accumulator=step * 7
        )
        response = fixture.publish(manifest, manifest_artifact)
        assert response.status_code == 201, response.text
        published.append((response.json(), manifest_artifact, state_artifact))
    return published


def _member_login(fixture, tenant_ids):
    client = fixture.client
    bootstrap = client.post(
        "/v1/internal/admin-bootstrap",
        headers={"X-Nexa-Bootstrap-Secret": "b" * 32, "Idempotency-Key": "b14-list-admin-key"},
        json={
            "username": "b14-list@example.test",
            "display_name": "B14 List",
            "password": "correct-horse-battery-staple",
        },
    )
    assert bootstrap.status_code == 201, bootstrap.text
    user_id = UUID(bootstrap.json()["user_id"])
    with fixture.engine.begin() as connection:
        for tenant_id in tenant_ids:
            connection.execute(
                insert(s.memberships).values(tenant_id=tenant_id, user_id=user_id, role="MEMBER")
            )
    login = client.post(
        "/v1/auth/login",
        headers={"Origin": "https://nexa.test"},
        json={"username": "b14-list@example.test", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200, login.text
    return {"Origin": "https://nexa.test", "X-CSRF-Token": login.json()["csrf_token"]}


def test_list_checkpoints_is_owned_paged_newest_first_and_marks_corrupt(
    checkpoint_job, migrated_postgres_engine, tmp_path
):
    published = _publish_series(checkpoint_job, 3)
    pending = checkpoint_job.reserve()
    assert pending.status_code == 201, pending.text
    newest = published[-1][0]
    with checkpoint_job.engine.begin() as connection:
        member_tenant = seed_tenant_graph(connection, label="b14-list-member")["tenant_id"]
        outsider_tenant = seed_tenant_graph(connection, label="b14-list-outsider")["tenant_id"]
        connection.execute(
            insert(s.checkpoint_corruptions).values(
                checkpoint_id=UUID(newest["checkpoint_id"]),
                tenant_id=checkpoint_job.graph["tenant_id"],
                reason_code="CHECKPOINT_BLOB_CHECKSUM_MISMATCH",
            )
        )
    write = _member_login(checkpoint_job, [checkpoint_job.graph["tenant_id"], member_tenant])
    client = checkpoint_job.client
    tenant = {"X-Nexa-Tenant-Id": str(checkpoint_job.graph["tenant_id"])}
    url = f"/v1/jobs/{checkpoint_job.job_id}/checkpoints"

    first = client.get(url, headers=tenant, params={"page_size": 2})
    assert first.status_code == 200, first.text
    body = first.json()
    assert set(body) == {"items", "page"}
    assert [item["sequence"] for item in body["items"]] == [3, 2]
    assert [item["state"] for item in body["items"]] == ["CORRUPT", "COMMITTED"]
    expected = {**newest, "state": "CORRUPT"}
    assert body["items"][0] == expected
    assert body["items"][1] == published[1][0]
    assert body["page"]["page_size"] == 2
    cursor = body["page"]["next_cursor"]
    assert isinstance(cursor, str)
    second = client.get(url, headers=tenant, params={"page_size": 2, "cursor": cursor})
    assert second.status_code == 200, second.text
    assert [item["sequence"] for item in second.json()["items"]] == [1]
    assert second.json()["items"][0] == published[0][0]
    assert second.json()["page"]["next_cursor"] is None
    whole = client.get(url, headers=tenant)
    assert [item["sequence"] for item in whole.json()["items"]] == [3, 2, 1]
    assert whole.json()["page"] == {"next_cursor": None, "page_size": 50}
    # The open reservation (sequence 4) is never listed.
    assert pending.json()["sequence"] == 4

    for bad in ("x" * 16, cursor[:-2] + ("AA" if not cursor.endswith("AA") else "BB")):
        invalid = client.get(url, headers=tenant, params={"cursor": bad})
        assert invalid.status_code == 400, invalid.text
        assert invalid.json()["code"] == "invalid_cursor"
    for size in (0, 101):
        assert client.get(url, headers=tenant, params={"page_size": size}).status_code == 422

    member_view = client.get(url, headers={"X-Nexa-Tenant-Id": str(member_tenant)})
    assert member_view.status_code == 404, member_view.text
    assert member_view.json()["code"] == "resource_not_found"
    outsider = client.get(url, headers={"X-Nexa-Tenant-Id": str(outsider_tenant)})
    assert outsider.status_code == 403, outsider.text
    assert outsider.json()["code"] == "permission_denied"
    missing = client.get(f"/v1/jobs/{new_uuid7()}/checkpoints", headers=tenant)
    assert missing.status_code == 404, missing.text
    rebound = client.get(
        f"/v1/jobs/{new_uuid7()}/checkpoints", headers=tenant, params={"cursor": cursor}
    )
    assert rebound.status_code == 404

    tokens = {}
    for scope in ("jobs:read", "jobs:write"):
        created = client.post(
            "/v1/tokens",
            headers={**write, "Idempotency-Key": f"b14-list-token-{scope[5:]}"},
            json={"name": f"b14-{scope[5:]}", "scopes": [scope], "expires_in_seconds": 300},
        )
        assert created.status_code == 201, created.text
        tokens[scope] = created.json()["token"]
    with _client(migrated_postgres_engine, tmp_path) as cli_client:
        cli_client.cookies.clear()
        read = cli_client.get(
            url, headers={**tenant, "Authorization": f"Bearer {tokens['jobs:read']}"}
        )
        assert read.status_code == 200, read.text
        assert read.json() == whole.json()
        denied = cli_client.get(
            url, headers={**tenant, "Authorization": f"Bearer {tokens['jobs:write']}"}
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["code"] == "permission_denied"
        anonymous = cli_client.get(url, headers=tenant)
        assert anonymous.status_code == 401, anonymous.text


def test_every_published_checkpoint_stays_referenced_and_unreclaimable(checkpoint_job):
    published = _publish_series(checkpoint_job, 3)
    services = checkpoint_job.client.app.state.services
    with checkpoint_job.engine.begin() as connection:
        connection.execute(
            update(s.upload_sessions).values(expires_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
    assert services.artifact.expire_uploads(limit=1000) == 0

    rows = checkpoint_job.rows()
    assert [row["sequence"] for row in rows["checkpoints"]] == [1, 2, 3]
    referenced = {(owner, purpose): artifact for owner, purpose, _, artifact in rows["references"]}
    with checkpoint_job.engine.connect() as connection:
        blobs = {
            row["artifact_id"]: row for row in connection.execute(select(s.artifacts)).mappings()
        }
    for record, manifest_artifact, state_artifact in published:
        owner = UUID(record["checkpoint_id"])
        assert referenced[(owner, "CHECKPOINT_MANIFEST")] == UUID(manifest_artifact["artifact_id"])
        assert referenced[(owner, "CHECKPOINT_FILE")] == UUID(state_artifact["artifact_id"])
        for artifact_id in (manifest_artifact["artifact_id"], state_artifact["artifact_id"]):
            blob = blobs[UUID(artifact_id)]
            assert blob["state"] == "COMMITTED"
            with pytest.raises(ApplicationError) as refused:
                services.artifact.issue_gc_token(
                    blob_key=blob["blob_key"],
                    checksum=blob["checksum"],
                    size_bytes=blob["size_bytes"],
                    expires_at=datetime(2100, 1, 1, tzinfo=UTC),
                )
            assert refused.value.status == 409
            assert services.artifact.store.inspect(blob["blob_key"]).checksum == blob["checksum"]


def test_adopted_open_cycle_publishes_under_the_new_authority_only(checkpoint_job):
    fixture = checkpoint_job
    client = fixture.client
    reservation = fixture.reserve().json()
    key = "b14-adopt-cycle"
    state = fixture.state(30, 99)
    old = dict(fixture.authority)
    original_state = _upload(
        client, fixture.credential, old, "CHECKPOINT_FILE", state, key + "-state"
    )
    current = _create_incarnation(
        client, fixture.credential, nonce=str(new_uuid7()), key="b14-adopt-new-incarnation"
    )
    adopted = client.post(
        f"{fixture.base}/adopt",
        headers={
            "Authorization": f"Bearer {fixture.credential}",
            "X-Callback-Id": str(new_uuid7()),
        },
        json={
            "prior_authority": old,
            "current_worker_incarnation_id": current["worker_incarnation_id"],
            "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
        },
    )
    assert adopted.status_code == 200, adopted.text
    transferred = adopted.json()["transferred_checkpoint_reservation"]
    assert {k: transferred[k] for k in ("callback_id", "checkpoint_id", "sequence")} == {
        k: reservation[k] for k in ("callback_id", "checkpoint_id", "sequence")
    }
    new = adopted.json()["authority"]
    assert new["job_fence"] == old["job_fence"] and new["attempt_id"] == old["attempt_id"]

    # A second reserve under the adopted authority never opens a parallel cycle.
    assert fixture.reserve(authority=new).status_code == 409
    fixture.authority = new
    manifest, manifest_artifact, state_artifact = fixture.checkpoint(
        reservation, step=30, accumulator=99, key=key
    )
    assert state_artifact == original_state
    stale = fixture.publish(manifest, manifest_artifact, authority=old)
    assert stale.status_code == 409, stale.text
    assert fixture.rows()["checkpoints"] == []
    callback = str(new_uuid7())
    # The new incarnation has not reported inventory yet: transient, never a defect.
    early = fixture.publish(manifest, manifest_artifact, callback_id=callback)
    assert early.status_code == 503, early.text
    assert early.headers["retry-after"] == "1"
    rows = fixture.rows()
    assert [r["state"] for r in rows["reservations"]] == ["RESERVED"]
    assert rows["attempt"]["state"] == "CHECKPOINTING"
    assert rows["events"] == [] and rows["checkpoints"] == []
    reconciliation = client.get(
        f"/v1/workers/{WORKER_ID}/reconciliation?page_size=100",
        headers={
            "Authorization": f"Bearer {fixture.credential}",
            "X-Worker-Incarnation-Id": current["worker_incarnation_id"],
        },
    )
    assert reconciliation.status_code == 200, reconciliation.text
    heartbeat = client.post(
        f"/v1/workers/{WORKER_ID}/heartbeat",
        headers={
            "Authorization": f"Bearer {fixture.credential}",
            "X-Callback-Id": str(new_uuid7()),
        },
        json={
            "worker_incarnation_id": current["worker_incarnation_id"],
            "observed_health": "READY",
            "reconcile_complete": True,
            "inventory": _inventory(),
            "observed_containers": [],
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    published = fixture.publish(manifest, manifest_artifact, callback_id=callback)
    assert published.status_code == 201, published.text
    assert published.json()["checkpoint_id"] == reservation["checkpoint_id"]
    rows = fixture.rows()
    assert [(r["state"], r["sequence"]) for r in rows["reservations"]] == [("COMMITTED", 1)]
    assert rows["events"] == [(1, "CHECKPOINT_COMMITTED", "CHECKPOINT_COMMITTED")]
    assert rows["attempt"]["state"] == "RUNNING"
