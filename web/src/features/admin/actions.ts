// Worker action matrix and transition text (docs/web-ui.md A6). The UI only shows or hides;
// the server decides, and "done" texts appear only when the API data shows the condition.
import type { AllocationPage, ResourceCapacityVector, Worker, WorkerAdminState, WorkerHealth } from "../../api/types";

export type WorkerAction = "drain" | "disable" | "enable";

export interface WorkerActionVisibility {
  drain: boolean;
  disable: boolean;
  enable: boolean;
}

/** Only admin_state decides; health and quarantine are checked by the server (409 with its reason). */
export function workerActions(
  adminState: WorkerAdminState,
  _health?: WorkerHealth,
  _quarantined?: number,
): WorkerActionVisibility {
  return {
    drain: adminState === "ENABLED",
    disable: adminState !== "DISABLED",
    enable: adminState !== "ENABLED",
  };
}

export const WORKER_ACTION_LABELS: Record<WorkerAction, string> = {
  drain: "Ngừng nhận job",
  disable: "Tắt worker",
  enable: "Bật lại",
};

export const WORKER_ACTION_CONSEQUENCES: Record<WorkerAction, string> = {
  drain: "Worker ngừng nhận job mới. Job đang chạy tiếp tục tới khi kết thúc. Không dừng job nào. Hoàn tác bằng Bật lại.",
  disable:
    "Worker ngừng nhận job mới. Mọi lần chạy đang có quyền bị thu hồi (fence) và được yêu cầu dừng. Tài nguyên giữ ở trạng thái QUARANTINED, vẫn tính vào hạn mức, cho tới khi worker xác nhận dọn dẹp. Job sẽ được khôi phục ở lần chạy mới theo checkpoint nếu có.",
  enable: "Chỉ bật được khi worker có heartbeat mới đạt READY, đã reconcile và không còn phân bổ QUARANTINED.",
};

/** Allocations of one worker on the first page (≤ 100) of one state. */
export interface AllocationSummary extends ResourceCapacityVector {
  count: number;
  /** The server has a next page: counts and sums are lower bounds. */
  incomplete: boolean;
}

export function summarizeAllocations(page: AllocationPage, workerId: string): AllocationSummary {
  const summary: AllocationSummary = { count: 0, cpu_millis: 0, memory_bytes: 0, gpu_count: 0, incomplete: false };
  for (const allocation of page.items) {
    if (allocation.worker_id !== workerId) continue;
    summary.count += 1;
    summary.cpu_millis += allocation.resources.cpu_millis;
    summary.memory_bytes += allocation.resources.memory_bytes;
    summary.gpu_count += allocation.resources.gpu_count;
  }
  summary.incomplete = page.page.next_cursor !== null;
  return summary;
}

export interface CapacityRows {
  allocatable: ResourceCapacityVector | null;
  held: ResourceCapacityVector;
  /** Client-side estimate; the scheduler uses the server's own data. */
  free: ResourceCapacityVector | null;
  incomplete: boolean;
}

export function capacityRows(
  allocatable: ResourceCapacityVector | null,
  held: AllocationSummary,
  quarantined: AllocationSummary,
): CapacityRows {
  const sum: ResourceCapacityVector = {
    cpu_millis: held.cpu_millis + quarantined.cpu_millis,
    memory_bytes: held.memory_bytes + quarantined.memory_bytes,
    gpu_count: held.gpu_count + quarantined.gpu_count,
  };
  const free =
    allocatable === null
      ? null
      : {
          cpu_millis: Math.max(0, allocatable.cpu_millis - sum.cpu_millis),
          memory_bytes: Math.max(0, allocatable.memory_bytes - sum.memory_bytes),
          gpu_count: Math.max(0, allocatable.gpu_count - sum.gpu_count),
        };
  return { allocatable, held: sum, free, incomplete: held.incomplete || quarantined.incomplete };
}

function countText(summary: AllocationSummary): string {
  return summary.incomplete ? `ít nhất ${summary.count}` : String(summary.count);
}

export function enableBlockedNote(adminState: WorkerAdminState, quarantined: AllocationSummary): string | null {
  if (adminState !== "DISABLED" || quarantined.count === 0) return null;
  return `Còn ${countText(quarantined)} phân bổ chờ worker xác nhận dọn dẹp; máy chủ sẽ từ chối bật lại cho tới khi dọn xong`;
}

export interface TransitionStatus {
  /** idle: opened on a settled worker, nothing to wait for. */
  phase: "idle" | "waiting" | "done";
  text: string | null;
}

/**
 * What the worker is waiting for, from the latest worker + first HELD/QUARANTINED pages.
 * `lastAction` is the action sent from this page, if any; admin_state from the server wins.
 * `allocationsAfterAction` is false while the HELD/QUARANTINED pages are the ones read before that
 * action: they cannot show that drain/disable finished (a job may have been placed meanwhile).
 */
export function transitionStatus(
  worker: Pick<Worker, "admin_state" | "health">,
  held: AllocationSummary,
  quarantined: AllocationSummary,
  lastAction: WorkerAction | null,
  allocationsAfterAction = true,
): TransitionStatus {
  const settled = (text: string): TransitionStatus => (lastAction === null ? { phase: "idle", text: null } : { phase: "done", text });
  const settledByAllocations = (text: string): TransitionStatus =>
    lastAction !== null && !allocationsAfterAction
      ? { phase: "waiting", text: "Đang kiểm tra lại phân bổ sau thao tác" }
      : settled(text);
  switch (worker.admin_state) {
    case "DRAINING":
      if (held.count > 0 || held.incomplete) {
        return { phase: "waiting", text: `Đang chờ ${countText(held)} job đang chạy kết thúc` };
      }
      return settledByAllocations("Đã ngừng nhận job; không còn job đang chạy");
    case "DISABLED":
      if (quarantined.count > 0 || quarantined.incomplete || held.count > 0 || held.incomplete) {
        return {
          phase: "waiting",
          text: `Đang chờ worker xác nhận dọn dẹp (còn ${countText(quarantined)} phân bổ QUARANTINED)`,
        };
      }
      return settledByAllocations("Worker đã xác nhận dọn dẹp xong");
    case "ENABLED":
      if (worker.health !== "READY") {
        return {
          phase: "waiting",
          text: lastAction === "enable" ? "Đã bật; đang chờ worker báo READY" : "Đang chờ worker báo READY",
        };
      }
      return settled("Worker sẵn sàng nhận job");
  }
}
