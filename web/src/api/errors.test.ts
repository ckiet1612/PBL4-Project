import { describe, expect, it } from "vitest";

import type { components } from "./generated";
import { ApiError } from "./client";
import { CONTRACT_ERROR_CODES, describeError, ERROR_MESSAGES, isBadCursorError } from "./errors";

type ContractCode = components["schemas"]["ErrorCode"];

// Kept literal on purpose: adding a code to the contract fails `tsc` (Record<> in errors.ts)
// and this list fails here, so a new code always gets a reviewed Vietnamese message.
const EXPECTED: ContractCode[] = [
  "authentication_required",
  "invalid_csrf",
  "permission_denied",
  "resource_not_found",
  "validation_failed",
  "invalid_cursor",
  "idempotency_conflict",
  "idempotency_in_progress",
  "one_time_secret_unavailable",
  "precondition_required",
  "version_conflict",
  "state_conflict",
  "infeasible_request",
  "rate_limited",
  "quota_exceeded",
  "queue_full",
  "dependency_unavailable",
  "checksum_mismatch",
  "payload_too_large",
  "storage_pressure",
  "stale_authority",
  "lease_expired",
  "callback_replayed",
  "internal_error",
];

function apiError(code: string, status: number, extra: Partial<{ message: string; retryAfter: number }> = {}) {
  return new ApiError({
    status,
    code: code as ContractCode,
    requestId: "01890a5d-ac96-7000-8000-00000000abcd",
    serverMessage: extra.message ?? "Server detail",
    reason: null,
    retryAfterSeconds: extra.retryAfter ?? null,
  });
}

describe("error map", () => {
  it("covers every contract error code with a Vietnamese title", () => {
    expect([...CONTRACT_ERROR_CODES].sort()).toEqual([...EXPECTED].sort());
    for (const code of EXPECTED) {
      const message = ERROR_MESSAGES[code];
      expect(message.title.length).toBeGreaterThan(5);
    }
  });

  it("covers client-side failures", () => {
    for (const code of ["network_error", "timeout", "unexpected_response"] as const) {
      expect(ERROR_MESSAGES[code].title.length).toBeGreaterThan(5);
    }
  });

  it("always keeps the request id and never exposes a stack", () => {
    for (const code of EXPECTED) {
      const view = describeError(apiError(code, 400));
      expect(view.requestId).toBe("01890a5d-ac96-7000-8000-00000000abcd");
      expect(JSON.stringify(view)).not.toMatch(/at .*\.(ts|js):\d+/);
    }
    const unknown = describeError(new Error("boom\n    at secret (file.ts:1:1)"));
    expect(unknown.title).toBe(ERROR_MESSAGES.internal_error.title);
    expect(JSON.stringify(unknown)).not.toContain("secret");
  });

  it("required cases carry the wording and hints from the task", () => {
    expect(describeError(apiError("quota_exceeded", 429, { retryAfter: 5 })).title).toMatch(/^Tenant đã chạm hạn mức/);
    expect(describeError(apiError("quota_exceeded", 429)).hint).toMatch(/quản trị viên tenant/);
    expect(describeError(apiError("infeasible_request", 422)).title).toBe(
      "Tài nguyên yêu cầu vượt khả năng của hệ thống",
    );
    for (const code of ["queue_full", "dependency_unavailable", "storage_pressure"]) {
      expect(describeError(apiError(code, 503, { retryAfter: 2 })).title).toMatch(/tạm thời/);
    }
    const rate = describeError(apiError("rate_limited", 429, { retryAfter: 4 }));
    expect(rate.retryAfterSeconds).toBe(4);
  });

  it("does not offer a countdown for quota_exceeded: waiting seconds does not free quota", () => {
    expect(describeError(apiError("quota_exceeded", 429, { retryAfter: 1 })).retryAfterSeconds).toBeNull();
    expect(describeError(apiError("queue_full", 503, { retryAfter: 2 })).retryAfterSeconds).toBe(2);
  });

  it("shows the server message as secondary detail only where it helps", () => {
    const conflict = describeError(apiError("state_conflict", 409, { message: "Job admission is off" }));
    expect(conflict.detail).toBe("Job admission is off");
    const validation = describeError(apiError("validation_failed", 422, { message: "spec.priority: too large" }));
    expect(validation.detail).toBe("spec.priority: too large");
    const internal = describeError(apiError("internal_error", 500, { message: "Traceback: boom" }));
    expect(internal.detail).toBeNull();
    const auth = describeError(apiError("authentication_required", 401));
    expect(auth.detail).toBeNull();
  });
});

describe("isBadCursorError", () => {
  it("treats a rejected or malformed URL cursor as a stale page position", () => {
    expect(isBadCursorError(apiError("invalid_cursor", 400), "a".repeat(40))).toBe(true);
    // Too short or too long for the query contract (16–2048): FastAPI answers validation_failed.
    expect(isBadCursorError(apiError("validation_failed", 400), "tampered")).toBe(true);
  });

  it("does not hide other failures, or validation errors without a cursor", () => {
    expect(isBadCursorError(apiError("validation_failed", 400), null)).toBe(false);
    expect(isBadCursorError(apiError("invalid_cursor", 400), null)).toBe(false);
    expect(isBadCursorError(apiError("internal_error", 500), "a".repeat(40))).toBe(false);
    expect(isBadCursorError(null, "a".repeat(40))).toBe(false);
  });
});
