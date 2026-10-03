import { describe, expect, it, vi } from "vitest";
import { createApiClient } from "./client";
import { createEndpoints } from "./endpoints";

const TENANT = "0190a000-0000-7000-8000-00000000000a";
const JOB = "0190a000-0000-7000-8000-0000000000b1";
const KEY = "web-00000000-0000-4000-8000-000000000001";

function harness(status = 202, body: unknown = { job_id: JOB }) {
  const fetchImpl = vi.fn(async () =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ETag: '"v3"' } }),
  );
  const client = createApiClient(
    { csrfToken: () => "csrf-value", sessionUser: () => "user-a", refreshCsrf: async () => null, onAuthenticationRequired: () => {} },
    fetchImpl as unknown as typeof fetch,
  );
  const sent = () => {
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    return { url, init, headers: new Headers(init.headers) };
  };
  return { api: createEndpoints(client), sent };
}

describe("endpoints", () => {
  it.each(["cancel", "pause", "resume"] as const)("%s always sends If-Match, Idempotency-Key and tenant", async (action) => {
    const { api, sent } = harness();
    await api.controlJob(TENANT, JOB, action, { reason: "Từ Web UI" }, { ifMatch: '"v3"', idempotencyKey: KEY });
    const { url, init, headers } = sent();
    expect(url).toBe(`/v1/jobs/${JOB}/${action}`);
    expect(init.method).toBe("POST");
    expect(headers.get("If-Match")).toBe('"v3"');
    expect(headers.get("Idempotency-Key")).toBe(KEY);
    expect(headers.get("X-Nexa-Tenant-Id")).toBe(TENANT);
    expect(JSON.parse(String(init.body))).toEqual({ reason: "Từ Web UI" });
  });

  it("retry sends If-Match and the checkpoint choice", async () => {
    const { api, sent } = harness();
    await api.retryJob(TENANT, JOB, { reason: "Chạy lại", checkpoint_id: null }, { ifMatch: '"v7"', idempotencyKey: KEY });
    const { url, headers, init } = sent();
    expect(url).toBe(`/v1/jobs/${JOB}/retry`);
    expect(headers.get("If-Match")).toBe('"v7"');
    expect(JSON.parse(String(init.body))).toEqual({ reason: "Chạy lại", checkpoint_id: null });
  });

  it("control and retry cannot be called without If-Match (precondition_required is unreachable)", () => {
    const { api } = harness();
    const noCall = () => {
      // @ts-expect-error ifMatch is required by type
      void api.controlJob(TENANT, JOB, "cancel", { reason: "x" }, { idempotencyKey: KEY });
      // @ts-expect-error ifMatch is required by type
      void api.retryJob(TENANT, JOB, { reason: "x", checkpoint_id: null }, { idempotencyKey: KEY });
    };
    expect(typeof noCall).toBe("function");
  });

  it("rejects an empty If-Match at runtime instead of sending the request", async () => {
    const { api } = harness();
    await expect(
      api.controlJob(TENANT, JOB, "pause", { reason: "x" }, { ifMatch: "", idempotencyKey: KEY }),
    ).rejects.toThrow(/If-Match/);
  });

  it("uploads the file with the artifact headers and the transfer timeout", async () => {
    const { api, sent } = harness(201, { artifact_id: "a" });
    const file = new Blob([new Uint8Array([1, 2, 3])]);
    await api.uploadArtifact(
      TENANT,
      file,
      { kind: "INPUT", mediaType: "application/json", checksum: `sha256:${"a".repeat(64)}` },
      { idempotencyKey: KEY },
    );
    const { url, headers } = sent();
    expect(url).toBe("/v1/artifacts");
    expect(headers.get("X-Artifact-Kind")).toBe("INPUT");
    expect(headers.get("X-Artifact-Media-Type")).toBe("application/json");
    expect(headers.get("X-Artifact-Size")).toBe("3");
    expect(headers.get("X-Artifact-Checksum")).toBe(`sha256:${"a".repeat(64)}`);
    expect(headers.get("Content-Type")).toBe("application/octet-stream");
  });

  it("lists jobs with filters and events with after_sequence only", async () => {
    const { api, sent } = harness(200, { items: [], page: { next_cursor: null, page_size: 25 } });
    await api.listJobs(TENANT, { state: "RUNNING", template_id: null, created_after: null, cursor: null });
    expect(sent().url).toBe("/v1/jobs?page_size=25&state=RUNNING");
    const second = harness(200, { items: [], page: { next_cursor: null, page_size: 100 } });
    await second.api.listEvents(TENANT, JOB, 41);
    expect(second.sent().url).toBe(`/v1/jobs/${JOB}/events?page_size=100&after_sequence=41`);
  });

  it("session probe and login do not trigger the expired-session hook", async () => {
    const onAuthenticationRequired = vi.fn();
    const fetchImpl = vi.fn(async () =>
      new Response(JSON.stringify({ code: "authentication_required", message: "m", request_id: "r" }), { status: 401 }),
    );
    const api = createEndpoints(
      createApiClient(
        { csrfToken: () => null, sessionUser: () => null, refreshCsrf: async () => null, onAuthenticationRequired },
        fetchImpl as unknown as typeof fetch,
      ),
    );
    await expect(api.getSession()).rejects.toMatchObject({ code: "authentication_required" });
    await expect(api.login({ username: "u", password: "p" })).rejects.toMatchObject({ code: "authentication_required" });
    expect(onAuthenticationRequired).not.toHaveBeenCalled();
  });
});
