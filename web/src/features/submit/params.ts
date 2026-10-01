// Template parameters from `parameter_schema`: inline validation and helper text only.
// The server re-validates everything; nothing here grants or infers capability.
import type { ParameterDefinition } from "../../api/types";

export type ParameterValue = number | boolean | string;

export type Parsed<T> = { value: T; error?: never } | { error: string; value?: never };

const NUMBER = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 10 });

/** Accepts "." or "," as the decimal separator; no thousands separators. */
export function parseDecimal(raw: string): number | null {
  const text = raw.trim().replace(",", ".");
  if (!/^-?(\d+(\.\d*)?|\.\d+)(e-?\d+)?$/i.test(text)) return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

export function checkBounds(value: number, minimum: number | null, maximum: number | null): string | null {
  if (minimum !== null && value < minimum) return `Tối thiểu ${NUMBER.format(minimum)}`;
  if (maximum !== null && value > maximum) return `Tối đa ${NUMBER.format(maximum)}`;
  return null;
}

export function parseParameter(def: ParameterDefinition, raw: string): Parsed<ParameterValue | undefined> {
  const text = raw.trim();
  if (text === "") return def.required ? { error: "Bắt buộc nhập" } : { value: undefined };
  switch (def.type) {
    case "INTEGER": {
      if (!/^-?\d+$/.test(text)) return { error: "Phải là số nguyên" };
      const value = Number(text);
      if (!Number.isSafeInteger(value)) return { error: "Số quá lớn" };
      const bound = checkBounds(value, def.minimum, def.maximum);
      return bound ? { error: bound } : { value };
    }
    case "NUMBER": {
      const value = parseDecimal(text);
      if (value === null) return { error: "Phải là số" };
      const bound = checkBounds(value, def.minimum, def.maximum);
      return bound ? { error: bound } : { value };
    }
    case "BOOLEAN":
      if (text === "true") return { value: true };
      if (text === "false") return { value: false };
      return { error: "Chọn có hoặc không" };
    case "ENUM":
      return def.enum_values?.includes(text) ? { value: text } : { error: "Chọn một giá trị trong danh sách" };
    case "STRING":
      return { value: text };
  }
}

const TYPE_LABELS: Record<ParameterDefinition["type"], string> = {
  INTEGER: "Số nguyên",
  NUMBER: "Số thực",
  BOOLEAN: "Có/không",
  STRING: "Chuỗi",
  ENUM: "Chọn một giá trị",
};

export function parameterHelp(def: ParameterDefinition): string {
  const parts = [TYPE_LABELS[def.type]];
  const { minimum, maximum } = def;
  if (minimum !== null && maximum !== null) parts.push(`từ ${NUMBER.format(minimum)} đến ${NUMBER.format(maximum)}`);
  else if (minimum !== null) parts.push(`tối thiểu ${NUMBER.format(minimum)}`);
  else if (maximum !== null) parts.push(`tối đa ${NUMBER.format(maximum)}`);
  const text = parts.join(", ");
  return def.unit ? `${text} (${def.unit})` : text;
}

/** Form value from the template default; templates v1 have none (UX-A05). */
export function initialParameterValue(def: ParameterDefinition): string {
  return def.default === null || def.default === undefined ? "" : String(def.default);
}
