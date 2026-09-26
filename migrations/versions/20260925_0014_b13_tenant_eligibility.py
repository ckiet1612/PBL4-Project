"""Record tenant enable boundaries and rebuild preexisting queue eligibility."""

from alembic import op

revision = "20260925_0014"
down_revision = "20260925_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION nexa_b13_tenant_eligibility() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE policy tenant_policies%ROWTYPE;
        DECLARE inventory jsonb;
        DECLARE hc bigint; hm bigint; hg bigint;
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
            SELECT coalesce(sum(cpu_millis), 0), coalesce(sum(memory_bytes), 0),
                   coalesce(sum(gpu_count), 0) INTO hc, hm, hg
            FROM allocations WHERE tenant_id = NEW.tenant_id AND state <> 'RELEASED';
            SELECT to_jsonb(i) INTO inventory FROM workers w JOIN worker_inventories i
              ON i.worker_id = w.worker_id
             AND i.inventory_version = w.current_inventory_version LIMIT 1;
            current_state := nexa_b13_eligibility_state(policy.cpu_limit_millis,
                policy.memory_limit_bytes, policy.gpu_limit, hc, hm, hg, inventory);
            INSERT INTO queue_eligibility_events
                (tenant_id, occurred_at, old_state, new_state)
            VALUES (NEW.tenant_id, boundary,
                current_state || jsonb_build_object('enabled', OLD.enabled),
                current_state || jsonb_build_object('enabled', NEW.enabled));
            RETURN NEW;
        END $$;
        CREATE TRIGGER b13_tenant_eligibility AFTER UPDATE OF enabled ON tenants
        FOR EACH ROW EXECUTE FUNCTION nexa_b13_tenant_eligibility();

        INSERT INTO queue_eligibility_events
            (tenant_id, occurred_at, old_state, new_state)
        SELECT p.tenant_id, clock_timestamp(),
               jsonb_build_object('rebuild_current_state', true),
               nexa_b13_eligibility_state(p.cpu_limit_millis,
                   p.memory_limit_bytes, p.gpu_limit,
                   coalesce(held.cpu, 0)::bigint, coalesce(held.memory, 0)::bigint,
                   coalesce(held.gpu, 0)::bigint, inventory.current_inventory)
                   || jsonb_build_object('enabled', t.enabled)
        FROM tenant_policies p JOIN tenants t ON t.tenant_id = p.tenant_id
        LEFT JOIN LATERAL (
            SELECT sum(a.cpu_millis) AS cpu, sum(a.memory_bytes) AS memory,
                   sum(a.gpu_count) AS gpu
            FROM allocations a WHERE a.tenant_id = p.tenant_id AND a.state <> 'RELEASED'
        ) held ON true
        LEFT JOIN LATERAL (
            SELECT to_jsonb(i) AS current_inventory
            FROM workers w JOIN worker_inventories i ON i.worker_id = w.worker_id
              AND i.inventory_version = w.current_inventory_version LIMIT 1
        ) inventory ON true
        WHERE p.is_current AND EXISTS (
            SELECT 1 FROM jobs j WHERE j.tenant_id = p.tenant_id
              AND j.state = 'QUEUED' LIMIT 1
        );
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER b13_tenant_eligibility ON tenants")
    op.execute("DROP FUNCTION nexa_b13_tenant_eligibility()")
