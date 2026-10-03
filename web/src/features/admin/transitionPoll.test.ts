import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import type { PollOutcome, Visibility } from "../../api/polling";
import { TRANSITION_POLL, TRANSITION_POLL_LIMIT_MS, startTransitionPoll } from "./transitionPoll";

function fakeVisibility() {
  let hidden = false;
  const listeners = new Set<() => void>();
  const visibility: Visibility = {
    hidden: () => hidden,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
  return {
    visibility,
    set(value: boolean) {
      hidden = value;
      for (const listener of listeners) listener();
    },
  };
}

/** run() that records the fake time of each call in `times`. */
function scripted(outcomes: Array<PollOutcome | Error>, times: number[] = []) {
  const queue = [...outcomes];
  return vi.fn(async (_signal: AbortSignal): Promise<PollOutcome> => {
    times.push(Date.now());
    const next = queue.shift() ?? "unchanged";
    if (next instanceof Error) throw next;
    return next;
  });
}

function apiError(code: "rate_limited" | "permission_denied", status: number, retryAfterSeconds: number | null) {
  return new ApiError({ status, code, requestId: null, serverMessage: null, reason: null, retryAfterSeconds });
}

describe("worker transition polling (docs/web-ui.md A6)", () => {
  beforeEach(() => vi.useFakeTimers({ now: 0 }));
  afterEach(() => vi.useRealTimers());

  it("uses the documented schedule", () => {
    expect(TRANSITION_POLL).toEqual({ initialMs: 5000, maxMs: 60_000, factor: 1.5 });
    expect(TRANSITION_POLL_LIMIT_MS).toBe(600_000);
  });

  it("waits 5 s before the first read, then backs off ×1.5 up to 60 s", async () => {
    const times: number[] = [];
    const run = scripted([], times);
    const { visibility } = fakeVisibility();
    const poll = startTransitionPoll({ run, visibility, onExpire: vi.fn() });
    await vi.advanceTimersByTimeAsync(400_000);
    poll.stop();
    const gaps = times.map((t, i) => Math.floor(t - (i === 0 ? 0 : times[i - 1])));
    expect(gaps.slice(0, 9)).toEqual([5000, 7500, 11_250, 16_875, 25_312, 37_968, 56_953, 60_000, 60_000]);
  });

  it("stops once the API shows completion", async () => {
    const run = scripted(["unchanged", "done"]);
    const onExpire = vi.fn();
    const { visibility } = fakeVisibility();
    startTransitionPoll({ run, visibility, onExpire });
    await vi.advanceTimersByTimeAsync(TRANSITION_POLL_LIMIT_MS + 60_000);
    expect(run).toHaveBeenCalledTimes(2);
    expect(onExpire).not.toHaveBeenCalled();
  });

  it("gives up after 10 minutes and says so", async () => {
    const run = scripted([]);
    const onExpire = vi.fn();
    const { visibility } = fakeVisibility();
    startTransitionPoll({ run, visibility, onExpire });
    await vi.advanceTimersByTimeAsync(TRANSITION_POLL_LIMIT_MS - 1);
    expect(onExpire).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(onExpire).toHaveBeenCalledTimes(1);
    const calls = run.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(run).toHaveBeenCalledTimes(calls);
  });

  it("pauses while the tab is hidden and reads as soon as it is visible", async () => {
    const run = scripted([]);
    const tab = fakeVisibility();
    startTransitionPoll({ run, visibility: tab.visibility, onExpire: vi.fn() });
    tab.set(true);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(run).not.toHaveBeenCalled();
    tab.set(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("honours Retry-After on errors", async () => {
    const times: number[] = [];
    const run = scripted([apiError("rate_limited", 429, 45)], times);
    const onError = vi.fn();
    const { visibility } = fakeVisibility();
    startTransitionPoll({ run, visibility, onExpire: vi.fn(), onError });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(times.slice(0, 2)).toEqual([5000, 50_000]);
  });

  it("stops on errors that another read cannot fix", async () => {
    const run = scripted([apiError("permission_denied", 403, null)]);
    const onError = vi.fn();
    const { visibility } = fakeVisibility();
    startTransitionPoll({ run, visibility, onExpire: vi.fn(), onError });
    await vi.advanceTimersByTimeAsync(TRANSITION_POLL_LIMIT_MS);
    expect(run).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledTimes(1);
  });

  it("stop() (leaving the page) cancels everything", async () => {
    const run = scripted([]);
    const onExpire = vi.fn();
    const { visibility } = fakeVisibility();
    const poll = startTransitionPoll({ run, visibility, onExpire });
    poll.stop();
    await vi.advanceTimersByTimeAsync(TRANSITION_POLL_LIMIT_MS + 1);
    expect(run).not.toHaveBeenCalled();
    expect(onExpire).not.toHaveBeenCalled();
  });
});
