import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./client";
import { DETAIL_POLL, LIST_POLL, Poller, type PollOutcome, type Visibility } from "./polling";

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
    listenerCount: () => listeners.size,
  };
}

function scripted(outcomes: Array<PollOutcome | Error>) {
  const queue = [...outcomes];
  return vi.fn(async (_signal: AbortSignal): Promise<PollOutcome> => {
    const next = queue.shift() ?? "unchanged";
    if (next instanceof Error) throw next;
    return next;
  });
}

function retryable(code: "rate_limited" | "queue_full", seconds: number) {
  return new ApiError({
    status: code === "rate_limited" ? 429 : 503,
    code,
    requestId: null,
    serverMessage: null,
    reason: null,
    retryAfterSeconds: seconds,
  });
}

const schedule = { initialMs: 2000, maxMs: 30_000, factor: 1.5 };

describe("Poller", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("uses the documented schedules", () => {
    expect(DETAIL_POLL).toEqual({ initialMs: 2000, maxMs: 30_000, factor: 1.5 });
    expect(LIST_POLL).toEqual({ initialMs: 10_000, maxMs: 60_000, factor: 1.5 });
  });

  it("backs off while nothing changes, up to the cap", async () => {
    const run = scripted([]);
    const { visibility } = fakeVisibility();
    const poller = new Poller({ schedule, run, visibility });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(run).toHaveBeenCalledTimes(1);
    const delays: number[] = [];
    for (let i = 0; i < 10; i += 1) {
      delays.push(poller.delayMs);
      await vi.advanceTimersByTimeAsync(poller.delayMs);
    }
    expect(delays).toEqual([3000, 4500, 6750, 10125, 15188, 22781, 30000, 30000, 30000, 30000]);
    expect(run).toHaveBeenCalledTimes(11);
    poller.stop();
  });

  it("returns to the fast rate when the resource changes", async () => {
    const run = scripted(["unchanged", "unchanged", "changed"]);
    const poller = new Poller({ schedule, run, visibility: fakeVisibility().visibility });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(3000);
    expect(poller.delayMs).toBe(4500);
    await vi.advanceTimersByTimeAsync(4500);
    expect(run).toHaveBeenCalledTimes(3);
    expect(poller.delayMs).toBe(2000);
    poller.stop();
  });

  it("stops for good once the resource is done (terminal)", async () => {
    const run = scripted(["unchanged", "done"]);
    const poller = new Poller({ schedule, run, visibility: fakeVisibility().visibility });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(3000);
    expect(poller.state).toBe("done");
    await vi.advanceTimersByTimeAsync(600_000);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("pauses while the tab is hidden and resumes immediately when shown", async () => {
    const run = scripted([]);
    const tab = fakeVisibility();
    const poller = new Poller({ schedule, run, visibility: tab.visibility });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    tab.set(true);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(run).toHaveBeenCalledTimes(1);
    expect(poller.state).toBe("paused");
    tab.set(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(run).toHaveBeenCalledTimes(2);
    poller.stop();
    expect(tab.listenerCount()).toBe(0);
  });

  it("does not start while hidden", async () => {
    const run = scripted([]);
    const tab = fakeVisibility();
    tab.set(true);
    const poller = new Poller({ schedule, run, visibility: tab.visibility });
    poller.start();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(run).not.toHaveBeenCalled();
    tab.set(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(run).toHaveBeenCalledTimes(1);
    poller.stop();
  });

  it("waits at least Retry-After on 429/503", async () => {
    const run = scripted([retryable("rate_limited", 45), "unchanged"]);
    const onError = vi.fn();
    const poller = new Poller({ schedule, run, visibility: fakeVisibility().visibility, onError });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(poller.delayMs).toBe(45_000);
    await vi.advanceTimersByTimeAsync(44_999);
    expect(run).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(run).toHaveBeenCalledTimes(2);
    poller.stop();
  });

  it("backs off exponentially on network errors, with a cap", async () => {
    const network = () =>
      new ApiError({ status: 0, code: "network_error", requestId: null, serverMessage: null, reason: null, retryAfterSeconds: null });
    const run = scripted([network(), network(), network(), network(), network(), network()]);
    const poller = new Poller({ schedule, run, visibility: fakeVisibility().visibility, onError: () => undefined });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    const delays: number[] = [];
    for (let i = 0; i < 5; i += 1) {
      delays.push(poller.delayMs);
      await vi.advanceTimersByTimeAsync(poller.delayMs);
    }
    expect(delays).toEqual([4000, 8000, 16_000, 30_000, 30_000]);
    poller.stop();
  });

  it("keeps a single request in flight; kick during a request runs once more right after", async () => {
    let resolve: (value: PollOutcome) => void = () => undefined;
    let active = 0;
    let maxActive = 0;
    const run = vi.fn(
      (_signal: AbortSignal) =>
        new Promise<PollOutcome>((done) => {
          active += 1;
          maxActive = Math.max(maxActive, active);
          resolve = (value) => {
            active -= 1;
            done(value);
          };
        }),
    );
    const poller = new Poller({ schedule, run, visibility: fakeVisibility().visibility });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    poller.kick();
    poller.kick();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(run).toHaveBeenCalledTimes(1);
    resolve("unchanged");
    await vi.advanceTimersByTimeAsync(0);
    expect(run).toHaveBeenCalledTimes(2);
    expect(maxActive).toBe(1);
    resolve("unchanged");
    await vi.advanceTimersByTimeAsync(0);
    poller.stop();
  });

  it("kick resets the backoff and runs now", async () => {
    const run = scripted([]);
    const poller = new Poller({ schedule, run, visibility: fakeVisibility().visibility });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(3000);
    await vi.advanceTimersByTimeAsync(4500);
    expect(poller.delayMs).toBe(10_125 / 1.5);
    poller.kick();
    await vi.advanceTimersByTimeAsync(0);
    expect(run).toHaveBeenCalledTimes(4);
    expect(poller.delayMs).toBe(2000);
    poller.stop();
  });

  it("stop aborts the in-flight request and schedules nothing", async () => {
    let seen: AbortSignal | undefined;
    const run = vi.fn((signal: AbortSignal) => {
      seen = signal;
      return new Promise<PollOutcome>(() => undefined);
    });
    const poller = new Poller({ schedule, run, visibility: fakeVisibility().visibility });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    poller.stop();
    expect(seen?.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(600_000);
    expect(run).toHaveBeenCalledTimes(1);
  });
});
