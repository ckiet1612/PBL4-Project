import { describe, expect, it } from "vitest";
import {
  ADMIN_STATE_LABELS,
  ALLOCATION_STATE_LABELS,
  HEALTH_LABELS,
  auditActionLabel,
  recoveryEventLabel,
} from "./labels";

describe("admin labels", () => {
  it("cover every worker and allocation enum", () => {
    expect(Object.keys(HEALTH_LABELS).sort()).toEqual(["READY", "STARTING", "SUSPECT", "UNAVAILABLE"]);
    expect(Object.keys(ADMIN_STATE_LABELS).sort()).toEqual(["DISABLED", "DRAINING", "ENABLED"]);
    expect(Object.keys(ALLOCATION_STATE_LABELS).sort()).toEqual(["HELD", "QUARANTINED", "RELEASED"]);
  });

  it("translate known recovery events and keep unknown ones raw", () => {
    expect(recoveryEventLabel("ATTEMPT_FENCED")).toBe("Lần chạy bị thu hồi quyền (fence)");
    expect(recoveryEventLabel("CHECKPOINT_FALLBACK_TO_INPUT")).toBe("Không dùng được checkpoint, chạy lại từ đầu vào");
    expect(recoveryEventLabel("SOMETHING_NEW")).toBe("SOMETHING_NEW");
  });

  it("translate known audit actions and keep unknown ones raw", () => {
    expect(auditActionLabel("admin.worker.drain")).toBe("Ngừng nhận job trên worker");
    expect(auditActionLabel("auth.login")).toBe("Đăng nhập");
    expect(auditActionLabel("admin.something.new")).toBe("admin.something.new");
  });
});
