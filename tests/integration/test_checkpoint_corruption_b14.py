import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.schema import checkpoint_corruptions, checkpoints

from ._factories import seed_authority, seed_job, seed_tenant_graph, seed_worker
from .test_constraints_artifacts import _insert_recognized_manifest

pytestmark = pytest.mark.postgres


def _committed_checkpoint(engine, label):
    with engine.begin() as connection:
        graph = seed_tenant_graph(connection, label=label)
        job = seed_job(connection, graph)
        worker = seed_worker(connection, label=label)
        authority = seed_authority(connection, graph, job, worker)
        _insert_recognized_manifest(connection, graph, job, authority, record_type="checkpoint")
        checkpoint_id = connection.execute(
            select(checkpoints.c.checkpoint_id).where(checkpoints.c.job_id == job["job_id"])
        ).scalar_one()
    return graph, checkpoint_id


def test_corruption_mark_is_one_way_and_insert_only(migrated_postgres_engine) -> None:
    graph, checkpoint_id = _committed_checkpoint(migrated_postgres_engine, "corrupt-once")
    with migrated_postgres_engine.begin() as connection:
        connection.execute(
            checkpoint_corruptions.insert(),
            {
                "tenant_id": graph["tenant_id"],
                "checkpoint_id": checkpoint_id,
                "reason_code": "CHECKSUM_MISMATCH",
            },
        )

    for statement in (
        update(checkpoint_corruptions).values(reason_code="MANIFEST_INVALID"),
        delete(checkpoint_corruptions),
    ):
        with (
            pytest.raises(IntegrityError, match="checkpoint_corruptions rows are immutable"),
            migrated_postgres_engine.begin() as connection,
        ):
            connection.execute(statement)

    with (
        pytest.raises(IntegrityError, match="pk_checkpoint_corruptions"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            checkpoint_corruptions.insert(),
            {
                "tenant_id": graph["tenant_id"],
                "checkpoint_id": checkpoint_id,
                "reason_code": "BLOB_MISSING",
            },
        )
    with migrated_postgres_engine.connect() as connection:
        row = connection.execute(select(checkpoint_corruptions)).one()
    assert row.reason_code == "CHECKSUM_MISMATCH"
    assert row.detected_at is not None


def test_corruption_mark_requires_same_tenant_committed_checkpoint(
    migrated_postgres_engine,
) -> None:
    graph, checkpoint_id = _committed_checkpoint(migrated_postgres_engine, "corrupt-owner")
    with migrated_postgres_engine.begin() as connection:
        other = seed_tenant_graph(connection, label="corrupt-other")

    for tenant_id, target in (
        (other["tenant_id"], checkpoint_id),
        (graph["tenant_id"], new_uuid7()),
    ):
        with (
            pytest.raises(IntegrityError, match="fk_checkpoint_corruptions_checkpoint"),
            migrated_postgres_engine.begin() as connection,
        ):
            connection.execute(
                checkpoint_corruptions.insert(),
                {"tenant_id": tenant_id, "checkpoint_id": target, "reason_code": "BLOB_MISSING"},
            )

    with (
        pytest.raises(IntegrityError, match="ck_checkpoint_corruptions_reason_code"),
        migrated_postgres_engine.begin() as connection,
    ):
        connection.execute(
            checkpoint_corruptions.insert(),
            {
                "tenant_id": graph["tenant_id"],
                "checkpoint_id": checkpoint_id,
                "reason_code": "bad reason",
            },
        )
