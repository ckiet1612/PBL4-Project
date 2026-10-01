import { fixture, storageState } from "../fixture";
import { cpuSpec, expect, signIn, test, watch } from "../support";

// Scenarios 1–3. The `login` user signs in through the form; stored sessions are never logged out.

test("1. login: generic error, secure cookie, no web storage, reload keeps session, logout → 401", async ({
  page,
  context,
}) => {
  const seed = fixture();
  await page.goto("/");
  await expect(page).toHaveURL(/\/login/);

  await page.getByLabel("Tên đăng nhập").fill(seed.users.login.username);
  await page.getByLabel("Mật khẩu").fill("definitely-not-the-password");
  await page.getByRole("button", { name: "Đăng nhập" }).click();
  await expect(page.getByText("Tên đăng nhập hoặc mật khẩu không đúng")).toBeVisible();
  // Same message for an unknown user: the form never says which part was wrong.
  await page.getByLabel("Tên đăng nhập").fill("nobody@example.test");
  await page.getByRole("button", { name: "Đăng nhập" }).click();
  await expect(page.getByText("Tên đăng nhập hoặc mật khẩu không đúng")).toBeVisible();

  await page.getByLabel("Tên đăng nhập").fill("");
  await signIn(page, "login");
  await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/t/${seed.tenants.a}/jobs$`));

  const cookie = (await context.cookies()).find((c) => c.name === "nexa_session");
  expect(cookie, "nexa_session cookie").toBeDefined();
  expect(cookie!.httpOnly).toBe(true);
  expect(cookie!.secure).toBe(true);
  expect(cookie!.sameSite).toBe("Lax");

  const stored = await page.evaluate(async () => ({
    local: localStorage.length,
    session: sessionStorage.length,
    indexed: (await indexedDB.databases()).length,
    documentCookie: document.cookie,
  }));
  expect(stored).toEqual({ local: 0, session: 0, indexed: 0, documentCookie: "" });

  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();

  await page.getByRole("button", { name: "Tài khoản" }).click();
  await page.getByRole("button", { name: "Đăng xuất" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("button", { name: "Đăng nhập" })).toBeVisible();
  const after = await page.evaluate(async () => (await fetch("/v1/auth/session")).status);
  expect(after).toBe(401);
});

test.describe("with member A", () => {
  test.use({ storageState: storageState("member_a") });

  test("2. CSRF: a mutation without X-CSRF-Token is refused with 403 invalid_csrf", async ({ page }) => {
    const seed = fixture();
    await page.goto(`/t/${seed.tenants.a}/jobs`);
    await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
    const outcome = await page.evaluate(
      async ({ tenant, spec }) => {
        const response = await fetch("/v1/jobs", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Nexa-Tenant-Id": tenant,
            "Idempotency-Key": `e2e-${crypto.randomUUID()}`,
          },
          body: JSON.stringify({ spec }),
        });
        return { status: response.status, code: ((await response.json()) as { code: string }).code };
      },
      { tenant: seed.tenants.a, spec: cpuSpec(seed.artifacts.a_input.artifact_id) },
    );
    expect(outcome).toEqual({ status: 403, code: "invalid_csrf" });
  });
});

test("1b. login with a dot-segment next (/.//evil.example) lands on an internal page without a page error (B17-RV01)", async ({
  browser,
}) => {
  const seed = fixture();
  const context = await browser.newContext();
  await watch(context);
  const page = await context.newPage();
  await page.goto("/login?next=/.//evil.example");
  await signIn(page, "login");
  await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
  expect(new URL(page.url()).origin).toBe(new URL(seed.base_url).origin);
  expect(new URL(page.url()).pathname).toBe(`/t/${seed.tenants.a}/jobs`);
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
  await context.close();
});

test("3. revoked session: logout in page 1, next action in page 2 → login with notice, next returns there", async ({
  browser,
}) => {
  const seed = fixture();
  const context = await browser.newContext();
  await watch(context);
  const first = await context.newPage();
  await first.goto("/login");
  await signIn(first, "login");
  await expect(first.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();

  const second = await context.newPage();
  const dataPath = `/t/${seed.tenants.a}/data`;
  await second.goto(`${dataPath}?kind=INPUT`);
  await expect(second.getByRole("heading", { level: 1, name: "Dữ liệu" })).toBeVisible();

  await first.getByRole("button", { name: "Tài khoản" }).click();
  await first.getByRole("button", { name: "Đăng xuất" }).click();
  await expect(first).toHaveURL(/\/login$/);

  await second.getByRole("button", { name: "Làm mới" }).click();
  await expect(second).toHaveURL(/\/login\?/);
  const url = new URL(second.url());
  expect(url.searchParams.get("next")).toBe(`${dataPath}?kind=INPUT`);
  expect(url.searchParams.get("expired")).toBe("1");
  await expect(second.getByText("Phiên đăng nhập đã hết hạn. Đăng nhập lại để tiếp tục")).toBeVisible();

  await signIn(second, "login");
  await expect(second.getByRole("heading", { level: 1, name: "Dữ liệu" })).toBeVisible();
  expect(new URL(second.url()).pathname + new URL(second.url()).search).toBe(`${dataPath}?kind=INPUT`);
  await context.close();
});
