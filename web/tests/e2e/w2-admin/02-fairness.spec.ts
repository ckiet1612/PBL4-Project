import type { Page } from "@playwright/test";

import { fixture } from "../fixture";
import { adminContextAs, expect, test } from "../admin-support";

// Scenario 18 after 14–17: tenants A and B ran real jobs on the worker, tenant Q did not.

async function totals(page: Page): Promise<Record<string, number>> {
  const table = page.locator("table").filter({ has: page.locator("caption", { hasText: "Theo tenant" }) });
  const rows = await table.locator("tbody tr").evaluateAll((items) =>
    items.map((row) => [row.children[0]?.textContent?.trim() ?? "", row.children[1]?.textContent?.trim() ?? ""]),
  );
  // vi-VN numbers: "." groups thousands, "," is the decimal separator.
  return Object.fromEntries(rows.map(([tenant, value]) => [tenant, Number(value.replaceAll(".", "").replace(",", "."))]));
}

test("18. fairness with real allocations: tenants that ran have service > 0; the tenant filter narrows the report", async ({
  browser,
}) => {
  const seed = fixture();
  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  await page.goto("/admin/fairness");
  await expect(page.getByRole("heading", { level: 1, name: "Fairness" })).toBeVisible();
  await expect(page.locator("caption", { hasText: "Theo bucket" })).toBeVisible();

  const all = await totals(page);
  expect(all["b17-tenant-a"]).toBeGreaterThan(0);
  expect(all["b17-tenant-b"]).toBeGreaterThan(0);
  expect(all["b17-tenant-quota"] ?? 0).toBe(0);

  const form = page.getByRole("form", { name: "Khoảng báo cáo fairness" });
  await form.getByLabel("Tenant").selectOption({ label: "b17-tenant-b" });
  await expect(form.getByLabel("Tenant")).toHaveValue(seed.tenants.b);
  await form.getByRole("button", { name: "Xem báo cáo" }).click();
  await expect(page).toHaveURL(new RegExp(`tenant_id=${seed.tenants.b}`));
  await expect.poll(async () => Object.keys(await totals(page))).toEqual(["b17-tenant-b"]);
  const bucketTenants = page
    .locator("table")
    .filter({ has: page.locator("caption", { hasText: "Theo bucket" }) })
    .locator("tbody tr td:nth-child(2)");
  await expect(bucketTenants.filter({ hasNotText: "b17-tenant-b" })).toHaveCount(0);

  await form.getByLabel("Tenant").selectOption({ label: "b17-tenant-quota" });
  await expect(form.getByLabel("Tenant")).toHaveValue(seed.tenants.q);
  await form.getByRole("button", { name: "Xem báo cáo" }).click();
  await expect(page.getByText("Không có phân bổ nào trong khoảng này")).toBeVisible();

  const reads = calls.filter((call) => call.path === "/v1/admin/fairness");
  expect(reads.map((call) => call.search.get("tenant_id"))).toEqual([null, seed.tenants.b, seed.tenants.q]);
  await context.close();
});
