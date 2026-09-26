"""Record exact queue eligibility boundaries without rewriting a tenant queue."""

from alembic import op
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20260925_0013"
down_revision = "20260925_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "queue_eligibility_events",
        Column("event_id", BigInteger, primary_key=True, autoincrement=True),
        Column(
            "tenant_id",
            UUID(as_uuid=True),
            ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("occurred_at", DateTime(timezone=True), nullable=False),
        Column("old_state", JSONB, nullable=False),
        Column("new_state", JSONB, nullable=False),
        Column("last_job_id", UUID(as_uuid=True)),
        Column("last_processed_at", DateTime(timezone=True)),
        Column("completed_at", DateTime(timezone=True)),
    )
    op.create_index(
        "ix_queue_eligibility_pending",
        "queue_eligibility_events",
        ["tenant_id", "event_id"],
        postgresql_where="completed_at IS NULL",
    )
    op.create_index("ix_job_specs_b13_cpu", "job_specs", ["tenant_id", "cpu_millis", "job_id"])
    op.create_index("ix_job_specs_b13_memory", "job_specs", ["tenant_id", "memory_bytes", "job_id"])
    op.execute("""
        CREATE FUNCTION nexa_b13_eligibility_state(
            p_cpu bigint, p_memory bigint, p_gpu integer,
            p_held_cpu bigint, p_held_memory bigint, p_held_gpu bigint,
            p_inventory jsonb
        ) RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
            SELECT jsonb_build_object(
                'cpu', greatest(0, least(p_cpu - p_held_cpu,
                    coalesce((p_inventory->>'allocatable_cpu_millis')::bigint, 0))),
                'memory', greatest(0, least(p_memory - p_held_memory,
                    coalesce((p_inventory->>'allocatable_memory_bytes')::bigint, 0))),
                'gpu', greatest(0, least(p_gpu - p_held_gpu,
                    coalesce((p_inventory->>'allocatable_gpu_count')::bigint, 0))),
                'inventory', p_inventory
            )
        $$;

        CREATE FUNCTION nexa_b13_emit_eligibility(
            p_tenant uuid, p_old jsonb, p_new jsonb
        ) RETURNS void LANGUAGE plpgsql AS $$
        DECLARE capabilities_changed boolean;
        BEGIN
            IF p_old = p_new THEN RETURN; END IF;
            capabilities_changed :=
                p_old->'inventory'->'architecture' IS DISTINCT FROM
                    p_new->'inventory'->'architecture'
                OR p_old->'inventory'->'workload_capabilities' IS DISTINCT FROM
                    p_new->'inventory'->'workload_capabilities';
            IF capabilities_changed THEN
                IF NOT EXISTS (SELECT 1 FROM jobs WHERE tenant_id = p_tenant
                               AND state = 'QUEUED' LIMIT 1) THEN RETURN; END IF;
            ELSIF NOT EXISTS (
                SELECT 1 FROM job_specs sp JOIN jobs j ON j.job_id = sp.job_id
                WHERE sp.tenant_id = p_tenant AND j.state = 'QUEUED'
                  AND j.desired_state = 'RUNNING' AND j.recovery_intent IS NULL
                  AND (
                    (sp.cpu_millis > least((p_old->>'cpu')::bigint, (p_new->>'cpu')::bigint)
                     AND sp.cpu_millis <= greatest(
                         (p_old->>'cpu')::bigint, (p_new->>'cpu')::bigint))
                    OR (sp.memory_bytes > least(
                         (p_old->>'memory')::bigint, (p_new->>'memory')::bigint)
                     AND sp.memory_bytes <= greatest(
                         (p_old->>'memory')::bigint, (p_new->>'memory')::bigint))
                    OR (sp.gpu_count > least((p_old->>'gpu')::integer, (p_new->>'gpu')::integer)
                     AND sp.gpu_count <= greatest(
                         (p_old->>'gpu')::integer, (p_new->>'gpu')::integer))
                  ) LIMIT 1
            ) THEN RETURN; END IF;
            INSERT INTO queue_eligibility_events
                (tenant_id, occurred_at, old_state, new_state)
            VALUES (p_tenant, clock_timestamp(), p_old, p_new);
        END $$;

        CREATE FUNCTION nexa_b13_policy_eligibility() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE previous tenant_policies%ROWTYPE;
        DECLARE inventory jsonb;
        DECLARE hc bigint; hm bigint; hg bigint;
        BEGIN
            IF NOT NEW.is_current THEN RETURN NEW; END IF;
            IF TG_OP = 'INSERT' THEN
                SELECT * INTO previous FROM tenant_policies
                WHERE tenant_id = NEW.tenant_id AND version < NEW.version
                ORDER BY version DESC LIMIT 1;
                IF NOT FOUND THEN RETURN NEW; END IF;
            ELSE
                IF NOT OLD.is_current THEN RETURN NEW; END IF;
                previous := OLD;
            END IF;
            SELECT coalesce(sum(cpu_millis), 0), coalesce(sum(memory_bytes), 0),
                   coalesce(sum(gpu_count), 0) INTO hc, hm, hg
            FROM allocations WHERE tenant_id = NEW.tenant_id AND state <> 'RELEASED';
            SELECT to_jsonb(i) INTO inventory FROM workers w JOIN worker_inventories i
              ON i.worker_id = w.worker_id
             AND i.inventory_version = w.current_inventory_version LIMIT 1;
            PERFORM nexa_b13_emit_eligibility(
                NEW.tenant_id,
                nexa_b13_eligibility_state(previous.cpu_limit_millis,
                    previous.memory_limit_bytes, previous.gpu_limit, hc, hm, hg, inventory),
                nexa_b13_eligibility_state(NEW.cpu_limit_millis,
                    NEW.memory_limit_bytes, NEW.gpu_limit, hc, hm, hg, inventory));
            RETURN NEW;
        END $$;
        CREATE TRIGGER b13_policy_eligibility_insert AFTER INSERT ON tenant_policies
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_policy_eligibility();
        CREATE TRIGGER b13_policy_eligibility_update
        AFTER UPDATE OF cpu_limit_millis, memory_limit_bytes, gpu_limit, is_current
        ON tenant_policies FOR EACH ROW EXECUTE FUNCTION nexa_b13_policy_eligibility();

        CREATE FUNCTION nexa_b13_allocation_eligibility() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE policy tenant_policies%ROWTYPE;
        DECLARE inventory jsonb;
        DECLARE hc bigint; hm bigint; hg bigint;
        DECLARE oc bigint := 0; om bigint := 0; og bigint := 0;
        DECLARE nc bigint := 0; nm bigint := 0; ng bigint := 0;
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.state <> 'RELEASED' THEN
                oc := OLD.cpu_millis; om := OLD.memory_bytes; og := OLD.gpu_count;
            END IF;
            IF NEW.state <> 'RELEASED' THEN
                nc := NEW.cpu_millis; nm := NEW.memory_bytes; ng := NEW.gpu_count;
            END IF;
            IF oc = nc AND om = nm AND og = ng THEN RETURN NEW; END IF;
            SELECT * INTO policy FROM tenant_policies
            WHERE tenant_id = NEW.tenant_id AND is_current;
            IF NOT FOUND THEN RETURN NEW; END IF;
            SELECT coalesce(sum(cpu_millis), 0), coalesce(sum(memory_bytes), 0),
                   coalesce(sum(gpu_count), 0) INTO hc, hm, hg
            FROM allocations WHERE tenant_id = NEW.tenant_id AND state <> 'RELEASED';
            SELECT to_jsonb(i) INTO inventory FROM workers w JOIN worker_inventories i
              ON i.worker_id = w.worker_id
             AND i.inventory_version = w.current_inventory_version LIMIT 1;
            PERFORM nexa_b13_emit_eligibility(
                NEW.tenant_id,
                nexa_b13_eligibility_state(policy.cpu_limit_millis,
                    policy.memory_limit_bytes, policy.gpu_limit,
                    hc - nc + oc, hm - nm + om, hg - ng + og, inventory),
                nexa_b13_eligibility_state(policy.cpu_limit_millis,
                    policy.memory_limit_bytes, policy.gpu_limit, hc, hm, hg, inventory));
            RETURN NEW;
        END $$;
        CREATE TRIGGER b13_allocation_eligibility_insert AFTER INSERT ON allocations
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_allocation_eligibility();
        CREATE TRIGGER b13_allocation_eligibility_update
        AFTER UPDATE OF state, cpu_millis, memory_bytes, gpu_count ON allocations
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_allocation_eligibility();

        CREATE FUNCTION nexa_b13_inventory_eligibility() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE old_inventory jsonb; new_inventory jsonb;
        DECLARE policy tenant_policies%ROWTYPE;
        DECLARE hc bigint; hm bigint; hg bigint;
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
                SELECT coalesce(sum(cpu_millis), 0), coalesce(sum(memory_bytes), 0),
                       coalesce(sum(gpu_count), 0) INTO hc, hm, hg
                FROM allocations WHERE tenant_id = policy.tenant_id AND state <> 'RELEASED';
                PERFORM nexa_b13_emit_eligibility(
                    policy.tenant_id,
                    nexa_b13_eligibility_state(policy.cpu_limit_millis,
                        policy.memory_limit_bytes, policy.gpu_limit,
                        hc, hm, hg, old_inventory),
                    nexa_b13_eligibility_state(policy.cpu_limit_millis,
                        policy.memory_limit_bytes, policy.gpu_limit,
                        hc, hm, hg, new_inventory));
            END LOOP;
            RETURN NEW;
        END $$;
        CREATE TRIGGER b13_inventory_pointer AFTER UPDATE OF current_inventory_version ON workers
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_inventory_eligibility();
        CREATE TRIGGER b13_inventory_attributes
        AFTER UPDATE OF allocatable_cpu_millis, allocatable_memory_bytes,
                        allocatable_gpu_count, architecture, workload_capabilities
        ON worker_inventories FOR EACH ROW EXECUTE FUNCTION nexa_b13_inventory_eligibility();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER b13_inventory_attributes ON worker_inventories")
    op.execute("DROP TRIGGER b13_inventory_pointer ON workers")
    op.execute("DROP FUNCTION nexa_b13_inventory_eligibility()")
    op.execute("DROP TRIGGER b13_allocation_eligibility_update ON allocations")
    op.execute("DROP TRIGGER b13_allocation_eligibility_insert ON allocations")
    op.execute("DROP FUNCTION nexa_b13_allocation_eligibility()")
    op.execute("DROP TRIGGER b13_policy_eligibility_update ON tenant_policies")
    op.execute("DROP TRIGGER b13_policy_eligibility_insert ON tenant_policies")
    op.execute("DROP FUNCTION nexa_b13_policy_eligibility()")
    op.execute("DROP FUNCTION nexa_b13_emit_eligibility(uuid, jsonb, jsonb)")
    op.execute(
        "DROP FUNCTION nexa_b13_eligibility_state("
        "bigint, bigint, integer, bigint, bigint, bigint, jsonb)"
    )
    op.drop_index("ix_job_specs_b13_memory", table_name="job_specs")
    op.drop_index("ix_job_specs_b13_cpu", table_name="job_specs")
    op.drop_index("ix_queue_eligibility_pending", table_name="queue_eligibility_events")
    op.drop_table("queue_eligibility_events")
