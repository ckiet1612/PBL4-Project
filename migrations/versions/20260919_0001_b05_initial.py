"""Create the B05 PostgreSQL schema.

Revision ID: 20260919_0001
Revises: None
"""

from collections.abc import Iterable
from decimal import MAX_EMAX, MIN_ETINY

from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import AddConstraint, CreateIndex, CreateTable

from nexa.infrastructure.persistence.schema_v1 import metadata

revision = "20260919_0001"
down_revision = None
branch_labels = None
depends_on = None


def _sql(ddl: object) -> str:
    return str(ddl.compile(dialect=postgresql.dialect()))


def _alter_constraints() -> Iterable[object]:
    for table in metadata.tables.values():
        for constraint in table.foreign_key_constraints:
            if constraint.use_alter:
                yield constraint


def _create_decimal_functions() -> None:
    statements = [
        f"""
        CREATE FUNCTION nexa_decimal_is_valid(encoded text) RETURNS boolean
        LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
        DECLARE digits text;
        DECLARE exponent_text text;
        DECLARE exponent_value bigint;
        BEGIN
          IF encoded !~ '^[01]:(0|[1-9][0-9]*):(0|-?[1-9][0-9]*)$' THEN
            RETURN false;
          END IF;
          exponent_text := split_part(encoded, ':', 3);
          IF length(ltrim(exponent_text, '-')) > {len(str(abs(MIN_ETINY)))} THEN
            RETURN false;
          END IF;
          BEGIN
            exponent_value := exponent_text::bigint;
          EXCEPTION WHEN numeric_value_out_of_range THEN
            RETURN false;
          END;
          IF exponent_value NOT BETWEEN {MIN_ETINY} AND {MAX_EMAX} THEN
            RETURN false;
          END IF;
          digits := split_part(encoded, ':', 2);
          RETURN digits = '0'
            OR exponent_value + length(digits)::bigint - 1 <= {MAX_EMAX};
        END
        $$
        """,
        """
        CREATE FUNCTION nexa_decimal_is_nonnegative(encoded text) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
          SELECT nexa_decimal_is_valid(encoded)
            AND (split_part(encoded, ':', 1) = '0' OR split_part(encoded, ':', 2) = '0')
        $$
        """,
        """
        CREATE FUNCTION nexa_decimal_is_positive(encoded text) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
          SELECT nexa_decimal_is_valid(encoded)
            AND split_part(encoded, ':', 1) = '0'
            AND split_part(encoded, ':', 2) <> '0'
        $$
        """,
        """
        CREATE FUNCTION nexa_decimal_zero_rank(encoded text) RETURNS integer
        LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
        BEGIN
          IF NOT nexa_decimal_is_valid(encoded) THEN
            RAISE EXCEPTION 'invalid exact decimal encoding'
              USING ERRCODE = '23514';
          END IF;
          IF split_part(encoded, ':', 2) = '0' THEN
            RETURN 0;
          END IF;
          RETURN 1;
        END
        $$
        """,
        """
        CREATE FUNCTION nexa_decimal_adjusted_exponent(encoded text) RETURNS numeric
        LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
        DECLARE digits text;
        BEGIN
          IF NOT nexa_decimal_is_valid(encoded) THEN
            RAISE EXCEPTION 'invalid exact decimal encoding'
              USING ERRCODE = '23514';
          END IF;
          digits := split_part(encoded, ':', 2);
          IF digits = '0' THEN
            RETURN 0;
          END IF;
          RETURN split_part(encoded, ':', 3)::numeric + length(digits) - 1;
        END
        $$
        """,
        """
        CREATE FUNCTION nexa_decimal_normalized_significand(encoded text) RETURNS text
        LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
        DECLARE digits text;
        DECLARE normalized text;
        BEGIN
          IF NOT nexa_decimal_is_valid(encoded) THEN
            RAISE EXCEPTION 'invalid exact decimal encoding'
              USING ERRCODE = '23514';
          END IF;
          digits := split_part(encoded, ':', 2);
          IF digits = '0' THEN
            RETURN '0';
          END IF;
          normalized := rtrim(digits, '0');
          RETURN normalized;
        END
        $$
        """,
        """
        CREATE FUNCTION nexa_decimal_compare(left_value text, right_value text) RETURNS integer
        LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
        DECLARE left_sign integer;
        DECLARE right_sign integer;
        DECLARE left_zero boolean;
        DECLARE right_zero boolean;
        DECLARE left_exponent numeric;
        DECLARE right_exponent numeric;
        DECLARE left_significand text;
        DECLARE right_significand text;
        DECLARE magnitude_comparison integer;
        BEGIN
          IF NOT nexa_decimal_is_valid(left_value)
             OR NOT nexa_decimal_is_valid(right_value) THEN
            RAISE EXCEPTION 'invalid exact decimal encoding'
              USING ERRCODE = '23514';
          END IF;
          left_sign := split_part(left_value, ':', 1)::integer;
          right_sign := split_part(right_value, ':', 1)::integer;
          left_zero := split_part(left_value, ':', 2) = '0';
          right_zero := split_part(right_value, ':', 2) = '0';
          IF left_zero AND right_zero THEN
            RETURN 0;
          END IF;
          IF left_zero THEN
            IF right_sign = 1 THEN
              RETURN 1;
            END IF;
            RETURN -1;
          END IF;
          IF right_zero THEN
            IF left_sign = 1 THEN
              RETURN -1;
            END IF;
            RETURN 1;
          END IF;
          IF left_sign <> right_sign THEN
            IF left_sign = 1 THEN
              RETURN -1;
            END IF;
            RETURN 1;
          END IF;

          left_exponent := nexa_decimal_adjusted_exponent(left_value);
          right_exponent := nexa_decimal_adjusted_exponent(right_value);
          left_significand := nexa_decimal_normalized_significand(left_value);
          right_significand := nexa_decimal_normalized_significand(right_value);
          IF left_exponent < right_exponent THEN
            magnitude_comparison := -1;
          ELSIF left_exponent > right_exponent THEN
            magnitude_comparison := 1;
          ELSIF left_significand COLLATE "C" < right_significand COLLATE "C" THEN
            magnitude_comparison := -1;
          ELSIF left_significand COLLATE "C" > right_significand COLLATE "C" THEN
            magnitude_comparison := 1;
          ELSE
            magnitude_comparison := 0;
          END IF;
          IF left_sign = 1 THEN
            RETURN -magnitude_comparison;
          END IF;
          RETURN magnitude_comparison;
        END
        $$
        """,
    ]
    for statement in statements:
        op.execute(statement)


def _drop_decimal_functions() -> None:
    for signature in (
        "nexa_decimal_compare(text, text)",
        "nexa_decimal_normalized_significand(text)",
        "nexa_decimal_adjusted_exponent(text)",
        "nexa_decimal_zero_rank(text)",
        "nexa_decimal_is_positive(text)",
        "nexa_decimal_is_nonnegative(text)",
        "nexa_decimal_is_valid(text)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature} CASCADE")


def upgrade() -> None:
    _create_decimal_functions()
    for table in metadata.sorted_tables:
        op.execute(_sql(CreateTable(table)))
    for constraint in _alter_constraints():
        op.execute(_sql(AddConstraint(constraint)))
    for table in metadata.sorted_tables:
        for index in sorted(table.indexes, key=lambda item: item.name or ""):
            op.execute(_sql(CreateIndex(index)))

    op.execute(
        "INSERT INTO nexa_schema_metadata "
        "(singleton_key, schema_generation, contract_version) "
        "VALUES ('nexa', 1, '1.0.0-b01')"
    )
    _create_constraint_functions_and_triggers()


def downgrade() -> None:
    _drop_constraint_functions()
    for table in reversed(metadata.sorted_tables):
        op.execute(f'DROP TABLE IF EXISTS "{table.name}" CASCADE')
    _drop_decimal_functions()


def _create_constraint_functions_and_triggers() -> None:
    statements = [
        """
        CREATE FUNCTION nexa_check_membership_set() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE affected_tenant_id uuid;
        DECLARE prior_tenant_id uuid;
        BEGIN
          affected_tenant_id := CASE
            WHEN TG_OP = 'DELETE' THEN OLD.tenant_id
            ELSE NEW.tenant_id
          END;
          prior_tenant_id := CASE WHEN TG_OP = 'UPDATE' THEN OLD.tenant_id ELSE NULL END;
          IF NOT EXISTS (
            SELECT 1 FROM membership_sets WHERE tenant_id = affected_tenant_id
          ) AND EXISTS (
            SELECT 1 FROM tenants WHERE tenant_id = affected_tenant_id
          ) THEN
            RAISE EXCEPTION 'tenant must have a membership set'
              USING ERRCODE = '23514';
          END IF;
          IF prior_tenant_id IS NOT NULL AND prior_tenant_id <> affected_tenant_id
             AND EXISTS (SELECT 1 FROM tenants WHERE tenant_id = prior_tenant_id)
             AND NOT EXISTS (
               SELECT 1 FROM membership_sets WHERE tenant_id = prior_tenant_id
             ) THEN
            RAISE EXCEPTION 'tenant must have a membership set'
              USING ERRCODE = '23514';
          END IF;
          IF TG_OP = 'DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        END
        $$
        """,
        """
        CREATE CONSTRAINT TRIGGER trg_tenants_membership_set
        AFTER INSERT OR UPDATE ON tenants
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION nexa_check_membership_set()
        """,
        """
        CREATE CONSTRAINT TRIGGER trg_membership_sets_tenant
        AFTER INSERT OR UPDATE OR DELETE ON membership_sets
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION nexa_check_membership_set()
        """,
        """
        CREATE FUNCTION nexa_check_job_complete() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM job_specs WHERE job_id = NEW.job_id)
             OR NOT EXISTS (SELECT 1 FROM logical_sessions WHERE job_id = NEW.job_id) THEN
            RAISE EXCEPTION 'job must have exactly one spec and logical session'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END
        $$
        """,
        """
        CREATE CONSTRAINT TRIGGER trg_jobs_complete
        AFTER INSERT OR UPDATE ON jobs
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION nexa_check_job_complete()
        """,
        """
        CREATE FUNCTION nexa_check_job_children_complete() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE affected_job_id uuid;
        DECLARE prior_job_id uuid;
        BEGIN
          affected_job_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.job_id ELSE NEW.job_id END;
          prior_job_id := CASE WHEN TG_OP = 'UPDATE' THEN OLD.job_id ELSE NULL END;
          IF EXISTS (SELECT 1 FROM jobs WHERE job_id = affected_job_id)
             AND (
               NOT EXISTS (SELECT 1 FROM job_specs WHERE job_id = affected_job_id)
               OR NOT EXISTS (SELECT 1 FROM logical_sessions WHERE job_id = affected_job_id)
             ) THEN
            RAISE EXCEPTION 'job must have exactly one spec and logical session'
              USING ERRCODE = '23514';
          END IF;
          IF prior_job_id IS NOT NULL AND prior_job_id <> affected_job_id
             AND EXISTS (SELECT 1 FROM jobs WHERE job_id = prior_job_id)
             AND (
               NOT EXISTS (SELECT 1 FROM job_specs WHERE job_id = prior_job_id)
               OR NOT EXISTS (SELECT 1 FROM logical_sessions WHERE job_id = prior_job_id)
             ) THEN
            RAISE EXCEPTION 'job must have exactly one spec and logical session'
              USING ERRCODE = '23514';
          END IF;
          IF TG_OP = 'DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        END
        $$
        """,
        """
        CREATE CONSTRAINT TRIGGER trg_job_specs_complete
        AFTER INSERT OR UPDATE OR DELETE ON job_specs
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION nexa_check_job_children_complete()
        """,
        """
        CREATE CONSTRAINT TRIGGER trg_logical_sessions_complete
        AFTER INSERT OR UPDATE OR DELETE ON logical_sessions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION nexa_check_job_children_complete()
        """,
        """
        CREATE FUNCTION nexa_reject_immutable_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME
            USING ERRCODE = '23514';
        END
        $$
        """,
        """
        CREATE TRIGGER trg_job_specs_immutable
        BEFORE UPDATE OR DELETE ON job_specs
        FOR EACH ROW EXECUTE FUNCTION nexa_reject_immutable_mutation()
        """,
        """
        CREATE FUNCTION nexa_remove_artifact_reference_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' OR (
            NEW.artifact_id IS DISTINCT FROM OLD.artifact_id
            OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
            OR NEW.kind IS DISTINCT FROM OLD.kind
            OR NEW.media_type IS DISTINCT FROM OLD.media_type
            OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
            OR NEW.checksum IS DISTINCT FROM OLD.checksum
            OR NEW.blob_key IS DISTINCT FROM OLD.blob_key
            OR NEW.state IS DISTINCT FROM OLD.state
          ) THEN
            DELETE FROM artifact_reference_guards
            WHERE tenant_id = OLD.tenant_id AND artifact_id = OLD.artifact_id;
          END IF;
          IF TG_OP = 'DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER trg_artifacts_sync_reference_guard_before
        BEFORE UPDATE OR DELETE ON artifacts
        FOR EACH ROW EXECUTE FUNCTION nexa_remove_artifact_reference_guard()
        """,
        """
        CREATE FUNCTION nexa_create_artifact_reference_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.state = 'COMMITTED' THEN
            INSERT INTO artifact_reference_guards (tenant_id, artifact_id)
            VALUES (NEW.tenant_id, NEW.artifact_id)
            ON CONFLICT (tenant_id, artifact_id) DO NOTHING;
          END IF;
          RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER trg_artifacts_sync_reference_guard_after
        AFTER INSERT OR UPDATE ON artifacts
        FOR EACH ROW EXECUTE FUNCTION nexa_create_artifact_reference_guard()
        """,
        """
        CREATE FUNCTION nexa_validate_job_spec_artifacts() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF (
            NEW.input_artifact_id IS NOT NULL
            AND NOT EXISTS (
              SELECT 1 FROM artifacts
              WHERE artifact_id = NEW.input_artifact_id
                AND tenant_id = NEW.tenant_id
                AND state = 'COMMITTED'
            )
          ) OR (
            NEW.model_artifact_id IS NOT NULL
            AND NOT EXISTS (
              SELECT 1 FROM artifacts
              WHERE artifact_id = NEW.model_artifact_id
                AND tenant_id = NEW.tenant_id
                AND state = 'COMMITTED'
            )
          ) THEN
            RAISE EXCEPTION 'job spec artifacts must be committed'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END
        $$
        """,
        """
        CREATE CONSTRAINT TRIGGER trg_job_specs_committed_artifacts
        AFTER INSERT OR UPDATE ON job_specs
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION nexa_validate_job_spec_artifacts()
        """,
        """
        CREATE FUNCTION nexa_validate_committed_artifact_reference() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE referenced_artifact_id uuid;
        DECLARE expected_checksum text;
        DECLARE artifact_state text;
        DECLARE artifact_checksum text;
        BEGIN
          CASE TG_TABLE_NAME
            WHEN 'recognized_chunks' THEN
              referenced_artifact_id := NEW.artifact_id;
              expected_checksum := NEW.checksum;
            WHEN 'log_segments' THEN
              referenced_artifact_id := NEW.artifact_id;
              expected_checksum := NEW.checksum;
            WHEN 'artifact_references' THEN
              referenced_artifact_id := NEW.artifact_id;
              expected_checksum := NULL;
            ELSE
              RAISE EXCEPTION 'unsupported artifact reference table'
                USING ERRCODE = '23514';
          END CASE;
          SELECT state, checksum INTO artifact_state, artifact_checksum
          FROM artifacts
          WHERE artifact_id = referenced_artifact_id AND tenant_id = NEW.tenant_id;
          IF artifact_state IS DISTINCT FROM 'COMMITTED'
             OR (
               expected_checksum IS NOT NULL
               AND artifact_checksum IS DISTINCT FROM expected_checksum
             ) THEN
            RAISE EXCEPTION 'artifact references require committed artifacts with exact checksum'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER trg_artifact_references_committed_artifact
        BEFORE INSERT OR UPDATE ON artifact_references
        FOR EACH ROW EXECUTE FUNCTION nexa_validate_committed_artifact_reference()
        """,
        """
        CREATE TRIGGER trg_recognized_chunks_committed_artifact
        BEFORE INSERT OR UPDATE ON recognized_chunks
        FOR EACH ROW EXECUTE FUNCTION nexa_validate_committed_artifact_reference()
        """,
        """
        CREATE TRIGGER trg_log_segments_committed_artifact
        BEFORE INSERT OR UPDATE ON log_segments
        FOR EACH ROW EXECUTE FUNCTION nexa_validate_committed_artifact_reference()
        """,
        """
        CREATE FUNCTION nexa_protect_referenced_artifact() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE artifact_is_referenced boolean;
        BEGIN
          SELECT
            EXISTS (
              SELECT 1 FROM job_specs
              WHERE tenant_id = OLD.tenant_id
                AND (
                  input_artifact_id = OLD.artifact_id
                  OR model_artifact_id = OLD.artifact_id
                )
            )
            OR EXISTS (
              SELECT 1 FROM checkpoints
              WHERE tenant_id = OLD.tenant_id
                AND manifest_artifact_id = OLD.artifact_id
            )
            OR EXISTS (
              SELECT 1 FROM results
              WHERE tenant_id = OLD.tenant_id
                AND manifest_artifact_id = OLD.artifact_id
            )
            OR EXISTS (
              SELECT 1 FROM recognized_chunks
              WHERE tenant_id = OLD.tenant_id
                AND artifact_id = OLD.artifact_id
            )
            OR EXISTS (
              SELECT 1 FROM log_segments
              WHERE tenant_id = OLD.tenant_id
                AND artifact_id = OLD.artifact_id
            )
            OR EXISTS (
              SELECT 1 FROM artifact_references
              WHERE tenant_id = OLD.tenant_id
                AND artifact_id = OLD.artifact_id
            )
          INTO artifact_is_referenced;
          IF artifact_is_referenced AND (
            TG_OP = 'DELETE'
            OR NEW.artifact_id IS DISTINCT FROM OLD.artifact_id
            OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
            OR NEW.kind IS DISTINCT FROM OLD.kind
            OR NEW.media_type IS DISTINCT FROM OLD.media_type
            OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
            OR NEW.checksum IS DISTINCT FROM OLD.checksum
            OR NEW.blob_key IS DISTINCT FROM OLD.blob_key
            OR NEW.state IS DISTINCT FROM OLD.state
          ) THEN
            RAISE EXCEPTION 'referenced artifact identity is immutable'
              USING ERRCODE = '23514';
          END IF;
          IF TG_OP = 'DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER trg_artifacts_referenced_immutable
        BEFORE UPDATE OR DELETE ON artifacts
        FOR EACH ROW EXECUTE FUNCTION nexa_protect_referenced_artifact()
        """,
        """
        CREATE TRIGGER trg_checkpoints_immutable
        BEFORE UPDATE OR DELETE ON checkpoints
        FOR EACH ROW EXECUTE FUNCTION nexa_reject_immutable_mutation()
        """,
        """
        CREATE TRIGGER trg_results_immutable
        BEFORE UPDATE OR DELETE ON results
        FOR EACH ROW EXECUTE FUNCTION nexa_reject_immutable_mutation()
        """,
        """
        CREATE TRIGGER trg_recognized_chunks_immutable
        BEFORE UPDATE OR DELETE ON recognized_chunks
        FOR EACH ROW EXECUTE FUNCTION nexa_reject_immutable_mutation()
        """,
        """
        CREATE TRIGGER trg_log_segments_immutable
        BEFORE UPDATE OR DELETE ON log_segments
        FOR EACH ROW EXECUTE FUNCTION nexa_reject_immutable_mutation()
        """,
        """
        CREATE TRIGGER trg_events_append_only
        BEFORE UPDATE OR DELETE ON events
        FOR EACH ROW EXECUTE FUNCTION nexa_reject_immutable_mutation()
        """,
        """
        CREATE TRIGGER trg_audit_records_append_only
        BEFORE UPDATE OR DELETE ON audit_records
        FOR EACH ROW EXECUTE FUNCTION nexa_reject_immutable_mutation()
        """,
        """
        CREATE FUNCTION nexa_protect_terminal_job() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.job_fence < OLD.job_fence THEN
            RAISE EXCEPTION 'job fence cannot decrease'
              USING ERRCODE = '23514';
          END IF;
          IF OLD.state IN ('SUCCEEDED', 'FAILED', 'CANCELLED') AND (
            NEW.state IS DISTINCT FROM OLD.state OR
            NEW.desired_state IS DISTINCT FROM OLD.desired_state OR
            NEW.recovery_intent IS DISTINCT FROM OLD.recovery_intent OR
            NEW.job_fence IS DISTINCT FROM OLD.job_fence OR
            NEW.retry_count IS DISTINCT FROM OLD.retry_count OR
            NEW.retry_of_job_id IS DISTINCT FROM OLD.retry_of_job_id
          ) THEN
            RAISE EXCEPTION 'terminal job authority and state are immutable'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER trg_jobs_terminal_immutable
        BEFORE UPDATE ON jobs
        FOR EACH ROW EXECUTE FUNCTION nexa_protect_terminal_job()
        """,
        """
        CREATE FUNCTION nexa_protect_referenced_template_version() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM job_specs
            WHERE template_id = OLD.template_id AND template_version = OLD.version
          ) THEN
            RAISE EXCEPTION 'referenced template versions are immutable'
              USING ERRCODE = '23514';
          END IF;
          RETURN COALESCE(NEW, OLD);
        END
        $$
        """,
        """
        CREATE TRIGGER trg_template_versions_referenced_immutable
        BEFORE UPDATE OR DELETE ON template_versions
        FOR EACH ROW EXECUTE FUNCTION nexa_protect_referenced_template_version()
        """,
        """
        CREATE FUNCTION nexa_validate_authority_lineage() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE predecessor attempt_authority_grants%ROWTYPE;
        BEGIN
          IF TG_OP = 'UPDATE' THEN
            IF NEW.grant_id <> OLD.grant_id
               OR NEW.tenant_id <> OLD.tenant_id
               OR NEW.job_id <> OLD.job_id
               OR NEW.attempt_id <> OLD.attempt_id
               OR NEW.allocation_id <> OLD.allocation_id
               OR NEW.lease_id <> OLD.lease_id
               OR NEW.worker_id <> OLD.worker_id
               OR NEW.worker_incarnation_id <> OLD.worker_incarnation_id
               OR NEW.job_fence <> OLD.job_fence
               OR NEW.predecessor_grant_id IS DISTINCT FROM OLD.predecessor_grant_id
               OR NEW.callback_id <> OLD.callback_id
               OR NEW.granted_at <> OLD.granted_at
               OR (OLD.ended_at IS NOT NULL AND NEW.ended_at IS DISTINCT FROM OLD.ended_at) THEN
              RAISE EXCEPTION 'authority grant identity and history are immutable'
                USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
          END IF;
          IF NEW.predecessor_grant_id IS NULL THEN
            IF EXISTS (
              SELECT 1 FROM attempt_authority_grants
              WHERE attempt_id = NEW.attempt_id
            ) THEN
              RAISE EXCEPTION 'non-initial authority grant requires a predecessor'
                USING ERRCODE = '23514';
            END IF;
          ELSE
            SELECT * INTO predecessor FROM attempt_authority_grants
            WHERE grant_id = NEW.predecessor_grant_id;
            IF NOT FOUND OR predecessor.attempt_id <> NEW.attempt_id
               OR predecessor.job_id <> NEW.job_id
               OR predecessor.tenant_id <> NEW.tenant_id
               OR predecessor.allocation_id <> NEW.allocation_id
               OR predecessor.lease_id <> NEW.lease_id
               OR predecessor.job_fence <> NEW.job_fence
               OR predecessor.ended_at IS NULL
               OR predecessor.worker_incarnation_id = NEW.worker_incarnation_id THEN
              RAISE EXCEPTION 'invalid authority predecessor lineage'
                USING ERRCODE = '23514';
            END IF;
          END IF;
          RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER trg_attempt_authority_lineage
        BEFORE INSERT OR UPDATE ON attempt_authority_grants
        FOR EACH ROW EXECUTE FUNCTION nexa_validate_authority_lineage()
        """,
        """
        CREATE FUNCTION nexa_validate_allocation_gpu_claims() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE target_allocation allocations%ROWTYPE;
        DECLARE allocation_id_to_check uuid;
        DECLARE allocation_ids uuid[];
        DECLARE active_claim_count integer;
        DECLARE mismatched_claim_count integer;
        BEGIN
          IF TG_TABLE_NAME = 'allocation_gpu_claims' THEN
            allocation_ids := ARRAY[
              CASE WHEN TG_OP = 'DELETE' THEN OLD.allocation_id ELSE NEW.allocation_id END,
              CASE WHEN TG_OP = 'UPDATE' THEN OLD.allocation_id ELSE NULL END
            ];
          ELSE
            allocation_ids := ARRAY[NEW.allocation_id];
          END IF;
          FOREACH allocation_id_to_check IN ARRAY allocation_ids LOOP
            CONTINUE WHEN allocation_id_to_check IS NULL;
            SELECT * INTO target_allocation FROM allocations
            WHERE allocation_id = allocation_id_to_check;
            SELECT count(*), count(*) FILTER (WHERE worker_id <> target_allocation.worker_id)
              INTO active_claim_count, mismatched_claim_count
            FROM allocation_gpu_claims
            WHERE allocation_id = target_allocation.allocation_id AND released_at IS NULL;
            IF mismatched_claim_count <> 0 THEN
              RAISE EXCEPTION 'GPU claim worker does not match allocation worker'
                USING ERRCODE = '23514';
            END IF;
            IF target_allocation.state = 'RELEASED' AND active_claim_count <> 0 THEN
              RAISE EXCEPTION 'released allocation cannot retain active GPU claims'
                USING ERRCODE = '23514';
            END IF;
            IF target_allocation.state <> 'RELEASED'
               AND active_claim_count <> target_allocation.gpu_count THEN
              RAISE EXCEPTION 'unreleased allocation GPU claim count must equal gpu_count'
                USING ERRCODE = '23514';
            END IF;
          END LOOP;
          IF TG_OP = 'DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        END
        $$
        """,
        """
        CREATE CONSTRAINT TRIGGER trg_allocations_gpu_claims
        AFTER INSERT OR UPDATE ON allocations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION nexa_validate_allocation_gpu_claims()
        """,
        """
        CREATE CONSTRAINT TRIGGER trg_allocation_gpu_claims_consistency
        AFTER INSERT OR UPDATE OR DELETE ON allocation_gpu_claims
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION nexa_validate_allocation_gpu_claims()
        """,
        """
        CREATE FUNCTION nexa_validate_artifact_reference_owner() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE owner_exists boolean;
        BEGIN
          CASE NEW.owner_type
            WHEN 'JOB_SPEC' THEN
              SELECT EXISTS (
                SELECT 1 FROM job_specs
                WHERE job_id = NEW.owner_id AND tenant_id = NEW.tenant_id
              ) INTO owner_exists;
            WHEN 'CHECKPOINT' THEN
              SELECT EXISTS (
                SELECT 1 FROM checkpoints
                WHERE checkpoint_id = NEW.owner_id AND tenant_id = NEW.tenant_id
              ) INTO owner_exists;
            WHEN 'RESULT' THEN
              SELECT EXISTS (
                SELECT 1 FROM results
                WHERE result_id = NEW.owner_id AND tenant_id = NEW.tenant_id
              ) INTO owner_exists;
            WHEN 'RECOGNIZED_CHUNK' THEN
              SELECT EXISTS (
                SELECT 1 FROM recognized_chunks
                WHERE recognized_chunk_id = NEW.owner_id AND tenant_id = NEW.tenant_id
              ) INTO owner_exists;
            WHEN 'LOG_SEGMENT' THEN
              SELECT EXISTS (
                SELECT 1 FROM log_segments
                WHERE log_segment_id = NEW.owner_id AND tenant_id = NEW.tenant_id
              ) INTO owner_exists;
            WHEN 'UPLOAD_SESSION' THEN
              SELECT EXISTS (
                SELECT 1 FROM upload_sessions
                WHERE upload_id = NEW.owner_id AND tenant_id = NEW.tenant_id
              ) INTO owner_exists;
            ELSE
              owner_exists := false;
          END CASE;
          IF NOT owner_exists THEN
            RAISE EXCEPTION 'artifact reference owner does not exist in tenant'
              USING ERRCODE = '23503';
          END IF;
          RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER trg_artifact_references_owner
        BEFORE INSERT OR UPDATE ON artifact_references
        FOR EACH ROW EXECUTE FUNCTION nexa_validate_artifact_reference_owner()
        """,
        """
        CREATE FUNCTION nexa_protect_referenced_upload_owner() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE owner_is_referenced boolean;
        BEGIN
          SELECT EXISTS (
            SELECT 1 FROM artifact_references
            WHERE owner_type = 'UPLOAD_SESSION'
              AND owner_id = OLD.upload_id
              AND tenant_id = OLD.tenant_id
          ) INTO owner_is_referenced;
          IF owner_is_referenced AND (
            TG_OP = 'DELETE'
            OR NEW.upload_id IS DISTINCT FROM OLD.upload_id
            OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
          ) THEN
            RAISE EXCEPTION 'referenced artifact owner identity is immutable'
              USING ERRCODE = '23514';
          END IF;
          IF TG_OP = 'DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER trg_upload_sessions_referenced_owner
        BEFORE UPDATE OR DELETE ON upload_sessions
        FOR EACH ROW EXECUTE FUNCTION nexa_protect_referenced_upload_owner()
        """,
        """
        CREATE FUNCTION nexa_validate_recognized_record() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE reservation_state text;
        DECLARE artifact_state text;
        DECLARE artifact_checksum text;
        BEGIN
          IF TG_TABLE_NAME = 'checkpoints' THEN
            SELECT state INTO reservation_state FROM checkpoint_reservations
            WHERE checkpoint_id = NEW.checkpoint_id;
          ELSE
            SELECT state INTO reservation_state FROM result_reservations
            WHERE result_id = NEW.result_id;
          END IF;
          SELECT state, checksum INTO artifact_state, artifact_checksum FROM artifacts
          WHERE artifact_id = NEW.manifest_artifact_id AND tenant_id = NEW.tenant_id;
          IF reservation_state <> 'COMMITTED'
             OR artifact_state <> 'COMMITTED'
             OR artifact_checksum IS DISTINCT FROM NEW.manifest_checksum THEN
            RAISE EXCEPTION 'recognized metadata requires committed reservation and artifact'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END
        $$
        """,
        """
        CREATE CONSTRAINT TRIGGER trg_checkpoints_committed_sources
        AFTER INSERT ON checkpoints
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION nexa_validate_recognized_record()
        """,
        """
        CREATE CONSTRAINT TRIGGER trg_results_committed_sources
        AFTER INSERT ON results
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION nexa_validate_recognized_record()
        """,
        """
        CREATE FUNCTION nexa_validate_recognized_reservation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE reservation_state text;
        DECLARE recognized_record_exists boolean;
        BEGIN
          IF TG_TABLE_NAME = 'checkpoint_reservations' THEN
            SELECT state INTO reservation_state FROM checkpoint_reservations
            WHERE checkpoint_id = NEW.checkpoint_id;
            SELECT EXISTS (
              SELECT 1 FROM checkpoints WHERE checkpoint_id = NEW.checkpoint_id
            ) INTO recognized_record_exists;
          ELSE
            SELECT state INTO reservation_state FROM result_reservations
            WHERE result_id = NEW.result_id;
            SELECT EXISTS (
              SELECT 1 FROM results WHERE result_id = NEW.result_id
            ) INTO recognized_record_exists;
          END IF;
          IF recognized_record_exists AND reservation_state <> 'COMMITTED' THEN
            RAISE EXCEPTION 'recognized metadata requires committed reservation'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END
        $$
        """,
        """
        CREATE CONSTRAINT TRIGGER trg_checkpoint_reservations_recognized
        AFTER UPDATE ON checkpoint_reservations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION nexa_validate_recognized_reservation()
        """,
        """
        CREATE CONSTRAINT TRIGGER trg_result_reservations_recognized
        AFTER UPDATE ON result_reservations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION nexa_validate_recognized_reservation()
        """,
    ]
    for statement in statements:
        op.execute(statement)


def _drop_constraint_functions() -> None:
    for name in (
        "nexa_create_artifact_reference_guard",
        "nexa_remove_artifact_reference_guard",
        "nexa_protect_referenced_artifact",
        "nexa_validate_committed_artifact_reference",
        "nexa_validate_job_spec_artifacts",
        "nexa_protect_referenced_upload_owner",
        "nexa_validate_recognized_reservation",
        "nexa_validate_recognized_record",
        "nexa_validate_artifact_reference_owner",
        "nexa_validate_allocation_gpu_claims",
        "nexa_validate_authority_lineage",
        "nexa_protect_referenced_template_version",
        "nexa_protect_terminal_job",
        "nexa_reject_immutable_mutation",
        "nexa_check_job_children_complete",
        "nexa_check_job_complete",
        "nexa_check_membership_set",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {name}() CASCADE")
