// One admin form or dialog = one AdminIntent. The same body keeps its Idempotency-Key and the
// If-Match it was first sent with until the outcome is definitive (IntentTracker); a second
// confirm while a request is in flight sends nothing. No automatic resend.
import { IntentTracker, withInProgressRetry } from "../../api/idempotency";

export const INTENT_BUSY = Symbol("intent-busy");

export interface IntentOptions {
  idempotencyKey: string;
  /** ETag of the latest GET; null for create requests. */
  ifMatch: string | null;
}

export class AdminIntent {
  private readonly tracker = new IntentTracker();
  private pending: { key: string; ifMatch: string | null } | null = null;
  private inFlight = false;

  get busy(): boolean {
    return this.inFlight;
  }

  /** A send ended with an unknown outcome: the same body must reuse the key (and If-Match). */
  get unsettled(): boolean {
    return !this.inFlight && this.pending !== null && this.tracker.unsettled;
  }

  async send<T>(
    fingerprint: string,
    ifMatch: string | null,
    request: (options: IntentOptions) => Promise<T>,
  ): Promise<T | typeof INTENT_BUSY> {
    if (this.inFlight) return INTENT_BUSY;
    this.inFlight = true;
    const key = this.tracker.keyFor(fingerprint);
    if (this.pending?.key !== key) this.pending = { key, ifMatch };
    const options = { idempotencyKey: key, ifMatch: this.pending.ifMatch };
    try {
      const result = await withInProgressRetry(() => request(options));
      this.tracker.settle();
      this.pending = null;
      return result;
    } catch (error) {
      this.tracker.settle(error);
      if (!this.tracker.unsettled) this.pending = null;
      throw error;
    } finally {
      this.inFlight = false;
    }
  }
}

/** One dialog's intent and the values it last sent; kept by the page across cancel/reopen. */
export class IntentSlot<D> {
  readonly intent = new AdminIntent();
  sent: D | null = null;

  /** Values to reopen the dialog with: the last sent ones while their outcome is unknown. */
  get reopen(): D | null {
    // Once the outcome is known the values are of no use; drop them (a draft may hold a password).
    if (this.intent.busy) return null;
    if (!this.intent.unsettled) this.sent = null;
    return this.sent;
  }
}

/**
 * Slots by dialog key (B18-RV10). A dialog that is cancelled after a timeout and opened again
 * gets its values back, so confirming resends the same Idempotency-Key instead of a duplicate.
 */
export class IntentSlots<D> {
  private readonly slots = new Map<string, IntentSlot<D>>();

  slot(key: string): IntentSlot<D> {
    let slot = this.slots.get(key);
    if (slot === undefined) {
      slot = new IntentSlot<D>();
      this.slots.set(key, slot);
    }
    return slot;
  }
}
