import type { Route } from "@playwright/test";

import { fixture, storageState } from "../fixture";
import { Api, contextAs, cpuSpec, expect, jobPath, test } from "../support";

// Scenarios 10–11 on QUEUED jobs (QUEUED + cancel → CANCELLED directly, state-machines.md).

test.use({ storageState: storageState("member_a") });

test("11. cancel a QUEUED job: dismissing changes nothing, confirming gives CANCELLED", async ({ page }) => {
  const seed = fixture();
  const api = await Api.as("member_a");
  const job = await api.submit(seed.tenants.a, cpuSpec(seed.artifacts.a_input.artifact_id, 1101));
  const cancels: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/cancel")) cancels.push(request.url());
  });

  await page.goto(jobPath(seed.tenants.a, job.job_id));
  const status = page.locator(".status-block");
  await expect(status.getByText("Đang xếp hàng")).toBeVisible();

  await page.getByRole("button", { name: "Hủy job" }).click();
  const dialog = page.getByRole("dialog", { name: "Hủy job?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Bỏ qua" }).click();
  await expect(dialog).toBeHidden();
  expect(cancels).toEqual([]);
  const untouched = await api.job(seed.tenants.a, job.job_id);
  expect(untouched.job.state).toBe("QUEUED");
  expect(untouched.job.version).toBe(job.version);

  await page.getByRole("button", { name: "Hủy job" }).click();
  await dialog.getByRole("button", { name: "Hủy job" }).click();
  await expect(dialog).toBeHidden();
  await expect(status.getByText("Đã hủy")).toBeVisible();
  await expect(page.getByRole("button", { name: "Hủy job" })).toHaveCount(0);
  expect(cancels).toHaveLength(1);
  expect((await api.job(seed.tenants.a, job.job_id)).job.state).toBe("CANCELLED");
  await api.dispose();
});

test("10. 412: tab 2 acts on a stale ETag after tab 1 cancelled → reload, explain, no resend", async ({ browser }) => {
  const seed = fixture();
  const api = await Api.as("member_a");
  const job = await api.submit(seed.tenants.a, cpuSpec(seed.artifacts.a_input.artifact_id, 1001));
  const context = await contextAs(browser, "member_a");
  const tab1 = await context.newPage();
  const tab2 = await context.newPage();
  await tab1.goto(jobPath(seed.tenants.a, job.job_id));
  await tab2.goto(jobPath(seed.tenants.a, job.job_id));
  await expect(tab2.locator(".status-block").getByText("Đang xếp hàng")).toBeVisible();

  // Tab 2 is between two polls: its next job read is held, so it still shows the old version.
  const jobUrl = `**/v1/jobs/${job.job_id}`;
  const held: Route[] = [];
  const hold = (route: Route) => {
    held.push(route);
  };
  await tab2.route(jobUrl, hold);

  await tab1.getByRole("button", { name: "Hủy job" }).click();
  await tab1.getByRole("dialog", { name: "Hủy job?" }).getByRole("button", { name: "Hủy job" }).click();
  await expect(tab1.locator(".status-block").getByText("Đã hủy")).toBeVisible();

  const statuses: number[] = [];
  tab2.on("response", (response) => {
    if (response.request().method() === "POST" && response.url().endsWith("/cancel")) statuses.push(response.status());
  });
  await tab2.getByRole("button", { name: "Hủy job" }).click();
  const dialog = tab2.getByRole("dialog", { name: "Hủy job?" });
  await tab2.unroute(jobUrl, hold);
  await dialog.getByRole("button", { name: "Hủy job" }).click();
  await expect(dialog).toBeHidden();
  await expect(tab2.getByText(/Job vừa thay đổi \(trạng thái mới: Đã hủy\)\. Kiểm tra rồi thử lại\./)).toBeVisible();
  await expect(tab2.locator(".status-block").getByText("Đã hủy")).toBeVisible();
  await expect(tab2.getByRole("button", { name: "Hủy job" })).toHaveCount(0);
  expect(statuses).toEqual([412]);
  for (const route of held) await route.continue().catch(() => undefined);

  const after = await api.job(seed.tenants.a, job.job_id);
  expect(after.job.state).toBe("CANCELLED");
  // Exactly one cancel was applied: created (v1) → cancelled (v2).
  expect(after.job.version).toBe(job.version + 1);
  await context.close();
  await api.dispose();
});
