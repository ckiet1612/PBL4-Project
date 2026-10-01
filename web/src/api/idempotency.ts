// One user intent = one Idempotency-Key. Keys live in memory only.
import { ApiError, type ClientErrorCode } from "./client";

export const IDEMPOTENCY_KEY_PATTERN = /^[!-~]{16,128}$/;

export function newIdempotencyKey(): string {
  return `web-${crypto.randomUUID()}`;
}

/** Outcomes after which the same intent may be resent: the server may not have decided yet. */
const RESEND_SAME_KEY = new Set<ClientErrorCode>(["network_error", "timeout", "idempotency_in_progress", "rate_limited"]);

/**
 * Tracks the pending intent of one form or dialog. The same body keeps its key until the
 * request settles definitively; a changed body, a success or a definitive error starts a
 * new intent with a new key.
 */
export class IntentTracker {
  private pending: { key: string; fingerprint: string } | null = null;

  keyFor(fingerprint: string): string {
    if (this.pending?.fingerprint !== fingerprint) {
      this.pending = { key: newIdempotencyKey(), fingerprint };
    }
    return this.pending.key;
  }

  settle(error?: unknown): void {
    if (error instanceof ApiError && (RESEND_SAME_KEY.has(error.code) || isProxyFailure(error))) return;
    this.pending = null;
  }
}

/**
 * A 5xx without the API error envelope was produced by the proxy (for example Caddy's empty
 * 502 when the API died after committing): the outcome is unknown, so the same key must be
 * resent to replay a committed response instead of creating a duplicate. A 5xx with an
 * envelope is an answer the server stored under the key, so it stays definitive.
 */
function isProxyFailure(error: ApiError): boolean {
  return error.code === "unexpected_response" && error.status >= 500;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** idempotency_in_progress: wait Retry-After (at least 1 s) and resend, a bounded number of times. */
export async function withInProgressRetry<T>(send: () => Promise<T>, maxAttempts = 3): Promise<T> {
  for (let attempt = 1; ; attempt += 1) {
    try {
      return await send();
    } catch (error) {
      if (!(error instanceof ApiError) || error.code !== "idempotency_in_progress" || attempt >= maxAttempts) {
        throw error;
      }
      await sleep(Math.max(1, error.retryAfterSeconds ?? 1) * 1000);
    }
  }
}
