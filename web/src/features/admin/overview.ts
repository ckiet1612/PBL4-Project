// "Cần chú ý" on /admin: only conditions visible in the five overview reads (docs/web-ui.md A3).
import type { OperationalMode, Worker } from "../../api/types";
import { shortId } from "../../components/format";
import type { AllocationSummary } from "./actions";
import { ADMIN_STATE_LABELS, HEALTH_LABELS } from "./labels";
import { MODE_LABELS } from "./modes";

export interface AttentionItem {
  text: string;
  to: string;
}

export interface OverviewFacts {
  mode: OperationalMode;
  workers: Worker[];
  workersIncomplete: boolean;
  /** First QUARANTINED page, all workers. */
  quarantined: AllocationSummary;
  recoveryEvents: number;
  recoveryIncomplete: boolean;
}

function atLeast(count: number, incomplete: boolean): string {
  return incomplete ? `ít nhất ${count}` : String(count);
}

export function attentionItems(facts: OverviewFacts): AttentionItem[] {
  const items: AttentionItem[] = [];
  if (facts.mode !== "NORMAL") {
    items.push({ text: `Hệ thống đang ở chế độ ${MODE_LABELS[facts.mode]}`, to: "/admin/policy" });
  }
  if (facts.workers.length === 0 && !facts.workersIncomplete) {
    items.push({ text: "Chưa có worker nào đăng ký; khởi động worker cục bộ", to: "/admin/workers" });
  }
  for (const worker of facts.workers) {
    const to = `/admin/workers/${worker.worker_id}`;
    if (worker.health !== "READY") {
      items.push({ text: `Worker ${shortId(worker.worker_id)}: ${HEALTH_LABELS[worker.health]}`, to });
    }
    if (worker.admin_state !== "ENABLED") {
      items.push({ text: `Worker ${shortId(worker.worker_id)}: ${ADMIN_STATE_LABELS[worker.admin_state]}`, to });
    }
  }
  if (facts.quarantined.count > 0) {
    // Single-node: with one worker the allocations are its own.
    const only = facts.workers.length === 1 ? facts.workers[0] : null;
    items.push({
      text: `Có ${atLeast(facts.quarantined.count, facts.quarantined.incomplete)} phân bổ QUARANTINED chờ worker xác nhận dọn dẹp`,
      to: only ? `/admin/workers/${only.worker_id}` : "/admin/workers",
    });
  }
  if (facts.recoveryEvents > 0) {
    items.push({
      text: `Có ${atLeast(facts.recoveryEvents, facts.recoveryIncomplete)} sự kiện khôi phục trong 24 giờ qua`,
      to: "/admin/recovery",
    });
  }
  return items;
}
