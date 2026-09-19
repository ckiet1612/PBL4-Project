import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema import (
    artifact_reference_guards,
    artifact_references,
    artifacts,
    attempt_authority_grants,
    attempt_leases,
    callback_receipts,
    checkpoint_reservations,
    checkpoints,
    events,
    idempotency_records,
    job_specs,
    log_segments,
    recognized_chunks,
    result_reservations,
    results,
)

from ._factories import CHECKSUM, seed_authority, seed_job, seed_tenant_graph, seed_worker

pytestmark = pytest.mark.postgres


def _insert_artifact(connection, graph, *, kind: str, suffix: str, state: str = "COMMITTED"):
    artifact_id = new_uuid7()
    connection.execute(
        artifacts.insert(),
        {
            "artifact_id": artifact_id,
            "tenant_id": graph["tenant_id"],
            "kind": kind,
            "media_type": "application/json",
            "size_bytes": 128,
            "checksum": CHECKSUM,
            "blob_key": f"blob/{suffix}",
            "state": state,
            "version": 1,
        },
    )
    return artifact_id


def _wait_for_lock_or_finish(engine, backend_pid: int, finished: threading.Event) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if finished.is_set():
            return False
        with engine.connect() as connection:
            waiting_on_lock = connection.execute(
                text(
                    "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :backend_pid"
                ),
                {"backend_pid": backend_pid},
            ).scalar_one_or_none()
        if waiting_on_lock:
            return True
        time.sleep(0.01)
    raise AssertionError("concurrent recognition neither blocked nor finished")


def _insert_recognized_manifest(
    connection,
    graph,
    job,
    authority,
    *,
    record_type: str,
    manifest_checksum: str = CHECKSUM,
):
    manifest_id = _insert_artifact(
        connection,
        graph,
        kind=f"{record_type.upper()}_MANIFEST",
        suffix=f"recognized-{record_type}",
    )
    record_id = new_uuid7()
    if record_type == "checkpoint":
        connection.execute(
            checkpoint_reservations.insert(),
            {
                "checkpoint_id": record_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "authority_grant_id": authority["grant_id"],
                "sequence": 1,
                "callback_id": new_uuid7(),
                "state": "COMMITTED",
                "ended_at": datetime.now(UTC),
            },
        )
        connection.execute(
            checkpoints.insert(),
            {
                "checkpoint_id": record_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "sequence": 1,
                "manifest_artifact_id": manifest_id,
                "manifest_checksum": manifest_checksum,
                "provenance": {},
                "compatibility": {},
                "state": "COMMITTED",
            },
        )
    else:
        connection.execute(
            result_reservations.insert(),
            {
                "result_id": record_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "authority_grant_id": authority["grant_id"],
                "callback_id": new_uuid7(),
                "state": "COMMITTED",
            },
        )
        connection.execute(
            results.insert(),
            {
                "result_id": record_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "manifest_artifact_id": manifest_id,
                "manifest_checksum": manifest_checksum,
            },
        )
    return manifest_id


def _insert_idempotency(connection, *, context: str | None, suffix: str):
    connection.execute(
        idempotency_records.insert(),
        {
            "idempotency_id": new_uuid7(),
            "context": context,
            "principal_id": "principal-1",
            "operation_id": "submitJob",
            "idempotency_key": f"0123456789abcdef{suffix}",
            "request_hash": CHECKSUM,
            "state": "PENDING",
            "one_time_secret": False,
            "expires_at": datetime.now(UTC) + timedelta(days=30),
        },
    )


def test_idempotency_scope_rejects_null_invalid_and_duplicate_global_context(
    migrated_postgres_engine,
) -> None:
    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        _insert_idempotency(connection, context=None, suffix="-null")

    with (
        pytest.raises(IntegrityError, match="context_namespace"),
        migrated_postgres_engine.begin() as connection,
    ):
        _insert_idempotency(connection, context="global", suffix="-invalid")

    with migrated_postgres_engine.begin() as connection:
        _insert_idempotency(connection, context="GLOBAL", suffix="-same")
    with (
        pytest.raises(IntegrityError, match="uq_idempotency_scope"),
        migrated_postgres_engine.begin() as connection,
    ):
        _insert_idempotency(connection, context="GLOBAL", suffix="-same")


def test_worker_and_tenant_idempotency_namespaces_are_valid(migrated_postgres_engine) -> None:
    worker_id = new_uuid7()
    tenant_id = new_uuid7()
    with migrated_postgres_engine.begin() as connection:
        _insert_idempotency(connection, context=f"WORKER:{worker_id}", suffix="-worker")
        _insert_idempotency(connection, context=str(tenant_id), suffix="-tenant")
        _insert_idempotency(connection, context="BOOTSTRAP", suffix="-bootstrap")


def test_callback_scope_is_unique(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        worker = seed_worker(connection, label="callback")
        callback_id = new_uuid7()
        values = {
            "receipt_id": new_uuid7(),
            "worker_id": worker["worker_id"],
            "operation_id": "workerRenewAttempt",
            "callback_id": callback_id,
            "payload_hash": CHECKSUM,
            "acknowledgment": {"accepted": True},
        }
        connection.execute(callback_receipts.insert(), values)

    with (
        pytest.raises(IntegrityError, match="uq_callback_scope"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            callback_receipts.insert(),
            {**values, "receipt_id": new_uuid7(), "payload_hash": "sha256:" + "c" * 64},
        )


def test_checkpoint_reservation_sequence_and_active_attempt_are_unique(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="checkpoint")
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label="checkpoint")
        authority = seed_authority(connection, graph, job, worker)
        connection.execute(
            checkpoint_reservations.insert(),
            {
                "checkpoint_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "authority_grant_id": authority["grant_id"],
                "sequence": 1,
                "callback_id": new_uuid7(),
                "state": "RESERVED",
            },
        )

    with (
        pytest.raises(IntegrityError, match="uq_checkpoint_reservations_reserved_attempt"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            checkpoint_reservations.insert(),
            {
                "checkpoint_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "authority_grant_id": authority["grant_id"],
                "sequence": 2,
                "callback_id": new_uuid7(),
                "state": "RESERVED",
            },
        )


def test_checkpoint_sequence_is_unique_across_attempts(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="checkpoint-cross-attempt")
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label="checkpoint-cross-attempt")
        first_authority = seed_authority(connection, graph, job, worker)
        connection.execute(
            checkpoint_reservations.insert(),
            {
                "checkpoint_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": first_authority["attempt_id"],
                "authority_grant_id": first_authority["grant_id"],
                "sequence": 1,
                "callback_id": new_uuid7(),
                "state": "ABANDONED",
                "ended_at": datetime.now(UTC),
            },
        )
        connection.execute(
            attempt_authority_grants.update()
            .where(attempt_authority_grants.c.grant_id == first_authority["grant_id"])
            .values(ended_at=datetime.now(UTC))
        )
        connection.execute(
            attempt_leases.update()
            .where(attempt_leases.c.lease_id == first_authority["lease_id"])
            .values(revoked_at=datetime.now(UTC), revoke_reason="RETRY")
        )
        second_authority = seed_authority(
            connection,
            graph,
            job,
            worker,
            attempt_number=2,
            job_fence=2,
        )

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        connection.execute(
            checkpoint_reservations.insert(),
            {
                "checkpoint_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": second_authority["attempt_id"],
                "authority_grant_id": second_authority["grant_id"],
                "sequence": 1,
                "callback_id": new_uuid7(),
                "state": "RESERVED",
            },
        )


def test_only_one_result_reservation_can_be_active_per_job(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="result-reservation-unique")
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label="result-reservation-unique")
        authority = seed_authority(connection, graph, job, worker)
        connection.execute(
            result_reservations.insert(),
            {
                "result_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "authority_grant_id": authority["grant_id"],
                "callback_id": new_uuid7(),
                "state": "ACTIVE",
            },
        )

    with (
        pytest.raises(IntegrityError, match="uq_result_reservations_active_job"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            result_reservations.insert(),
            {
                "result_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "authority_grant_id": authority["grant_id"],
                "callback_id": new_uuid7(),
                "state": "ACTIVE",
            },
        )


def test_committed_checkpoint_requires_exact_reservation_attempt_and_artifact(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="checkpoint-commit")
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label="checkpoint-commit")
        authority = seed_authority(connection, graph, job, worker)
        checkpoint_id = new_uuid7()
        manifest_id = _insert_artifact(
            connection, graph, kind="CHECKPOINT_MANIFEST", suffix="checkpoint-manifest"
        )
        connection.execute(
            checkpoint_reservations.insert(),
            {
                "checkpoint_id": checkpoint_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "authority_grant_id": authority["grant_id"],
                "sequence": 1,
                "callback_id": new_uuid7(),
                "state": "COMMITTED",
                "ended_at": datetime.now(UTC),
            },
        )
        connection.execute(
            checkpoints.insert(),
            {
                "checkpoint_id": checkpoint_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "sequence": 1,
                "manifest_artifact_id": manifest_id,
                "manifest_checksum": CHECKSUM,
                "provenance": {},
                "compatibility": {},
                "state": "COMMITTED",
            },
        )

    with migrated_postgres_engine.connect() as connection:
        assert connection.execute(checkpoints.select()).one().checkpoint_id == checkpoint_id


def test_job_spec_rejects_staging_input_artifact(migrated_postgres_engine) -> None:
    with (
        pytest.raises(IntegrityError, match="fk_job_specs_input_artifact"),
        migrated_postgres_engine.begin() as connection,
    ):
        graph = seed_tenant_graph(connection, label="staging-input")
        connection.execute(
            artifacts.update()
            .where(artifacts.c.artifact_id == graph["artifact_id"])
            .values(state="STAGING")
        )
        seed_job(connection, graph)


def test_reference_guard_cannot_be_created_for_staging_artifact(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="staging-guard")
        artifact_id = _insert_artifact(
            connection,
            graph,
            kind="INPUT",
            suffix="staging-guard",
            state="STAGING",
        )

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        connection.execute(
            artifact_reference_guards.insert(),
            {"tenant_id": graph["tenant_id"], "artifact_id": artifact_id},
        )


@pytest.mark.parametrize(
    "artifact_values",
    [
        pytest.param({"checksum": "sha256:" + "c" * 64}, id="checksum"),
        pytest.param({"blob_key": "blob/accepted-input/mutated"}, id="blob-key"),
        pytest.param({"state": "STAGING"}, id="state"),
    ],
)
def test_accepted_input_artifact_identity_is_immutable(
    migrated_postgres_engine,
    artifact_values,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="accepted-input")
        seed_job(connection, graph)

    with (
        pytest.raises(IntegrityError, match="referenced artifact identity is immutable"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            artifacts.update()
            .where(artifacts.c.artifact_id == graph["artifact_id"])
            .values(**artifact_values)
        )


@pytest.mark.parametrize("record_type", ["checkpoint", "result"])
def test_recognized_manifest_requires_matching_artifact_checksum(
    migrated_postgres_engine,
    record_type,
) -> None:
    with (
        pytest.raises(
            IntegrityError, match="recognized metadata requires committed reservation and artifact"
        ),
        migrated_postgres_engine.begin() as connection,
    ):
        graph = seed_tenant_graph(connection, label=f"{record_type}-checksum")
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label=f"{record_type}-checksum")
        authority = seed_authority(connection, graph, job, worker)
        _insert_recognized_manifest(
            connection,
            graph,
            job,
            authority,
            record_type=record_type,
            manifest_checksum="sha256:" + "c" * 64,
        )


@pytest.mark.parametrize("record_type", ["checkpoint", "result"])
@pytest.mark.parametrize(
    "artifact_values",
    [
        pytest.param({"checksum": "sha256:" + "c" * 64}, id="checksum"),
        pytest.param({"blob_key": "blob/recognized-manifest/mutated"}, id="blob-key"),
        pytest.param({"state": "STAGING"}, id="state"),
    ],
)
def test_recognized_manifest_artifact_identity_is_immutable(
    migrated_postgres_engine,
    record_type,
    artifact_values,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label=f"{record_type}-immutable")
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label=f"{record_type}-immutable")
        authority = seed_authority(connection, graph, job, worker)
        manifest_id = _insert_recognized_manifest(
            connection,
            graph,
            job,
            authority,
            record_type=record_type,
        )

    with (
        pytest.raises(IntegrityError, match="referenced artifact identity is immutable"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            artifacts.update()
            .where(artifacts.c.artifact_id == manifest_id)
            .values(**artifact_values)
        )


def test_unreferenced_artifact_can_be_cleaned_up(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="unreferenced-cleanup")
        artifact_id = _insert_artifact(
            connection,
            graph,
            kind="RESULT_FILE",
            suffix="unreferenced-cleanup",
        )
        connection.execute(
            artifacts.update()
            .where(artifacts.c.artifact_id == artifact_id)
            .values(state="DELETING")
        )
        connection.execute(artifacts.delete().where(artifacts.c.artifact_id == artifact_id))

    with migrated_postgres_engine.connect() as connection:
        assert (
            connection.execute(
                artifacts.select().where(artifacts.c.artifact_id == artifact_id)
            ).first()
            is None
        )


@pytest.mark.parametrize("recognition_type", ["input", "checkpoint", "result"])
def test_artifact_deletion_state_serializes_with_concurrent_recognition(
    migrated_postgres_engine,
    recognition_type: str,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label=f"artifact-race-{recognition_type}")
        manifest_id = _insert_artifact(
            connection,
            graph,
            kind="INPUT" if recognition_type == "input" else f"{recognition_type.upper()}_MANIFEST",
            suffix=f"artifact-race-{recognition_type}",
        )
        if recognition_type != "input":
            job = seed_job(connection, graph)
            worker = seed_worker(connection, label=f"artifact-race-{recognition_type}")
            authority = seed_authority(connection, graph, job, worker)

    cleanup_connection = migrated_postgres_engine.connect()
    cleanup_transaction = cleanup_connection.begin()
    cleanup_connection.execute(
        artifacts.update().where(artifacts.c.artifact_id == manifest_id).values(state="DELETING")
    )

    recognition_started = threading.Event()
    recognition_finished = threading.Event()
    recognition_error: list[BaseException] = []
    recognition_backend_pid: list[int] = []

    def recognize_artifact() -> None:
        try:
            with migrated_postgres_engine.begin() as connection:
                connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                recognition_backend_pid.append(
                    connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                )
                recognition_started.set()
                if recognition_type == "input":
                    seed_job(connection, graph, artifact_id=manifest_id)
                    return

                record_id = new_uuid7()
                if recognition_type == "checkpoint":
                    connection.execute(
                        checkpoint_reservations.insert(),
                        {
                            "checkpoint_id": record_id,
                            "tenant_id": graph["tenant_id"],
                            "job_id": job["job_id"],
                            "attempt_id": authority["attempt_id"],
                            "authority_grant_id": authority["grant_id"],
                            "sequence": 1,
                            "callback_id": new_uuid7(),
                            "state": "COMMITTED",
                            "ended_at": datetime.now(UTC),
                        },
                    )
                    connection.execute(
                        checkpoints.insert(),
                        {
                            "checkpoint_id": record_id,
                            "tenant_id": graph["tenant_id"],
                            "job_id": job["job_id"],
                            "attempt_id": authority["attempt_id"],
                            "sequence": 1,
                            "manifest_artifact_id": manifest_id,
                            "manifest_checksum": CHECKSUM,
                            "provenance": {},
                            "compatibility": {},
                            "state": "COMMITTED",
                        },
                    )
                    return

                connection.execute(
                    result_reservations.insert(),
                    {
                        "result_id": record_id,
                        "tenant_id": graph["tenant_id"],
                        "job_id": job["job_id"],
                        "attempt_id": authority["attempt_id"],
                        "authority_grant_id": authority["grant_id"],
                        "callback_id": new_uuid7(),
                        "state": "COMMITTED",
                    },
                )
                connection.execute(
                    results.insert(),
                    {
                        "result_id": record_id,
                        "tenant_id": graph["tenant_id"],
                        "job_id": job["job_id"],
                        "attempt_id": authority["attempt_id"],
                        "manifest_artifact_id": manifest_id,
                        "manifest_checksum": CHECKSUM,
                    },
                )
        except BaseException as exc:
            recognition_error.append(exc)
        finally:
            recognition_finished.set()

    thread = threading.Thread(target=recognize_artifact, daemon=True)
    thread.start()
    assert recognition_started.wait(timeout=5)
    recognition_blocked_on_cleanup = _wait_for_lock_or_finish(
        migrated_postgres_engine, recognition_backend_pid[0], recognition_finished
    )
    cleanup_transaction.commit()
    cleanup_connection.close()
    assert recognition_finished.wait(timeout=5)
    thread.join(timeout=1)

    assert recognition_blocked_on_cleanup
    assert len(recognition_error) == 1
    assert isinstance(recognition_error[0], IntegrityError)
    recognized_table, artifact_column = {
        "input": (job_specs, job_specs.c.input_artifact_id),
        "checkpoint": (checkpoints, checkpoints.c.manifest_artifact_id),
        "result": (results, results.c.manifest_artifact_id),
    }[recognition_type]
    with migrated_postgres_engine.connect() as connection:
        assert (
            connection.execute(
                select(artifacts.c.state).where(artifacts.c.artifact_id == manifest_id)
            ).scalar_one()
            == "DELETING"
        )
        assert (
            connection.execute(
                select(func.count())
                .select_from(recognized_table)
                .where(artifact_column == manifest_id)
            ).scalar_one()
            == 0
        )


def test_only_committed_artifacts_can_be_referenced_by_reference_chunk_or_log(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="committed-reference")
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label="committed-reference")
        authority = seed_authority(connection, graph, job, worker)
        staging_reference = _insert_artifact(
            connection,
            graph,
            kind="INPUT",
            suffix="staging-reference",
            state="STAGING",
        )
        staging_chunk = _insert_artifact(
            connection,
            graph,
            kind="RESULT_FILE",
            suffix="staging-chunk",
            state="STAGING",
        )
        staging_log = _insert_artifact(
            connection,
            graph,
            kind="LOG_SEGMENT",
            suffix="staging-log",
            state="STAGING",
        )

    with (
        pytest.raises(IntegrityError, match="artifact references require committed artifacts"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            artifact_references.insert(),
            {
                "tenant_id": graph["tenant_id"],
                "artifact_id": staging_reference,
                "owner_type": "JOB_SPEC",
                "owner_id": job["job_id"],
                "purpose": "INPUT",
                "logical_name": "staging.input",
            },
        )

    with (
        pytest.raises(IntegrityError, match="artifact references require committed artifacts"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            recognized_chunks.insert(),
            {
                "recognized_chunk_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "session_id": job["session_id"],
                "chunk_id": "staging-chunk",
                "range_start": 0,
                "range_end": 10,
                "artifact_id": staging_chunk,
                "checksum": CHECKSUM,
                "source_attempt_id": authority["attempt_id"],
                "source_job_fence": 1,
            },
        )

    with (
        pytest.raises(IntegrityError, match="artifact references require committed artifacts"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            log_segments.insert(),
            {
                "log_segment_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "start_offset": 0,
                "end_offset": 10,
                "artifact_id": staging_log,
                "checksum": CHECKSUM,
                "truncated": False,
            },
        )


def test_chunk_and_log_checksums_must_match_their_artifacts(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="artifact-checksum")
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label="artifact-checksum")
        authority = seed_authority(connection, graph, job, worker)
        chunk_artifact = _insert_artifact(
            connection,
            graph,
            kind="RESULT_FILE",
            suffix="chunk-checksum",
        )
        log_artifact = _insert_artifact(
            connection,
            graph,
            kind="LOG_SEGMENT",
            suffix="log-checksum",
        )

    mismatched_checksum = "sha256:" + "c" * 64
    with (
        pytest.raises(IntegrityError, match="exact checksum"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            recognized_chunks.insert(),
            {
                "recognized_chunk_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "session_id": job["session_id"],
                "chunk_id": "checksum-mismatch",
                "range_start": 0,
                "range_end": 10,
                "artifact_id": chunk_artifact,
                "checksum": mismatched_checksum,
                "source_attempt_id": authority["attempt_id"],
                "source_job_fence": 1,
            },
        )

    with (
        pytest.raises(IntegrityError, match="exact checksum"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            log_segments.insert(),
            {
                "log_segment_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "start_offset": 0,
                "end_offset": 10,
                "artifact_id": log_artifact,
                "checksum": mismatched_checksum,
                "truncated": False,
            },
        )


def test_reserved_checkpoint_is_not_recognized_as_committed(migrated_postgres_engine) -> None:
    with (
        pytest.raises(
            IntegrityError, match="recognized metadata requires committed reservation and artifact"
        ),
        migrated_postgres_engine.begin() as connection,
    ):
        graph = seed_tenant_graph(connection, label="checkpoint-reserved")
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label="checkpoint-reserved")
        authority = seed_authority(connection, graph, job, worker)
        checkpoint_id = new_uuid7()
        manifest_id = _insert_artifact(
            connection, graph, kind="CHECKPOINT_MANIFEST", suffix="checkpoint-reserved"
        )
        connection.execute(
            checkpoint_reservations.insert(),
            {
                "checkpoint_id": checkpoint_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "authority_grant_id": authority["grant_id"],
                "sequence": 1,
                "callback_id": new_uuid7(),
                "state": "RESERVED",
            },
        )
        connection.execute(
            checkpoints.insert(),
            {
                "checkpoint_id": checkpoint_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "sequence": 1,
                "manifest_artifact_id": manifest_id,
                "manifest_checksum": CHECKSUM,
                "provenance": {},
                "compatibility": {},
                "state": "COMMITTED",
            },
        )


def test_active_result_reservation_is_not_recognized_as_final(migrated_postgres_engine) -> None:
    with (
        pytest.raises(
            IntegrityError, match="recognized metadata requires committed reservation and artifact"
        ),
        migrated_postgres_engine.begin() as connection,
    ):
        graph = seed_tenant_graph(connection, label="result-active")
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label="result-active")
        authority = seed_authority(connection, graph, job, worker)
        result_id = new_uuid7()
        manifest_id = _insert_artifact(
            connection, graph, kind="RESULT_MANIFEST", suffix="result-active"
        )
        connection.execute(
            result_reservations.insert(),
            {
                "result_id": result_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "authority_grant_id": authority["grant_id"],
                "callback_id": new_uuid7(),
                "state": "ACTIVE",
            },
        )
        connection.execute(
            results.insert(),
            {
                "result_id": result_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "manifest_artifact_id": manifest_id,
                "manifest_checksum": CHECKSUM,
            },
        )


def test_recognized_checkpoint_and_result_reservations_cannot_be_abandoned(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="recognized-reservation")
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label="recognized-reservation")
        authority = seed_authority(connection, graph, job, worker)
        checkpoint_id = new_uuid7()
        checkpoint_manifest_id = _insert_artifact(
            connection,
            graph,
            kind="CHECKPOINT_MANIFEST",
            suffix="recognized-checkpoint",
        )
        connection.execute(
            checkpoint_reservations.insert(),
            {
                "checkpoint_id": checkpoint_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "authority_grant_id": authority["grant_id"],
                "sequence": 1,
                "callback_id": new_uuid7(),
                "state": "COMMITTED",
                "ended_at": datetime.now(UTC),
            },
        )
        connection.execute(
            checkpoints.insert(),
            {
                "checkpoint_id": checkpoint_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "sequence": 1,
                "manifest_artifact_id": checkpoint_manifest_id,
                "manifest_checksum": CHECKSUM,
                "provenance": {},
                "compatibility": {},
                "state": "COMMITTED",
            },
        )
        result_id = new_uuid7()
        result_manifest_id = _insert_artifact(
            connection, graph, kind="RESULT_MANIFEST", suffix="recognized-result"
        )
        connection.execute(
            result_reservations.insert(),
            {
                "result_id": result_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "authority_grant_id": authority["grant_id"],
                "callback_id": new_uuid7(),
                "state": "COMMITTED",
            },
        )
        connection.execute(
            results.insert(),
            {
                "result_id": result_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "manifest_artifact_id": result_manifest_id,
                "manifest_checksum": CHECKSUM,
            },
        )

    with (
        pytest.raises(IntegrityError, match="recognized metadata requires committed reservation"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            checkpoint_reservations.update()
            .where(checkpoint_reservations.c.checkpoint_id == checkpoint_id)
            .values(state="ABANDONED")
        )

    with (
        pytest.raises(IntegrityError, match="recognized metadata requires committed reservation"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            result_reservations.update()
            .where(result_reservations.c.result_id == result_id)
            .values(state="ABANDONED")
        )


def test_only_one_final_result_and_one_event_sequence_per_job(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="result")
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label="result")
        authority = seed_authority(connection, graph, job, worker)
        manifest_id = _insert_artifact(
            connection, graph, kind="RESULT_MANIFEST", suffix="result-one"
        )
        result_id = new_uuid7()
        connection.execute(
            result_reservations.insert(),
            {
                "result_id": result_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "authority_grant_id": authority["grant_id"],
                "callback_id": new_uuid7(),
                "state": "COMMITTED",
            },
        )
        connection.execute(
            results.insert(),
            {
                "result_id": result_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "manifest_artifact_id": manifest_id,
                "manifest_checksum": CHECKSUM,
            },
        )
        connection.execute(
            events.insert(),
            {
                "event_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "sequence": 1,
                "event_type": "JOB_ACCEPTED",
                "actor_type": "USER",
                "actor_id": str(graph["user_id"]),
                "safe_metadata": {},
            },
        )

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        second_result_id = new_uuid7()
        connection.execute(
            result_reservations.insert(),
            {
                "result_id": second_result_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "authority_grant_id": authority["grant_id"],
                "callback_id": new_uuid7(),
                "state": "COMMITTED",
            },
        )
        connection.execute(
            results.insert(),
            {
                "result_id": second_result_id,
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "attempt_id": authority["attempt_id"],
                "manifest_artifact_id": manifest_id,
                "manifest_checksum": CHECKSUM,
            },
        )

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        connection.execute(
            events.insert(),
            {
                "event_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job["job_id"],
                "sequence": 1,
                "event_type": "DUPLICATE",
                "actor_type": "SYSTEM",
                "actor_id": "coordinator",
                "safe_metadata": {},
            },
        )


def test_recognized_chunk_cannot_mix_job_and_logical_session(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="chunk")
        job_one = seed_job(connection, graph)
        job_two = seed_job(connection, graph)
        worker = seed_worker(connection, label="chunk")
        authority = seed_authority(connection, graph, job_one, worker)
        chunk_artifact = _insert_artifact(connection, graph, kind="RESULT_FILE", suffix="chunk")

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        connection.execute(
            recognized_chunks.insert(),
            {
                "recognized_chunk_id": new_uuid7(),
                "tenant_id": graph["tenant_id"],
                "job_id": job_one["job_id"],
                "session_id": job_two["session_id"],
                "chunk_id": "chunk-1",
                "range_start": 0,
                "range_end": 10,
                "artifact_id": chunk_artifact,
                "checksum": CHECKSUM,
                "source_attempt_id": authority["attempt_id"],
                "source_job_fence": 1,
            },
        )


def test_artifact_reference_owner_is_validated_on_insert_and_update(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="reference")
        other_graph = seed_tenant_graph(connection, label="reference-other")
        job = seed_job(connection, graph)
        connection.execute(
            artifact_references.insert(),
            {
                "tenant_id": graph["tenant_id"],
                "artifact_id": graph["artifact_id"],
                "owner_type": "JOB_SPEC",
                "owner_id": job["job_id"],
                "purpose": "INPUT",
                "logical_name": "input.data",
            },
        )

    with (
        pytest.raises(IntegrityError, match="owner does not exist in tenant"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            artifact_references.update()
            .where(artifact_references.c.owner_id == job["job_id"])
            .values(owner_id=new_uuid7())
        )

    with pytest.raises(IntegrityError), migrated_postgres_engine.begin() as connection:
        connection.execute(
            artifact_references.update()
            .where(artifact_references.c.owner_id == job["job_id"])
            .values(artifact_id=other_graph["artifact_id"])
        )
