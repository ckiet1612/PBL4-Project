import type { Page } from "@playwright/test";

import { fixture, storageState } from "../fixture";
import { Api, chooseCpu, contextAs, expect, fillCpu, openForm, test } from "../support";

// Scenarios 6–7. W1 has no worker: accepted jobs stay QUEUED with waiting_for_worker.

async function expectQueuedDetail(page: Page, title: string): Promise<string> {
  await expect(page).toHaveURL(/\/jobs\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
  await expect(page.getByText("Đã nhận job")).toBeVisible();
  const status = page.locator(".status-block");
  await expect(status.getByText("Đang xếp hàng")).toBeVisible();
  await expect(status.getByText("Chờ worker sẵn sàng")).toBeVisible();
  return page.url().split("/").pop()!;
}

function countJobPosts(page: Page): { keys: string[] } {
  const seen = { keys: [] as string[] };
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/v1/jobs") {
      seen.keys.push(request.headers()["idempotency-key"] ?? "");
    }
  });
  return seen;
}

test.describe("member A", () => {
  test.use({ storageState: storageState("member_a") });

  test("6a. cpu-iterative is accepted: detail shows QUEUED and waits for a worker", async ({ page }) => {
    const seed = fixture();
    await openForm(page, seed.tenants.a);
    await fillCpu(page, seed.artifacts.a_input.artifact_id, "601");
    await page.getByRole("button", { name: "Gửi job" }).click();
    const jobId = await expectQueuedDetail(page, "CPU iterative");

    const api = await Api.as("member_a");
    const { job } = await api.job(seed.tenants.a, jobId);
    expect(job.state).toBe("QUEUED");
    expect(job.waiting_reason).toBe("waiting_for_worker");
    expect(job.spec.parameters).toEqual({ iterations: 10, seed: 601, modulus: 1000003 });
    await api.dispose();
  });

  test("6b. invalid fields are blocked inline and nothing is sent", async ({ page }) => {
    const seed = fixture();
    const posts = countJobPosts(page);
    await openForm(page, seed.tenants.a);
    await chooseCpu(page);
    await page.getByLabel("Dữ liệu đầu vào").selectOption(seed.artifacts.a_input.artifact_id);
    await page.getByLabel("iterations", { exact: true }).fill("0");
    await page.getByLabel("seed", { exact: true }).fill("1");
    await page.getByLabel("CPU (core)").fill("abc");
    await page.getByRole("button", { name: "Gửi job" }).click();

    const summary = page.getByRole("alert").filter({ hasText: "lỗi cần sửa trước khi gửi" });
    await expect(summary).toBeVisible();
    await expect(summary).toBeFocused();
    await expect(summary).toContainText("Còn 3 lỗi cần sửa trước khi gửi");
    await expect(page.getByLabel("iterations", { exact: true })).toHaveAttribute("aria-invalid", "true");
    await expect(page.getByLabel("modulus", { exact: true })).toHaveAttribute("aria-invalid", "true");
    await expect(page.getByLabel("CPU (core)")).toHaveAttribute("aria-invalid", "true");
    expect(posts.keys).toEqual([]);

    await page.getByLabel("iterations", { exact: true }).fill("10");
    await page.getByLabel("modulus", { exact: true }).fill("7");
    await page.getByLabel("CPU (core)").fill("1");
    await expect(summary).toBeHidden();
  });

  test("6c. an error only the server can detect (9 cores over the template bound) is shown in its group", async ({
    page,
  }) => {
    const seed = fixture();
    await openForm(page, seed.tenants.a);
    await page.getByRole("radio", { name: /PyTorch CIFAR-10 CNN training/ }).check();
    await page.getByLabel("Dữ liệu đầu vào").selectOption(seed.artifacts.a_dataset.artifact_id);
    await page.getByLabel("epochs", { exact: true }).fill("1");
    await page.getByLabel("batch_size", { exact: true }).fill("32");
    await page.getByLabel("learning_rate", { exact: true }).fill("0,01");
    await page.getByLabel("seed", { exact: true }).fill("603");
    await page.getByLabel("subset_size", { exact: true }).fill("100");
    await page.getByLabel("CPU (core)").fill("9");
    const response = page.waitForResponse(
      (r) => r.request().method() === "POST" && new URL(r.url()).pathname === "/v1/jobs",
    );
    await page.getByRole("button", { name: "Gửi job" }).click();
    expect((await response).status()).toBe(422);
    const group = page.getByRole("group", { name: "4. Tài nguyên" });
    await expect(group.getByText("Tài nguyên yêu cầu vượt khả năng của hệ thống")).toBeVisible();
    await expect(page).toHaveURL(/\/jobs\/new$/);
  });

  test("6f. pytorch-cifar10-cnn and batch-inference forms follow parameter_schema and are accepted", async ({
    page,
  }) => {
    const seed = fixture();
    await openForm(page, seed.tenants.a);
    await page.getByRole("radio", { name: /PyTorch CIFAR-10 CNN training/ }).check();
    const params = page.getByRole("group", { name: "3. Tham số" });
    for (const name of ["epochs", "batch_size", "learning_rate", "seed", "subset_size"]) {
      await expect(params.getByLabel(name, { exact: true })).toBeVisible();
    }
    await expect(params.locator("input, select")).toHaveCount(5);
    await page.getByLabel("Dữ liệu đầu vào").selectOption(seed.artifacts.a_dataset.artifact_id);
    await page.getByLabel("epochs", { exact: true }).fill("1");
    await page.getByLabel("batch_size", { exact: true }).fill("32");
    await page.getByLabel("learning_rate", { exact: true }).fill("0,01");
    await page.getByLabel("seed", { exact: true }).fill("604");
    await page.getByLabel("subset_size", { exact: true }).fill("100");
    await page.getByRole("button", { name: "Gửi job" }).click();
    await expectQueuedDetail(page, "PyTorch CIFAR-10 CNN training (CPU)");

    await openForm(page, seed.tenants.a);
    await page.getByRole("radio", { name: /Chunked batch inference/ }).check();
    for (const name of ["chunk_size", "batch_size"]) {
      await expect(params.getByLabel(name, { exact: true })).toBeVisible();
    }
    const format = params.getByLabel("output_format", { exact: true });
    await expect(format.locator("option")).toHaveText(["Chọn", "JSONL", "PARQUET"]);
    await expect(params.locator("input, select")).toHaveCount(3);
    await page.getByLabel("Dữ liệu đầu vào").selectOption(seed.artifacts.a_dataset.artifact_id);
    await page.getByLabel("Model").selectOption(seed.artifacts.a_model.artifact_id);
    await page.getByLabel("chunk_size", { exact: true }).fill("100");
    await page.getByLabel("batch_size", { exact: true }).fill("16");
    await format.selectOption("JSONL");
    await page.getByRole("button", { name: "Gửi job" }).click();
    const jobId = await expectQueuedDetail(page, "Chunked batch inference (CPU)");

    const api = await Api.as("member_a");
    const { job } = await api.job(seed.tenants.a, jobId);
    expect(job.spec.parameters).toEqual({ chunk_size: 100, batch_size: 16, output_format: "JSONL" });
    expect(job.spec.model_artifact_id).toBe(seed.artifacts.a_model.artifact_id);
    await api.dispose();
  });

  test("7a. double click creates exactly one job", async ({ page }) => {
    const seed = fixture();
    const api = await Api.as("member_a");
    const before = (await api.allJobs(seed.tenants.a)).length;
    const posts = countJobPosts(page);
    await openForm(page, seed.tenants.a);
    await fillCpu(page, seed.artifacts.a_input.artifact_id, "701");
    await page.getByRole("button", { name: "Gửi job" }).dblclick();
    await expectQueuedDetail(page, "CPU iterative");
    expect(posts.keys).toHaveLength(1);
    expect((await api.allJobs(seed.tenants.a)).length).toBe(before + 1);
    await api.dispose();
  });

  test("7b. the first response is cut: the resend reuses the Idempotency-Key and one job exists", async ({
    page,
  }) => {
    const seed = fixture();
    const api = await Api.as("member_a");
    const before = (await api.allJobs(seed.tenants.a)).length;
    const posts = countJobPosts(page);
    let cut = 0;
    await page.route("**/v1/jobs", async (route) => {
      if (route.request().method() !== "POST" || cut > 0) return route.fallback();
      cut += 1;
      // The server commits the job; the browser never sees the answer.
      const response = await route.fetch();
      expect(response.status()).toBe(202);
      await route.abort("connectionreset");
    });
    await openForm(page, seed.tenants.a);
    await fillCpu(page, seed.artifacts.a_input.artifact_id, "702");
    await page.getByRole("button", { name: "Gửi job" }).click();
    await expect(page.getByRole("button", { name: "Gửi job" })).toBeEnabled();
    await expect(page.locator(".error-panel")).toBeVisible();
    expect((await api.allJobs(seed.tenants.a)).length).toBe(before + 1);

    await page.getByRole("button", { name: "Gửi job" }).click();
    const jobId = await expectQueuedDetail(page, "CPU iterative");
    expect(posts.keys).toHaveLength(2);
    expect(posts.keys[0]).toMatch(/^web-[0-9a-f-]{36}$/);
    expect(posts.keys[1]).toBe(posts.keys[0]);
    const after = await api.allJobs(seed.tenants.a);
    expect(after.length).toBe(before + 1);
    expect(after.some((job) => job.job_id === jobId)).toBe(true);
    await api.dispose();
  });

  test("7c. the proxy answers 502 without an envelope after the commit: the resend reuses the key (B17-RV02)", async ({
    page,
  }) => {
    const seed = fixture();
    const api = await Api.as("member_a");
    const before = (await api.allJobs(seed.tenants.a)).length;
    const posts = countJobPosts(page);
    let replaced = 0;
    await page.route("**/v1/jobs", async (route) => {
      if (route.request().method() !== "POST" || replaced > 0) return route.fallback();
      replaced += 1;
      // The API commits; the browser gets what Caddy sends when the upstream dies: an empty 502.
      const response = await route.fetch();
      expect(response.status()).toBe(202);
      await route.fulfill({ status: 502, body: "" });
    });
    await openForm(page, seed.tenants.a);
    await fillCpu(page, seed.artifacts.a_input.artifact_id, "703");
    await page.getByRole("button", { name: "Gửi job" }).click();
    await expect(page.getByRole("button", { name: "Gửi job" })).toBeEnabled();
    await expect(page.locator(".error-panel")).toBeVisible();
    expect((await api.allJobs(seed.tenants.a)).length).toBe(before + 1);

    await page.getByRole("button", { name: "Gửi job" }).click();
    const jobId = await expectQueuedDetail(page, "CPU iterative");
    expect(posts.keys).toHaveLength(2);
    expect(posts.keys[1]).toBe(posts.keys[0]);
    const after = await api.allJobs(seed.tenants.a);
    expect(after.length).toBe(before + 1);
    expect(after.some((job) => job.job_id === jobId)).toBe(true);
    await api.dispose();
  });
});

test("6d. low-quota tenant: the second job is refused with a clear quota message and no countdown", async ({
  browser,
}) => {
  const seed = fixture();
  const context = await contextAs(browser, "quota");
  const page = await context.newPage();
  await openForm(page, seed.tenants.q);
  await fillCpu(page, seed.artifacts.q_input.artifact_id, "606");
  await page.getByRole("button", { name: "Gửi job" }).click();
  await expectQueuedDetail(page, "CPU iterative");

  await openForm(page, seed.tenants.q);
  await fillCpu(page, seed.artifacts.q_input.artifact_id, "607");
  const response = page.waitForResponse(
    (r) => r.request().method() === "POST" && new URL(r.url()).pathname === "/v1/jobs",
  );
  await page.getByRole("button", { name: "Gửi job" }).click();
  expect((await response).status()).toBe(429);
  await expect(page.getByText("Tenant đã chạm hạn mức")).toBeVisible();
  await expect(page.getByText(/Thử lại sau/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Gửi job" })).toBeEnabled();
  await context.close();
});
