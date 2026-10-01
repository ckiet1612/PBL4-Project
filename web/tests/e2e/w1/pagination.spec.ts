import type { Page } from "@playwright/test";

import { fixture, storageState } from "../fixture";
import { Api, cpuSpec, expect, test, trainingSpec } from "../support";

// Scenario 9 on tenant B: more than two pages of jobs seeded through the real API.

const PAGE = 25;
const CPU_JOBS = 55;
const TRAINING_JOBS = 3;
const CANCELLED_JOBS = 3;

test.use({ storageState: storageState("member_b") });

let expected: string[] = [];
let cancelled: string[] = [];
let training: string[] = [];

test.beforeAll(async () => {
  const seed = fixture();
  const api = await Api.as("member_b");
  const dataset = await api.upload(
    seed.tenants.b,
    Buffer.from("B17 placeholder dataset for tenant B (never executed)\n"),
    "DATASET",
    "application/vnd.apache.arrow.file",
  );
  const cpu = [];
  for (let i = 0; i < CPU_JOBS; i += 1) {
    cpu.push(await api.submit(seed.tenants.b, cpuSpec(seed.artifacts.b_input.artifact_id, 9000 + i)));
  }
  for (let i = 0; i < TRAINING_JOBS; i += 1) {
    await api.submit(seed.tenants.b, trainingSpec(dataset.artifact_id, 9100 + i));
  }
  for (const job of cpu.slice(0, CANCELLED_JOBS)) await api.cancel(seed.tenants.b, job.job_id);
  const all = await api.allJobs(seed.tenants.b);
  expected = all.map((job) => job.job_id);
  cancelled = all.filter((job) => job.state === "CANCELLED").map((job) => job.job_id);
  training = all.filter((job) => job.spec.template_id === "pytorch-cifar10-cnn").map((job) => job.job_id);
  await api.dispose();
});

function shownIds(page: Page): Promise<string[]> {
  return page
    .locator("tbody tr td:first-child .short-id code")
    .evaluateAll((codes) => codes.map((code) => code.getAttribute("title") ?? ""));
}

test("9. cursor pages forward/back/first, filters, empty result, tampered cursor, page_size", async ({ page }) => {
  const seed = fixture();
  expect(expected.length).toBeGreaterThan(2 * PAGE);
  const listSizes: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/v1/jobs" && request.method() === "GET") listSizes.push(url.searchParams.get("page_size") ?? "");
  });

  await page.goto(`/t/${seed.tenants.b}/jobs`);
  await expect.poll(() => shownIds(page)).toEqual(expected.slice(0, PAGE));
  const pager = page.getByRole("navigation", { name: "Phân trang" });
  await expect(pager.getByRole("button", { name: "Trang trước" })).toBeDisabled();

  await pager.getByRole("button", { name: "Trang sau" }).click();
  await expect.poll(() => shownIds(page)).toEqual(expected.slice(PAGE, 2 * PAGE));
  await expect(page).toHaveURL(/cursor=/);
  const realCursor = new URL(page.url()).searchParams.get("cursor") ?? "";
  expect(realCursor.length).toBeGreaterThanOrEqual(16);
  await pager.getByRole("button", { name: "Trang sau" }).click();
  await expect.poll(() => shownIds(page)).toEqual(expected.slice(2 * PAGE, 3 * PAGE));
  if (expected.length <= 3 * PAGE) await expect(pager.getByRole("button", { name: "Trang sau" })).toBeDisabled();

  await pager.getByRole("button", { name: "Trang trước" }).click();
  await expect.poll(() => shownIds(page)).toEqual(expected.slice(PAGE, 2 * PAGE));
  await pager.getByRole("button", { name: "Về trang đầu" }).click();
  await expect.poll(() => shownIds(page)).toEqual(expected.slice(0, PAGE));
  await expect(page).not.toHaveURL(/cursor=/);

  await page.getByLabel("Trạng thái").selectOption("CANCELLED");
  await expect(page).toHaveURL(/state=CANCELLED/);
  await expect.poll(() => shownIds(page)).toEqual(cancelled);
  await page.getByLabel("Trạng thái").selectOption("");
  await page.getByLabel("Template").selectOption("pytorch-cifar10-cnn");
  await expect(page).toHaveURL(/template=pytorch-cifar10-cnn/);
  await expect.poll(() => shownIds(page)).toEqual(training);

  await page.getByLabel("Trạng thái").selectOption("SUCCEEDED");
  await expect(page.getByText("Không có job khớp bộ lọc")).toBeVisible();
  await expect(page.locator("tbody tr")).toHaveCount(0);
  await page.locator(".empty-state").getByRole("button", { name: "Xóa bộ lọc" }).click();
  await expect.poll(() => shownIds(page)).toEqual(expected.slice(0, PAGE));
  expect(new URL(page.url()).search).toBe("");

  // A real cursor with its middle rewritten (server: invalid_cursor), then one too short for the
  // 16–2048 query contract (server: validation_failed). Both go back to page one with a notice.
  const middle = Math.floor(realCursor.length / 2);
  const forged = `${realCursor.slice(0, middle)}${realCursor[middle] === "A" ? "B" : "A"}${realCursor.slice(middle + 1)}`;
  for (const [cursor, code] of [
    [forged, "invalid_cursor"],
    ["tampered", "validation_failed"],
  ] as const) {
    const refused = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/v1/jobs" && response.url().includes("cursor="),
    );
    await page.goto(`/t/${seed.tenants.b}/jobs?cursor=${encodeURIComponent(cursor)}`);
    const response = await refused;
    expect(response.status()).toBe(400);
    expect(((await response.json()) as { code: string }).code).toBe(code);
    await expect(page.getByText("Vị trí trang không còn hợp lệ, đã quay về trang đầu")).toBeVisible();
    await expect(page).not.toHaveURL(/cursor=/);
    await expect(page.locator(".error-panel")).toHaveCount(0);
    await expect.poll(() => shownIds(page)).toEqual(expected.slice(0, PAGE));
  }

  expect(listSizes.length).toBeGreaterThan(0);
  expect(new Set(listSizes)).toEqual(new Set([String(PAGE)]));
});
