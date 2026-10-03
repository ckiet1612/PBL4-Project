import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  SESSION_SWITCHED,
  createApiClient,
  isAbortError,
  parseRetryAfter,
  type ClientHooks,
} from "./client";

interface Recorded {
  url: string;
  method: string;
  headers: Headers;
  body: BodyInit | null | undefined;
  credentials: RequestCredentials | undefined;
}

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function envelope(code: string, extra: Record<string, unknown> = {}) {
  return {
    code,
    message: `server says ${code}`,
    request_id: "01890a5d-ac96-7000-8000-00000000abcd",
    ...extra,
  };
}

function setup(responses: Array<Response | (() => Promise<Response>)>, hooks: Partial<ClientHooks> = {}) {
  const calls: Recorded[] = [];
  const queue = [...responses];
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: String(input),
      method: init?.method ?? "GET",
      headers: new Headers(init?.headers),
      body: init?.body,
      credentials: init?.credentials,
    });
    const next = queue.shift();
    if (next === undefined) throw new Error("unexpected request");
    return typeof next === "function" ? next() : next;
  });
  const fullHooks: ClientHooks = {
    csrfToken: () => "csrf-in-memory",
    sessionUser: () => "user-a",
    refreshCsrf: async () => "csrf-refreshed",
    onAuthenticationRequired: vi.fn(),
    ...hooks,
  };
  const client = createApiClient(fullHooks, fetchImpl as unknown as typeof fetch);
  return { client, calls, hooks: fullHooks, fetchImpl };
}

describe("api client headers", () => {
  it("reads use /v1 on the same origin with the tenant header and no mutation headers", async () => {
    const { client, calls } = setup([jsonResponse(200, { items: [] }, { ETag: '"v3"', "X-Request-Id": "rid-1" })]);
    const response = await client.request<{ items: unknown[] }>({
      path: "/jobs",
      tenantId: "tenant-1",
      query: { page_size: 25, cursor: null, state: undefined, template_id: "cpu-iterative" },
    });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/v1/jobs?page_size=25&template_id=cpu-iterative");
    expect(calls[0].method).toBe("GET");
    expect(calls[0].credentials).toBe("same-origin");
    expect(calls[0].headers.get("X-Nexa-Tenant-Id")).toBe("tenant-1");
    expect(calls[0].headers.get("X-CSRF-Token")).toBeNull();
    expect(calls[0].headers.get("Idempotency-Key")).toBeNull();
    expect(calls[0].headers.get("If-Match")).toBeNull();
    expect(response.etag).toBe('"v3"');
    expect(response.requestId).toBe("rid-1");
    expect(response.data).toEqual({ items: [] });
  });

  it("user-scoped reads carry no tenant header", async () => {
    const { client, calls } = setup([jsonResponse(200, { items: [] })]);
    await client.request({ path: "/tokens", query: { page_size: 50 } });
    expect(calls[0].headers.get("X-Nexa-Tenant-Id")).toBeNull();
  });

  it("mutations send CSRF, Idempotency-Key, If-Match and a JSON body", async () => {
    const { client, calls } = setup([jsonResponse(202, { job_id: "j" }, { ETag: '"v8"' })]);
    await client.request({
      method: "POST",
      path: "/jobs/j/cancel",
      tenantId: "tenant-1",
      json: { reason: "Hủy từ Web UI" },
      idempotencyKey: "web-0123456789abcdef",
      ifMatch: '"v7"',
    });
    const headers = calls[0].headers;
    expect(calls[0].method).toBe("POST");
    expect(headers.get("X-CSRF-Token")).toBe("csrf-in-memory");
    expect(headers.get("Idempotency-Key")).toBe("web-0123456789abcdef");
    expect(headers.get("If-Match")).toBe('"v7"');
    expect(headers.get("X-Nexa-Tenant-Id")).toBe("tenant-1");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(calls[0].body).toBe(JSON.stringify({ reason: "Hủy từ Web UI" }));
  });

  it("binary uploads send the blob as octet-stream with the extra headers", async () => {
    const { client, calls } = setup([jsonResponse(201, { artifact_id: "a" })]);
    const blob = new Blob(["abc"]);
    await client.request({
      method: "POST",
      path: "/artifacts",
      tenantId: "t",
      body: blob,
      idempotencyKey: "web-0123456789abcdef",
      headers: { "X-Artifact-Kind": "INPUT" },
    });
    expect(calls[0].body).toBe(blob);
    expect(calls[0].headers.get("Content-Type")).toBe("application/octet-stream");
    expect(calls[0].headers.get("X-Artifact-Kind")).toBe("INPUT");
  });

  it("204 responses resolve with null data", async () => {
    const { client } = setup([new Response(null, { status: 204 })]);
    const response = await client.request({ method: "DELETE", path: "/tokens/x", idempotencyKey: "web-0123456789abcdef" });
    expect(response.status).toBe(204);
    expect(response.data).toBeNull();
  });

  it("blob responses keep the ETag for checksum verification", async () => {
    const { client } = setup([
      new Response("bytes", { status: 200, headers: { ETag: '"sha256:ab"', "Content-Type": "application/octet-stream" } }),
    ]);
    const response = await client.request<Blob>({ path: "/artifacts/a/content", tenantId: "t", expect: "blob" });
    expect(response.etag).toBe('"sha256:ab"');
    expect(await response.data.text()).toBe("bytes");
  });
});

describe("api client errors", () => {
  it("parses the error envelope, reason and Retry-After", async () => {
    const { client } = setup([
      jsonResponse(429, envelope("quota_exceeded", { reason: "tenant_outstanding" }), { "Retry-After": "7" }),
    ]);
    const error = await client.request({ path: "/jobs", tenantId: "t" }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(429);
    expect(apiError.code).toBe("quota_exceeded");
    expect(apiError.requestId).toBe("01890a5d-ac96-7000-8000-00000000abcd");
    expect(apiError.serverMessage).toBe("server says quota_exceeded");
    expect(apiError.reason).toBe("tenant_outstanding");
    expect(apiError.retryAfterSeconds).toBe(7);
  });

  it("a response without an envelope is an unexpected_response carrying the request id header", async () => {
    const { client } = setup([new Response("<html>bad gateway</html>", { status: 502, headers: { "X-Request-Id": "rid-9" } })]);
    const error = (await client.request({ path: "/jobs", tenantId: "t" }).catch((caught: unknown) => caught)) as ApiError;
    expect(error.code).toBe("unexpected_response");
    expect(error.status).toBe(502);
    expect(error.requestId).toBe("rid-9");
  });

  it("network failures become network_error", async () => {
    const { client } = setup([() => Promise.reject(new TypeError("Failed to fetch"))]);
    const error = (await client.request({ path: "/jobs", tenantId: "t" }).catch((caught: unknown) => caught)) as ApiError;
    expect(error.code).toBe("network_error");
    expect(error.status).toBe(0);
  });

  it("401 authentication_required notifies the session once and still rejects", async () => {
    const { client, hooks } = setup([jsonResponse(401, envelope("authentication_required"))]);
    const error = (await client.request({ path: "/jobs", tenantId: "t" }).catch((caught: unknown) => caught)) as ApiError;
    expect(error.code).toBe("authentication_required");
    expect(hooks.onAuthenticationRequired).toHaveBeenCalledTimes(1);
  });

  it("session probes and login do not trigger the expiry handler", async () => {
    const { client, hooks } = setup([jsonResponse(401, envelope("authentication_required"))]);
    await client.request({ path: "/auth/session", authProbe: true }).catch(() => undefined);
    expect(hooks.onAuthenticationRequired).not.toHaveBeenCalled();
  });

  it("invalid_csrf refreshes the session once and resends the same key and body", async () => {
    const refreshCsrf = vi.fn(async () => "csrf-refreshed");
    const { client, calls, hooks } = setup(
      [jsonResponse(403, envelope("invalid_csrf")), jsonResponse(202, { ok: true })],
      { refreshCsrf },
    );
    const response = await client.request({
      method: "POST",
      path: "/jobs",
      tenantId: "t",
      json: { spec: {} },
      idempotencyKey: "web-0123456789abcdef",
    });
    expect(response.status).toBe(202);
    expect(calls).toHaveLength(2);
    expect(calls[1].headers.get("X-CSRF-Token")).toBe("csrf-refreshed");
    expect(calls[1].headers.get("Idempotency-Key")).toBe("web-0123456789abcdef");
    expect(calls[1].body).toBe(calls[0].body);
    // The refresh is asked for the user the request was sent as (B18-RV03).
    expect(refreshCsrf).toHaveBeenCalledWith("user-a");
    expect(hooks.onAuthenticationRequired).not.toHaveBeenCalled();
  });

  it("a second invalid_csrf sends the user to login", async () => {
    const { client, calls, hooks } = setup([
      jsonResponse(403, envelope("invalid_csrf")),
      jsonResponse(403, envelope("invalid_csrf")),
    ]);
    const error = (await client
      .request({ method: "POST", path: "/jobs", tenantId: "t", json: {}, idempotencyKey: "web-0123456789abcdef" })
      .catch((caught: unknown) => caught)) as ApiError;
    expect(error.code).toBe("invalid_csrf");
    expect(calls).toHaveLength(2);
    expect(hooks.onAuthenticationRequired).toHaveBeenCalledTimes(1);
  });
});

describe("api client when the refreshed session belongs to another user (B18-R14)", () => {
  it("does not resend the mutation and does not send the new user to login", async () => {
    const { client, calls, hooks } = setup([jsonResponse(403, envelope("invalid_csrf"))], {
      refreshCsrf: async () => SESSION_SWITCHED,
    });
    const error = (await client
      .request({ method: "POST", path: "/admin/tenants", json: { slug: "x" }, idempotencyKey: "web-0123456789abcdef" })
      .catch((caught: unknown) => caught)) as ApiError;
    expect(error.code).toBe("invalid_csrf");
    expect(calls).toHaveLength(1);
    expect(hooks.onAuthenticationRequired).not.toHaveBeenCalled();
  });
});

describe("api client timeouts and aborts", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function hangingFetch() {
    return (_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(init.signal?.reason));
      });
  }

  it("times out with a timeout error", async () => {
    const client = createApiClient(
      { csrfToken: () => null, sessionUser: () => null, refreshCsrf: async () => null, onAuthenticationRequired: () => undefined },
      hangingFetch() as typeof fetch,
    );
    const pending = client.request({ path: "/jobs", tenantId: "t", timeoutMs: 1000 }).catch((caught: unknown) => caught);
    await vi.advanceTimersByTimeAsync(1000);
    const error = (await pending) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("timeout");
  });

  it("a caller abort rejects with an AbortError, not an ApiError", async () => {
    const client = createApiClient(
      { csrfToken: () => null, sessionUser: () => null, refreshCsrf: async () => null, onAuthenticationRequired: () => undefined },
      hangingFetch() as typeof fetch,
    );
    const controller = new AbortController();
    const pending = client.request({ path: "/jobs", tenantId: "t", signal: controller.signal }).catch((caught: unknown) => caught);
    controller.abort();
    const error = await pending;
    expect(isAbortError(error)).toBe(true);
  });
});

describe("api client never logs", () => {
  it("does not write to the console on success or failure", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((method) =>
      vi.spyOn(console, method).mockImplementation(() => undefined),
    );
    const { client } = setup([
      jsonResponse(200, { ok: true }),
      jsonResponse(500, envelope("internal_error")),
      () => Promise.reject(new TypeError("Failed to fetch")),
    ]);
    await client.request({ path: "/jobs", tenantId: "t" });
    await client.request({ method: "POST", path: "/jobs", json: { password: "x" }, idempotencyKey: "web-0123456789abcdef" }).catch(() => undefined);
    await client.request({ path: "/jobs" }).catch(() => undefined);
    for (const spy of spies) expect(spy).not.toHaveBeenCalled();
  });
});

describe("parseRetryAfter", () => {
  it("accepts delta seconds and HTTP dates, rejects garbage", () => {
    expect(parseRetryAfter("3", 0)).toBe(3);
    expect(parseRetryAfter("0", 0)).toBe(0);
    expect(parseRetryAfter(null, 0)).toBeNull();
    expect(parseRetryAfter("soon", 0)).toBeNull();
    const now = Date.parse("2026-10-01T00:00:00Z");
    expect(parseRetryAfter("Thu, 01 Oct 2026 00:00:05 GMT", now)).toBe(5);
    expect(parseRetryAfter("Thu, 01 Oct 2026 00:00:00 GMT", now + 9000)).toBe(0);
  });
});
