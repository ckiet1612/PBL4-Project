import { describe, expect, it } from "vitest";
import type { Allocation, AllocationPage, Worker, WorkerAdminState, WorkerHealth } from "../../api/types";
import {
  capacityRows,
  enableBlockedNote,
  summarizeAllocations,
  transitionStatus,
  workerActions,
  type AllocationSummary,
} from "./actions";

const WORKER = "0190a000-0000-7000-8000-000000000001";
const OTHER = "0190a000-0000-7000-8000-000000000002";

function allocation(worker_id: string, cpu_millis: number, memory_bytes: number, gpu_count = 0): Allocation {
  return {
    allocation_id: `a-${cpu_millis}-${memory_bytes}`,
    tenant_id: "t",
    job_id: "j",
    attempt_id: "at",
    worker_id,
    resources: { cpu_millis, memory_bytes, gpu_count },
    gpu_uuids: [],
    state: "HELD",
    held_at: "2026-10-01T00:00:00Z",
    quarantined_at: null,
    released_at: null,
  } as Allocation;
}

function page(items: Allocation[], next_cursor: string | null = null): AllocationPage {
  return { items, page: { next_cursor, page_size: 100 } } as AllocationPage;
}

const none: AllocationSummary = { count: 0, cpu_millis: 0, memory_bytes: 0, gpu_count: 0, incomplete: false };
const some = (count: number, incomplete = false): AllocationSummary => ({ ...none, count, incomplete });

function worker(admin_state: WorkerAdminState, health: WorkerHealth): Pick<Worker, "admin_state" | "health"> {
  return { admin_state, health };
}

describe("worker action matrix (docs/web-ui.md A6)", () => {
  const healths: WorkerHealth[] = ["STARTING", "READY", "SUSPECT", "UNAVAILABLE"];
  const rows: [WorkerAdminState, { drain: boolean; disable: boolean; enable: boolean }][] = [
    ["ENABLED", { drain: true, disable: true, enable: false }],
    ["DRAINING", { drain: false, disable: true, enable: true }],
    ["DISABLED", { drain: false, disable: false, enable: true }],
  ];
  it.each(rows)("%s", (adminState, expected) => {
    // Only admin_state decides what is shown; health and quarantine are the server's call.
    for (const health of healths) {
      for (const quarantined of [0, 3]) {
        expect(workerActions(adminState, health, quarantined)).toEqual(expected);
      }
    }
  });

  it("explains why the server will refuse enable while quarantined allocations remain", () => {
    expect(enableBlockedNote("DISABLED", some(2))).toBe(
      "Còn 2 phân bổ chờ worker xác nhận dọn dẹp; máy chủ sẽ từ chối bật lại cho tới khi dọn xong",
    );
    expect(enableBlockedNote("DISABLED", some(100, true))).toContain("Còn ít nhất 100 phân bổ");
    expect(enableBlockedNote("DISABLED", none)).toBeNull();
    expect(enableBlockedNote("DRAINING", some(2))).toBeNull();
  });
});

describe("allocation summary", () => {
  it("sums the first page for this worker and flags a next page as incomplete", () => {
    const summary = summarizeAllocations(
      page([allocation(WORKER, 1500, 1024, 0), allocation(WORKER, 500, 2048, 1), allocation(OTHER, 9000, 9)], "next-cursor-0000"),
      WORKER,
    );
    expect(summary).toEqual({ count: 2, cpu_millis: 2000, memory_bytes: 3072, gpu_count: 1, incomplete: true });
    expect(summarizeAllocations(page([]), WORKER)).toEqual(none);
  });

  it("computes the client-side free estimate and never shows negatives", () => {
    const rows = capacityRows(
      { cpu_millis: 4000, memory_bytes: 8 * 2 ** 30, gpu_count: 0 },
      { ...none, count: 1, cpu_millis: 1000, memory_bytes: 2 ** 30, gpu_count: 0 },
      { ...none, count: 1, cpu_millis: 4000, memory_bytes: 2 ** 30, gpu_count: 0, incomplete: true },
    );
    expect(rows.held).toEqual({ cpu_millis: 5000, memory_bytes: 2 * 2 ** 30, gpu_count: 0 });
    expect(rows.free).toEqual({ cpu_millis: 0, memory_bytes: 6 * 2 ** 30, gpu_count: 0 });
    expect(rows.incomplete).toBe(true);
    expect(capacityRows(null, none, none).free).toBeNull();
  });
});

describe("transition status: only what the API shows", () => {
  it("drain waits for HELD allocations to go away", () => {
    expect(transitionStatus(worker("DRAINING", "READY"), some(2), none, "drain")).toEqual({
      phase: "waiting",
      text: "Đang chờ 2 job đang chạy kết thúc",
    });
    expect(transitionStatus(worker("DRAINING", "READY"), some(0, true), none, "drain").phase).toBe("waiting");
    expect(transitionStatus(worker("DRAINING", "READY"), none, none, "drain")).toEqual({
      phase: "done",
      text: "Đã ngừng nhận job; không còn job đang chạy",
    });
  });

  it("disable waits for the worker to confirm cleanup", () => {
    expect(transitionStatus(worker("DISABLED", "READY"), none, some(3), "disable")).toEqual({
      phase: "waiting",
      text: "Đang chờ worker xác nhận dọn dẹp (còn 3 phân bổ QUARANTINED)",
    });
    expect(transitionStatus(worker("DISABLED", "READY"), some(1), none, "disable").phase).toBe("waiting");
    expect(transitionStatus(worker("DISABLED", "UNAVAILABLE"), none, none, "disable")).toEqual({
      phase: "done",
      text: "Worker đã xác nhận dọn dẹp xong",
    });
  });

  it("lists read before the action never prove drain or disable finished", () => {
    const checking = { phase: "waiting", text: "Đang kiểm tra lại phân bổ sau thao tác" };
    expect(transitionStatus(worker("DRAINING", "READY"), none, none, "drain", false)).toEqual(checking);
    expect(transitionStatus(worker("DISABLED", "UNAVAILABLE"), none, none, "disable", false)).toEqual(checking);
    expect(transitionStatus(worker("DRAINING", "READY"), some(2), none, "drain", false)).toEqual({
      phase: "waiting",
      text: "Đang chờ 2 job đang chạy kết thúc",
    });
    expect(transitionStatus(worker("ENABLED", "READY"), none, none, "enable", false).phase).toBe("done");
  });

  it("enable waits for READY", () => {
    expect(transitionStatus(worker("ENABLED", "STARTING"), none, none, "enable")).toEqual({
      phase: "waiting",
      text: "Đã bật; đang chờ worker báo READY",
    });
    expect(transitionStatus(worker("ENABLED", "SUSPECT"), none, none, null)).toEqual({
      phase: "waiting",
      text: "Đang chờ worker báo READY",
    });
    expect(transitionStatus(worker("ENABLED", "READY"), some(4), none, "enable")).toEqual({
      phase: "done",
      text: "Worker sẵn sàng nhận job",
    });
  });

  it("is idle when the page opens on a settled worker", () => {
    expect(transitionStatus(worker("ENABLED", "READY"), some(4), none, null).phase).toBe("idle");
    expect(transitionStatus(worker("DRAINING", "READY"), none, none, null).phase).toBe("idle");
    expect(transitionStatus(worker("DISABLED", "UNAVAILABLE"), none, none, null).phase).toBe("idle");
    expect(transitionStatus(worker("DRAINING", "READY"), some(1), none, null).phase).toBe("waiting");
    expect(transitionStatus(worker("DISABLED", "READY"), none, some(1), null).phase).toBe("waiting");
  });

  it("follows the server's admin_state, not the action that was sent", () => {
    // A drain whose reply raced with someone else's disable: report what the worker is now.
    expect(transitionStatus(worker("DISABLED", "READY"), none, some(1), "drain").text).toContain("dọn dẹp");
  });
});
