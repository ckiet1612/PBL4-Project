import { describe, expect, it } from "vitest";
import type { FairnessBucket } from "../../api/types";
import { BUCKET_PRESETS, checkFairnessQuery, sortBuckets, tenantTotals } from "./fairness";

const FROM = "2026-09-01T00:00:00.000Z";
const at = (ms: number) => new Date(Date.parse(FROM) + ms).toISOString();
const DAY = 86_400_000;

describe("fairness pre-check mirrors the server bounds (fairness_report.py)", () => {
  it("accepts the default and the exact limits", () => {
    expect(checkFairnessQuery(FROM, at(DAY), 3600)).toBeNull();
    expect(checkFairnessQuery(FROM, at(31 * DAY), 86_400)).toBeNull();
    expect(checkFairnessQuery(FROM, at(1000 * 60_000), 60)).toBeNull();
    expect(checkFairnessQuery(FROM, at(1000), 1)).toBeNull();
  });

  it("rejects an empty or reversed range", () => {
    expect(checkFairnessQuery(FROM, FROM, 60)).toEqual({ field: "to", message: "Thời điểm kết thúc phải sau thời điểm bắt đầu" });
    expect(checkFairnessQuery(FROM, at(-1), 60)?.field).toBe("to");
  });

  it("rejects more than 31 days", () => {
    expect(checkFairnessQuery(FROM, at(31 * DAY + 1000), 86_400)).toEqual({
      field: "to",
      message: "Khoảng thời gian tối đa 31 ngày",
    });
  });

  it("rejects buckets outside 1..86400 or not whole seconds", () => {
    for (const bucket of [0, 86_401, 1.5, Number.NaN]) {
      expect(checkFairnessQuery(FROM, at(DAY), bucket)).toEqual({
        field: "bucket",
        message: "Độ dài bucket phải là số nguyên từ 1 đến 86400 giây",
      });
    }
  });

  it("rejects more than 1000 buckets, counting a partial last bucket", () => {
    expect(checkFairnessQuery(FROM, at(1000 * 60_000 + 1000), 60)).toEqual({
      field: "bucket",
      message: "Tối đa 1000 bucket; tăng độ dài bucket hoặc thu hẹp khoảng thời gian",
    });
  });

  it("rejects unreadable timestamps", () => {
    expect(checkFairnessQuery("", at(DAY), 60)?.field).toBe("from");
  });

  it("offers minute, five minutes, hour and day presets", () => {
    expect(BUCKET_PRESETS.map((p) => p.seconds)).toEqual([60, 300, 3600, 86_400]);
  });
});

function bucket(tenant_id: string, start: number, dominant: number, normalized: number, occupancy: number): FairnessBucket {
  return {
    tenant_id,
    start_at: at(start),
    end_at: at(start + 3_600_000),
    weight: 1,
    dominant_resource_time_seconds: dominant,
    normalized_service: normalized,
    allocation_occupancy_seconds: occupancy,
  };
}

describe("fairness table", () => {
  const rows = [
    bucket("t-b", 3_600_000, 2, 1, 10),
    bucket("t-a", 3_600_000, 1.5, 3, 20),
    bucket("t-b", 0, 0.5, 0.25, 5),
  ];

  it("sorts by bucket start, then tenant", () => {
    expect(sortBuckets(rows).map((r) => [r.start_at, r.tenant_id])).toEqual([
      [at(0), "t-b"],
      [at(3_600_000), "t-a"],
      [at(3_600_000), "t-b"],
    ]);
  });

  it("totals per tenant within this report", () => {
    expect(tenantTotals(rows)).toEqual([
      { tenant_id: "t-a", dominant_resource_time_seconds: 1.5, normalized_service: 3, allocation_occupancy_seconds: 20 },
      { tenant_id: "t-b", dominant_resource_time_seconds: 2.5, normalized_service: 1.25, allocation_occupancy_seconds: 15 },
    ]);
    expect(tenantTotals([])).toEqual([]);
  });
});
