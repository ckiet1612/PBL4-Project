// Worker detail auto-refresh while a transition is pending (docs/web-ui.md A6, UX-A17).
// Every read writes audit rows, so it waits 5 s first, backs off, and gives up after 10 minutes.
import { ApiError, type ClientErrorCode } from "../../api/client";
import { Poller, type PollOutcome, type PollSchedule, type Visibility } from "../../api/polling";

export const TRANSITION_POLL: PollSchedule = { initialMs: 5000, maxMs: 60_000, factor: 1.5 };
export const TRANSITION_POLL_LIMIT_MS = 10 * 60_000;

/** Another identical read cannot fix these. */
const DEFINITIVE = new Set<ClientErrorCode>([
  "authentication_required",
  "permission_denied",
  "resource_not_found",
  "validation_failed",
]);

export interface TransitionPollOptions {
  /** Reads worker + HELD + QUARANTINED; "done" once the completion text applies. */
  run(signal: AbortSignal): Promise<PollOutcome>;
  /** 10 minutes without completion: "Đã dừng tự cập nhật, bấm Làm mới". */
  onExpire(): void;
  onError?(error: unknown): void;
  visibility?: Visibility;
}

export function startTransitionPoll(options: TransitionPollOptions): { stop(): void } {
  let finished = false;
  const poller = new Poller({
    schedule: TRANSITION_POLL,
    visibility: options.visibility,
    async run(signal) {
      let outcome: PollOutcome;
      try {
        outcome = await options.run(signal);
      } catch (error) {
        if (error instanceof ApiError && DEFINITIVE.has(error.code)) {
          options.onError?.(error);
          finish();
          return "done";
        }
        throw error;
      }
      if (outcome === "done") finish();
      return outcome;
    },
    onError: options.onError,
  });
  const first = setTimeout(() => poller.start(), TRANSITION_POLL.initialMs);
  const deadline = setTimeout(() => {
    finish();
    options.onExpire();
  }, TRANSITION_POLL_LIMIT_MS);

  function finish() {
    if (finished) return;
    finished = true;
    clearTimeout(first);
    clearTimeout(deadline);
    poller.stop();
  }

  return { stop: finish };
}
