import { describe, expect, it } from "vitest";
import {
  ATTEMPT_STATE_LABELS,
  FAILURE_CLASS_LABELS,
  JOB_STATE_LABELS,
  STATE_TONE,
  WAITING_REASON_LABELS,
  waitingReasonLabel,
} from "./labels";

describe("labels", () => {
  it("names all 11 job states and 6 waiting reasons", () => {
    expect(Object.keys(JOB_STATE_LABELS)).toHaveLength(11);
    expect(Object.keys(WAITING_REASON_LABELS)).toHaveLength(6);
    expect(Object.keys(ATTEMPT_STATE_LABELS)).toHaveLength(10);
    expect(Object.keys(FAILURE_CLASS_LABELS)).toHaveLength(7);
  });

  it("never labels a pending transition as already stopped", () => {
    expect(JOB_STATE_LABELS.CANCELLING).toBe("Đang hủy (chờ xác nhận dừng)");
    for (const state of ["CANCELLING", "PAUSING", "RECOVERING"] as const) {
      const label = JOB_STATE_LABELS[state];
      expect(label).toMatch(/^Đang /);
      expect(label).not.toMatch(/Đã |hoàn tất/i);
      expect(STATE_TONE[state]).toBe("pending");
    }
  });

  it("labels are unique so a state is never ambiguous", () => {
    const values = Object.values(JOB_STATE_LABELS);
    expect(new Set(values).size).toBe(values.length);
  });

  it("maps a null waiting reason to nothing", () => {
    expect(waitingReasonLabel(null)).toBeNull();
    expect(waitingReasonLabel("waiting_for_worker")).toBe(WAITING_REASON_LABELS.waiting_for_worker);
  });
});
