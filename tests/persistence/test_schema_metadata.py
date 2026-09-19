from sqlalchemy import Text

from nexa.infrastructure.persistence.schema import metadata

REQUIRED_TABLES = {
    "nexa_schema_metadata",
    "tenants",
    "users",
    "membership_sets",
    "memberships",
    "system_role_grants",
    "browser_sessions",
    "cli_tokens",
    "worker_credentials",
    "templates",
    "template_versions",
    "artifacts",
    "artifact_reference_guards",
    "jobs",
    "job_specs",
    "logical_sessions",
    "attempts",
    "retry_schedules",
    "sweep_parents",
    "sweep_children",
    "workers",
    "worker_incarnations",
    "worker_inventories",
    "gpu_devices",
    "coordinator_leadership",
    "allocations",
    "allocation_gpu_claims",
    "attempt_leases",
    "attempt_authority_grants",
    "container_identities",
    "policy_versions",
    "tenant_policies",
    "admission_counters",
    "rate_buckets",
    "fairness_state",
    "fairness_ledgers",
    "allocation_ledger_segments",
    "reservations",
    "queue_heads",
    "events",
    "audit_records",
    "idempotency_records",
    "callback_receipts",
    "checkpoint_reservations",
    "checkpoints",
    "checkpoint_references",
    "result_reservations",
    "results",
    "recognized_chunks",
    "log_segments",
    "artifact_references",
    "upload_sessions",
}


def _foreign_key_column_sets(table_name: str) -> set[tuple[str, ...]]:
    return {
        tuple(element.parent.name for element in constraint.elements)
        for constraint in metadata.tables[table_name].foreign_key_constraints
    }


def test_metadata_covers_the_complete_b05_postgresql_model() -> None:
    assert set(metadata.tables) >= REQUIRED_TABLES


def test_credentials_store_hashes_without_raw_secret_columns() -> None:
    expected_hashes = {
        "users": {"password_hash"},
        "browser_sessions": {"secret_hash", "csrf_secret_hash"},
        "cli_tokens": {"token_hash"},
        "worker_credentials": {"credential_hash"},
    }
    forbidden = {"password", "secret", "csrf_secret", "token", "credential"}

    for table_name, hashes in expected_hashes.items():
        columns = set(metadata.tables[table_name].columns.keys())
        assert hashes <= columns
        assert not (forbidden & columns)


def test_fairness_values_use_exact_decimal_text_columns() -> None:
    columns = [
        metadata.tables["tenant_policies"].c.weight,
        metadata.tables["rate_buckets"].c.tokens,
        metadata.tables["rate_buckets"].c.capacity,
        metadata.tables["rate_buckets"].c.refill_rate,
        metadata.tables["fairness_state"].c.virtual_floor,
        metadata.tables["fairness_ledgers"].c.virtual_score,
        metadata.tables["allocation_ledger_segments"].c.dominant_share,
        metadata.tables["allocation_ledger_segments"].c.weight,
        metadata.tables["allocation_ledger_segments"].c.charged_amount,
    ]

    for column in columns:
        assert type(column.type).__name__ == "ExactDecimalText"
        assert isinstance(column.type.impl, Text)


def test_tenant_scoped_provenance_uses_composite_foreign_keys() -> None:
    expected = {
        "job_specs": {("tenant_id", "job_id")},
        "logical_sessions": {("tenant_id", "job_id")},
        "attempts": {("tenant_id", "job_id")},
        "allocations": {
            ("tenant_id", "job_id", "attempt_id", "worker_id"),
        },
        "checkpoints": {
            ("tenant_id", "job_id", "attempt_id"),
            ("tenant_id", "manifest_artifact_id"),
        },
        "results": {
            ("tenant_id", "job_id", "attempt_id"),
            ("tenant_id", "manifest_artifact_id"),
        },
        "recognized_chunks": {
            ("tenant_id", "job_id"),
            ("tenant_id", "job_id", "session_id"),
            ("tenant_id", "job_id", "source_attempt_id", "source_job_fence"),
            ("tenant_id", "artifact_id"),
        },
        "artifact_references": {
            ("tenant_id", "artifact_id"),
        },
    }

    for table_name, required_sets in expected.items():
        assert required_sets <= _foreign_key_column_sets(table_name)
