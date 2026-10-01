import { createHash } from "node:crypto";
import { open, readFile, rm } from "node:fs/promises";

import { fixture, storageState } from "../fixture";
import { Api, expect, test } from "../support";

// Scenario 8: upload → list → pick in the form; download with SHA-256; oversize → CLI.

test.use({ storageState: storageState("member_a") });

test("8a. upload an INPUT, see it listed, pick it in the form, download it with a matching SHA-256", async ({
  page,
}) => {
  const seed = fixture();
  const content = Buffer.from(JSON.stringify({ initial_value: 8, note: `e2e-${Date.now()}` }));
  const checksum = `sha256:${createHash("sha256").update(content).digest("hex")}`;

  await page.goto(`/t/${seed.tenants.a}/data`);
  await page.getByRole("button", { name: "Tải lên tệp" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Tải lên tệp" });
  await dialog.getByLabel("Loại dữ liệu").selectOption("INPUT");
  await expect(dialog.getByLabel("Media type")).toHaveValue("application/vnd.nexa.cpu-iterative-input+json");
  await dialog.getByLabel("Tệp", { exact: true }).setInputFiles({ name: "input.json", mimeType: "application/json", buffer: content });
  await dialog.getByRole("button", { name: "Tải lên", exact: true }).click();
  await expect(dialog).toBeHidden();

  const notice = page.locator(".notice").filter({ hasText: "Đã tải lên" });
  await expect(notice).toBeVisible();
  const artifactId = (await notice.locator("code").getAttribute("title"))!;
  expect(artifactId).toMatch(/^[0-9a-f-]{36}$/);

  const api = await Api.as("member_a");
  const stored = await api.get(`/artifacts/${artifactId}`, seed.tenants.a);
  expect(stored.status()).toBe(200);
  expect(((await stored.json()) as { checksum: string }).checksum).toBe(checksum);
  await api.dispose();

  const download = page.getByRole("button", { name: `Tải xuống ${artifactId}` });
  await expect(download).toBeVisible();
  const [file] = await Promise.all([page.waitForEvent("download"), download.click()]);
  const bytes = await readFile((await file.path())!);
  expect(`sha256:${createHash("sha256").update(bytes).digest("hex")}`).toBe(checksum);
  expect(file.suggestedFilename()).not.toMatch(/[/\\]/);

  await notice.getByRole("link", { name: "Tạo job với tệp này" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Tạo job" })).toBeVisible();
  await expect(page.getByLabel("Dữ liệu đầu vào")).toHaveValue(artifactId);
});

test("8b. a file over the browser limit is never read: the CLI command is shown instead", async ({ page }, info) => {
  const seed = fixture();
  // Sparse file just over 256 MiB: cheap to create, and the browser only needs its size.
  const path = info.outputPath("oversize.bin");
  const handle = await open(path, "w");
  await handle.truncate(256 * 1024 * 1024 + 1);
  await handle.close();
  const uploads: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/v1/artifacts") uploads.push(request.url());
  });
  try {
    await page.goto(`/t/${seed.tenants.a}/data`);
    await page.getByRole("button", { name: "Tải lên tệp" }).first().click();
    const dialog = page.getByRole("dialog", { name: "Tải lên tệp" });
    await dialog.getByLabel("Tệp", { exact: true }).setInputFiles(path);
    await expect(dialog.getByText(/Tệp lớn hơn 256 MiB: tải lên bằng CLI/)).toBeVisible();
    await expect(dialog.getByText(/nexa artifact upload FILE --kind INPUT/)).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Tải lên", exact: true })).toBeDisabled();
    await expect(dialog.getByText("Đang tính SHA-256…")).toHaveCount(0);
    expect(uploads).toEqual([]);
  } finally {
    await rm(path, { force: true });
  }
});
