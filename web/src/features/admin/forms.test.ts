import { describe, expect, it } from "vitest";
import { checkDisplayName, checkGlobalLimit, checkPasswords, checkSlug, checkUsername } from "./forms";

describe("admin form checks (contract bounds)", () => {
  it("slug follows the contract pattern", () => {
    expect(checkSlug("lab-a")).toBeNull();
    expect(checkSlug("a".repeat(64))).toBeNull();
    expect(checkSlug("ab")).toBe("3–64 ký tự: chữ thường, số, dấu gạch ngang; bắt đầu bằng chữ, không kết thúc bằng gạch ngang");
    expect(checkSlug("Lab")).not.toBeNull();
    expect(checkSlug("lab-")).not.toBeNull();
    expect(checkSlug("1lab")).not.toBeNull();
    expect(checkSlug("a".repeat(65))).not.toBeNull();
  });

  it("display name is 1–100 characters after trimming", () => {
    expect(checkDisplayName("Lab A")).toBeNull();
    expect(checkDisplayName("   ")).toBe("Nhập tên hiển thị (tối đa 100 ký tự)");
    expect(checkDisplayName("x".repeat(101))).toBe("Nhập tên hiển thị (tối đa 100 ký tự)");
  });

  it("username is 3–254 characters", () => {
    expect(checkUsername("bob")).toBeNull();
    expect(checkUsername("bo")).toBe("Tên đăng nhập 3–254 ký tự");
  });

  it("password is 12–1024 characters and entered twice", () => {
    expect(checkPasswords("correct horse", "correct horse")).toEqual({});
    expect(checkPasswords("short", "short")).toEqual({ password: "Mật khẩu 12–1024 ký tự" });
    expect(checkPasswords("correct horse", "correct house")).toEqual({ confirm: "Hai lần nhập mật khẩu không khớp" });
  });

  it("global limit is an integer from 1 to 1,000,000", () => {
    expect(checkGlobalLimit("1")).toEqual({ value: 1, error: null });
    expect(checkGlobalLimit("1000000")).toEqual({ value: 1_000_000, error: null });
    for (const text of ["0", "1000001", "1,5", "", "-3", "1e3"]) {
      expect(checkGlobalLimit(text)).toEqual({ value: null, error: "Số nguyên từ 1 đến 1.000.000" });
    }
  });
});
