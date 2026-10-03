import { describe, expect, it } from "vitest";
import { checkRange, defaultRange, RANGE_PRESETS } from "./ranges";

describe("time ranges for fairness, recovery and audit (UX-A19)", () => {
  it("defaults to the last 24 hours, in UTC, ending at the next whole minute so the latest records are in", () => {
    expect(defaultRange(Date.parse("2026-10-01T12:34:56.789Z"))).toEqual({
      from: "2026-09-30T12:35:00.000Z",
      to: "2026-10-01T12:35:00.000Z",
    });
    expect(defaultRange(Date.parse("2026-10-01T12:34:00.000Z"), 3_600_000)).toEqual({
      from: "2026-10-01T11:34:00.000Z",
      to: "2026-10-01T12:34:00.000Z",
    });
  });

  it("offers 1 hour, 24 hours and 7 days", () => {
    expect(RANGE_PRESETS.map((p) => p.ms)).toEqual([3_600_000, 86_400_000, 604_800_000]);
  });

  it("requires from ≤ to for audit/recovery", () => {
    expect(checkRange("2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z")).toBeNull();
    expect(checkRange("2026-10-02T00:00:00Z", "2026-10-01T00:00:00Z")).toBe("Thời điểm bắt đầu phải trước thời điểm kết thúc");
    expect(checkRange(null, "2026-10-01T00:00:00Z")).toBe("Chọn thời điểm bắt đầu và kết thúc");
  });
});
