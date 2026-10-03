import { fixture } from "../fixture";
import { expectNoHorizontalScroll } from "../support";
import { adminContextAs, expect, test } from "../admin-support";

// Scenario 19 (390×844, after w2-admin on the same W2 stack so the worker detail is real):
// admin nav, Overview, worker detail and the tenant policy form without page-level horizontal scroll.

test("19. mobile admin: nav, overview, worker detail and tenant policy form without horizontal scroll", async ({
  browser,
}) => {
  const seed = fixture();
  const { context } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  const nav = page.getByRole("navigation", { name: "Điều hướng quản trị" });

  await page.goto("/admin");
  await expect(page.getByRole("heading", { level: 1, name: "Tổng quan" })).toBeVisible();
  await expect(page.getByText("Sức chứa").first()).toBeVisible();
  await expect(nav).toBeVisible();
  await expectNoHorizontalScroll(page);

  await nav.getByRole("link", { name: "Worker" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Worker" })).toBeVisible();
  await page.locator("tbody tr").filter({ has: page.locator(`code[title="${seed.worker_id}"]`) }).getByRole("link").click();
  await expect(page.getByRole("heading", { level: 1, name: /^Worker/ })).toBeVisible();
  await expect(page.getByRole("region", { name: "Inventory" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Thao tác" }).getByRole("button").first()).toBeVisible();
  await expectNoHorizontalScroll(page);

  await page.goto(`/admin/tenants/${seed.tenants.a}?tab=policy`);
  const form = page.getByRole("form", { name: "Chính sách tenant" });
  await expect(form).toBeVisible();
  await expect(form.getByRole("button").last()).toBeVisible();
  await expectNoHorizontalScroll(page);
  await context.close();
});
