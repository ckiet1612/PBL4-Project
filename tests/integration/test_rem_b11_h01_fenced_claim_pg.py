"""Remediation B11-H01 over PostgreSQL: a claim fenced before ``/start`` is released.

The worker process dies right after the claim commits and before it journals anything. The
reaper later fences the claimed attempt. The next incarnation must prove that no container with
the exact startup identity exists, send one ``NO_CONTAINER`` cleanup the API accepts, and finish
reconciliation so the worker can report READY again.
"""

from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.worker.agent import WorkerAgent
from nexa.worker.client import WorkerApiClient
from nexa.worker.docker_client import DockerCli
from nexa.worker.executor import DockerExecutor
from nexa.worker.journal import ExecutionJournal
from nexa.worker.state import PendingOperationStore
from tests.api.test_http_contract import _client
from tests.integration._factories import seed_job
from tests.integration.test_control_b15 import _expire_lease, _reconcile_and_heartbeat
from tests.integration.test_coordinator_b11 import seed_dispatchable
from tests.integration.test_retry_b14 import _job, _leader
from tests.integration.test_worker_api_b10 import (
    INSTALLATION_ID,
    WORKER_ID,
    _bootstrap_worker,
    _create_incarnation,
)
from tests.worker.test_rem_b11_h01_fenced_claim import LabelDocker

pytestmark = pytest.mark.postgres


class ProcessDeath(BaseException):
    """The worker process ends here; nothing after this point runs."""


def _agent(api, incarnation_id, tmp_path, docker, provider):
    journal = ExecutionJournal(tmp_path / "journal")
    return WorkerAgent(
        worker_id=WORKER_ID,
        incarnation_id=incarnation_id,
        installation_id=INSTALLATION_ID,
        client=api,
        journal=journal,
        state=PendingOperationStore(tmp_path / "pending.json", boot_id="boot"),
        docker=DockerCli(docker),
        executor=DockerExecutor(
            journal,
            docker,
            image_ref="registry.invalid/cpu",
            staging_root=tmp_path / "staging",
            installation_id=INSTALLATION_ID,
        ),
        provider=provider,
    )


def _die_after_claim():
    raise ProcessDeath


def test_a_claim_fenced_before_start_is_released_by_the_next_incarnation(
    migrated_postgres_engine, tmp_path
):
    from tests.integration.test_jobs_b08 import _submit_body

    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        first = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="rem-b11-h01-first"
        )
        graph, _, _ = seed_dispatchable(
            engine,
            count=0,
            template_id="cpu-iterative",
            existing_worker=(UUID(WORKER_ID), UUID(first["worker_incarnation_id"])),
        )
        with engine.begin() as connection:
            spec = _submit_body(graph["artifact_id"])["spec"]
            spec["resources"]["memory_bytes"] = 1_073_741_824
            job_id = seed_job(connection, graph, canonical_spec=spec)["job_id"]
            connection.execute(update(s.jobs).values(eligible_since=func.clock_timestamp()))
            connection.execute(update(s.admission_counters).values(outstanding=1))
        service, epoch = _leader(engine)
        service.tick(epoch)

        api = WorkerApiClient("http://testserver", credential, transport=client._transport)
        cleanups: list[tuple[str, str]] = []
        cleanup = api.cleanup
        api.cleanup = lambda attempt_id, callback_id, body: (
            cleanups.append((attempt_id, callback_id)) or cleanup(attempt_id, callback_id, body)
        )
        docker = LabelDocker()
        dead = _agent(
            api,
            first["worker_incarnation_id"],
            tmp_path,
            docker,
            SimpleNamespace(discover=_die_after_claim),
        )
        offer = api.poll(WORKER_ID, first["worker_incarnation_id"])["offer"]
        attempt_id = offer["authority"]["attempt_id"]
        with pytest.raises(ProcessDeath):
            dead._dispatch_offer(offer)
        with engine.connect() as connection:
            attempt = connection.execute(select(s.attempts)).mappings().one()
            lease = connection.execute(select(s.attempt_leases)).mappings().one()
        assert attempt["claimed_at"] is not None and attempt["started_at"] is None
        assert not dead.journal.exists(attempt_id) and docker.containers == {}
        assert [value["operation"] for value in dead.state.operations.values()] == ["claim"]

        second = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="rem-b11-h01-second"
        )
        agent = _agent(
            api,
            second["worker_incarnation_id"],
            tmp_path,
            docker,
            SimpleNamespace(discover=_die_after_claim),
        )
        # While the claim is still live the new incarnation cannot prove anything yet.
        assert not agent.reconcile_once().complete
        assert cleanups == []

        _expire_lease(engine, str(lease["lease_id"]))
        assert service.reap_leases(epoch) == 1
        assert _job(engine, job_id)["state"] == "RECOVERING"

        result = agent.reconcile_once()

        assert result.complete, result
        assert [value for value, _ in cleanups] == [attempt_id]
        assert agent.state.operations == {}
        assert agent.journal.load(attempt_id).state == "TOMBSTONED"
        with engine.connect() as connection:
            allocation = (
                connection.execute(
                    select(s.allocations).where(
                        s.allocations.c.allocation_id == UUID(offer["authority"]["allocation_id"])
                    )
                )
                .mappings()
                .one()
            )
        assert allocation["state"] == "RELEASED"
        assert _job(engine, job_id)["state"] != "RECOVERING"

        # A second pass finds nothing left to resolve and sends nothing.
        again = agent.reconcile_once()
        assert again.complete and len(cleanups) == 1
        assert _reconcile_and_heartbeat(client, engine, credential, second, []) == "READY"
