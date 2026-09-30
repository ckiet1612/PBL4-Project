"""Remediation B14-R04: a Job the node cannot run waits; a runnable Job skips bad checkpoints.

Two different situations share the word "incompatible":

* the destination cannot run the Job at all (template, architecture or inventory): the
  Job keeps waiting with a visible ``waiting_for_compatibility`` and is never auto-failed,
  its committed checkpoints untouched;
* the destination runs the Job but no committed checkpoint is compatible with it: a
  ``restart_safe`` Job falls back to input with an event, any other Job follows the
  existing contract (no restore, start refused, INCOMPATIBLE without a container).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from nexa.coordinator.eligibility import _compatible
from nexa.domain.scheduling import Dispatch
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration.test_checkpoint_b14 import CheckpointFixture
from tests.integration.test_checkpoint_restore_b14 import (
    _claim,
    _commit,
    _corruptions,
    _next_attempt,
    _restore_events,
    _seed_admission_counters,
    _start,
)
from tests.integration.test_retry_b14 import _fail_and_cleanup, _job, _job_events, _leader
from tests.integration.test_worker_api_b10 import WORKER_ID, _inventory

pytestmark = pytest.mark.postgres

BOTH = ["linux/amd64", "linux/arm64"]


def _template(fixture):
    with fixture.engine.connect() as connection:
        return (
            connection.execute(
                select(s.template_versions, s.templates.c.enabled)
                .join(s.templates, s.templates.c.template_id == s.template_versions.c.template_id)
                .where(s.template_versions.c.template_id == fixture.graph["template_id"])
            )
            .mappings()
            .one()
        )


def _heartbeat(fixture, architecture):
    """A READY inventory of `architecture` carrying the template's verified image."""
    inventory = _inventory()
    inventory["architecture"] = architecture
    inventory["images"][0].update(
        image_digest=_template(fixture)["image_digest"], architecture=architecture
    )
    response = fixture.client.post(
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
    assert response.status_code == 200, response.text


def _runs_on_current_inventory(fixture):
    """The evaluator dispatch and retry promotion use to decide the node can run the Job."""
    with fixture.engine.connect() as connection:
        version = connection.execute(select(s.workers.c.current_inventory_version)).scalar_one()
        inventory = (
            connection.execute(
                select(s.worker_inventories).where(
                    s.worker_inventories.c.inventory_version == version
                )
            )
            .mappings()
            .one()
        )
    return _compatible(dict(_template(fixture)), dict(inventory))


def _tenant_policy(fixture):
    """The fixture tenant's quota, so only compatibility can block its retry."""
    with fixture.engine.begin() as connection:
        connection.execute(
            s.tenant_policies.insert().values(
                tenant_id=fixture.graph["tenant_id"],
                version=1,
                weight=Decimal(1),
                cpu_limit_millis=6000,
                memory_limit_bytes=12 * 1024**3,
                gpu_limit=0,
                outstanding_limit=100,
                user_outstanding_limit=100,
                tenant_active_limit=1,
                user_active_limit=1,
                tenant_rate_per_second=Decimal(10),
                tenant_rate_burst=Decimal(10),
                user_rate_per_second=Decimal(10),
                user_rate_burst=Decimal(10),
                is_current=True,
            )
        )


def _checkpoint_ids(fixture):
    return [str(row["checkpoint_id"]) for row in fixture.rows()["checkpoints"]]


def test_node_incompatible_retry_waits_visibly_and_keeps_its_checkpoints(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        # restart_safe, amd64 only: an arm64 inventory cannot run the Job at all.
        fixture = CheckpointFixture(engine, client, label="r04-node")
        _tenant_policy(fixture)
        committed = [
            _commit(fixture, step=10, accumulator=5)["record"]["checkpoint_id"],
            _commit(fixture, step=20, accumulator=6)["record"]["checkpoint_id"],
        ]
        job, _, _ = _fail_and_cleanup(fixture)
        assert job["state"] == "RETRY_WAIT"
        with engine.begin() as connection:
            connection.execute(
                update(s.retry_schedules)
                .where(s.retry_schedules.c.job_id == fixture.job_id)
                .values(ready_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        _heartbeat(fixture, "linux/arm64")
        assert not _runs_on_current_inventory(fixture)
        service, epoch = _leader(engine)

        assert service.promote_retries(epoch) == 0
        assert not isinstance(service.tick(epoch), Dispatch)
        blocked = _job(engine, fixture.job_id)
        assert (blocked["state"], blocked["waiting_reason"], blocked["retry_count"]) == (
            "RETRY_WAIT",
            "waiting_for_compatibility",
            1,
        )
        assert blocked["terminal_at"] is None
        assert _job_events(engine, fixture.job_id)[-1] == (
            "RETRY_BLOCKED",
            "waiting_for_compatibility",
            "COORDINATOR",
        )
        # Waiting is not a restore decision: no checkpoint is judged, marked or skipped.
        assert _restore_events(fixture) == []
        assert _corruptions(fixture) == {}
        assert _checkpoint_ids(fixture) == committed

        _heartbeat(fixture, "linux/amd64")
        assert _runs_on_current_inventory(fixture)
        assert service.promote_retries(epoch) == 1
        queued = _job(engine, fixture.job_id)
        assert (queued["state"], queued["waiting_reason"]) == ("QUEUED", None)
        assert _checkpoint_ids(fixture) == committed
        assert _corruptions(fixture) == {}


@pytest.mark.parametrize("restart_safe", [True, False], ids=["restart-safe", "not-restart-safe"])
def test_runnable_job_whose_checkpoints_are_all_incompatible(
    migrated_postgres_engine, tmp_path, restart_safe
):
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        # The template runs on both architectures; the checkpoints were written on amd64.
        fixture = CheckpointFixture(
            engine,
            client,
            label="r04-safe" if restart_safe else "r04-unsafe",
            restart_safe=restart_safe,
            architectures=BOTH,
        )
        _commit(fixture, step=10, accumulator=5)
        _commit(fixture, step=20, accumulator=6)
        _heartbeat(fixture, "linux/arm64")
        assert _runs_on_current_inventory(fixture)
        _next_attempt(fixture)
        _seed_admission_counters(fixture)

        claimed = _claim(fixture)
        assert claimed.status_code == 200, claimed.text
        context = claimed.json()["execution_context"]
        assert context["restore_checkpoint"] is None
        assert _corruptions(fixture) == {}
        started = _start(fixture, context)
        if restart_safe:
            assert _restore_events(fixture) == [
                ("CHECKPOINT_INCOMPATIBLE", "CHECKPOINT_COMPATIBILITY_MISMATCH"),
                ("CHECKPOINT_FALLBACK_TO_INPUT", "CHECKPOINT_FALLBACK_TO_INPUT"),
            ]
            assert started.status_code == 200, started.text
            return

        assert _restore_events(fixture) == [
            ("CHECKPOINT_INCOMPATIBLE", "CHECKPOINT_COMPATIBILITY_MISMATCH"),
            ("CHECKPOINT_RESTORE_UNAVAILABLE", "CHECKPOINT_RESTORE_UNAVAILABLE"),
        ]
        assert started.status_code == 409, started.text
        # The worker's refusal: INCOMPATIBLE before any container, then its tombstone
        # cleanup; the failure class has no automatic retry.
        proof = {
            "proof_type": "NO_CONTAINER",
            "startup_nonce": context["startup_nonce"],
            "executor_operation_sequence": 1,
            "tombstone_sequence": 1,
            "observed_at": datetime.now(UTC).isoformat(),
            "inspection_checksum": "sha256:" + "a" * 64,
        }
        failed = fixture.post(
            "/fail",
            {
                "authority": fixture.authority,
                "failure_class": "INCOMPATIBLE",
                "reason_code": "CHECKPOINT_RESTORE_UNAVAILABLE",
                "observation": {"observation_type": "NO_CONTAINER", "proof": proof},
            },
        )
        assert failed.status_code == 200, failed.text
        authority = {k: v for k, v in fixture.authority.items() if k != "lease_id"}
        cleaned = fixture.post("/cleanup", {**authority, "proof": proof})
        assert cleaned.status_code == 200, cleaned.text
        assert cleaned.json()["allocation_state"] == "RELEASED"
        rows = fixture.rows()
        assert (rows["job"]["state"], rows["job"]["retry_count"]) == ("FAILED", 0)
        assert (rows["attempt"]["failure_class"], rows["attempt"]["failure_reason"]) == (
            "INCOMPATIBLE",
            "CHECKPOINT_RESTORE_UNAVAILABLE",
        )
        assert rows["attempt"]["started_at"] is None
        assert _corruptions(fixture) == {}
