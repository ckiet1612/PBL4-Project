import type { Page } from "@playwright/test";

import { fixture } from "../fixture";
import { Api, cpuSpec, type Job } from "../support";
import { AdminApi, adminContextAs, expect, test } from "../admin-support";

// Scenario 7: the cross-tenant queue (read-only). Jobs of tenants A and B are seeded through
// the real API; W1 has no worker, so they stay QUEUED unless cancelled here.

const PAGE = 25;
const A_JOBS = 30;

let cancelledB = "";

test.beforeAll(async () => {
  const seed = fixture();
  const a = await Api.as("member_a");
  for (let i = 0; i < A_JOBS; i += 1) await a.submit(seed.tenants.a, cpuSpec(seed.artifacts.a_input.artifact_id, 7000 + i));
  await a.dispose();
  const b = await Api.as("member_b");
  const job = await b.submit(seed.tenants.b, cpuSpec(seed.artifacts.b_input.artifact_id, 7100));
  cancelledB = (await b.cancel(seed.tenants.b, job.job_id)).job_id;
  await b.dispose();
});

/** Every job the admin API returns for a filter, in its order. */
async function adminJobs(admin: AdminApi, query: string): Promise<Job[]> {
  const jobs: Job[] = [];
  let cursor: string | null = null;
  do {
    const next: string = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
    const { body } = await admin.read<{ items: Job[]; page: { next_cursor: string | null } }>(
      `/jobs?page_size=100&${query}${next}`,
    );
    jobs.push(...body.items);
    cursor = body.page.next_cursor;
  } while (cursor !== null);
  return jobs;
}

function shownIds(page: Page): Promise<string[]> {
  return page
    .locator("tbody tr td:first-child .short-id code")
    .evaluateAll((codes) => codes.map((code) => code.getAttribute("title") ?? ""));
}

test("7. queue: tenant/state/waiting-reason filters, pages forward/back/first, broken cursor, read-only job detail", async ({
  browser,
}) => {
  const seed = fixture();
  const admin = await AdminApi.as();
  const allA = (await adminJobs(admin, `tenant_id=${seed.tenants.a}`)).map((job) => job.job_id);
  expect(allA.length).toBeGreaterThan(PAGE);
  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();

  await page.goto("/admin/jobs");
  await expect(page.getByRole("heading", { level: 1, name: "Hàng chờ" })).toBeVisible();
  await expect(page.locator("tbody tr").first()).toBeVisible();
  // Both tenants are in the unfiltered queue.
  const tenantCells = page.locator("tbody tr td:nth-child(2)");
  await expect(tenantCells.filter({ hasText: "b17-tenant-a" }).first()).toBeVisible();

  const filters = page.getByRole("form", { name: "Bộ lọc hàng chờ" });
  // Each change waits for the form to show the new value: the URL updates before the render
  // that the next select's change handler builds on.
  await filters.getByLabel("Tenant").selectOption({ label: "b17-tenant-a" });
  await expect(filters.getByLabel("Tenant")).toHaveValue(seed.tenants.a);
  await expect(page).toHaveURL(new RegExp(`tenant_id=${seed.tenants.a}`));
  await expect.poll(() => shownIds(page)).toEqual(allA.slice(0, PAGE));
  await expect(tenantCells.filter({ hasNotText: "b17-tenant-a" })).toHaveCount(0);

  const pager = page.getByRole("navigation", { name: "Phân trang" });
  await expect(pager.getByRole("button", { name: "Trang trước" })).toBeDisabled();
  await pager.getByRole("button", { name: "Trang sau" }).click();
  await expect(page).toHaveURL(/cursor=/);
  await expect.poll(() => shownIds(page)).toEqual(allA.slice(PAGE, 2 * PAGE));
  await pager.getByRole("button", { name: "Trang trước" }).click();
  await expect.poll(() => shownIds(page)).toEqual(allA.slice(0, PAGE));
  await expect(page).not.toHaveURL(/cursor=/);
  await pager.getByRole("button", { name: "Trang sau" }).click();
  await expect.poll(() => shownIds(page)).toEqual(allA.slice(PAGE, 2 * PAGE));
  await pager.getByRole("button", { name: "Về trang đầu" }).click();
  await expect.poll(() => shownIds(page)).toEqual(allA.slice(0, PAGE));
  await expect(page).not.toHaveURL(/cursor=/);

  // A cursor the server refuses: back to page one of the same filter, with a notice.
  await page.goto(`/admin/jobs?tenant_id=${seed.tenants.a}&cursor=${"x".repeat(24)}`);
  await expect(page.getByText("Vị trí trang không còn hợp lệ, đã quay về trang đầu")).toBeVisible();
  await expect(page).not.toHaveURL(/cursor=/);
  await expect(page.locator(".error-panel")).toHaveCount(0);
  await expect.poll(() => shownIds(page)).toEqual(allA.slice(0, PAGE));

  // waiting_reason: the value the scheduler reports for these QUEUED jobs (observed, not assumed).
  const queuedA = await adminJobs(admin, `tenant_id=${seed.tenants.a}&state=QUEUED`);
  const reason = queuedA[0].waiting_reason;
  expect(reason).not.toBeNull();
  const byReason = (await adminJobs(admin, `tenant_id=${seed.tenants.a}&waiting_reason=${reason}`)).map((job) => job.job_id);
  await filters.getByLabel("Lý do chờ").selectOption(reason!);
  await expect(filters.getByLabel("Lý do chờ")).toHaveValue(reason!);
  await expect(page).toHaveURL(new RegExp(`waiting_reason=${reason}`));
  await expect.poll(() => shownIds(page)).toEqual(byReason.slice(0, PAGE));

  // State filter on tenant B: exactly the cancelled job.
  await filters.getByRole("button", { name: "Xóa bộ lọc" }).click();
  await expect(page).not.toHaveURL(/tenant_id=/);
  for (const label of ["Tenant", "Trạng thái", "Lý do chờ"]) await expect(filters.getByLabel(label)).toHaveValue("");
  await filters.getByLabel("Tenant").selectOption({ label: "b17-tenant-b" });
  await expect(filters.getByLabel("Tenant")).toHaveValue(seed.tenants.b);
  await filters.getByLabel("Trạng thái").selectOption("CANCELLED");
  await expect(filters.getByLabel("Trạng thái")).toHaveValue("CANCELLED");
  await expect(page).toHaveURL(/state=CANCELLED/);
  const cancelled = (await adminJobs(admin, `tenant_id=${seed.tenants.b}&state=CANCELLED`)).map((job) => job.job_id);
  expect(cancelled).toContain(cancelledB);
  await expect.poll(() => shownIds(page)).toEqual(cancelled.slice(0, PAGE));

  // Job detail: read-only, no job control.
  await page.locator("tbody tr").first().getByRole("link").first().click();
  await expect(page).toHaveURL(new RegExp(`/admin/jobs/${cancelled[0]}$`));
  await expect(page.getByRole("heading", { level: 1, name: /Job/ })).toBeVisible();
  await expect(page.getByText("Khu quản trị chỉ xem. Điều khiển job cần là thành viên của tenant.")).toBeVisible();
  await expect(page.getByText("Đã hủy").first()).toBeVisible();
  // Only Làm mới and the copy buttons; no cancel/pause/resume/retry.
  const labels = await page
    .locator(".admin-content button")
    .evaluateAll((buttons) =>
      buttons
        .filter((button) => !(button.getAttribute("aria-label") ?? "").startsWith("Sao chép"))
        .map((button) => button.textContent?.trim() ?? ""),
    );
  expect(labels.filter((label) => label !== "Làm mới")).toEqual([]);

  const sizes = calls.filter((call) => call.path === "/v1/admin/jobs").map((call) => Number(call.search.get("page_size")));
  expect(sizes.length).toBeGreaterThan(0);
  expect(sizes.every((size) => size >= 1 && size <= 100)).toBe(true);
  expect(calls.every((call) => call.method === "GET")).toBe(true);
  await admin.dispose();
  await context.close();
});
