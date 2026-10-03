// Worker health and admin state: text plus tone, never colour alone.
import type { WorkerAdminState, WorkerHealth } from "../../../api/types";
import { ADMIN_STATE_LABELS, ADMIN_STATE_TONE, HEALTH_LABELS, HEALTH_TONE } from "../labels";

export function HealthBadge({ health }: { health: WorkerHealth }) {
  return <span className={`badge tone-${HEALTH_TONE[health]}`}>{HEALTH_LABELS[health]}</span>;
}

export function AdminStateBadge({ state }: { state: WorkerAdminState }) {
  return <span className={`badge tone-${ADMIN_STATE_TONE[state]}`}>{ADMIN_STATE_LABELS[state]}</span>;
}
