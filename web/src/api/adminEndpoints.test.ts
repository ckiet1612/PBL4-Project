import { describe, expect, it, vi } from "vitest";
import { createApiClient } from "./client";
import { createEndpoints } from "./endpoints";

const ID = "0190a000-0000-7000-8000-0000000000c1";
const USER = "0190a000-0000-7000-8000-0000000000d1";
const KEY = "web-00000000-0000-4000-8000-000000000002";

function harness(status = 200, body: unknown = {}) {
  const fetchImpl = vi.fn(async () =>
    status === 204
      ? new Response(null, { status, headers: { ETag: '"v2"' } })
      : new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ETag: '"v2"' } }),
  );
  const client = createApiClient(
    { csrfToken: () => "csrf-value", sessionUser: () => "user-a", refreshCsrf: async () => null, onAuthenticationRequired: () => {} },
    fetchImpl as unknown as typeof fetch,
  );
  const sent = (index = 0) => {
    const [url, init] = fetchImpl.mock.calls[index] as unknown as [string, RequestInit];
    return { url, init, headers: new Headers(init.headers), body: init.body ? JSON.parse(String(init.body)) : null };
  };
  return { admin: createEndpoints(client).admin, sent, fetchImpl };
}

describe("admin endpoints (no tenant header; docs/web-ui.md A5)", () => {
  it("reads use the documented page sizes and never send a tenant header", async () => {
    const { admin, sent } = harness();
    const signal = new AbortController().signal;
    await admin.listWorkers(null, signal);
    await admin.listWorkers(null, signal, 10);
    await admin.listAllocations("QUARANTINED", signal);
    await admin.listJobs(
      { tenant_id: ID, user_id: null, state: "QUEUED", waiting_reason: null, created_after: null, cursor: "c".repeat(16) },
      signal,
    );
    await admin.listTenants(null, signal, 100);
    await admin.listUsers("u".repeat(16), signal);
    await admin.listMemberships(ID, null, signal);
    await admin.queryFairness({ from: "2026-09-30T00:00:00.000Z", to: "2026-10-01T00:00:00.000Z", bucket_seconds: 3600, tenant_id: null }, signal);
    await admin.listRecoveryEvents({ from: "a", to: "b", cursor: null }, signal, 10);
    await admin.listAudit({ from: "a", to: "b", action: "admin.worker.drain", cursor: null }, signal);
    const urls = Array.from({ length: 10 }, (_, i) => sent(i));
    expect(urls.map((u) => u.url)).toEqual([
      "/v1/admin/workers?page_size=25",
      "/v1/admin/workers?page_size=10",
      "/v1/admin/allocations?page_size=100&state=QUARANTINED",
      `/v1/admin/jobs?page_size=25&tenant_id=${ID}&state=QUEUED&cursor=${"c".repeat(16)}`,
      "/v1/admin/tenants?page_size=100",
      `/v1/admin/users?page_size=25&cursor=${"u".repeat(16)}`,
      `/v1/admin/tenants/${ID}/memberships?page_size=25`,
      "/v1/admin/fairness?from=2026-09-30T00%3A00%3A00.000Z&to=2026-10-01T00%3A00%3A00.000Z&bucket_seconds=3600",
      "/v1/admin/recovery-events?page_size=10&from=a&to=b",
      "/v1/admin/audit?page_size=25&from=a&to=b&action=admin.worker.drain",
    ]);
    for (const request of urls) {
      expect(request.headers.has("X-Nexa-Tenant-Id")).toBe(false);
      expect(request.headers.has("Idempotency-Key")).toBe(false);
    }
  });

  it("worker actions send reason, If-Match and Idempotency-Key", async () => {
    const { admin, sent } = harness(202);
    await admin.workerAction(ID, "drain", { reason: "bảo trì" }, { ifMatch: '"v4"', idempotencyKey: KEY });
    const { url, init, headers, body } = sent();
    expect(url).toBe(`/v1/admin/workers/${ID}/drain`);
    expect(init.method).toBe("POST");
    expect(headers.get("If-Match")).toBe('"v4"');
    expect(headers.get("Idempotency-Key")).toBe(KEY);
    expect(headers.has("X-Nexa-Tenant-Id")).toBe(false);
    expect(body).toEqual({ reason: "bảo trì" });
  });

  it("guarded mutations refuse to send without If-Match", async () => {
    const { admin, fetchImpl } = harness();
    await expect(admin.updateTenant(ID, { display_name: "x" }, { ifMatch: "", idempotencyKey: KEY })).rejects.toThrow();
    await expect(admin.workerAction(ID, "enable", { reason: "x" }, { ifMatch: "", idempotencyKey: KEY })).rejects.toThrow();
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("membership changes carry the MembershipSet ETag", async () => {
    const { admin, sent } = harness(200, { tenant_id: ID, role: "MEMBER" });
    await admin.upsertMembership(ID, { user_id: USER, role: "MEMBER" }, { ifMatch: '"v1"', idempotencyKey: KEY });
    expect(sent().url).toBe(`/v1/admin/tenants/${ID}/memberships`);
    expect(sent().body).toEqual({ user_id: USER, role: "MEMBER" });
    expect(sent().headers.get("If-Match")).toBe('"v1"');

    const deleted = harness(204);
    const response = await deleted.admin.deleteMembership(ID, USER, { ifMatch: '"v2"', idempotencyKey: KEY });
    expect(deleted.sent().url).toBe(`/v1/admin/tenants/${ID}/memberships/${USER}`);
    expect(deleted.sent().init.method).toBe("DELETE");
    expect(response.etag).toBe('"v2"');
  });

  it("creates and patches send exactly the given fields", async () => {
    const { admin, sent } = harness(201);
    await admin.createTenant({ slug: "lab-a", display_name: "Lab A" }, { idempotencyKey: KEY });
    await admin.updatePolicy({ global_outstanding_limit: 500 }, { ifMatch: '"v3"', idempotencyKey: KEY });
    await admin.updateTenantPolicy(ID, { weight: 0.5 }, { ifMatch: '"v3"', idempotencyKey: KEY });
    await admin.updateUser(USER, { enabled: false }, { ifMatch: '"v3"', idempotencyKey: KEY });
    expect([0, 1, 2, 3].map((i) => [sent(i).init.method, sent(i).url, sent(i).body])).toEqual([
      ["POST", "/v1/admin/tenants", { slug: "lab-a", display_name: "Lab A" }],
      ["PATCH", "/v1/admin/policy", { global_outstanding_limit: 500 }],
      ["PATCH", `/v1/admin/tenants/${ID}/policy`, { weight: 0.5 }],
      ["PATCH", `/v1/admin/users/${USER}`, { enabled: false }],
    ]);
  });
});
