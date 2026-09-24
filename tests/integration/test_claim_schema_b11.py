import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from tests.integration._factories import seed_authority, seed_job, seed_tenant_graph, seed_worker

pytestmark = pytest.mark.postgres


def test_b11_claim_snapshot_and_dispatch_epoch_are_write_once(migrated_postgres_engine):
    engine = migrated_postgres_engine
    columns = {column["name"] for column in inspect(engine).get_columns("attempts")}
    assert {
        "dispatch_coordinator_epoch",
        "execution_context",
        "claimed_at",
        "executor_operation_sequence",
    } <= columns
    with engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="b11-claim-schema")
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label="b11-claim-schema")
        authority = seed_authority(connection, graph, job, worker)
        connection.execute(
            text(
                "UPDATE attempts SET dispatch_coordinator_epoch=1, execution_context='{}'::jsonb, "
                "claimed_at=clock_timestamp(), executor_operation_sequence=1 WHERE attempt_id=:id"
            ),
            {"id": authority["attempt_id"]},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("UPDATE attempts SET dispatch_coordinator_epoch=2 WHERE attempt_id=:id"),
            {"id": authority["attempt_id"]},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("UPDATE attempts SET execution_context='[]'::jsonb WHERE attempt_id=:id"),
            {"id": authority["attempt_id"]},
        )


def test_attempt_upload_persists_original_authority_grant(migrated_postgres_engine):
    columns = {
        column["name"]
        for column in inspect(migrated_postgres_engine).get_columns("upload_sessions")
    }
    assert "authority_grant_id" in columns
    foreign_keys = inspect(migrated_postgres_engine).get_foreign_keys("upload_sessions")
    assert any(
        row["constrained_columns"] == ["authority_grant_id"]
        and row["referred_table"] == "attempt_authority_grants"
        for row in foreign_keys
    )
