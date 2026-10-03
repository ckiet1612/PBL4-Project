import { describe, expect, it } from "vitest";
import type { Worker } from "../../api/types";
import type { AllocationSummary } from "./actions";
import { attentionItems } from "./overview";

const NONE: AllocationSummary = { count: 0, cpu_millis: 0, memory_bytes: 0, gpu_count: 0, incomplete: false };

function worker(id: string, health: Worker["health"], admin_state: Worker["admin_state"]): Worker {
  return {
    worker_id: id,
    current_incarnation_id: null,
    health,
    admin_state,
    inventory: null,
    version: 1,
    last_heartbeat_at: null,
    ready_at: null,
  };
}

const ID = "01900000-0000-7000-8000-0000000000aa";

describe("overview attention list", () => {
  it("is empty for a healthy system", () => {
    expect(
      attentionItems({
        mode: "NORMAL",
        workers: [worker(ID, "READY", "ENABLED")],
        workersIncomplete: false,
        quarantined: NONE,
        recoveryEvents: 0,
        recoveryIncomplete: false,
      }),
    ).toEqual([]);
  });

  it("lists every condition that needs an admin, with where to go", () => {
    const items = attentionItems({
      mode: "ADMISSION_OFF",
      workers: [worker(ID, "SUSPECT", "DRAINING")],
      workersIncomplete: false,
      quarantined: { ...NONE, count: 2 },
      recoveryEvents: 10,
      recoveryIncomplete: true,
    });
    expect(items).toEqual([
      { text: "Hệ thống đang ở chế độ Ngừng nhận job", to: "/admin/policy" },
      { text: "Worker …000000aa: Nghi ngờ (mất heartbeat)", to: `/admin/workers/${ID}` },
      { text: "Worker …000000aa: Ngừng nhận job", to: `/admin/workers/${ID}` },
      { text: "Có 2 phân bổ QUARANTINED chờ worker xác nhận dọn dẹp", to: `/admin/workers/${ID}` },
      { text: "Có ít nhất 10 sự kiện khôi phục trong 24 giờ qua", to: "/admin/recovery" },
    ]);
  });

  it("says when there is no worker yet", () => {
    expect(
      attentionItems({
        mode: "NORMAL",
        workers: [],
        workersIncomplete: false,
        quarantined: NONE,
        recoveryEvents: 0,
        recoveryIncomplete: false,
      }),
    ).toEqual([{ text: "Chưa có worker nào đăng ký; khởi động worker cục bộ", to: "/admin/workers" }]);
  });
});
