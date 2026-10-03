import { AdminApi, adminContextAs, expect, test, uniqueName } from "../admin-support";

// Scenario 2: create (one intent per form, also across an empty 502), rename, disable/enable.

test("2a. create: a double click creates one tenant and opens it", async ({ browser }) => {
  const admin = await AdminApi.as();
  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  const slug = uniqueName("b18-dbl");
  await page.goto("/admin/tenants");
  await page.getByRole("button", { name: "Tạo tenant" }).click();
  const form = page.getByRole("form", { name: "Tạo tenant" });
  await form.getByLabel("Slug").fill(slug);
  await form.getByLabel("Tên hiển thị").fill("Double click");
  await form.getByRole("button", { name: "Tạo tenant" }).dblclick();
  await expect(page).toHaveURL(/\/admin\/tenants\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { level: 1, name: /Double click/ })).toBeVisible();

  const posts = calls.filter((call) => call.method === "POST" && call.path === "/v1/admin/tenants");
  expect(posts).toHaveLength(1);
  expect(posts[0].idempotencyKey).toMatch(/^web-[0-9a-f-]{36}$/);
  const created = await admin.tenantsWithPrefix(slug);
  expect(created).toHaveLength(1);
  expect(page.url().endsWith(created[0].tenant_id)).toBe(true);
  await admin.dispose();
  await context.close();
});

test("2b. the proxy answers 502 without an envelope after the commit: the resend reuses the key, one tenant", async ({
  browser,
}) => {
  const admin = await AdminApi.as();
  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  const slug = uniqueName("b18-502");
  let replaced = 0;
  await page.route("**/v1/admin/tenants", async (route) => {
    if (route.request().method() !== "POST" || replaced > 0) return route.fallback();
    replaced += 1;
    const response = await route.fetch();
    expect(response.status()).toBe(201);
    await route.fulfill({ status: 502, body: "" });
  });
  await page.goto("/admin/tenants");
  await page.getByRole("button", { name: "Tạo tenant" }).click();
  const form = page.getByRole("form", { name: "Tạo tenant" });
  await form.getByLabel("Slug").fill(slug);
  await form.getByLabel("Tên hiển thị").fill("After 502");
  const submit = form.getByRole("button", { name: "Tạo tenant" });
  await submit.click();
  await expect(form.locator(".error-panel")).toBeVisible();
  await expect(submit).toBeEnabled();
  expect(await admin.tenantsWithPrefix(slug)).toHaveLength(1);

  await submit.click();
  await expect(page).toHaveURL(/\/admin\/tenants\/[0-9a-f-]{36}$/);
  const posts = calls.filter((call) => call.method === "POST" && call.path === "/v1/admin/tenants");
  expect(posts).toHaveLength(2);
  expect(posts[1].idempotencyKey).toBe(posts[0].idempotencyKey);
  const created = await admin.tenantsWithPrefix(slug);
  expect(created).toHaveLength(1);
  expect(page.url().endsWith(created[0].tenant_id)).toBe(true);
  await admin.dispose();
  await context.close();
});

test("2c. rename with the ETag of the latest read, then disable and enable through the dialog", async ({ browser }) => {
  const admin = await AdminApi.as();
  const tenant = await admin.tenant(uniqueName("b18-ren"));
  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  await page.goto(`/admin/tenants/${tenant.tenant_id}`);
  await expect(page.getByRole("tab", { name: "Thông tin" })).toHaveAttribute("aria-selected", "true");

  const rename = page.getByRole("form", { name: "Đổi tên tenant" });
  await rename.getByLabel("Tên hiển thị").fill("Renamed tenant");
  await rename.getByRole("button", { name: "Đổi tên" }).click();
  await expect(page.getByText("Đã đổi tên tenant.")).toBeVisible();
  await expect(page.getByRole("heading", { level: 1, name: /Renamed tenant/ })).toBeVisible();
  const patches = () => calls.filter((call) => call.method === "PATCH" && call.path === `/v1/admin/tenants/${tenant.tenant_id}`);
  expect(patches()[0].ifMatch).toBe(`"v${tenant.version}"`);

  await page.getByRole("button", { name: "Tắt", exact: true }).click();
  const disable = page.getByRole("dialog", { name: `Tắt tenant ${tenant.slug}?` });
  await expect(disable.getByText("Job đang chạy không bị dừng.")).toBeVisible();
  await disable.getByRole("button", { name: "Tắt tenant" }).click();
  await expect(page.getByText("Tenant đã được tắt.")).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 }).getByText("Đã tắt")).toBeVisible();
  expect((await admin.read<{ enabled: boolean }>(`/tenants/${tenant.tenant_id}`)).body.enabled).toBe(false);

  await page.getByRole("button", { name: "Bật", exact: true }).click();
  await page.getByRole("dialog", { name: `Bật tenant ${tenant.slug}?` }).getByRole("button", { name: "Bật tenant" }).click();
  await expect(page.getByText("Tenant đã được bật.")).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 }).getByText("Đang bật")).toBeVisible();
  const final = await admin.read<{ enabled: boolean; display_name: string; version: number }>(`/tenants/${tenant.tenant_id}`);
  expect(final.body).toMatchObject({ enabled: true, display_name: "Renamed tenant", version: tenant.version + 3 });
  // Each write used the ETag returned by the previous one.
  expect(patches().map((call) => call.ifMatch)).toEqual([
    `"v${tenant.version}"`,
    `"v${tenant.version + 1}"`,
    `"v${tenant.version + 2}"`,
  ]);
  await admin.dispose();
  await context.close();
});

test("2d. one mutation per tenant: while a rename is in flight enable/disable is disabled, and the other way round (B18-RV04)", async ({
  browser,
}) => {
  const admin = await AdminApi.as();
  const tenant = await admin.tenant(uniqueName("b18-one"));
  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  // Holds each PATCH until the test has looked at the other control.
  await page.route(`**/v1/admin/tenants/${tenant.tenant_id}`, async (route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    await held;
    await route.fallback();
  });
  await page.goto(`/admin/tenants/${tenant.tenant_id}`);
  const rename = page.getByRole("form", { name: "Đổi tên tenant" });
  const toggle = page.getByRole("button", { name: "Tắt", exact: true });
  await rename.getByLabel("Tên hiển thị").fill("One at a time");
  await rename.getByRole("button", { name: "Đổi tên" }).click();
  await expect(rename.getByRole("button", { name: "Đang lưu…" })).toBeDisabled();
  await expect(toggle).toBeDisabled();
  release();
  await expect(page.getByText("Đã đổi tên tenant.")).toBeVisible();
  await expect(toggle).toBeEnabled();

  let releaseToggle: () => void = () => {};
  const heldToggle = new Promise<void>((resolve) => (releaseToggle = resolve));
  await page.unroute(`**/v1/admin/tenants/${tenant.tenant_id}`);
  await page.route(`**/v1/admin/tenants/${tenant.tenant_id}`, async (route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    await heldToggle;
    await route.fallback();
  });
  await rename.getByLabel("Tên hiển thị").fill("Second name");
  await toggle.click();
  await page.getByRole("dialog", { name: `Tắt tenant ${tenant.slug}?` }).getByRole("button", { name: "Tắt tenant" }).click();
  await expect(rename.getByRole("button", { name: "Đổi tên" })).toBeDisabled();
  releaseToggle();
  await expect(page.getByText("Tenant đã được tắt.")).toBeVisible();
  await expect(rename.getByRole("button", { name: "Đổi tên" })).toBeEnabled();

  const patches = calls.filter((call) => call.method === "PATCH" && call.path === `/v1/admin/tenants/${tenant.tenant_id}`);
  expect(patches.map((call) => call.ifMatch)).toEqual([`"v${tenant.version}"`, `"v${tenant.version + 1}"`]);
  const final = await admin.read<{ enabled: boolean; display_name: string }>(`/tenants/${tenant.tenant_id}`);
  expect(final.body).toMatchObject({ enabled: false, display_name: "One at a time" });
  await admin.dispose();
  await context.close();
});

test("2e. create after a bare 502, cancelled and opened again: the values come back and the resend reuses the key (B18-RV10)", async ({
  browser,
}) => {
  const admin = await AdminApi.as();
  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  const slug = uniqueName("b18-reopen");
  let replaced = 0;
  await page.route("**/v1/admin/tenants", async (route) => {
    if (route.request().method() !== "POST" || replaced > 0) return route.fallback();
    replaced += 1;
    const response = await route.fetch();
    expect(response.status()).toBe(201);
    await route.fulfill({ status: 502, body: "" });
  });
  await page.goto("/admin/tenants");
  await page.getByRole("button", { name: "Tạo tenant" }).click();
  let form = page.getByRole("form", { name: "Tạo tenant" });
  await form.getByLabel("Slug").fill(slug);
  await form.getByLabel("Tên hiển thị").fill("Reopened");
  await form.getByRole("button", { name: "Tạo tenant" }).click();
  await expect(form.locator(".error-panel")).toBeVisible();
  await form.getByRole("button", { name: "Bỏ qua" }).click();
  await expect(form).toHaveCount(0);

  await page.getByRole("button", { name: "Tạo tenant" }).click();
  form = page.getByRole("form", { name: "Tạo tenant" });
  await expect(form.getByLabel("Slug")).toHaveValue(slug);
  await expect(form.getByLabel("Tên hiển thị")).toHaveValue("Reopened");
  await form.getByRole("button", { name: "Tạo tenant" }).click();
  await expect(page).toHaveURL(/\/admin\/tenants\/[0-9a-f-]{36}$/);
  const posts = calls.filter((call) => call.method === "POST" && call.path === "/v1/admin/tenants");
  expect(posts).toHaveLength(2);
  expect(posts[1].idempotencyKey).toBe(posts[0].idempotencyKey);
  const created = await admin.tenantsWithPrefix(slug);
  expect(created).toHaveLength(1);
  expect(page.url().endsWith(created[0].tenant_id)).toBe(true);

  await admin.dispose();
  await context.close();
});
