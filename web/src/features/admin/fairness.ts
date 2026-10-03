// Fairness report: client pre-check of the server bounds (src/nexa/application/fairness_report.py)
// and the per-tenant totals of one report.
import type { FairnessBucket } from "../../api/types";

export const MAX_FAIRNESS_RANGE_MS = 31 * 86_400_000;
export const MAX_FAIRNESS_BUCKETS = 1000;
export const MAX_BUCKET_SECONDS = 86_400;
export const DEFAULT_BUCKET_SECONDS = 3600;

export const BUCKET_PRESETS = [
  { seconds: 60, label: "1 phút" },
  { seconds: 300, label: "5 phút" },
  { seconds: 3600, label: "1 giờ" },
  { seconds: 86_400, label: "1 ngày" },
] as const;

export const FAIRNESS_EXPLANATION =
  "Thời gian tài nguyên trội = Σ (tỉ lệ tài nguyên trội × thời gian giữ). Dịch vụ chuẩn hóa = phần trên chia cho trọng số; tenant có dịch vụ chuẩn hóa thấp hơn được ưu tiên. Thời gian chiếm dụng = tổng thời gian giữ allocation.";

export interface FairnessQueryError {
  field: "from" | "to" | "bucket";
  message: string;
}

export function checkFairnessQuery(from: string, to: string, bucketSeconds: number): FairnessQueryError | null {
  const start = Date.parse(from);
  const end = Date.parse(to);
  if (Number.isNaN(start)) return { field: "from", message: "Chọn thời điểm bắt đầu" };
  if (Number.isNaN(end)) return { field: "to", message: "Chọn thời điểm kết thúc" };
  if (end <= start) return { field: "to", message: "Thời điểm kết thúc phải sau thời điểm bắt đầu" };
  if (end - start > MAX_FAIRNESS_RANGE_MS) return { field: "to", message: "Khoảng thời gian tối đa 31 ngày" };
  if (!Number.isInteger(bucketSeconds) || bucketSeconds < 1 || bucketSeconds > MAX_BUCKET_SECONDS) {
    return { field: "bucket", message: "Độ dài bucket phải là số nguyên từ 1 đến 86400 giây" };
  }
  if (Math.ceil((end - start) / (bucketSeconds * 1000)) > MAX_FAIRNESS_BUCKETS) {
    return { field: "bucket", message: "Tối đa 1000 bucket; tăng độ dài bucket hoặc thu hẹp khoảng thời gian" };
  }
  return null;
}

export function sortBuckets(buckets: readonly FairnessBucket[]): FairnessBucket[] {
  return [...buckets].sort(
    (a, b) => Date.parse(a.start_at) - Date.parse(b.start_at) || a.tenant_id.localeCompare(b.tenant_id),
  );
}

export interface TenantTotal {
  tenant_id: string;
  dominant_resource_time_seconds: number;
  normalized_service: number;
  allocation_occupancy_seconds: number;
}

/** Sums over the buckets of this report only ("tổng trong báo cáo này"). */
export function tenantTotals(buckets: readonly FairnessBucket[]): TenantTotal[] {
  const totals = new Map<string, TenantTotal>();
  for (const bucket of buckets) {
    const total = totals.get(bucket.tenant_id) ?? {
      tenant_id: bucket.tenant_id,
      dominant_resource_time_seconds: 0,
      normalized_service: 0,
      allocation_occupancy_seconds: 0,
    };
    total.dominant_resource_time_seconds += bucket.dominant_resource_time_seconds;
    total.normalized_service += bucket.normalized_service;
    total.allocation_occupancy_seconds += bucket.allocation_occupancy_seconds;
    totals.set(bucket.tenant_id, total);
  }
  return [...totals.values()].sort((a, b) => a.tenant_id.localeCompare(b.tenant_id));
}
