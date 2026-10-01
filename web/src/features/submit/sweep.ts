// Sweep dimensions for pytorch-cifar10-cnn (UX-A12). Children = product of value counts, ≤ 100.
import { SWEEP_MAX_CHILDREN } from "../../api/limits";
import type { ParameterDefinition } from "../../api/types";
import { parseParameter, type ParameterValue } from "./params";

export const SWEEP_MAX_DIMENSIONS = 16;

export type SweepValues = { values: ParameterValue[]; error?: never } | { error: string; values?: never };

/**
 * Values are separated by ";" when present, otherwise by ",", so decimal commas still work
 * with ";" ("0,1; 0,01") and integers can be typed as "1, 2, 3".
 */
export function parseSweepValues(def: ParameterDefinition, raw: string): SweepValues {
  const separator = raw.includes(";") ? ";" : ",";
  const parts = raw
    .split(separator)
    .map((part) => part.trim())
    .filter((part) => part !== "");
  if (parts.length === 0) return { error: "Nhập ít nhất một giá trị" };
  if (parts.length > SWEEP_MAX_CHILDREN) return { error: `Tối đa ${SWEEP_MAX_CHILDREN} giá trị` };
  const values: ParameterValue[] = [];
  const seen = new Set<string>();
  for (const part of parts) {
    const parsed = parseParameter({ ...def, required: true }, part);
    if (parsed.error !== undefined) return { error: `${part}: ${parsed.error}` };
    const value = parsed.value as ParameterValue;
    const key = JSON.stringify(value);
    if (seen.has(key)) return { error: `Giá trị bị trùng: ${part}` };
    seen.add(key);
    values.push(value);
  }
  return { values };
}

export function sweepChildCount(dimensions: ParameterValue[][]): number {
  if (dimensions.length === 0) return 0;
  return dimensions.reduce((product, values) => product * values.length, 1);
}
