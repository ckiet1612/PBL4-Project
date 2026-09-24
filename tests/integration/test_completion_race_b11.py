"""Fenced completion/cleanup races and repeated success accounting in PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID

import pytest
import rfc8785
from sqlalchemy import insert, select, update

from nexa.api.execution_cleanup_schemas import CleanupRequest
from nexa.api.schemas import CompleteRequest
from nexa.application.errors import ApplicationError
from nexa.application.json_codec import jcs_request_hash
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration._factories import seed_authority, seed_job, seed_worker
from tests.integration.test_execution_closure_b11 import _checksum, _upload
from tests.integration.test_worker_api_b10 import WORKER_ID, _bootstrap_worker, _create_incarnation
from tests.integration.test_worker_authority_b10 import CONTAINER_ID, DIGEST, _running_authority

pytestmark = pytest.mark.postgres


def _authority(prior):
    return {
        key: prior[key]
        for key in (
            "worker_id",
            "worker_incarnation_id",
            "attempt_id",
            "allocation_id",
            "lease_id",
            "job_fence",
        )
    }


def _prepare_completion(client, engine, credential, authority, label):
    with engine.connect() as connection:
        attempt = (
            connection.execute(
                select(s.attempts).where(s.attempts.c.attempt_id == UUID(authority["attempt_id"]))
            )
            .mappings()
            .one()
        )
        job = (
            connection.execute(select(s.jobs).where(s.jobs.c.job_id == attempt["job_id"]))
            .mappings()
            .one()
        )
        spec = (
            connection.execute(select(s.job_specs).where(s.job_specs.c.job_id == job["job_id"]))
            .mappings()
            .one()
        )
        template = (
            connection.execute(
                select(s.template_versions).where(
                    s.template_versions.c.template_id == spec["template_id"],
                    s.template_versions.c.version == spec["template_version"],
                )
            )
            .mappings()
            .one()
        )
        input_artifact = (
            connection.execute(
                select(s.artifacts).where(s.artifacts.c.artifact_id == spec["input_artifact_id"])
            )
            .mappings()
            .one()
        )
        session_id = connection.execute(
            select(s.logical_sessions.c.session_id).where(
                s.logical_sessions.c.job_id == job["job_id"]
            )
        ).scalar_one()
        nonce = attempt["startup_nonce"]
    reserve = client.post(
        f"/v1/attempts/{authority['attempt_id']}/result-reservations",
        headers={"Authorization": f"Bearer {credential}", "X-Callback-Id": str(new_uuid7())},
        json={"authority": authority},
    )
    assert reserve.status_code == 201, reserve.text
    output = _upload(
        client,
        credential,
        authority,
        "RESULT_FILE",
        "application/vnd.nexa.cpu-iterative-result+json",
        f'{{"value":"{label}"}}'.encode(),
        f"b11-race-output-{label}-0001",
    )
    manifest = {
        "kind": "RESULT",
        "schema_version": 1,
        "result_id": reserve.json()["result_id"],
        "created_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "provenance": {
            "tenant_id": str(job["tenant_id"]),
            "job_id": str(job["job_id"]),
            "session_id": str(session_id),
            "attempt_id": authority["attempt_id"],
            "job_fence": authority["job_fence"],
            "input_checksum": input_artifact["checksum"],
            "spec_checksum": spec["spec_checksum"],
            "template_id": spec["template_id"],
            "template_version": spec["template_version"],
            "adapter_id": template["adapter_id"],
            "adapter_version": template["adapter_version"],
            "image_digest": template["image_digest"],
        },
        "status": "SUCCEEDED",
        "files": [
            {
                "artifact_id": output["artifact_id"],
                "logical_name": "result.json",
                "media_type": output["media_type"],
                "size_bytes": output["size_bytes"],
                "checksum": output["checksum"],
            }
        ],
        "metrics": {},
    }
    manifest["manifest_checksum"] = _checksum(rfc8785.dumps(manifest))
    artifact = _upload(
        client,
        credential,
        authority,
        "RESULT_MANIFEST",
        "application/json",
        rfc8785.dumps(manifest),
        f"b11-race-manifest-{label}-0001",
    )
    body = {
        "authority": authority,
        "result_manifest_artifact_id": artifact["artifact_id"],
        "manifest": manifest,
    }
    return CompleteRequest.model_validate(body), jcs_request_hash(body), nonce


def _cleanup_request(authority, nonce, container_id):
    body = {
        **{key: value for key, value in authority.items() if key != "lease_id"},
        "proof": {
            "proof_type": "CONTAINER_STOPPED",
            "startup_nonce": str(nonce),
            "executor_operation_sequence": 2,
            "container": {"container_id": container_id, "runtime_identity_digest": DIGEST},
            "stopped_at": datetime.now(UTC).isoformat(),
            "exit_code": 0,
            "inspection_checksum": "sha256:" + "c" * 64,
        },
    }
    return CleanupRequest.model_validate(body), jcs_request_hash(body)


def test_concurrent_complete_and_cleanup_charge_two_successful_jobs_once(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b11-race-incarnation"
        )
        with engine.begin() as connection:
            seed_worker(
                connection,
                label="b11-race",
                existing=(UUID(WORKER_ID), UUID(incarnation["worker_incarnation_id"])),
            )
        first = _running_authority(engine, incarnation)
        with engine.begin() as connection:
            job = connection.execute(select(s.jobs)).mappings().one()
            spec = connection.execute(select(s.job_specs)).mappings().one()
            graph = {
                "tenant_id": job["tenant_id"],
                "user_id": job["submitter_user_id"],
                "artifact_id": spec["input_artifact_id"],
                "template_id": spec["template_id"],
            }
            second_job = seed_job(connection, graph, state="RUNNING")
            ids = seed_authority(
                connection,
                graph,
                second_job,
                {
                    "worker_id": UUID(WORKER_ID),
                    "incarnation_id": UUID(incarnation["worker_incarnation_id"]),
                },
            )
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
            second_container = "d" * 64
            connection.execute(
                insert(s.container_identities).values(
                    tenant_id=graph["tenant_id"],
                    job_id=second_job["job_id"],
                    attempt_id=ids["attempt_id"],
                    allocation_id=ids["allocation_id"],
                    startup_nonce=nonce,
                    executor_create_sequence=1,
                    container_id=second_container,
                    runtime_identity_digest=DIGEST,
                    created_at=datetime.now(UTC),
                )
            )
            for scope, identity in (
                ("GLOBAL", "global"),
                ("TENANT", str(graph["tenant_id"])),
                ("USER", f"{graph['tenant_id']}:{graph['user_id']}"),
            ):
                connection.execute(
                    insert(s.admission_counters).values(
                        scope_type=scope,
                        scope_id=identity,
                        outstanding=2,
                        active_attempts=2,
                    )
                )
        second = {
            "worker_id": WORKER_ID,
            "worker_incarnation_id": incarnation["worker_incarnation_id"],
            "attempt_id": str(ids["attempt_id"]),
            "allocation_id": str(ids["allocation_id"]),
            "lease_id": str(ids["lease_id"]),
            "job_fence": 1,
        }
        worker_service = client.app.state.services.worker
        first_auth = _authority(first)
        complete, payload_hash, first_nonce = _prepare_completion(
            client, engine, credential, first_auth, "first"
        )
        first_callback, competing_callback = new_uuid7(), new_uuid7()

        def finish(callback):
            try:
                return worker_service.complete_attempt(
                    credential=credential,
                    attempt_id=UUID(first_auth["attempt_id"]),
                    callback_id=callback,
                    payload_hash=payload_hash,
                    request=complete,
                )
            except ApplicationError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(finish, (first_callback, competing_callback)))
        assert (
            sum(
                isinstance(outcome, dict) and outcome.get("accepted") is True
                for outcome in outcomes
            )
            == 1
        )
        assert (
            sum(
                isinstance(outcome, ApplicationError) and outcome.status == 409
                for outcome in outcomes
            )
            == 1
        )
        with engine.connect() as connection:
            assert len(connection.execute(select(s.results)).all()) == 1
            assert set(
                connection.execute(
                    select(
                        s.admission_counters.c.outstanding, s.admission_counters.c.active_attempts
                    )
                )
            ) == {(1, 2)}
        complete_second, hash_second, _ = _prepare_completion(
            client, engine, credential, second, "second"
        )
        assert (
            worker_service.complete_attempt(
                credential=credential,
                attempt_id=UUID(second["attempt_id"]),
                callback_id=new_uuid7(),
                payload_hash=hash_second,
                request=complete_second,
            )["accepted"]
            is True
        )
        with engine.connect() as connection:
            assert len(connection.execute(select(s.results)).all()) == 2
            assert set(
                connection.execute(
                    select(
                        s.admission_counters.c.outstanding, s.admission_counters.c.active_attempts
                    )
                )
            ) == {(0, 2)}
        cleanup, cleanup_hash = _cleanup_request(first_auth, first_nonce, CONTAINER_ID)

        def release(callback):
            return worker_service.report_cleanup(
                worker_id=UUID(WORKER_ID),
                credential=credential,
                attempt_id=UUID(first_auth["attempt_id"]),
                callback_id=callback,
                payload_hash=cleanup_hash,
                request=cleanup,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            released = list(pool.map(release, (new_uuid7(), new_uuid7())))
        assert all(item["verified"] is True for item in released)
        with engine.connect() as connection:
            assert set(
                connection.execute(
                    select(
                        s.admission_counters.c.outstanding, s.admission_counters.c.active_attempts
                    )
                )
            ) == {(0, 1)}
        cleanup_two, second_hash = _cleanup_request(second, nonce, second_container)
        assert (
            worker_service.report_cleanup(
                worker_id=UUID(WORKER_ID),
                credential=credential,
                attempt_id=UUID(second["attempt_id"]),
                callback_id=new_uuid7(),
                payload_hash=second_hash,
                request=cleanup_two,
            )["verified"]
            is True
        )
        with engine.connect() as connection:
            assert set(
                connection.execute(
                    select(
                        s.admission_counters.c.outstanding, s.admission_counters.c.active_attempts
                    )
                )
            ) == {(0, 0)}
            assert set(connection.execute(select(s.allocations.c.state))) == {("RELEASED",)}
            assert len(connection.execute(select(s.results)).all()) == 2
