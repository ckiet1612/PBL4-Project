import { describe, expect, it, vi } from "vitest";

import { ApiError, SESSION_SWITCHED, createApiClient } from "../api/client";
import type { BrowserSession } from "../api/types";
import { createCsrfRefresher } from "./csrfRefresh";

function session(userId: string, csrf: string): BrowserSession {
  return {
    user_id: userId,
    username: `${userId}@example.test`,
    display_name: userId,
    system_role: "SYSTEM_ADMIN",
    memberships: [],
    csrf_token: csrf,
    expires_at: "2026-10-03T12:00:00.000Z",
  } as unknown as BrowserSession;
}

function invalidCsrf() {
  return new Response(
    JSON.stringify({ code: "invalid_csrf", message: "csrf", request_id: "01890a5d-ac96-7000-8000-00000000abcd" }),
    { status: 403, headers: { "Content-Type": "application/json" } },
  );
}

/** A tab whose in-memory user is `initialUser`; GET /auth/session answers `served` after a tick. */
function tab(initialUser: string | null, served: BrowserSession | null) {
  let user = initialUser;
  let csrf: string | null = initialUser === null ? null : "csrf-old";
  let resolveRead: (() => void) | null = null;
  const gate = new Promise<void>((resolve) => {
    resolveRead = resolve;
  });
  const readSession = vi.fn(async () => {
    await gate;
    return served;
  });
  const adopt = vi.fn((next: BrowserSession) => {
    user = next.user_id;
    csrf = next.csrf_token;
  });
  const switched = vi.fn();
  const refreshCsrf = createCsrfRefresher({ readSession, currentUser: () => user, adopt, switched });
  const sent: Array<{ path: string; csrf: string | null }> = [];
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    sent.push({ path: String(input), csrf: new Headers(init?.headers).get("X-CSRF-Token") });
    return invalidCsrf();
  });
  const onAuthenticationRequired = vi.fn();
  const client = createApiClient(
    { csrfToken: () => csrf, sessionUser: () => user, refreshCsrf, onAuthenticationRequired },
    fetchImpl as unknown as typeof fetch,
  );
  const mutate = (path: string) =>
    client
      .request({ method: "POST", path, json: {}, idempotencyKey: `web-${path.length}0123456789abcdef` })
      .catch((caught: unknown) => caught);
  return { client, mutate, sent, readSession, adopt, switched, onAuthenticationRequired, release: () => resolveRead?.() };
}

describe("CSRF refresh is bound to the user a request was sent as (B18-RV03)", () => {
  it("two concurrent mutations of A are not resent when the session now belongs to B", async () => {
    const t = tab("user-a", session("user-b", "csrf-b"));
    const first = t.mutate("/admin/tenants");
    const second = t.mutate("/admin/users");
    // Both mutations are rejected and waiting on the one shared session read.
    await vi.waitFor(() => expect(t.sent).toHaveLength(2));
    t.release();
    const errors = (await Promise.all([first, second])) as ApiError[];
    expect(errors.map((error) => error.code)).toEqual(["invalid_csrf", "invalid_csrf"]);
    expect(t.sent).toHaveLength(2);
    expect(t.sent.every((call) => call.csrf === "csrf-old")).toBe(true);
    expect(t.readSession).toHaveBeenCalledTimes(1);
    expect(t.switched).toHaveBeenCalledTimes(1);
    expect(t.onAuthenticationRequired).not.toHaveBeenCalled();
  });

  it("a mutation sent with no known user is never resent", async () => {
    const t = tab(null, session("user-b", "csrf-b"));
    const pending = t.mutate("/admin/tenants");
    await vi.waitFor(() => expect(t.sent).toHaveLength(1));
    t.release();
    expect(((await pending) as ApiError).code).toBe("invalid_csrf");
    expect(t.sent).toHaveLength(1);
    expect(t.onAuthenticationRequired).not.toHaveBeenCalled();
  });

  it("the same user gets the new token and both mutations are resent once", async () => {
    const t = tab("user-a", session("user-a", "csrf-a2"));
    const first = t.mutate("/admin/tenants");
    const second = t.mutate("/admin/users");
    await vi.waitFor(() => expect(t.sent).toHaveLength(2));
    t.release();
    await Promise.all([first, second]);
    expect(t.sent).toHaveLength(4);
    expect(t.sent.slice(2).map((call) => call.csrf)).toEqual(["csrf-a2", "csrf-a2"]);
    expect(t.readSession).toHaveBeenCalledTimes(1);
    expect(t.switched).not.toHaveBeenCalled();
  });

  it("a later refresh reads the session again instead of reusing the settled one", async () => {
    const readSession = vi.fn(async () => session("user-a", "csrf-a2"));
    const refresh = createCsrfRefresher({
      readSession,
      currentUser: () => "user-a",
      adopt: () => undefined,
      switched: () => undefined,
    });
    expect(await refresh("user-a")).toBe("csrf-a2");
    expect(await refresh("user-a")).toBe("csrf-a2");
    expect(readSession).toHaveBeenCalledTimes(2);
  });

  it("no session (expired) resolves to null; a switch observed twice notifies once", async () => {
    const none = createCsrfRefresher({
      readSession: async () => null,
      currentUser: () => "user-a",
      adopt: () => undefined,
      switched: () => undefined,
    });
    expect(await none("user-a")).toBeNull();

    let user: string | null = "user-a";
    const switched = vi.fn();
    const refresh = createCsrfRefresher({
      readSession: async () => session("user-b", "csrf-b"),
      currentUser: () => user,
      adopt: (next) => {
        user = next.user_id;
      },
      switched,
    });
    expect(await refresh("user-a")).toBe(SESSION_SWITCHED);
    expect(await refresh("user-a")).toBe(SESSION_SWITCHED);
    expect(switched).toHaveBeenCalledTimes(1);
  });
});
