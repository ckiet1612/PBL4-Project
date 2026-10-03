import { fixture } from "../fixture";
import { signIn } from "../support";
import { AdminApi, adminContextAs, anonymousContext, expect, test, uniqueName } from "../admin-support";

// Scenario 13, frozen side (D6): the stack is started with --operational-mode WRITE_FROZEN
// because the API cannot reach it (B18-R18). Admin reads work, admin writes are refused and
// shown, WRITE_FROZEN → ADMISSION_OFF is refused until restore verification exists (B18-R05).

test("13. WRITE_FROZEN: reads work, a tenant create is refused and shown, leaving the mode is refused", async ({
  browser,
}) => {
  const seed = fixture();
  expect(seed.operational_mode).toBe("WRITE_FROZEN");
  const admin = await AdminApi.as();
  const before = (await admin.read<{ version: number; operational_mode: string }>("/policy")).body;
  expect(before.operational_mode).toBe("WRITE_FROZEN");

  const { context } = await adminContextAs(browser, "admin");
  const page = await context.newPage();

  // Reads: overview (its policy read drives the banner for the session), queue, audit, tenants.
  const nav = page.getByRole("navigation", { name: "Điều hướng quản trị" });
  const banner = page.getByRole("status").filter({ hasText: "Hệ thống đang ở chế độ" });
  await page.goto("/admin");
  await expect(page.getByRole("heading", { level: 1, name: "Tổng quan" })).toBeVisible();
  await expect(banner).toContainText("Khóa ghi");
  await nav.getByRole("link", { name: "Hàng chờ" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Hàng chờ" })).toBeVisible();
  await nav.getByRole("link", { name: "Audit" }).click();
  await expect(page.locator("tbody tr").first()).toBeVisible();
  await nav.getByRole("link", { name: "Tenant" }).click();
  await expect(page.getByRole("cell", { name: "b17-tenant-a", exact: true }).first()).toBeVisible();
  await expect(banner).toContainText("Khóa ghi");
  await expect(page.locator(".error-panel")).toHaveCount(0);

  // A write: the server refuses it and the form says why.
  const slug = uniqueName("b18-frozen");
  await page.getByRole("button", { name: "Tạo tenant" }).click();
  const create = page.getByRole("form", { name: "Tạo tenant" });
  await create.getByLabel("Slug").fill(slug);
  await create.getByLabel("Tên hiển thị").fill("Frozen");
  const created = page.waitForResponse(
    (response) => response.request().method() === "POST" && new URL(response.url()).pathname === "/v1/admin/tenants",
  );
  await create.getByRole("button", { name: "Tạo tenant" }).click();
  // B18-RV08: the refusal is asserted on the wire (status, code, message), not only as text.
  const createAnswer = await created;
  expect(createAnswer.status()).toBe(409);
  expect(await createAnswer.json()).toMatchObject({
    code: "state_conflict",
    message: "Administrative mutations are disabled while writes are frozen",
  });
  await expect(
    create.locator(".error-panel").getByText("Chi tiết từ máy chủ: Administrative mutations are disabled while writes are frozen"),
  ).toBeVisible();
  await expect(page).toHaveURL(/\/admin\/tenants$/);
  expect(await admin.tenantsWithPrefix(slug)).toHaveLength(0);

  // Policy page: the limit is locked; the only way out is refused by the server.
  await page.goto("/admin/policy");
  await expect(page.getByText("Đang khóa: chế độ Khóa ghi không cho thay đổi quản trị.")).toBeVisible();
  await expect(page.getByLabel("Số job tồn đọng tối đa (mọi tenant)")).toBeDisabled();
  const mode = page.getByRole("form", { name: "Chế độ vận hành" });
  await expect(mode.getByRole("radio")).toHaveCount(1);
  await mode.getByRole("radio", { name: /^Ngừng nhận job —/ }).check();
  await mode.getByRole("button", { name: "Chuyển chế độ" }).click();
  const leave = page.waitForResponse(
    (response) => response.request().method() === "PATCH" && new URL(response.url()).pathname === "/v1/admin/policy",
  );
  await page
    .getByRole("dialog", { name: "Chuyển sang chế độ Ngừng nhận job?" })
    .getByRole("button", { name: "Chuyển chế độ" })
    .click();
  const leaveAnswer = await leave;
  expect(leaveAnswer.status()).toBe(409);
  expect(await leaveAnswer.json()).toMatchObject({
    code: "state_conflict",
    message: "Restore verification is required before leaving frozen mode",
  });
  await expect(
    mode.locator(".error-panel").getByText("Chi tiết từ máy chủ: Restore verification is required before leaving frozen mode"),
  ).toBeVisible();
  expect((await admin.read<{ version: number; operational_mode: string }>("/policy")).body).toMatchObject(before);

  // A member cannot sign in while frozen. The server answers 401 "Invalid credentials" before
  // checking the password (no enumeration, no rate metadata), so the page shows the generic
  // credentials message, not B17's frozen notice (B18-R21, observed here).
  const anonymous = await anonymousContext(browser);
  const login = await anonymous.newPage();
  await login.goto("/login");
  const answer = login.waitForResponse((response) => response.url().endsWith("/v1/auth/login"));
  await signIn(login, "login");
  expect((await answer).status()).toBe(401);
  await expect(login.getByRole("alert")).toHaveText("Tên đăng nhập hoặc mật khẩu không đúng");
  await expect(login).toHaveURL(/\/login/);
  expect(await login.evaluate(async () => (await fetch("/v1/auth/session")).status)).toBe(401);

  await admin.dispose();
  await anonymous.close();
  await context.close();
});
