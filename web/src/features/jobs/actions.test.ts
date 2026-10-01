import { describe, expect, it } from "vitest";
import type { DesiredState, JobState } from "../../api/types";
import { availableActions, canControl, defaultTab, isTerminal, pendingNote } from "./actions";

const job = (state: JobState, desired_state: DesiredState = "RUNNING") => ({ state, desired_state });

describe("action matrix (docs/web-ui.md §8, state-machines.md)", () => {
  const rows: [JobState, DesiredState, boolean, "hidden" | "enabled", boolean, boolean][] = [
    ["QUEUED", "RUNNING", true, "hidden", false, false],
    ["DISPATCHING", "RUNNING", true, "hidden", false, false],
    ["RUNNING", "RUNNING", true, "enabled", false, false],
    ["PAUSING", "PAUSED", true, "hidden", false, false],
    ["PAUSED", "PAUSED", true, "hidden", true, false],
    ["RECOVERING", "RUNNING", true, "hidden", false, false],
    ["RECOVERING", "PAUSED", true, "hidden", false, false],
    ["RETRY_WAIT", "RUNNING", true, "hidden", false, false],
    ["RUNNING", "CANCELLED", false, "hidden", false, false],
    ["PAUSED", "CANCELLED", false, "hidden", false, false],
    ["CANCELLING", "CANCELLED", false, "hidden", false, false],
    ["SUCCEEDED", "RUNNING", false, "hidden", false, false],
    ["CANCELLED", "CANCELLED", false, "hidden", false, false],
    ["FAILED", "RUNNING", false, "hidden", false, true],
  ];

  it.each(rows)("%s / %s", (state, desired, cancel, pause, resume, retry) => {
    expect(availableActions(job(state, desired), true)).toEqual({ cancel, pause, resume, retry });
  });

  it("disables pause with a reason when the template cannot checkpoint", () => {
    expect(availableActions(job("RUNNING"), false).pause).toBe("disabled");
    expect(availableActions(job("QUEUED"), false).pause).toBe("hidden");
  });

  it("covers every state", () => {
    const states: JobState[] = [
      "QUEUED", "DISPATCHING", "RUNNING", "PAUSING", "PAUSED", "RECOVERING",
      "RETRY_WAIT", "CANCELLING", "SUCCEEDED", "FAILED", "CANCELLED",
    ];
    expect(states.filter(isTerminal)).toEqual(["SUCCEEDED", "FAILED", "CANCELLED"]);
  });
});

describe("who may control", () => {
  const session = { user_id: "u1" };
  it("owner or tenant admin only", () => {
    expect(canControl({ user_id: "u1" }, session, "MEMBER")).toBe(true);
    expect(canControl({ user_id: "u2" }, session, "MEMBER")).toBe(false);
    expect(canControl({ user_id: "u2" }, session, "TENANT_ADMIN")).toBe(true);
  });
});

describe("pending notes never claim the job stopped", () => {
  it("explains requested but unconfirmed transitions", () => {
    expect(pendingNote(job("RUNNING", "CANCELLED"))).toBe("Đã yêu cầu hủy, chờ hệ thống xác nhận dừng");
    expect(pendingNote(job("CANCELLING", "CANCELLED"))).toBe("Đã yêu cầu hủy, chờ hệ thống xác nhận dừng");
    expect(pendingNote(job("PAUSING", "PAUSED"))).toBe("Đã yêu cầu tạm dừng, chờ checkpoint");
    expect(pendingNote(job("RECOVERING", "RUNNING"))).toMatch(/khôi phục/);
    expect(pendingNote(job("CANCELLED", "CANCELLED"))).toBeNull();
    expect(pendingNote(job("RUNNING"))).toBeNull();
  });
});

describe("default tab", () => {
  it("opens results for succeeded jobs", () => {
    expect(defaultTab("SUCCEEDED")).toBe("result");
    expect(defaultTab("RUNNING")).toBe("progress");
    expect(defaultTab("FAILED")).toBe("progress");
  });
});
