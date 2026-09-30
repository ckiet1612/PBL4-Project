"""Remediation B13-R12: every Nexa SQL function resolves names without the caller's search_path.

Tables are unchanged. PostgreSQL 17 runs ANALYZE/autoanalyze, CREATE INDEX and REINDEX with a
restricted search_path and pg_restore runs with an empty one, so a function reachable from a
CHECK constraint, an expression index or a trigger must not rely on the session search_path.
Migration 20260928_0021 enforces one of three rules per function, keyed by signature:

- ``QUALIFIED_DECIMAL_FUNCTIONS``: the Decimal helpers used by CHECK constraints and the
  ``ix_fairness_ledgers_score`` expression index call each other schema-qualified. They keep no
  function-level SET clause, so the SQL-language ones stay inlinable; their results are unchanged.
- ``CATALOG_ONLY_FUNCTIONS``: bodies that reference only ``pg_catalog`` objects.
- ``SEARCH_PATH_PINNED_FUNCTIONS``: trigger and helper functions that reference Nexa tables or
  functions carry ``SET search_path = pg_catalog, <their schema>, pg_temp``.
"""

from sqlalchemy import MetaData

from nexa.infrastructure.persistence import schema_v17

SCHEMA_GENERATION = schema_v17.SCHEMA_GENERATION
metadata = MetaData(naming_convention=dict(schema_v17.metadata.naming_convention))
for _table in schema_v17.metadata.tables.values():
    _table.to_metadata(metadata)

QUALIFIED_DECIMAL_FUNCTIONS = (
    "nexa_decimal_is_nonnegative(text)",
    "nexa_decimal_is_positive(text)",
    "nexa_decimal_zero_rank(text)",
    "nexa_decimal_adjusted_exponent(text)",
    "nexa_decimal_normalized_significand(text)",
    "nexa_decimal_compare(text, text)",
)

CATALOG_ONLY_FUNCTIONS = (
    "nexa_decimal_is_valid(text)",
    "nexa_b13_eligibility_state(bigint, bigint, integer, bigint, bigint, bigint, jsonb)",
)

SEARCH_PATH_PINNED_FUNCTIONS = (
    "nexa_check_membership_set()",
    "nexa_check_job_complete()",
    "nexa_check_job_children_complete()",
    "nexa_reject_immutable_mutation()",
    "nexa_remove_artifact_reference_guard()",
    "nexa_create_artifact_reference_guard()",
    "nexa_validate_job_spec_artifacts()",
    "nexa_validate_committed_artifact_reference()",
    "nexa_protect_referenced_artifact()",
    "nexa_protect_terminal_job()",
    "nexa_protect_referenced_template_version()",
    "nexa_validate_authority_lineage()",
    "nexa_validate_allocation_gpu_claims()",
    "nexa_validate_artifact_reference_owner()",
    "nexa_protect_referenced_upload_owner()",
    "nexa_validate_recognized_record()",
    "nexa_validate_recognized_reservation()",
    "nexa_b11_immutable_claim()",
    "nexa_b13_refresh_queue_head(uuid, integer)",
    "nexa_b13_job_head_change()",
    "nexa_b13_refresh_queue_submitter(uuid, uuid)",
    "nexa_b13_job_submitter_change()",
    "nexa_b13_counter_resume()",
    "nexa_b13_retry_eligibility()",
    "nexa_b13_emit_eligibility(uuid, jsonb, jsonb)",
    "nexa_b13_policy_eligibility()",
    "nexa_b13_allocation_eligibility()",
    "nexa_b13_inventory_eligibility()",
    "nexa_b13_tenant_eligibility()",
    "nexa_b13_quota_step(uuid, text, bigint, timestamp with time zone)",
    "nexa_b13_set_quota_headroom(uuid, bigint, bigint, boolean)",
    "nexa_b13_policy_capacity()",
    "nexa_b13_allocation_quota()",
    "nexa_b13_inventory_capacity()",
    "nexa_b13_tenant_capacity()",
    "nexa_b13_queue_size_delta(uuid, integer)",
    "nexa_b13_job_queue_size()",
    "nexa_b13_spec_queue_size()",
    "nexa_b16_guard_sweep_parent()",
)
