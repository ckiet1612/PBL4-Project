// Admin inputs in core/GiB/decimal, sent as millicores/bytes/JSON numbers (UX-A21).
const DECIMAL = /^(\d+)(?:[.,](\d+))?$/;
const GIB = 2 ** 30;

/** "0,5" or "0.5" → 0.5; no sign, exponent, grouping or empty parts. */
export function parseDecimal(text: string): number | null {
  const match = DECIMAL.exec(text.trim());
  if (!match) return null;
  const value = Number(`${match[1]}.${match[2] ?? "0"}`);
  return Number.isFinite(value) ? value : null;
}

/** Cores with at most three decimals → integer millicores, computed from the digits. */
export function parseCores(text: string): number | null {
  const match = DECIMAL.exec(text.trim());
  if (!match) return null;
  const fraction = match[2] ?? "";
  if (fraction.length > 3) return null;
  const millis = Number(match[1]) * 1000 + Number(fraction.padEnd(3, "0"));
  return Number.isSafeInteger(millis) ? millis : null;
}

/** GiB → bytes, rounded to a byte. */
export function parseGib(text: string): number | null {
  const value = parseDecimal(text);
  if (value === null) return null;
  const bytes = Math.round(value * GIB);
  return Number.isSafeInteger(bytes) ? bytes : null;
}

export function parseCount(text: string): number | null {
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return Number.isSafeInteger(value) ? value : null;
}

/** Fixed-point text without exponent or trailing zeros, decimal comma, no grouping. */
function plain(value: number, digits: number): string {
  return value.toFixed(digits).replace(/\.?0+$/, "").replace(".", ",");
}

/**
 * Number for an input field: the shortest decimal that reads back as the same double, written
 * out without exponent (B18-RV06). An untouched field therefore parses to the server value and
 * is never sent as a rounded change; tiny or huge values stay valid input.
 */
export function numberText(value: number): string {
  const text = String(value);
  const match = /^(\d)(?:\.(\d+))?e([+-]\d+)$/.exec(text);
  if (!match) return text.replace(".", ",");
  const digits = match[1] + (match[2] ?? "");
  const exponent = Number(match[3]);
  if (exponent < 0) return `0,${"0".repeat(-exponent - 1)}${digits}`;
  return digits.padEnd(exponent + 1, "0");
}

export function millisToCoresText(millis: number): string {
  return plain(millis / 1000, 3);
}

export function bytesToGibText(bytes: number): string {
  // 1 byte ≈ 9.3e-10 GiB: ten decimals keep the round trip exact to the byte.
  return plain(bytes / GIB, 10);
}
