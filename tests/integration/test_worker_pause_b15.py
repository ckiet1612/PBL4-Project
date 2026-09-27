"""B15 worker + PostgreSQL: the production agent and real runner against the real API.

Only Docker is faked (the B14 worker harness backend); every worker callback goes
through `WorkerApiClient` to the in-process API backed by PostgreSQL.
"""

import json
from dataclasses import asdict
from uuid import UUID

import pytest
from sqlalchemy import insert, select, update

from nexa.infrastructure.persistence import schema as s
from nexa.worker.client import WorkerApiClient
from nexa.worker.models import Authority
from nexa.worker.result_flow import checksum
from nexa.workloads.trusted_runner import RunnerState
from tests.api.test_http_contract import _client
from tests.integration import _factories as factories
from tests.integration import test_checkpoint_b14 as checkpoint_b14
from tests.integration.test_checkpoint_b14 import CheckpointFixture
from tests.integration.test_retry_b14 import _job
from tests.worker.test_checkpoint_flow_b14 import Harness
from tests.worker.test_control_flow_b15 import ListingBackend, controls

pytestmark = pytest.mark.postgres

CFP = "CHECKPOINT_FOR_PAUSE"
MEDIA_TYPE = "application/vnd.nexa.cpu-iterative-input+json"


class RecordingClient(WorkerApiClient):
    """The real client; keeps the claim answer so the test can write matching state."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.context = None
        self.failures = []

    def claim(self, attempt_id, callback_id, body):
        answer = super().claim(attempt_id, callback_id, body)
        self.context = answer["execution_context"]
        return answer

    def fail(self, attempt_id, callback_id, body):
        self.failures.append((body["failure_class"], body["reason_code"]))
        return super().fail(attempt_id, callback_id, body)


class CreatedLabelsBackend(ListingBackend):
    """Docker fake whose inspect reports the labels the worker created the container with."""

    def __init__(self, output):
        super().__init__(output)
        self.labels_created = {}

    def run(self, argv, timeout_seconds):
        if argv[1] == "create":
            self.labels_created = dict(
                argv[index + 1].split("=", 1)
                for index, value in enumerate(argv)
                if value == "--label"
            )
        result = super().run(argv, timeout_seconds)
        if argv[1] == "inspect" and result[0] == 0:
            payload = json.loads(result[1])
            payload[0]["Config"]["Labels"].update(self.labels_created)
            return 0, json.dumps(payload).encode(), b""
        return result


class ServerHarness(Harness):
    def __init__(self, tmp_path, fixture, client):
        super().__init__(
            tmp_path,
            None,
            api=client,
            backend=CreatedLabelsBackend(tmp_path / "fs" / "output"),
        )
        self.authority = Authority(**fixture.authority)
        self.agent = self._agent()

    def write_state(self, step, accumulator):
        self.context = self.api.context
        super().write_state(step, accumulator)


@pytest.fixture
def spec_checksummed_jobs(monkeypatch):
    """Seed Job specs with the checksum of their canonical spec.

    The factory writes a placeholder, and `job_specs` is immutable. The worker binds
    its checkpoint provenance to the checksum of the spec the claim hands out, and
    the server compares it with the stored one.
    """
    seed_job = checkpoint_b14.seed_job

    def seed(connection, graph, **kwargs):
        placeholder = factories.CHECKSUM
        factories.CHECKSUM = checksum(kwargs["canonical_spec"])
        try:
            return seed_job(connection, graph, **kwargs)
        finally:
            factories.CHECKSUM = placeholder

    monkeypatch.setattr(checkpoint_b14, "seed_job", seed)


def _checkpoint_for_pause_offer(fixture):
    """Turn the fixture's RUNNING attempt into a committed, unclaimed CFP offer."""
    tenant = fixture.graph["tenant_id"]
    scopes = (
        ("GLOBAL", "global"),
        ("TENANT", str(tenant)),
        ("USER", f"{tenant}:{fixture.graph['user_id']}"),
    )
    with fixture.engine.begin() as connection:
        connection.execute(s.container_identities.delete())
        connection.execute(
            update(s.attempts).values(
                state="CREATED", claimed_at=None, started_at=None, execution_intent=CFP
            )
        )
        connection.execute(
            update(s.jobs).values(state="DISPATCHING", desired_state="PAUSED", recovery_intent=CFP)
        )
        for scope, scope_id in scopes:
            connection.execute(
                insert(s.admission_counters).values(
                    scope_type=scope, scope_id=scope_id, outstanding=1, active_attempts=1
                )
            )


def _reservations(engine, fixture):
    with engine.connect() as connection:
        return (
            connection.execute(
                select(s.checkpoint_reservations.c.state).where(
                    s.checkpoint_reservations.c.job_id == fixture.job_id
                )
            )
            .scalars()
            .all()
        )


def _server_rows(engine, fixture):
    attempt_id = UUID(fixture.authority["attempt_id"])
    with engine.connect() as connection:
        attempt = (
            connection.execute(select(s.attempts).where(s.attempts.c.attempt_id == attempt_id))
            .mappings()
            .one()
        )
        allocation = (
            connection.execute(
                select(s.allocations).where(
                    s.allocations.c.allocation_id == UUID(fixture.authority["allocation_id"])
                )
            )
            .mappings()
            .one()
        )
        checkpoints = connection.execute(
            select(s.checkpoints.c.state, s.checkpoints.c.sequence).where(
                s.checkpoints.c.job_id == fixture.job_id
            )
        ).all()
        events = (
            connection.execute(
                select(s.events.c.event_type).where(s.events.c.job_id == fixture.job_id)
            )
            .scalars()
            .all()
        )
    return attempt, allocation, checkpoints, events


def test_checkpoint_for_pause_before_the_first_state_write_ends_paused(
    migrated_postgres_engine, tmp_path, spec_checksummed_jobs
):
    # B15-R20: the worker opens the pause on the runner's first progress frame,
    # which comes right after launch and before the workload's first state write.
    engine = migrated_postgres_engine
    (tmp_path / "api").mkdir()
    with _client(engine, tmp_path / "api") as api:
        fixture = CheckpointFixture(
            engine,
            api,
            label="b15-r20-worker",
            template_id="cpu-iterative",
            input_media_type=MEDIA_TYPE,
        )
        _checkpoint_for_pause_offer(fixture)
        client = RecordingClient("http://testserver", fixture.credential, transport=api._transport)
        harness = ServerHarness(tmp_path / "worker", fixture, client)
        harness.dispatch()
        assert client.context["execution_intent"] == CFP
        assert _job(engine, fixture.job_id)["state"] == "PAUSING"

        harness.runner.mark_workload_started()
        harness.runner.emit_progress(fraction=0.0, step=0)
        harness.cycle(6)
        assert [p["reason"] for p in controls(harness, "REQUEST_CHECKPOINT")] == ["PAUSE"]
        harness.runner.enforce_deadlines()
        harness.cycle(4)
        assert harness.runner.state is RunnerState.RUNNING
        assert client.failures == []
        attempt, _, checkpoints, _ = _server_rows(engine, fixture)
        # The pause reservation is open and waits for the state; nothing failed.
        assert attempt["state"] == "CHECKPOINTING"
        assert checkpoints == [] and _reservations(engine, fixture) == ["RESERVED"]

        harness.advance(5)
        harness.write_state(3, 11)
        harness.runner.enforce_deadlines()
        harness.cycle(12)

        assert [p["reason"] for p in controls(harness, "REQUEST_STOP")] == ["PAUSE"]
        assert client.failures == []
        job = _job(engine, fixture.job_id)
        assert (job["state"], job["desired_state"], job["retry_count"]) == ("PAUSED", "PAUSED", 0)
        assert job["recovery_intent"] is None and job["terminal_at"] is None
        attempt, allocation, checkpoints, events = _server_rows(engine, fixture)
        assert (attempt["state"], attempt["failure_reason"]) == ("CANCELLED", "PAUSE")
        assert (allocation["state"], allocation["release_reason"]) == (
            "RELEASED",
            "VERIFIED_CLEANUP",
        )
        assert [tuple(row) for row in checkpoints] == [("COMMITTED", 1)]
        assert _reservations(engine, fixture) == ["COMMITTED"]
        assert "ATTEMPT_FAILED" not in events
        assert asdict(harness.authority)["attempt_id"] == fixture.authority["attempt_id"]
        client.close()
