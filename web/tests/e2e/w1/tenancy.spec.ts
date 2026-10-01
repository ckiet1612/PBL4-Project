import { fixture } from "../fixture";
import { Api, contextAs, cpuSpec, expect, jobPath, test, type Job } from "../support";

// Scenarios 4–5: tenant isolation and roles, always checked against the real backend.

let jobA: Job;
let jobB: Job;

test.beforeAll(async () => {
  const seed = fixture();
  const memberA = await Api.as("member_a");
  const memberB = await Api.as("member_b");
  jobA = await memberA.submit(seed.tenants.a, cpuSpec(seed.artifacts.a_input.artifact_id, 401));
  jobB = await memberB.submit(seed.tenants.b, cpuSpec(seed.artifacts.b_input.artifact_id, 402));
  await memberA.dispose();
  await memberB.dispose();
});

test("4. two tenants: B cannot see, download or address A's job and data; multi switches tenant", async ({
  browser,
}) => {
  const seed = fixture();
  const context = await contextAs(browser, "member_b");
  const page = await context.newPage();

  // A's job under B's tenant: the API answers 404 and the page says "not found".
  await page.goto(jobPath(seed.tenants.b, jobA.job_id));
  await expect(page.getByText("Không tìm thấy job hoặc bạn không có quyền xem")).toBeVisible();
  // A's tenant on the URL: B is not a member, so the SPA shows no tenant pages at all.
  await page.goto(jobPath(seed.tenants.a, jobA.job_id));
  await expect(page.getByRole("heading", { name: "Không có quyền truy cập tenant này" })).toBeVisible();
  await page.goto(`/t/${seed.tenants.a}/data`);
  await expect(page.getByRole("heading", { name: "Không có quyền truy cập tenant này" })).toBeVisible();

  // Hand-written requests: B's own tenant header, then A's tenant header.
  const blocked = await page.evaluate(
    async ({ tenantA, tenantB, jobId, artifactId }) => {
      const read = async (path: string, tenant: string) => {
        const response = await fetch(path, { headers: { "X-Nexa-Tenant-Id": tenant } });
        const body = (await response.json()) as { code?: string };
        return `${response.status} ${body.code}`;
      };
      return {
        downloadAsB: await read(`/v1/artifacts/${artifactId}/content`, tenantB),
        downloadAsA: await read(`/v1/artifacts/${artifactId}/content`, tenantA),
        artifactAsA: await read(`/v1/artifacts/${artifactId}`, tenantA),
        jobAsB: await read(`/v1/jobs/${jobId}`, tenantB),
        jobAsA: await read(`/v1/jobs/${jobId}`, tenantA),
        listAsA: await read("/v1/jobs?page_size=25", tenantA),
      };
    },
    {
      tenantA: seed.tenants.a,
      tenantB: seed.tenants.b,
      jobId: jobA.job_id,
      artifactId: seed.artifacts.a_input.artifact_id,
    },
  );
  expect(blocked.downloadAsB).toBe("404 resource_not_found");
  expect(blocked.jobAsB).toBe("404 resource_not_found");
  for (const outcome of [blocked.downloadAsA, blocked.artifactAsA, blocked.jobAsA, blocked.listAsA]) {
    expect(outcome).toMatch(/^(403 permission_denied|404 resource_not_found)$/);
  }
  await context.close();

  // multi belongs to A and B: switching the tenant swaps the list.
  const multi = await contextAs(browser, "multi");
  const multiPage = await multi.newPage();
  await multiPage.goto(`/t/${seed.tenants.a}/jobs`);
  const linkA = multiPage.locator(`tbody a[href="${jobPath(seed.tenants.a, jobA.job_id)}"]`).first();
  const linkB = multiPage.locator(`tbody a[href="${jobPath(seed.tenants.b, jobB.job_id)}"]`).first();
  await expect(linkA).toBeVisible();
  await multiPage.getByLabel("Tenant").selectOption(seed.tenants.b);
  await expect(multiPage).toHaveURL(new RegExp(`/t/${seed.tenants.b}/jobs$`));
  await expect(linkB).toBeVisible();
  await expect(multiPage.locator(`tbody a[href*="${jobA.job_id}"]`)).toHaveCount(0);
  await multi.close();
});

test("5. roles: member A2 has no controls and a forced call gets 404; tenant admin A cancels", async ({ browser }) => {
  const seed = fixture();
  const peer = await contextAs(browser, "member_a2");
  const page = await peer.newPage();
  await page.goto(jobPath(seed.tenants.a, jobA.job_id));
  await expect(page.getByText("Chỉ người tạo job hoặc quản trị viên tenant được điều khiển job này")).toBeVisible();
  await expect(page.getByRole("button", { name: "Hủy job" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Tạm dừng" })).toHaveCount(0);

  const forced = await page.evaluate(
    async ({ tenant, jobId }) => {
      const session = (await (await fetch("/v1/auth/session")).json()) as { csrf_token: string };
      const current = await fetch(`/v1/jobs/${jobId}`, { headers: { "X-Nexa-Tenant-Id": tenant } });
      const response = await fetch(`/v1/jobs/${jobId}/cancel`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Nexa-Tenant-Id": tenant,
          "X-CSRF-Token": session.csrf_token,
          "Idempotency-Key": `e2e-${crypto.randomUUID()}`,
          "If-Match": current.headers.get("ETag") ?? "",
        },
        body: JSON.stringify({ reason: "forced by e2e" }),
      });
      return `${response.status} ${((await response.json()) as { code: string }).code}`;
    },
    { tenant: seed.tenants.a, jobId: jobA.job_id },
  );
  expect(forced).toBe("404 resource_not_found");
  await peer.close();

  const admin = await contextAs(browser, "admin_a");
  const adminPage = await admin.newPage();
  await adminPage.goto(jobPath(seed.tenants.a, jobA.job_id));
  await expect(adminPage.getByText("Đang xếp hàng").first()).toBeVisible();
  await adminPage.getByRole("button", { name: "Hủy job" }).click();
  const dialog = adminPage.getByRole("dialog", { name: "Hủy job?" });
  await dialog.getByRole("button", { name: "Hủy job" }).click();
  await expect(dialog).toBeHidden();
  await expect(adminPage.locator(".status-block").getByText("Đã hủy")).toBeVisible();
  await admin.close();

  const owner = await Api.as("member_a");
  expect((await owner.job(seed.tenants.a, jobA.job_id)).job.state).toBe("CANCELLED");
  await owner.dispose();
});
