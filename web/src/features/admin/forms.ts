// Client checks for admin forms, copied from the contract bounds (openapi.yaml). Only a hint:
// the server validates again and its 400/422 is shown on the field (B18-R19: slug is narrower there).
import { GLOBAL_LIMIT_MAX, GLOBAL_LIMIT_MIN } from "../../api/limits";
import { parseCount } from "./units";

export const SLUG_PATTERN = /^[a-z][a-z0-9-]{1,62}[a-z0-9]$/;
export const SLUG_HINT =
  "3–64 ký tự: chữ thường, số, dấu gạch ngang; bắt đầu bằng chữ, không kết thúc bằng gạch ngang";

export function checkSlug(slug: string): string | null {
  return SLUG_PATTERN.test(slug) ? null : SLUG_HINT;
}

export function checkDisplayName(name: string): string | null {
  const length = name.trim().length;
  return length >= 1 && length <= 100 ? null : "Nhập tên hiển thị (tối đa 100 ký tự)";
}

export function checkUsername(username: string): string | null {
  const length = username.trim().length;
  return length >= 3 && length <= 254 ? null : "Tên đăng nhập 3–254 ký tự";
}

export function checkPasswords(password: string, confirm: string): { password?: string; confirm?: string } {
  if (password.length < 12 || password.length > 1024) return { password: "Mật khẩu 12–1024 ký tự" };
  if (password !== confirm) return { confirm: "Hai lần nhập mật khẩu không khớp" };
  return {};
}

export function checkGlobalLimit(text: string): { value: number | null; error: string | null } {
  const value = parseCount(text);
  if (value === null || value < GLOBAL_LIMIT_MIN || value > GLOBAL_LIMIT_MAX) {
    return { value: null, error: "Số nguyên từ 1 đến 1.000.000" };
  }
  return { value, error: null };
}
