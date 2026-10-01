// Visibility-aware polling with backoff (PLAN:201). One request in flight per resource.
import { ApiError, isAbortError } from "./client";

export interface PollSchedule {
  initialMs: number;
  maxMs: number;
  factor: number;
}

/** Job detail while not terminal. */
export const DETAIL_POLL: PollSchedule = { initialMs: 2000, maxMs: 30_000, factor: 1.5 };
/** First page of the job list only. */
export const LIST_POLL: PollSchedule = { initialMs: 10_000, maxMs: 60_000, factor: 1.5 };

/** changed: back to the fast rate; unchanged: slow down; done: terminal, stop polling. */
export type PollOutcome = "changed" | "unchanged" | "done";

export interface Visibility {
  hidden(): boolean;
  subscribe(listener: () => void): () => void;
}

export const documentVisibility: Visibility = {
  hidden: () => document.visibilityState === "hidden",
  subscribe(listener) {
    document.addEventListener("visibilitychange", listener);
    return () => document.removeEventListener("visibilitychange", listener);
  },
};

export interface PollerOptions {
  schedule: PollSchedule;
  run(signal: AbortSignal): Promise<PollOutcome>;
  visibility?: Visibility;
  onError?(error: unknown): void;
}

export type PollerState = "idle" | "running" | "waiting" | "paused" | "done" | "stopped";

export class Poller {
  private readonly options: PollerOptions;
  private readonly visibility: Visibility;
  private backoff: number;
  private wait: number;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private controller: AbortController | null = null;
  private kickPending = false;
  /** The running request was started by kick(): the schedule restarts after it. */
  private kickedRun = false;
  private unsubscribe: (() => void) | null = null;
  state: PollerState = "idle";

  constructor(options: PollerOptions) {
    this.options = options;
    this.visibility = options.visibility ?? documentVisibility;
    this.backoff = options.schedule.initialMs;
    this.wait = options.schedule.initialMs;
  }

  /** Delay before the next scheduled run, in milliseconds. */
  get delayMs(): number {
    return Math.round(this.wait);
  }

  start(): void {
    if (this.state !== "idle") return;
    this.unsubscribe = this.visibility.subscribe(() => {
      if (!this.visibility.hidden() && this.state === "paused") this.tick();
    });
    this.tick();
  }

  /** User action or known change: reset the backoff and poll now (or right after the running request). */
  kick(): void {
    if (this.state === "stopped" || this.state === "idle") return;
    this.backoff = this.options.schedule.initialMs;
    this.wait = this.backoff;
    if (this.state === "running") {
      this.kickPending = true;
      return;
    }
    this.clearTimer();
    this.tick(true);
  }

  stop(): void {
    this.state = "stopped";
    this.clearTimer();
    this.controller?.abort();
    this.controller = null;
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  private clearTimer(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }

  private tick(kicked = false): void {
    if (this.state === "stopped") return;
    this.kickedRun = kicked;
    if (this.visibility.hidden()) {
      this.state = "paused";
      return;
    }
    this.state = "running";
    const controller = new AbortController();
    this.controller = controller;
    this.options
      .run(controller.signal)
      .then((outcome) => this.settle(outcome, null))
      .catch((error: unknown) => this.settle(null, error));
  }

  private settle(outcome: PollOutcome | null, error: unknown): void {
    if (this.state === "stopped") return;
    this.controller = null;
    const { initialMs, maxMs, factor } = this.options.schedule;
    if (error !== null) {
      if (isAbortError(error)) return;
      this.options.onError?.(error);
      this.backoff = Math.min(Math.max(this.backoff * 2, initialMs), maxMs);
      const retryAfter = error instanceof ApiError ? error.retryAfterSeconds : null;
      this.wait = Math.max(this.backoff, (retryAfter ?? 0) * 1000);
    } else if (outcome === "done") {
      this.state = "done";
      if (!this.kickPending) return;
    } else if (outcome === "changed" || this.kickedRun) {
      this.backoff = initialMs;
      this.wait = initialMs;
    } else {
      this.backoff = Math.min(this.backoff * factor, maxMs);
      this.wait = this.backoff;
    }
    if (this.kickPending) {
      this.kickPending = false;
      this.backoff = initialMs;
      this.wait = initialMs;
      this.tick(true);
      return;
    }
    this.state = "waiting";
    this.timer = setTimeout(() => {
      this.timer = null;
      this.tick();
    }, this.wait);
  }
}
