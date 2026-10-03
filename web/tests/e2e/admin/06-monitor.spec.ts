import type { Page } from "@playwright/test";

import { adminContextAs, expect, test } from "../admin-support";

// Scenarios 8 and 9 (runs after 01–05, so their writes are in the audit). W1 has no worker:
// fairness and recovery show their empty states.

/** Shift a datetime-local value ("YYYY-MM-DDTHH:mm", naive) by whole days. */
function shiftLocal(value: string, days: number): string {
  return new Date(Date.parse(`${value}:00Z`) + days * 86_400_000).toISOString().slice(0, 16);
}

/** The from/to bounds on the URL (B18-RV10: the default range is written there once). */
function urlRange(page: Page): { from: string | null; to: string | null } {
  const params = new URL(page.url()).searchParams;
  return { from: params.get("from"), to: params.get("to") };
}

async function auditRows(page: Page): Promise<{ action: string; reason: string }[]> {
  return page.locator("tbody tr").evaluateAll((rows) =>
    rows.map((row) => ({
      action: row.children[1]?.getAttribute("title") ?? "",
      reason: row.children[5]?.textContent?.trim() ?? "",
    })),
  );
}

test("8. audit: the writes of scenarios 2–6 and admin reads with their reasons; action, time range and pages", async ({
  browser,
}) => {
  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  await page.goto("/admin/audit");
  await expect(page.getByRole("heading", { level: 1, name: "Audit" })).toBeVisible();
  await expect(page.getByText("Danh sách gồm cả lượt xem của quản trị viên")).toBeVisible();
  await expect(page.locator("tbody tr").first()).toBeVisible();

  // The default 24 h range is put on the URL, so a reload and the next page keep the same bounds.
  await expect(page).toHaveURL(/[?&]from=[^&]+/);
  await expect(page).toHaveURL(/[?&]to=[^&]+/);
  const range = urlRange(page);
  await page.reload();
  await expect(page.locator("tbody tr").first()).toBeVisible();
  expect(urlRange(page)).toEqual(range);

  // Default 24 h, no action filter: more than one page; forward, back, first.
  const pager = page.getByRole("navigation", { name: "Phân trang" });
  const first = await auditRows(page);
  expect(first).toHaveLength(25);
  await pager.getByRole("button", { name: "Trang sau" }).click();
  await expect(page).toHaveURL(/cursor=/);
  expect(urlRange(page)).toEqual(range);
  await expect.poll(async () => JSON.stringify(await auditRows(page))).not.toBe(JSON.stringify(first));
  await pager.getByRole("button", { name: "Trang trước" }).click();
  await expect(page).not.toHaveURL(/cursor=/);
  await pager.getByRole("button", { name: "Trang sau" }).click();
  await pager.getByRole("button", { name: "Về trang đầu" }).click();
  await expect(page).not.toHaveURL(/cursor=/);

  const form = page.getByRole("form", { name: "Bộ lọc audit" });
  const expected: [string, string][] = [
    ["admin.tenant.create", "Tenant created"],
    ["admin.tenant.update", "Tenant updated"],
    ["admin.user.create", "User created"],
    ["admin.user.update", "User updated"],
    ["admin.membership.upsert", "Tenant membership created or replaced"],
    ["admin.membership.delete", "Tenant membership removed"],
    ["admin.policy.tenant.update", "Tenant policy version updated"],
    ["admin.policy.global.update", "Global policy version updated"],
    ["admin.job.list", "Cross-tenant job list read"],
    ["admin.tenant.get", "Tenant read"],
  ];
  for (const [action, reason] of expected) {
    await form.getByLabel("Hành động").selectOption(action);
    await form.getByRole("button", { name: "Lọc" }).click();
    await expect(page).toHaveURL(new RegExp(`action=${action.replaceAll(".", "\\.")}`));
    // Wait for the filtered result itself (the previous rows stay until the new page arrives).
    await expect
      .poll(async () => {
        const rows = await auditRows(page);
        return rows.length > 0 && rows.every((row) => row.action === action);
      }, `${action} rows`)
      .toBe(true);
    expect((await auditRows(page)).map((row) => row.reason)).toContain(reason);
  }

  // Time range in the future: nothing matches.
  const to = await form.getByLabel("Đến (giờ địa phương)").inputValue();
  await form.getByLabel("Đến (giờ địa phương)").fill(shiftLocal(to, 2));
  await form.getByLabel("Từ (giờ địa phương)").fill(shiftLocal(to, 1));
  await form.getByRole("button", { name: "Lọc" }).click();
  await expect(page.getByText("Không có bản ghi audit khớp bộ lọc")).toBeVisible();

  const reads = calls.filter((call) => call.path === "/v1/admin/audit");
  expect(reads.every((call) => call.method === "GET" && Number(call.search.get("page_size")) <= 100)).toBe(true);
  await context.close();
});

test("9. fairness and recovery: empty states, more than 31 days blocked in the form, bad parameters → 400", async ({
  browser,
}) => {
  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();

  await page.goto("/admin/recovery");
  await expect(page.getByRole("heading", { level: 1, name: "Khôi phục" })).toBeVisible();
  await expect(page.getByText("Không có sự kiện khôi phục trong khoảng này")).toBeVisible();
  await expect(page).toHaveURL(/[?&]from=[^&]+/);
  await expect(page).toHaveURL(/[?&]to=[^&]+/);
  const recoveryRange = urlRange(page);
  const recoveryReads = calls.filter((call) => call.path === "/v1/admin/recovery-events");
  expect(recoveryReads.length).toBeGreaterThan(0);
  expect(recoveryReads.every((call) => call.search.get("from") === recoveryRange.from)).toBe(true);

  await page.goto("/admin/fairness");
  await expect(page.getByRole("heading", { level: 1, name: "Fairness" })).toBeVisible();
  await expect(page.getByText("Không có phân bổ nào trong khoảng này")).toBeVisible();
  const form = page.getByRole("form", { name: "Khoảng báo cáo fairness" });
  const sent = calls.filter((call) => call.path === "/v1/admin/fairness").length;
  const to = await form.getByLabel("Đến (giờ địa phương)").inputValue();
  await form.getByLabel("Từ (giờ địa phương)").fill(shiftLocal(to, -40));
  await form.getByRole("button", { name: "Xem báo cáo" }).click();
  await expect(form.getByText("Khoảng thời gian tối đa 31 ngày")).toBeVisible();
  expect(calls.filter((call) => call.path === "/v1/admin/fairness")).toHaveLength(sent);

  // The server enforces the same bounds without the UI.
  const now = Date.now();
  const iso = (offsetDays: number) => new Date(now + offsetDays * 86_400_000).toISOString();
  for (const path of [
    `/v1/admin/fairness?from=${iso(-40)}&to=${iso(0)}&bucket_seconds=86400`,
    `/v1/admin/fairness?from=${iso(-1)}&to=${iso(0)}&bucket_seconds=0`,
    `/v1/admin/recovery-events?from=${iso(0)}&to=${iso(-1)}`,
    `/v1/admin/audit?page_size=101`,
  ]) {
    const response = await context.request.get(path);
    expect(response.status(), path).toBe(400);
  }
  await context.close();
});
