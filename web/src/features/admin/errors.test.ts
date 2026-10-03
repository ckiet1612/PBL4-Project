import { describe, expect, it } from "vitest";
import { ApiError, type ClientErrorCode } from "../../api/client";
import { POLICY_CONFLICT_HINT, adminErrorView, conflictMessage, validationField, versionFromEtag } from "./errors";

function error(code: ClientErrorCode, status: number, serverMessage: string | null = null) {
  return new ApiError({ status, code, requestId: "req-1", serverMessage, reason: null, retryAfterSeconds: null });
}

describe("admin error mapping (docs/web-ui.md Ánh xạ lỗi — quản trị)", () => {
  it("403 says system admin rights are needed", () => {
    const view = adminErrorView(error("permission_denied", 403));
    expect(view.title).toBe("Cần quyền quản trị hệ thống");
    expect(view.hint).toBeNull();
  });

  it("409 shows the server's reason", () => {
    const view = adminErrorView(error("state_conflict", 409, "Quarantined allocations still await verified cleanup"));
    expect(view.detail).toBe("Quarantined allocations still await verified cleanup");
  });

  it("other codes keep the shared B17 text", () => {
    expect(adminErrorView(error("precondition_required", 428)).title).toBe("Thiếu điều kiện phiên bản (If-Match)");
  });

  it("412 explains what changed", () => {
    expect(conflictMessage(3, 4)).toBe("Đối tượng vừa được thay đổi (v3 → v4). Kiểm tra rồi gửi lại.");
    expect(POLICY_CONFLICT_HINT).toBe("Hãy ngừng nhận job (drain) hoặc chờ job đang chạy kết thúc rồi thử lại");
  });

  it("attaches 400/422 messages to the matching field", () => {
    const fields = ["slug", "display_name"] as const;
    expect(validationField(error("validation_failed", 422, "Tenant slug does not match the approved format"), fields)).toBe("slug");
    expect(validationField(error("validation_failed", 422, "Display name length must be between 1 and 100 characters"), fields)).toBe(
      "display_name",
    );
    const policy = ["weight", "submit_burst", "user_submit_burst", "resource_limit"] as const;
    expect(validationField(error("validation_failed", 422, "user_submit_burst exceeds its maximum"), policy)).toBe("user_submit_burst");
    expect(validationField(error("validation_failed", 422, "submit_burst must be positive"), policy)).toBe("submit_burst");
    expect(validationField(error("validation_failed", 422, "resource_limit.cpu_millis must be nonnegative"), policy)).toBe(
      "resource_limit",
    );
    expect(validationField(error("validation_failed", 422, "Something else"), policy)).toBeNull();
    expect(validationField(error("state_conflict", 409, "weight"), policy)).toBeNull();
    expect(validationField(new Error("x"), policy)).toBeNull();
  });
});

describe("versionFromEtag", () => {
  it("reads the strong version ETag", () => {
    expect(versionFromEtag('"v12"')).toBe(12);
    expect(versionFromEtag('W/"v3"')).toBeNull();
    expect(versionFromEtag(null)).toBeNull();
    expect(versionFromEtag('"abc"')).toBeNull();
  });
});
