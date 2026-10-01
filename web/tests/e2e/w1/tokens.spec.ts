import { request } from "@playwright/test";

import { fixture, storageState } from "../fixture";
import { expect, test } from "../support";

// Scenario 13. The raw token stays inside this test: assertions compare booleans so a failure
// report never prints it.

test.use({ storageState: storageState("member_a") });

async function bearerStatus(token: string): Promise<number> {
  const seed = fixture();
  // An explicit empty storageState: the test-level member_a cookie must not ride along, or the
  // backend rejects the request for carrying both a session cookie and a bearer token (400).
  const http = await request.newContext({
    baseURL: seed.base_url,
    ignoreHTTPSErrors: true,
    storageState: { cookies: [], origins: [] },
  });
  const response = await http.get("/v1/jobs?page_size=1", {
    headers: { Authorization: `Bearer ${token}`, "X-Nexa-Tenant-Id": seed.tenants.a },
  });
  await http.dispose();
  return response.status();
}

test("13. token shown once, gone after reload, revocable; a member is never offered admin:*", async ({ page }) => {
  const name = `e2e-${Date.now()}`;
  await page.goto("/account/tokens");
  await expect(page.getByRole("heading", { level: 1, name: "Token CLI" })).toBeVisible();

  const scopes = page.getByRole("group", { name: "Quyền" });
  await expect(scopes.getByRole("checkbox")).toHaveCount(5);
  await expect(scopes.getByText("admin:read")).toHaveCount(0);
  await expect(scopes.getByText("admin:write")).toHaveCount(0);

  await page.getByLabel("Tên token").fill(name);
  await page.getByLabel("Hết hạn sau").selectOption("1");
  await page.getByRole("button", { name: "Tạo token" }).click();
  const secret = page.locator(".secret-box pre.secret code");
  await expect(secret).toBeVisible();
  const token = (await secret.textContent()) ?? "";
  expect(token.length > 20).toBe(true);
  expect(await bearerStatus(token)).toBe(200);
  await expect(page.getByRole("button", { name: `Thu hồi token ${name}` })).toBeVisible();

  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "Token CLI" })).toBeVisible();
  await expect(page.getByRole("button", { name: `Thu hồi token ${name}` })).toBeVisible();
  await expect(page.locator(".secret-box")).toHaveCount(0);
  expect((await page.content()).includes(token)).toBe(false);

  await page.getByRole("button", { name: `Thu hồi token ${name}` }).click();
  const dialog = page.getByRole("dialog", { name: "Thu hồi token?" });
  await dialog.getByRole("button", { name: "Thu hồi" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText(`Đã thu hồi token "${name}".`)).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: name }).getByText("Đã thu hồi")).toBeVisible();
  await expect(page.getByRole("button", { name: `Thu hồi token ${name}` })).toHaveCount(0);
  expect(await bearerStatus(token)).toBe(401);
});
