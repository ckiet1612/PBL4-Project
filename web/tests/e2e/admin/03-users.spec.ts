import {
  AdminApi,
  adminContextAs,
  anonymousContext,
  expect,
  expectNoBrowserStorage,
  newPassword,
  signInAs,
  test,
  uniqueName,
} from "../admin-support";

// Scenarios 3 and 4: users (password hygiene, login, disable revokes) and memberships
// (MembershipSet ETag, 412 across two admins). Users are created here: a membership change
// revokes the sessions of its user, so seeded stored sessions are never touched.

test("3. create a user with a password: it never leaves the form, the user signs in, disable revokes the open session", async ({
  browser,
}) => {
  const admin = await AdminApi.as();
  const { context } = await adminContextAs(browser, "admin");
  const urls: string[] = [];
  const consoleTexts: string[] = [];
  context.on("request", (request) => urls.push(request.url()));
  context.on("console", (message) => consoleTexts.push(message.text()));
  const page = await context.newPage();
  const username = `${uniqueName("b18-user")}@example.test`;
  const password = newPassword();

  await page.goto("/admin/users");
  await page.getByRole("button", { name: "Tạo user" }).click();
  const form = page.getByRole("form", { name: "Tạo user" });
  await form.getByLabel("Tên đăng nhập").fill(username);
  await form.getByLabel("Tên hiển thị").fill("B18 created user");
  await form.getByLabel("Mật khẩu", { exact: true }).fill(password);
  await form.getByLabel("Nhập lại mật khẩu").fill(password);
  await form.getByRole("button", { name: "Tạo user" }).click();
  await expect(page).toHaveURL(/\/admin\/users\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { level: 1, name: /B18 created user/ })).toBeVisible();
  const userId = page.url().split("/").pop()!;

  // The create form is gone and no field anywhere still holds the password.
  await expect(page.getByRole("form", { name: "Tạo user" })).toHaveCount(0);
  for (const input of await page.locator("input").all()) expect(await input.inputValue()).not.toBe(password);
  expect(await page.content()).not.toContain(password);
  expect(urls.some((url) => url.includes(password))).toBe(false);
  expect(consoleTexts.some((text) => text.includes(password))).toBe(false);
  await expectNoBrowserStorage(page);

  // The new user signs in elsewhere (no tenant yet).
  const userContext = await anonymousContext(browser);
  const userPage = await userContext.newPage();
  await userPage.goto("/login");
  await signInAs(userPage, username, password);
  await expect(userPage.getByRole("heading", { level: 1, name: "Chưa thuộc tenant nào" })).toBeVisible();
  await expect(userPage.getByRole("link", { name: "Quản trị" })).toHaveCount(0);

  // Disable: the open session is revoked at once.
  await page.getByRole("button", { name: "Tắt user" }).click();
  await page.getByRole("dialog", { name: `Tắt user ${username}?` }).getByRole("button", { name: "Tắt user" }).click();
  await expect(page.getByText("User đã bị tắt; phiên và token của user đã bị thu hồi.")).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 }).getByText("Đã tắt")).toBeVisible();
  await userPage.reload();
  await expect(userPage).toHaveURL(/\/login/);
  await expect(userPage.getByRole("button", { name: "Đăng nhập" })).toBeVisible();

  // Enable again: the revoked session stays revoked; a fresh login works.
  await page.getByRole("button", { name: "Bật lại user" }).click();
  await page
    .getByRole("dialog", { name: `Bật lại user ${username}?` })
    .getByRole("button", { name: "Bật lại user" })
    .click();
  await expect(page.getByText("User đã được bật lại.")).toBeVisible();
  expect(await userPage.evaluate(async () => (await fetch("/v1/auth/session")).status)).toBe(401);
  await signInAs(userPage, username, password);
  await expect(userPage.getByRole("heading", { level: 1, name: "Chưa thuộc tenant nào" })).toBeVisible();
  await expectNoBrowserStorage(userPage);

  const stored = await admin.read<{ enabled: boolean; username: string }>(`/users/${userId}`);
  expect(stored.body).toMatchObject({ enabled: true, username });
  expect(JSON.stringify(stored.body)).not.toContain(password);
  expect(consoleTexts.some((text) => text.includes(password))).toBe(false);
  await admin.dispose();
  await userContext.close();
  await context.close();
});

test("4. membership: add MEMBER → TENANT_ADMIN → remove with the MembershipSet ETag; a stale second admin gets 412 and no change", async ({
  browser,
}) => {
  const admin = await AdminApi.as();
  const tenant = await admin.tenant(uniqueName("b18-mem"));
  const username = `${uniqueName("b18-member")}@example.test`;
  const user = await admin.create<{ user_id: string }>("/users", {
    username,
    display_name: "B18 member",
    password: newPassword(),
    system_roles: [],
  });
  const members = `/tenants/${tenant.tenant_id}/memberships`;
  const initial = await admin.read<{ membership_set_version: number }>(members);

  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  await page.goto(`/admin/tenants/${tenant.tenant_id}?tab=members`);
  await expect(page.getByText("Tenant chưa có thành viên")).toBeVisible();

  await page.getByRole("button", { name: "Thêm thành viên" }).click();
  const add = page.getByRole("dialog", { name: "Thêm thành viên" });
  await add.getByLabel("Chọn user").selectOption({ label: `${username} (B18 member)` });
  await expect(add.getByLabel("Vai trò")).toHaveValue("MEMBER");
  await add.getByRole("button", { name: "Thêm", exact: true }).click();
  await expect(page.getByText("Đã thêm thành viên với vai trò Thành viên.")).toBeVisible();
  const row = page.getByRole("row").filter({ hasText: username });
  await expect(row.getByRole("cell", { name: "Thành viên", exact: true })).toBeVisible();

  await row.getByRole("button", { name: "Đổi vai trò" }).click();
  const role = page.getByRole("dialog", { name: "Đổi vai trò thành viên" });
  await expect(role.getByLabel("Vai trò")).toHaveValue("TENANT_ADMIN");
  await role.getByRole("button", { name: "Đổi vai trò" }).click();
  await expect(page.getByText("Đã đổi vai trò thành Quản trị viên tenant.")).toBeVisible();
  await expect(row.getByRole("cell", { name: "Quản trị viên tenant" })).toBeVisible();

  const writes = calls.filter((call) => call.method !== "GET" && call.path.startsWith(`/v1/admin${members}`));
  expect(writes.map((call) => call.ifMatch)).toEqual([
    `"v${initial.body.membership_set_version}"`,
    `"v${initial.body.membership_set_version + 1}"`,
  ]);

  // Second admin opens the same tab, then the first admin removes the member.
  const second = await adminContextAs(browser, "admin2");
  const stale = await second.context.newPage();
  await stale.goto(`/admin/tenants/${tenant.tenant_id}?tab=members`);
  await expect(stale.getByRole("button", { name: "Đổi vai trò" })).toBeVisible();

  await row.getByRole("button", { name: "Xóa", exact: true }).click();
  await page.getByRole("dialog", { name: "Xóa thành viên?" }).getByRole("button", { name: "Xóa thành viên" }).click();
  await expect(page.getByText("Đã xóa thành viên khỏi tenant.")).toBeVisible();
  await expect(page.getByText("Tenant chưa có thành viên")).toBeVisible();

  await stale.getByRole("button", { name: "Đổi vai trò" }).click();
  await stale.getByRole("dialog", { name: "Đổi vai trò thành viên" }).getByRole("button", { name: "Đổi vai trò" }).click();
  await expect(stale.getByText(/Đối tượng vừa được thay đổi \(v\d+ → v\d+\)\. Kiểm tra rồi gửi lại\./)).toBeVisible();
  await expect(stale.getByText("Tenant chưa có thành viên")).toBeVisible();
  const staleWrite = second.calls.filter((call) => call.method === "POST");
  expect(staleWrite).toHaveLength(1);
  expect(staleWrite[0].ifMatch).toBe(`"v${initial.body.membership_set_version + 2}"`);

  const final = await admin.read<{ items: { user_id: string }[]; membership_set_version: number }>(members);
  expect(final.body.items.map((item) => item.user_id)).not.toContain(user.user_id);
  expect(final.body.membership_set_version).toBe(initial.body.membership_set_version + 3);
  await admin.dispose();
  await second.context.close();
  await context.close();
});

test("4b. membership 412 keeps the dialog and its input; the next confirm is a new intent with the new ETag (B18-RV05)", async ({
  browser,
}) => {
  const admin = await AdminApi.as();
  const tenant = await admin.tenant(uniqueName("b18-mem412"));
  const [first, second] = await Promise.all(
    ["one", "two"].map((label) =>
      admin.create<{ user_id: string }>("/users", {
        username: `${uniqueName(`b18-m412-${label}`)}@example.test`,
        display_name: `B18 412 ${label}`,
        password: newPassword(),
        system_roles: [],
      }),
    ),
  );
  const members = `/tenants/${tenant.tenant_id}/memberships`;
  const initial = await admin.read<{ membership_set_version: number }>(members);

  const { context, calls } = await adminContextAs(browser, "admin2");
  const page = await context.newPage();
  await page.goto(`/admin/tenants/${tenant.tenant_id}?tab=members`);
  await expect(page.getByText("Tenant chưa có thành viên")).toBeVisible();
  await page.getByRole("button", { name: "Thêm thành viên" }).click();
  const add = page.getByRole("dialog", { name: "Thêm thành viên" });
  await add.getByLabel("Hoặc dán user ID").fill(second.user_id);
  await add.getByLabel("Vai trò").selectOption("TENANT_ADMIN");

  // Another admin changes the MembershipSet while the dialog is open.
  const other = await admin.api.post(`/admin${members}`, null, { user_id: first.user_id, role: "MEMBER" }, { "If-Match": initial.etag });
  expect(other.status(), await other.text()).toBe(200);

  const stale = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith(members));
  await add.getByRole("button", { name: "Thêm", exact: true }).click();
  expect((await stale).status()).toBe(412);
  await expect(add.getByText(`Đối tượng vừa được thay đổi (v${initial.body.membership_set_version} → v${initial.body.membership_set_version + 1}). Kiểm tra rồi gửi lại.`)).toBeVisible();
  await expect(add.getByLabel("Hoặc dán user ID")).toHaveValue(second.user_id);
  await expect(add.getByLabel("Vai trò")).toHaveValue("TENANT_ADMIN");
  // The page behind the dialog was re-read: the other admin's member is listed.
  await expect(page.locator("tbody").locator(`[title="${first.user_id}"]`)).toHaveCount(1);

  const accepted = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith(members));
  await add.getByRole("button", { name: "Thêm", exact: true }).click();
  expect((await accepted).status()).toBe(200);
  await expect(page.getByText("Đã thêm thành viên với vai trò Quản trị viên tenant.")).toBeVisible();
  await expect(add).toHaveCount(0);

  const writes = calls.filter((call) => call.method === "POST" && call.path === `/v1/admin${members}`);
  expect(writes.map((call) => call.ifMatch)).toEqual([
    `"v${initial.body.membership_set_version}"`,
    `"v${initial.body.membership_set_version + 1}"`,
  ]);
  expect(new Set(writes.map((call) => call.idempotencyKey)).size).toBe(2);
  const final = await admin.read<{ items: { user_id: string; role: string }[] }>(members);
  expect(final.body.items.map((item) => [item.user_id, item.role]).sort()).toEqual(
    [
      [first.user_id, "MEMBER"],
      [second.user_id, "TENANT_ADMIN"],
    ].sort(),
  );
  await admin.dispose();
  await context.close();
});
