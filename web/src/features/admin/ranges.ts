// Time ranges for fairness, recovery and audit: default last 24 h (UX-A19), sent as RFC3339 UTC.
export const DAY_MS = 86_400_000;

export const RANGE_PRESETS = [
  { ms: 3_600_000, label: "1 giờ" },
  { ms: DAY_MS, label: "24 giờ" },
  { ms: 7 * DAY_MS, label: "7 ngày" },
] as const;

export interface TimeRange {
  from: string;
  to: string;
}

/** Last `ms` up to now, on whole minutes so it round-trips through datetime-local inputs. The end
 *  is rounded up: rounding down would hide what happened in the current minute. */
export function defaultRange(now: number = Date.now(), ms: number = DAY_MS): TimeRange {
  const to = Math.ceil(now / 60_000) * 60_000;
  return { from: new Date(to - ms).toISOString(), to: new Date(to).toISOString() };
}

export function checkRange(from: string | null, to: string | null): string | null {
  if (!from || !to || Number.isNaN(Date.parse(from)) || Number.isNaN(Date.parse(to))) {
    return "Chọn thời điểm bắt đầu và kết thúc";
  }
  return Date.parse(from) <= Date.parse(to) ? null : "Thời điểm bắt đầu phải trước thời điểm kết thúc";
}
