"""Track held-quota headroom per tenant instead of rewriting queued job ages."""

from alembic import op
from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID

revision = "20260925_0017"
down_revision = "20260925_0016"
branch_labels = None
depends_on = None

_QUEUED = "state = 'QUEUED' AND desired_state = 'RUNNING' AND recovery_intent IS NULL"


def upgrade() -> None:
    op.create_table(
        "quota_headroom_steps",
        Column(
            "tenant_id",
            UUID(as_uuid=True),
            ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        Column("resource", Text, primary_key=True),
        Column("upper_bound", BigInteger, primary_key=True),
        Column("resumed_at", DateTime(timezone=True)),
        CheckConstraint(
            "resource IN ('cpu', 'memory')", name=op.f("ck_quota_headroom_steps_resource")
        ),
        CheckConstraint("upper_bound >= 0", name=op.f("ck_quota_headroom_steps_upper_bound")),
    )
    op.create_table(
        "queue_request_sizes",
        Column(
            "tenant_id",
            UUID(as_uuid=True),
            ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        Column("cpu_millis", BigInteger, primary_key=True),
        Column("memory_bytes", BigInteger, primary_key=True),
        Column("queued_jobs", Integer, nullable=False),
        CheckConstraint("queued_jobs > 0", name=op.f("ck_queue_request_sizes_queued_jobs")),
    )
    op.execute(f"""
        DROP TRIGGER b13_tenant_eligibility ON tenants;
        DROP TRIGGER b13_inventory_attributes ON worker_inventories;
        DROP TRIGGER b13_inventory_pointer ON workers;
        DROP TRIGGER b13_allocation_eligibility_update ON allocations;
        DROP TRIGGER b13_allocation_eligibility_insert ON allocations;
        DROP TRIGGER b13_policy_eligibility_update ON tenant_policies;
        DROP TRIGGER b13_policy_eligibility_insert ON tenant_policies;

        -- For sizes in (previous upper_bound, upper_bound], resumed_at is the
        -- last time held allocations stopped blocking them; NULL means never
        -- blocked since tracking began. The highest bound is limit - held.
        CREATE FUNCTION nexa_b13_quota_step(
            p_tenant uuid, p_resource text, p_headroom bigint, p_at timestamptz
        ) RETURNS void LANGUAGE plpgsql AS $$
        DECLARE top bigint;
        DECLARE kept timestamptz;
        BEGIN
            SELECT coalesce(max(upper_bound), -1) INTO top FROM quota_headroom_steps
            WHERE tenant_id = p_tenant AND resource = p_resource;
            IF p_headroom = top THEN RETURN; END IF;
            IF p_headroom > top THEN
                INSERT INTO quota_headroom_steps (tenant_id, resource, upper_bound, resumed_at)
                VALUES (p_tenant, p_resource, p_headroom, p_at);
                RETURN;
            END IF;
            SELECT resumed_at INTO kept FROM quota_headroom_steps
            WHERE tenant_id = p_tenant AND resource = p_resource AND upper_bound > p_headroom
            ORDER BY upper_bound LIMIT 1;
            DELETE FROM quota_headroom_steps
            WHERE tenant_id = p_tenant AND resource = p_resource AND upper_bound > p_headroom;
            IF p_headroom >= 0 AND NOT EXISTS (
                SELECT 1 FROM quota_headroom_steps WHERE tenant_id = p_tenant
                  AND resource = p_resource AND upper_bound = p_headroom
            ) THEN
                INSERT INTO quota_headroom_steps (tenant_id, resource, upper_bound, resumed_at)
                VALUES (p_tenant, p_resource, p_headroom, kept);
            END IF;
        END $$;

        CREATE FUNCTION nexa_b13_set_quota_headroom(
            p_tenant uuid, p_cpu_limit bigint, p_memory_limit bigint, p_initial boolean
        ) RETURNS void LANGUAGE plpgsql AS $$
        DECLARE hc bigint; hm bigint;
        DECLARE boundary timestamptz;
        BEGIN
            -- Serialize concurrent allocation changes of one tenant; the sums
            -- below then include every earlier committed change.
            PERFORM pg_advisory_xact_lock(hashtextextended(p_tenant::text, 130017));
            SELECT coalesce(sum(cpu_millis), 0), coalesce(sum(memory_bytes), 0)
            INTO hc, hm FROM allocations
            WHERE tenant_id = p_tenant AND state <> 'RELEASED';
            boundary := clock_timestamp();
            IF p_initial AND NOT EXISTS (
                SELECT 1 FROM quota_headroom_steps WHERE tenant_id = p_tenant
            ) THEN
                boundary := NULL;
            END IF;
            PERFORM nexa_b13_quota_step(p_tenant, 'cpu', p_cpu_limit - hc, boundary);
            PERFORM nexa_b13_quota_step(p_tenant, 'memory', p_memory_limit - hm, boundary);
        END $$;

        CREATE FUNCTION nexa_b13_policy_capacity() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE previous tenant_policies%ROWTYPE;
        DECLARE inventory jsonb;
        BEGIN
            IF NOT NEW.is_current THEN RETURN NEW; END IF;
            IF TG_OP = 'INSERT' THEN
                SELECT * INTO previous FROM tenant_policies
                WHERE tenant_id = NEW.tenant_id AND version < NEW.version
                ORDER BY version DESC LIMIT 1;
                PERFORM nexa_b13_set_quota_headroom(NEW.tenant_id, NEW.cpu_limit_millis,
                    NEW.memory_limit_bytes, NOT FOUND);
                IF previous.tenant_id IS NULL THEN RETURN NEW; END IF;
            ELSE
                PERFORM nexa_b13_set_quota_headroom(NEW.tenant_id, NEW.cpu_limit_millis,
                    NEW.memory_limit_bytes, false);
                IF NOT OLD.is_current THEN RETURN NEW; END IF;
                previous := OLD;
            END IF;
            SELECT to_jsonb(i) INTO inventory FROM workers w JOIN worker_inventories i
              ON i.worker_id = w.worker_id
             AND i.inventory_version = w.current_inventory_version LIMIT 1;
            PERFORM nexa_b13_emit_eligibility(
                NEW.tenant_id,
                nexa_b13_eligibility_state(previous.cpu_limit_millis,
                    previous.memory_limit_bytes, previous.gpu_limit, 0, 0, 0, inventory),
                nexa_b13_eligibility_state(NEW.cpu_limit_millis,
                    NEW.memory_limit_bytes, NEW.gpu_limit, 0, 0, 0, inventory));
            RETURN NEW;
        END $$;
        CREATE TRIGGER b13_policy_capacity_insert AFTER INSERT ON tenant_policies
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_policy_capacity();
        CREATE TRIGGER b13_policy_capacity_update
        AFTER UPDATE OF cpu_limit_millis, memory_limit_bytes, gpu_limit, is_current
        ON tenant_policies FOR EACH ROW EXECUTE FUNCTION nexa_b13_policy_capacity();

        -- Allocation changes move only the per-tenant headroom; queued job
        -- rows and eligibility events are untouched.
        CREATE FUNCTION nexa_b13_allocation_quota() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE policy tenant_policies%ROWTYPE;
        DECLARE oc bigint := 0; om bigint := 0;
        DECLARE nc bigint := 0; nm bigint := 0;
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.state <> 'RELEASED' THEN
                oc := OLD.cpu_millis; om := OLD.memory_bytes;
            END IF;
            IF NEW.state <> 'RELEASED' THEN
                nc := NEW.cpu_millis; nm := NEW.memory_bytes;
            END IF;
            IF oc = nc AND om = nm THEN RETURN NEW; END IF;
            SELECT * INTO policy FROM tenant_policies
            WHERE tenant_id = NEW.tenant_id AND is_current;
            IF NOT FOUND THEN RETURN NEW; END IF;
            PERFORM nexa_b13_set_quota_headroom(NEW.tenant_id, policy.cpu_limit_millis,
                policy.memory_limit_bytes, false);
            RETURN NEW;
        END $$;
        CREATE TRIGGER b13_allocation_quota_insert AFTER INSERT ON allocations
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_allocation_quota();
        CREATE TRIGGER b13_allocation_quota_update
        AFTER UPDATE OF state, cpu_millis, memory_bytes ON allocations
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_allocation_quota();

        CREATE FUNCTION nexa_b13_inventory_capacity() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE old_inventory jsonb; new_inventory jsonb;
        DECLARE policy tenant_policies%ROWTYPE;
        BEGIN
            IF TG_TABLE_NAME = 'workers' THEN
                IF OLD.current_inventory_version IS NOT DISTINCT FROM
                   NEW.current_inventory_version THEN RETURN NEW; END IF;
                SELECT to_jsonb(i) INTO old_inventory FROM worker_inventories i
                WHERE i.worker_id = OLD.worker_id
                  AND i.inventory_version = OLD.current_inventory_version;
                SELECT to_jsonb(i) INTO new_inventory FROM worker_inventories i
                WHERE i.worker_id = NEW.worker_id
                  AND i.inventory_version = NEW.current_inventory_version;
            ELSE
                IF NOT EXISTS (SELECT 1 FROM workers WHERE worker_id = NEW.worker_id
                    AND current_inventory_version = NEW.inventory_version) THEN RETURN NEW; END IF;
                old_inventory := to_jsonb(OLD);
                new_inventory := to_jsonb(NEW);
            END IF;
            FOR policy IN SELECT * FROM tenant_policies WHERE is_current
            LOOP
                PERFORM nexa_b13_emit_eligibility(
                    policy.tenant_id,
                    nexa_b13_eligibility_state(policy.cpu_limit_millis,
                        policy.memory_limit_bytes, policy.gpu_limit, 0, 0, 0, old_inventory),
                    nexa_b13_eligibility_state(policy.cpu_limit_millis,
                        policy.memory_limit_bytes, policy.gpu_limit, 0, 0, 0, new_inventory));
            END LOOP;
            RETURN NEW;
        END $$;
        CREATE TRIGGER b13_inventory_capacity_pointer
        AFTER UPDATE OF current_inventory_version ON workers
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_inventory_capacity();
        CREATE TRIGGER b13_inventory_capacity_attributes
        AFTER UPDATE OF allocatable_cpu_millis, allocatable_memory_bytes,
                        allocatable_gpu_count, architecture, workload_capabilities
        ON worker_inventories FOR EACH ROW EXECUTE FUNCTION nexa_b13_inventory_capacity();

        CREATE FUNCTION nexa_b13_tenant_capacity() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE policy tenant_policies%ROWTYPE;
        DECLARE inventory jsonb;
        DECLARE current_state jsonb;
        DECLARE boundary timestamptz;
        BEGIN
            IF OLD.enabled IS NOT DISTINCT FROM NEW.enabled THEN RETURN NEW; END IF;
            boundary := clock_timestamp();
            IF NEW.enabled THEN
                UPDATE admission_counters SET eligible_resumed_at = boundary
                WHERE scope_type = 'TENANT' AND scope_id = NEW.tenant_id::text;
            ELSE
                UPDATE reservations SET invalidated_at = boundary,
                    invalidation_reason = 'tenant_disabled'
                WHERE tenant_id = NEW.tenant_id AND invalidated_at IS NULL;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM jobs WHERE tenant_id = NEW.tenant_id
                           AND state = 'QUEUED' LIMIT 1) THEN RETURN NEW; END IF;
            SELECT * INTO policy FROM tenant_policies
            WHERE tenant_id = NEW.tenant_id AND is_current;
            SELECT to_jsonb(i) INTO inventory FROM workers w JOIN worker_inventories i
              ON i.worker_id = w.worker_id
             AND i.inventory_version = w.current_inventory_version LIMIT 1;
            current_state := nexa_b13_eligibility_state(policy.cpu_limit_millis,
                policy.memory_limit_bytes, policy.gpu_limit, 0, 0, 0, inventory);
            INSERT INTO queue_eligibility_events
                (tenant_id, occurred_at, old_state, new_state)
            VALUES (NEW.tenant_id, boundary,
                current_state || jsonb_build_object('enabled', OLD.enabled),
                current_state || jsonb_build_object('enabled', NEW.enabled));
            RETURN NEW;
        END $$;
        CREATE TRIGGER b13_tenant_capacity AFTER UPDATE OF enabled ON tenants
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_tenant_capacity();

        -- Count queued CPU jobs by request size so a tick can prove that no
        -- queued size fits the current headroom without walking the queue.
        CREATE FUNCTION nexa_b13_queue_size_delta(p_job uuid, p_delta integer)
        RETURNS void LANGUAGE plpgsql AS $$
        DECLARE spec job_specs%ROWTYPE;
        BEGIN
            SELECT * INTO spec FROM job_specs WHERE job_id = p_job;
            IF NOT FOUND OR spec.gpu_count <> 0 THEN RETURN; END IF;
            IF p_delta > 0 THEN
                INSERT INTO queue_request_sizes
                    (tenant_id, cpu_millis, memory_bytes, queued_jobs)
                VALUES (spec.tenant_id, spec.cpu_millis, spec.memory_bytes, 1)
                ON CONFLICT (tenant_id, cpu_millis, memory_bytes) DO UPDATE
                SET queued_jobs = queue_request_sizes.queued_jobs + 1;
                RETURN;
            END IF;
            UPDATE queue_request_sizes SET queued_jobs = queued_jobs - 1
            WHERE tenant_id = spec.tenant_id AND cpu_millis = spec.cpu_millis
              AND memory_bytes = spec.memory_bytes AND queued_jobs > 1;
            IF FOUND THEN RETURN; END IF;
            DELETE FROM queue_request_sizes
            WHERE tenant_id = spec.tenant_id AND cpu_millis = spec.cpu_millis
              AND memory_bytes = spec.memory_bytes;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'queue_request_sizes is missing a queued job size'
                    USING ERRCODE = 'data_corrupted';
            END IF;
        END $$;

        CREATE FUNCTION nexa_b13_job_queue_size() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE was_queued boolean := false;
        DECLARE is_queued boolean;
        BEGIN
            is_queued := NEW.state = 'QUEUED' AND NEW.desired_state = 'RUNNING'
                AND NEW.recovery_intent IS NULL;
            IF TG_OP = 'UPDATE' THEN
                was_queued := OLD.state = 'QUEUED' AND OLD.desired_state = 'RUNNING'
                    AND OLD.recovery_intent IS NULL;
            END IF;
            IF was_queued AND NOT is_queued THEN
                PERFORM nexa_b13_queue_size_delta(NEW.job_id, -1);
            ELSIF is_queued AND NOT was_queued THEN
                PERFORM nexa_b13_queue_size_delta(NEW.job_id, 1);
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER b13_job_queue_size_insert AFTER INSERT ON jobs
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_job_queue_size();
        CREATE TRIGGER b13_job_queue_size_update
        AFTER UPDATE OF state, desired_state, recovery_intent ON jobs
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_job_queue_size();

        CREATE FUNCTION nexa_b13_spec_queue_size() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM jobs WHERE job_id = NEW.job_id AND {_QUEUED}) THEN
                PERFORM nexa_b13_queue_size_delta(NEW.job_id, 1);
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER b13_spec_queue_size_insert AFTER INSERT ON job_specs
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_spec_queue_size();

        INSERT INTO quota_headroom_steps (tenant_id, resource, upper_bound, resumed_at)
        SELECT p.tenant_id, r.resource, r.headroom, NULL
        FROM tenant_policies p
        CROSS JOIN LATERAL (
            SELECT coalesce(sum(a.cpu_millis), 0)::bigint AS cpu,
                   coalesce(sum(a.memory_bytes), 0)::bigint AS memory
            FROM allocations a WHERE a.tenant_id = p.tenant_id AND a.state <> 'RELEASED'
        ) held
        CROSS JOIN LATERAL (VALUES
            ('cpu', p.cpu_limit_millis - held.cpu),
            ('memory', p.memory_limit_bytes - held.memory)
        ) r (resource, headroom)
        WHERE p.is_current AND r.headroom >= 0;

        INSERT INTO queue_request_sizes (tenant_id, cpu_millis, memory_bytes, queued_jobs)
        SELECT j.tenant_id, sp.cpu_millis, sp.memory_bytes, count(*)
        FROM jobs j JOIN job_specs sp ON sp.job_id = j.job_id
        WHERE j.{_QUEUED.replace(" AND ", " AND j.")} AND sp.gpu_count = 0
        GROUP BY j.tenant_id, sp.cpu_millis, sp.memory_bytes;

        -- Held quota no longer clears eligible_since. Every tenant with pending
        -- events or an unaged queued job gets a trailing capacity-only rebuild,
        -- replayed per job with the exact base-eligibility rules. An unstarted
        -- trailing rebuild is reused so no tenant gains an extra replay.
        UPDATE queue_eligibility_events e
        SET occurred_at = clock_timestamp(),
            new_state = nexa_b13_eligibility_state(p.cpu_limit_millis,
                p.memory_limit_bytes, p.gpu_limit, 0, 0, 0, inventory.current_inventory)
            || jsonb_build_object('enabled', t.enabled)
        FROM tenant_policies p JOIN tenants t ON t.tenant_id = p.tenant_id
        LEFT JOIN LATERAL (
            SELECT to_jsonb(i) AS current_inventory
            FROM workers w JOIN worker_inventories i ON i.worker_id = w.worker_id
              AND i.inventory_version = w.current_inventory_version LIMIT 1
        ) inventory ON true
        WHERE p.is_current AND e.tenant_id = p.tenant_id AND e.completed_at IS NULL
          AND e.last_job_id IS NULL AND e.old_state ? 'rebuild_current_state'
          AND e.event_id = (
              SELECT max(latest.event_id) FROM queue_eligibility_events latest
              WHERE latest.tenant_id = e.tenant_id AND latest.completed_at IS NULL
          );

        INSERT INTO queue_eligibility_events (tenant_id, occurred_at, old_state, new_state)
        SELECT p.tenant_id, clock_timestamp(),
               jsonb_build_object('rebuild_current_state', true),
               nexa_b13_eligibility_state(p.cpu_limit_millis,
                   p.memory_limit_bytes, p.gpu_limit, 0, 0, 0, inventory.current_inventory)
                   || jsonb_build_object('enabled', t.enabled)
        FROM tenant_policies p JOIN tenants t ON t.tenant_id = p.tenant_id
        LEFT JOIN LATERAL (
            SELECT to_jsonb(i) AS current_inventory
            FROM workers w JOIN worker_inventories i ON i.worker_id = w.worker_id
              AND i.inventory_version = w.current_inventory_version LIMIT 1
        ) inventory ON true
        WHERE p.is_current AND (
            EXISTS (
                SELECT 1 FROM queue_eligibility_events pending
                WHERE pending.tenant_id = p.tenant_id AND pending.completed_at IS NULL
            ) OR EXISTS (
                SELECT 1 FROM jobs j WHERE j.tenant_id = p.tenant_id
                  AND j.{_QUEUED.replace(" AND ", " AND j.")} AND j.eligible_since IS NULL
            )
        ) AND NOT EXISTS (
            SELECT 1 FROM queue_eligibility_events latest
            WHERE latest.tenant_id = p.tenant_id AND latest.completed_at IS NULL
              AND latest.last_job_id IS NULL
              AND latest.old_state ? 'rebuild_current_state'
              AND latest.event_id = (
                  SELECT max(newest.event_id) FROM queue_eligibility_events newest
                  WHERE newest.tenant_id = p.tenant_id AND newest.completed_at IS NULL
              )
        );
    """)


def downgrade() -> None:
    # Fold each queued job's quota resume boundary back into eligible_since and
    # clear the age of sizes that held quota blocks, as the per-job model did.
    op.execute(f"""
        UPDATE jobs j SET eligible_since = CASE
            WHEN qc.upper_bound IS NULL OR qm.upper_bound IS NULL THEN NULL
            ELSE greatest(j.eligible_since, qc.resumed_at, qm.resumed_at) END
        FROM job_specs sp
        LEFT JOIN LATERAL (
            SELECT q.upper_bound, q.resumed_at FROM quota_headroom_steps q
            WHERE q.tenant_id = sp.tenant_id AND q.resource = 'cpu'
              AND q.upper_bound >= sp.cpu_millis
            ORDER BY q.upper_bound LIMIT 1
        ) qc ON true
        LEFT JOIN LATERAL (
            SELECT q.upper_bound, q.resumed_at FROM quota_headroom_steps q
            WHERE q.tenant_id = sp.tenant_id AND q.resource = 'memory'
              AND q.upper_bound >= sp.memory_bytes
            ORDER BY q.upper_bound LIMIT 1
        ) qm ON true
        WHERE sp.job_id = j.job_id AND j.{_QUEUED.replace(" AND ", " AND j.")}
          AND j.eligible_since IS NOT NULL
          AND EXISTS (SELECT 1 FROM tenant_policies p
                      WHERE p.tenant_id = j.tenant_id AND p.is_current);

        -- Pending capacity-only events ignore held quota; a trailing rebuild
        -- restores the held-inclusive state the per-job model expects.
        INSERT INTO queue_eligibility_events (tenant_id, occurred_at, old_state, new_state)
        SELECT p.tenant_id, clock_timestamp(),
               jsonb_build_object('rebuild_current_state', true),
               nexa_b13_eligibility_state(p.cpu_limit_millis,
                   p.memory_limit_bytes, p.gpu_limit, held.cpu, held.memory, held.gpu,
                   inventory.current_inventory)
                   || jsonb_build_object('enabled', t.enabled)
        FROM tenant_policies p JOIN tenants t ON t.tenant_id = p.tenant_id
        CROSS JOIN LATERAL (
            SELECT coalesce(sum(a.cpu_millis), 0)::bigint AS cpu,
                   coalesce(sum(a.memory_bytes), 0)::bigint AS memory,
                   coalesce(sum(a.gpu_count), 0)::bigint AS gpu
            FROM allocations a WHERE a.tenant_id = p.tenant_id AND a.state <> 'RELEASED'
        ) held
        LEFT JOIN LATERAL (
            SELECT to_jsonb(i) AS current_inventory
            FROM workers w JOIN worker_inventories i ON i.worker_id = w.worker_id
              AND i.inventory_version = w.current_inventory_version LIMIT 1
        ) inventory ON true
        WHERE p.is_current AND EXISTS (
            SELECT 1 FROM queue_eligibility_events pending
            WHERE pending.tenant_id = p.tenant_id AND pending.completed_at IS NULL
        );

        DROP TRIGGER b13_spec_queue_size_insert ON job_specs;
        DROP FUNCTION nexa_b13_spec_queue_size();
        DROP TRIGGER b13_job_queue_size_update ON jobs;
        DROP TRIGGER b13_job_queue_size_insert ON jobs;
        DROP FUNCTION nexa_b13_job_queue_size();
        DROP FUNCTION nexa_b13_queue_size_delta(uuid, integer);
        DROP TRIGGER b13_tenant_capacity ON tenants;
        DROP FUNCTION nexa_b13_tenant_capacity();
        DROP TRIGGER b13_inventory_capacity_attributes ON worker_inventories;
        DROP TRIGGER b13_inventory_capacity_pointer ON workers;
        DROP FUNCTION nexa_b13_inventory_capacity();
        DROP TRIGGER b13_allocation_quota_update ON allocations;
        DROP TRIGGER b13_allocation_quota_insert ON allocations;
        DROP FUNCTION nexa_b13_allocation_quota();
        DROP TRIGGER b13_policy_capacity_update ON tenant_policies;
        DROP TRIGGER b13_policy_capacity_insert ON tenant_policies;
        DROP FUNCTION nexa_b13_policy_capacity();
        DROP FUNCTION nexa_b13_set_quota_headroom(uuid, bigint, bigint, boolean);
        DROP FUNCTION nexa_b13_quota_step(uuid, text, bigint, timestamptz);

        CREATE TRIGGER b13_policy_eligibility_insert AFTER INSERT ON tenant_policies
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_policy_eligibility();
        CREATE TRIGGER b13_policy_eligibility_update
        AFTER UPDATE OF cpu_limit_millis, memory_limit_bytes, gpu_limit, is_current
        ON tenant_policies FOR EACH ROW EXECUTE FUNCTION nexa_b13_policy_eligibility();
        CREATE TRIGGER b13_allocation_eligibility_insert AFTER INSERT ON allocations
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_allocation_eligibility();
        CREATE TRIGGER b13_allocation_eligibility_update
        AFTER UPDATE OF state, cpu_millis, memory_bytes, gpu_count ON allocations
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_allocation_eligibility();
        CREATE TRIGGER b13_inventory_pointer AFTER UPDATE OF current_inventory_version ON workers
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_inventory_eligibility();
        CREATE TRIGGER b13_inventory_attributes
        AFTER UPDATE OF allocatable_cpu_millis, allocatable_memory_bytes,
                        allocatable_gpu_count, architecture, workload_capabilities
        ON worker_inventories FOR EACH ROW EXECUTE FUNCTION nexa_b13_inventory_eligibility();
        CREATE TRIGGER b13_tenant_eligibility AFTER UPDATE OF enabled ON tenants
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_tenant_eligibility();
    """)
    op.drop_table("queue_request_sizes")
    op.drop_table("quota_headroom_steps")
