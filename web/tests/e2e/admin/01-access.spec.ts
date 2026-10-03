import { fixture } from "../fixture";
import { adminContextAs, expect, storageGuardChecks, test } from "../admin-support";

// Scenarios 1, 10 and 11: who sees the admin area, CSRF on admin writes, no polling of admin lists.

const GUARDED = ["/admin", "/admin/workers", "/admin/jobs", "/admin/audit"];

for (const user of ["member_a", "admin_a"] as const) {
  test(`1. ${user}: no admin link, guarded pages without any /v1/admin request, the API answers 403`, async ({
    browser,
  }) => {
    const seed = fixture();
    const { context, calls } = await adminContextAs(browser, user);
    const page = await context.newPage();
    await page.goto(`/t/${seed.tenants.a}/jobs`);
    await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Điều hướng chính" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Quản trị" })).toHaveCount(0);

    for (const path of [...GUARDED, `/admin/tenants/${seed.tenants.a}`]) {
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1, name: "Cần quyền quản trị hệ thống" })).toBeVisible();
      await expect(page.getByRole("navigation", { name: "Điều hướng quản trị" })).toHaveCount(0);
      await expect(page.getByRole("heading", { name: "Tổng quan" })).toHaveCount(0);
    }
    expect(calls.map((call) => `${call.method} ${call.path}`)).toEqual([]);

    const forced = await context.request.get("/v1/admin/tenants");
    expect(forced.status()).toBe(403);
    await context.close();
  });
}

test("1. a system admin without any membership gets the admin link and enters the area", async ({ browser }) => {
  const { context, calls } = await adminContextAs(browser, "admin2");
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.getByRole("link", { name: "Mở khu quản trị" })).toBeVisible();
  const nav = page.getByRole("navigation", { name: "Điều hướng chính" });
  await nav.getByRole("link", { name: "Quản trị" }).click();
  await expect(page).toHaveURL(/\/admin$/);
  await expect(page.getByRole("heading", { level: 1, name: "Tổng quan" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Điều hướng quản trị" })).toBeVisible();
  await expect(page.getByText("Mọi thao tác và lượt xem trong khu này đều được ghi audit")).toBeVisible();
  await expect.poll(() => calls.length).toBeGreaterThan(0);
  expect(calls.every((call) => call.method === "GET")).toBe(true);
  await context.close();
});

test("11. CSRF: an admin write without X-CSRF-Token is refused with 403 invalid_csrf", async ({ browser }) => {
  const { context } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  await page.goto("/admin/tenants");
  await expect(page.getByRole("heading", { level: 1, name: "Tenant" })).toBeVisible();
  const outcome = await page.evaluate(async () => {
    const response = await fetch("/v1/admin/tenants", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": `e2e-${crypto.randomUUID()}` },
      body: JSON.stringify({ slug: "csrf-probe", display_name: "csrf probe" }),
    });
    return { status: response.status, code: ((await response.json()) as { code: string }).code };
  });
  expect(outcome).toEqual({ status: 403, code: "invalid_csrf" });
  const listed = await context.request.get("/v1/admin/tenants?page_size=100");
  const slugs = ((await listed.json()) as { items: { slug: string }[] }).items.map((item) => item.slug);
  expect(slugs).not.toContain("csrf-probe");
  await context.close();
});

test("10. no polling: two simulated minutes on Overview, Workers and Queue send no /v1/admin request", async ({
  browser,
}) => {
  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  await page.clock.install();
  for (const [path, heading] of [
    ["/admin", "Tổng quan"],
    ["/admin/workers", "Worker"],
    ["/admin/jobs", "Hàng chờ"],
  ] as const) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
    await expect(page.getByText("Cập nhật lúc").first()).toBeVisible();
    await page.waitForLoadState("networkidle");
    const settled = calls.length;
    expect(settled).toBeGreaterThan(0);
    await page.clock.runFor(120_000);
    await page.waitForLoadState("networkidle");
    expect(calls.length, `${path}: requests after 120 s`).toBe(settled);
    calls.length = 0;
  }
  await context.close();
});

test("12. guard control: storage left behind fails the context close, also after the page closed (B18-RV02)", async ({
  browser,
}) => {
  async function openOverview() {
    const { context } = await adminContextAs(browser, "admin");
    const page = await context.newPage();
    await page.goto("/admin");
    await expect(page.getByRole("heading", { level: 1, name: "Tổng quan" })).toBeVisible();
    return { context, page };
  }

  // sessionStorage on an open page.
  const first = await openOverview();
  await first.page.evaluate(() => sessionStorage.setItem("b18-guard-control", "1"));
  await expect(first.context.close()).rejects.toThrow();
  await first.page.evaluate(() => sessionStorage.clear());
  await first.context.close();

  // localStorage after its page closed: found through the context's storage state.
  const second = await openOverview();
  await second.page.evaluate(() => localStorage.setItem("b18-guard-control", "1"));
  await second.page.close();
  await expect(second.context.close()).rejects.toThrow(/origins with localStorage or IndexedDB/);
  await second.context.close();

  // A clean context passes and is counted.
  const before = storageGuardChecks();
  const clean = await openOverview();
  await clean.context.close();
  expect(storageGuardChecks().pages).toBe(before.pages + 1);
});
