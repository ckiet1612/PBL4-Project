import { fixture } from "../fixture";
import { chooseCpu, expect, expectNoHorizontalScroll, signIn, test } from "../support";

// Responsive project (390×844): the main flows work and no page scrolls sideways.

test("mobile: login, list, submit, detail, cancel dialog, data and tokens without horizontal scroll", async ({
  page,
}) => {
  const seed = fixture();
  await page.goto("/login");
  await expectNoHorizontalScroll(page);
  await signIn(page, "mobile");
  await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
  await expectNoHorizontalScroll(page);

  await page.getByRole("link", { name: "Tạo job" }).first().click();
  await expect(page.getByRole("heading", { level: 1, name: "Tạo job" })).toBeVisible();
  await chooseCpu(page);
  await page.getByLabel("Dữ liệu đầu vào").selectOption(seed.artifacts.a_input.artifact_id);
  await page.getByLabel("iterations", { exact: true }).fill("10");
  await page.getByLabel("seed", { exact: true }).fill("1501");
  await page.getByLabel("modulus", { exact: true }).fill("97");
  await expectNoHorizontalScroll(page);
  await page.getByRole("button", { name: "Gửi job" }).click();

  await expect(page).toHaveURL(/\/jobs\/[0-9a-f-]{36}$/);
  const status = page.locator(".status-block");
  await expect(status.getByText("Đang xếp hàng")).toBeVisible();
  await expectNoHorizontalScroll(page);

  await page.getByRole("button", { name: "Hủy job" }).click();
  const dialog = page.getByRole("dialog", { name: "Hủy job?" });
  await expect(dialog).toBeVisible();
  const box = await dialog.boundingBox();
  expect(box !== null && box.x >= 0 && box.x + box.width <= 390).toBe(true);
  await expectNoHorizontalScroll(page);
  await dialog.getByRole("button", { name: "Hủy job" }).click();
  await expect(dialog).toBeHidden();
  await expect(status.getByText("Đã hủy")).toBeVisible();

  await page.getByRole("navigation", { name: "Điều hướng chính" }).getByRole("link", { name: "Jobs" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
  await expect(page.locator("tbody tr").first()).toBeVisible();
  await expectNoHorizontalScroll(page);

  await page.getByRole("navigation", { name: "Điều hướng chính" }).getByRole("link", { name: "Dữ liệu" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Dữ liệu" })).toBeVisible();
  await expect(page.locator("tbody tr").first()).toBeVisible();
  await expectNoHorizontalScroll(page);

  await page.getByRole("button", { name: "Tài khoản" }).click();
  await page.getByRole("link", { name: "Token CLI" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Token CLI" })).toBeVisible();
  await expectNoHorizontalScroll(page);
});
