import { describe, expect, it } from "vitest";
import { ApiError, type ClientErrorCode } from "../../api/client";
import { AdminIntent, INTENT_BUSY, IntentSlots } from "./intent";

function apiError(code: ClientErrorCode, status: number): ApiError {
  return new ApiError({ status, code, requestId: null, serverMessage: null, reason: null, retryAfterSeconds: null });
}

type Options = { idempotencyKey: string; ifMatch: string | null };

function recorder(outcomes: (unknown | Error)[]) {
  const calls: Options[] = [];
  const send = async (options: Options) => {
    calls.push(options);
    const next = outcomes.shift();
    if (next instanceof Error) throw next;
    return next;
  };
  return { calls, send };
}

describe("admin intent: one Idempotency-Key per intent, If-Match of the latest read", () => {
  it("resends the same key and the first If-Match after a network error or an empty proxy 5xx", async () => {
    const intent = new AdminIntent();
    const { calls, send } = recorder([apiError("network_error", 0), apiError("unexpected_response", 502), "ok"]);
    await expect(intent.send("rename:A", '"v1"', send)).rejects.toThrow();
    await expect(intent.send("rename:A", '"v2"', send)).rejects.toThrow();
    await expect(intent.send("rename:A", '"v3"', send)).resolves.toBe("ok");
    expect(new Set(calls.map((call) => call.idempotencyKey)).size).toBe(1);
    expect(calls.map((call) => call.ifMatch)).toEqual(['"v1"', '"v1"', '"v1"']);
  });

  it("starts a new intent after success, after a definitive error and when the body changes", async () => {
    const intent = new AdminIntent();
    const { calls, send } = recorder(["ok", apiError("version_conflict", 412), apiError("state_conflict", 409), "ok", "ok"]);
    await intent.send("a", '"v1"', send);
    await expect(intent.send("a", '"v2"', send)).rejects.toThrow();
    await expect(intent.send("a", '"v3"', send)).rejects.toThrow();
    await intent.send("a", '"v4"', send);
    await intent.send("b", '"v5"', send);
    expect(new Set(calls.map((call) => call.idempotencyKey)).size).toBe(5);
    expect(calls.map((call) => call.ifMatch)).toEqual(['"v1"', '"v2"', '"v3"', '"v4"', '"v5"']);
  });

  it("a changed body is a new intent even while the previous one is unresolved", async () => {
    const intent = new AdminIntent();
    const { calls, send } = recorder([apiError("network_error", 0), "ok"]);
    await expect(intent.send("a", '"v1"', send)).rejects.toThrow();
    await intent.send("b", '"v2"', send);
    expect(calls[0].idempotencyKey).not.toBe(calls[1].idempotencyKey);
    expect(calls[1].ifMatch).toBe('"v2"');
  });

  it("ignores a second confirm while the first request is in flight (double click)", async () => {
    const intent = new AdminIntent();
    let release: (value: string) => void = () => {};
    const calls: Options[] = [];
    const slow = (options: Options) => {
      calls.push(options);
      return new Promise<string>((resolve) => {
        release = resolve;
      });
    };
    const first = intent.send("create", null, slow);
    await expect(intent.send("create", null, slow)).resolves.toBe(INTENT_BUSY);
    expect(intent.busy).toBe(true);
    release("created");
    await expect(first).resolves.toBe("created");
    expect(calls).toHaveLength(1);
    expect(intent.busy).toBe(false);
  });

  it("create requests carry no If-Match", async () => {
    const intent = new AdminIntent();
    const { calls, send } = recorder(["ok"]);
    await intent.send("create", null, send);
    expect(calls[0].ifMatch).toBeNull();
    expect(calls[0].idempotencyKey).toMatch(/^[!-~]{16,128}$/);
  });
});

describe("intent slots outlive their dialog (B18-RV10)", () => {
  it("cancel and reopen after an unknown outcome restores the sent values and resends the same key", async () => {
    const slots = new IntentSlots<{ reason: string }>();
    const { calls, send } = recorder([apiError("network_error", 0), "ok"]);
    const first = slots.slot("drain");
    first.sent = { reason: "maintenance" };
    await expect(first.intent.send("drain:maintenance", '"v1"', send)).rejects.toThrow();
    // The dialog is cancelled (unmounted) and opened again for the same action.
    const reopened = slots.slot("drain");
    expect(reopened.reopen).toEqual({ reason: "maintenance" });
    await reopened.intent.send("drain:maintenance", '"v2"', send);
    expect(calls.map((call) => call.idempotencyKey)).toEqual([calls[0].idempotencyKey, calls[0].idempotencyKey]);
    expect(calls[1].ifMatch).toBe('"v1"');
  });

  it("nothing is restored before a send, after success, after a definitive error or for another slot", async () => {
    const slots = new IntentSlots<{ reason: string }>();
    const { send } = recorder(["ok", apiError("version_conflict", 412)]);
    expect(slots.slot("drain").reopen).toBeNull();
    const drain = slots.slot("drain");
    drain.sent = { reason: "a" };
    await drain.intent.send("drain:a", '"v1"', send);
    expect(slots.slot("drain").reopen).toBeNull();
    drain.sent = { reason: "b" };
    await expect(drain.intent.send("drain:b", '"v2"', send)).rejects.toThrow();
    expect(slots.slot("drain").reopen).toBeNull();
    expect(slots.slot("disable").reopen).toBeNull();
  });

  it("a read while the request is in flight keeps the values for an unknown outcome", async () => {
    const slot = new IntentSlots<{ reason: string }>().slot("drain");
    let fail: (error: Error) => void = () => {};
    slot.sent = { reason: "maintenance" };
    const pending = slot.intent.send("drain:maintenance", '"v1"', () => new Promise((_, reject) => (fail = reject)));
    expect(slot.reopen).toBeNull();
    fail(apiError("timeout", 0));
    await expect(pending).rejects.toThrow();
    expect(slot.reopen).toEqual({ reason: "maintenance" });
  });
});
