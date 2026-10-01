// Display formatting only: units, IDs and time. Never used to infer job or lease state.
const NUMBER = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 3 });
const SIZE = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 1 });
const BINARY_UNITS = ["B", "KiB", "MiB", "GiB", "TiB"] as const;

export function formatCores(cpuMillis: number): string {
  return `${NUMBER.format(cpuMillis / 1000)} core`;
}

export function formatBytes(bytes: number): string {
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < BINARY_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? String(value) : SIZE.format(value)} ${BINARY_UNITS[unit]}`;
}

/** UUIDv7 starts with a timestamp, so the random tail is what tells rows apart. */
export function shortId(id: string): string {
  return `…${id.slice(-8)}`;
}

export function shortChecksum(checksum: string): string {
  const [algorithm, hex] = checksum.includes(":") ? checksum.split(":", 2) : ["", checksum];
  return `${algorithm ? `${algorithm}:` : ""}${hex.slice(0, 12)}…`;
}

export function formatTime(iso: string, timeZone?: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat("vi-VN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZoneName: "short",
    timeZone,
  }).format(date);
}

/** `<input type="datetime-local">` value (local time) → UTC ISO for the API filter. */
export function localInputToUtc(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

/** UTC ISO → `<input type="datetime-local">` value in local time. */
export function utcToLocalInput(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
