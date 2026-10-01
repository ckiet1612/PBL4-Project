import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./client";
import { IDEMPOTENCY_KEY_PATTERN, IntentTracker, newIdempotencyKey, withInProgressRetry } from "./idempotency";

function error(code: string, retryAfterSeconds: number | null = null) {
  return new ApiError({
    status: code === "network_error" ? 0 : 409,
    code: code as ApiError["code"],
    requestId: null,
    serverMessage: null,
    reason: null,
    retryAfterSeconds,
  });
}

function withStatus(status: number, code: string) {
  return new ApiError({
    status,
    code: code as ApiError["code"],
    requestId: null,
    serverMessage: null,
    reason: null,
    retryAfterSeconds: null,
  });
}

describe("idempotency keys", () => {
  it("are random and match the contract pattern", () => {
    const keys = new Set(Array.from({ length: 50 }, () => newIdempotencyKey()));
    expect(keys.size).toBe(50);
    for (const key of keys) expect(key).toMatch(IDEMPOTENCY_KEY_PATTERN);
    expect(IDEMPOTENCY_KEY_PATTERN.source).toBe("^[!-~]{16,128}$");
  });
});

describe("IntentTracker", () => {
  it("reuses the key for the same body after a network error", () => {
    const tracker = new IntentTracker();
    const first = tracker.keyFor('{"a":1}');
    tracker.settle(error("network_error"));
    expect(tracker.keyFor('{"a":1}')).toBe(first);
  });

  it("reuses the key while a request is still in flight (double click)", () => {
    const tracker = new IntentTracker();
    expect(tracker.keyFor("body")).toBe(tracker.keyFor("body"));
  });

  it("reuses the key after idempotency_in_progress, rate_limited and timeout", () => {
    for (const code of ["idempotency_in_progress", "rate_limited", "timeout"]) {
      const tracker = new IntentTracker();
      const first = tracker.keyFor("body");
      tracker.settle(error(code));
      expect(tracker.keyFor("body")).toBe(first);
    }
  });

  it("reuses the key after a proxy 5xx without an envelope (B17-RV02)", () => {
    for (const status of [500, 502, 503, 504]) {
      const tracker = new IntentTracker();
      const first = tracker.keyFor("body");
      tracker.settle(withStatus(status, "unexpected_response"));
      expect(tracker.keyFor("body")).toBe(first);
    }
  });

  it("starts a new intent after a 5xx with an envelope or a non-5xx unexpected response", () => {
    const enveloped = new IntentTracker();
    const first = enveloped.keyFor("body");
    enveloped.settle(withStatus(503, "storage_pressure"));
    expect(enveloped.keyFor("body")).not.toBe(first);

    const odd = new IntentTracker();
    const second = odd.keyFor("body");
    odd.settle(withStatus(418, "unexpected_response"));
    expect(odd.keyFor("body")).not.toBe(second);
  });

  it("a new intent gets a new key: changed body, success or a definitive error", () => {
    const tracker = new IntentTracker();
    const first = tracker.keyFor("body");
    expect(tracker.keyFor("other body")).not.toBe(first);

    const second = tracker.keyFor("body");
    tracker.settle();
    expect(tracker.keyFor("body")).not.toBe(second);

    const third = tracker.keyFor("body");
    tracker.settle(error("validation_failed"));
    expect(tracker.keyFor("body")).not.toBe(third);
  });
});

describe("withInProgressRetry", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("waits Retry-After and resends a bounded number of times", async () => {
    const send = vi
      .fn<() => Promise<string>>()
      .mockRejectedValueOnce(error("idempotency_in_progress", 1))
      .mockRejectedValueOnce(error("idempotency_in_progress", 2))
      .mockResolvedValueOnce("done");
    const pending = withInProgressRetry(send);
    await vi.advanceTimersByTimeAsync(999);
    expect(send).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(send).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(2000);
    await expect(pending).resolves.toBe("done");
    expect(send).toHaveBeenCalledTimes(3);
  });

  it("gives up after the bound and rethrows", async () => {
    const send = vi.fn<() => Promise<string>>().mockRejectedValue(error("idempotency_in_progress", 1));
    const pending = withInProgressRetry(send, 3).catch((caught: unknown) => caught);
    await vi.advanceTimersByTimeAsync(10_000);
    const result = (await pending) as ApiError;
    expect(result.code).toBe("idempotency_in_progress");
    expect(send).toHaveBeenCalledTimes(3);
  });

  it("does not retry other errors", async () => {
    const send = vi.fn<() => Promise<string>>().mockRejectedValue(error("idempotency_conflict"));
    await expect(withInProgressRetry(send)).rejects.toMatchObject({ code: "idempotency_conflict" });
    expect(send).toHaveBeenCalledTimes(1);
  });
});
