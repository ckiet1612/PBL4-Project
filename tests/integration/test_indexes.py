import pytest
from sqlalchemy import inspect, text

from ._factories import seed_job, seed_tenant_graph

pytestmark = pytest.mark.postgres


EXPECTED_INDEXES = {
    "jobs": {
        "ix_jobs_queue_head",
        "ix_jobs_oldest_eligible",
        "ix_jobs_retry_ready",
        "ix_jobs_tenant_created_keyset",
    },
    "allocations": {"ix_allocations_unreleased_tenant", "ix_allocations_unreleased_worker"},
    "allocation_gpu_claims": {
        "uq_allocation_gpu_claims_active_device",
        "ix_allocation_gpu_claims_active_allocation",
    },
    "attempt_leases": {"uq_attempt_leases_active_attempt", "ix_attempt_leases_active_expiry"},
    "retry_schedules": {"ix_retry_schedules_ready"},
    "events": {"ix_events_job_sequence_keyset"},
    "idempotency_records": {"ix_idempotency_records_expiry"},
    "artifacts": {"ix_artifacts_tenant_created_keyset"},
    "checkpoints": {"ix_checkpoints_job_created"},
    "results": {"ix_results_tenant_created"},
    "fairness_ledgers": {"ix_fairness_ledgers_score"},
    "allocation_ledger_segments": {
        "ix_allocation_ledger_segments_open",
        "ix_allocation_ledger_segments_tenant_time",
    },
    "audit_records": {"ix_audit_records_created_action", "ix_audit_records_tenant_created"},
}


def test_contract_access_pattern_indexes_are_present(migrated_postgres_engine) -> None:
    database = inspect(migrated_postgres_engine)
    for table_name, expected in EXPECTED_INDEXES.items():
        actual = {index["name"] for index in database.get_indexes(table_name)}
        assert expected <= actual


def test_fairness_score_index_uses_bounded_decimal_ordering_key(
    migrated_postgres_engine,
) -> None:
    with migrated_postgres_engine.connect() as connection:
        index_definition = connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND indexname = 'ix_fairness_ledgers_score'"
            )
        ).scalar_one()

    assert "nexa_decimal_zero_rank" in index_definition
    assert "nexa_decimal_adjusted_exponent" in index_definition
    assert "nexa_decimal_normalized_significand" in index_definition
    assert '"left"(' in index_definition
    assert ", 256)" in index_definition
    assert 'COLLATE "C"' in index_definition
    assert index_definition.rfind("tenant_id") > index_definition.find(
        "nexa_decimal_normalized_significand"
    )


def test_queue_query_shape_can_use_bounded_head_index(migrated_postgres_engine) -> None:
    with migrated_postgres_engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="explain")
        seed_job(connection, graph)

    with migrated_postgres_engine.begin() as connection:
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        plan = (
            connection.execute(
                text(
                    "EXPLAIN (FORMAT TEXT) "
                    "SELECT job_id FROM jobs "
                    "WHERE state = 'QUEUED' AND tenant_id = :tenant_id "
                    "ORDER BY base_priority DESC, ready_sequence, job_id LIMIT 16"
                ),
                {"tenant_id": graph["tenant_id"]},
            )
            .scalars()
            .all()
        )

    assert "ix_jobs_queue_head" in "\n".join(plan)
