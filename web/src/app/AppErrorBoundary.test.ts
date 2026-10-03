import { describe, expect, it } from "vitest";
import { AppErrorBoundary, RENDER_ERROR_TITLE } from "./AppErrorBoundary";

describe("AppErrorBoundary (B18-R15)", () => {
  it("switches to the fallback on any render error without keeping the error", () => {
    expect(AppErrorBoundary.getDerivedStateFromError(new Error("secret stack"))).toEqual({ failed: true });
    expect(RENDER_ERROR_TITLE).toBe("Đã xảy ra lỗi hiển thị");
  });
});

describe("AppErrorBoundary resets on navigation (B18-RV10)", () => {
  it("a new route clears the fallback; the same route keeps it", () => {
    expect(AppErrorBoundary.getDerivedStateFromProps({ resetKey: "/admin/users" }, { failed: true, resetKey: "/admin/audit" })).toEqual({
      failed: false,
      resetKey: "/admin/users",
    });
    expect(AppErrorBoundary.getDerivedStateFromProps({ resetKey: "/admin/audit" }, { failed: true, resetKey: "/admin/audit" })).toBeNull();
  });

  it("without a reset key (the outermost boundary) the fallback stays until reload", () => {
    expect(AppErrorBoundary.getDerivedStateFromProps({}, { failed: true, resetKey: undefined })).toBeNull();
  });
});
