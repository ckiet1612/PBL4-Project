import { readFile } from "node:fs/promises";

import type { Page } from "@playwright/test";

import { fixture, storageState } from "../fixture";
import {
  Api,
  type Attempt,
  type Checkpoint,
  cpuSpec,
  expect,
  fillCpu,
  jobPath,
  openForm,
  sha256,
  test,
} from "../support";

// Scenarios 15–18 with a real coordinator, worker and cpu-iterative image (tier w2). Iteration
// counts follow the B15 measurement on this host (~250M iterations ≈ 45 s of compute).

test.use({ storageState: storageState("member_a") });

const START = { timeout: 120_000 };
const FINISH = { timeout: 180_000 };

function status(page: Page) {
  return page.locator(".status-block .badge");
}

/** Every GET the detail page sends for this job (job, progress, attempts, checkpoints, events, result). */
function countJobReads(page: Page, jobId: () => string | null): { count: number } {
  const seen = { count: 0 };
  page.on("request", (request) => {
    const id = jobId();
    if (id && request.method() === "GET" && new URL(request.url()).pathname.startsWith(`/v1/jobs/${id}`)) seen.count += 1;
  });
  return seen;
}

/** Records each status label the page renders, in order, without gaps between polls. */
async function recordStatusLabels(page: Page): Promise<string[]> {
  const labels: string[] = [];
  await page.exposeFunction("__b17StatusLabel", (label: string) => {
    if (labels[labels.length - 1] !== label) labels.push(label);
  });
  await page.addInitScript(() => {
    const report = () => {
      const label = document.querySelector(".status-block .badge")?.textContent?.trim();
      if (label) (window as unknown as { __b17StatusLabel(label: string): void }).__b17StatusLabel(label);
    };
    new MutationObserver(report).observe(document, { subtree: true, childList: true, characterData: true });
  });
  return labels;
}

test("15. cpu-iterative runs to SUCCEEDED: progress, result files and metrics, checksum-verified download, polling stops", async ({
  page,
}) => {
  const seed = fixture();
  await page.clock.install();
  let jobId: string | null = null;
  const reads = countJobReads(page, () => jobId);

  await openForm(page, seed.tenants.a);
  await fillCpu(page, seed.artifacts.a_input.artifact_id, "1501", "60000000");
  await page.getByRole("button", { name: "Gửi job" }).click();
  await expect(page).toHaveURL(/\/jobs\/[0-9a-f-]{36}$/);
  jobId = page.url().split("/").pop()!;

  await expect(status(page)).toHaveText("Đang chạy", START);
  await expect(page.getByRole("progressbar", { name: "Tiến độ" }).first()).toBeVisible(START);
  await expect(status(page)).toHaveText("Hoàn tất", FINISH);

  // SUCCEEDED opens the result tab by default.
  await expect(page.getByRole("heading", { name: "Tệp kết quả" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Chỉ số" })).toBeVisible();
  const api = await Api.as("member_a");
  const record = await api.result(seed.tenants.a, jobId);
  const manifestBytes = await api.download(seed.tenants.a, record.manifest_artifact_id);
  expect(sha256(manifestBytes)).toBe(record.manifest_checksum);
  const manifest = JSON.parse(manifestBytes.toString("utf8")) as {
    files: { logical_name: string; checksum: string }[];
    metrics: Record<string, unknown>;
  };
  expect(manifest.files.length).toBeGreaterThan(0);
  const metricRows = page.locator("section[aria-labelledby='metrics-title'] dt");
  await expect(metricRows).toHaveCount(Object.keys(manifest.metrics).length);

  const file = manifest.files[0];
  const downloading = page.waitForEvent("download");
  await page.getByRole("button", { name: `Tải xuống ${file.logical_name}` }).click();
  const download = await downloading;
  expect(sha256(await readFile((await download.path())!))).toBe(file.checksum);

  // Terminal: the detail poller is done. Two simulated minutes of timers send no further read.
  expect(reads.count).toBeGreaterThan(3);
  const settled = reads.count;
  await page.clock.runFor(120_000);
  expect(reads.count).toBe(settled);
  await api.dispose();
});

test("16. pause then resume: PAUSING → PAUSED with a committed checkpoint, then RUNNING → SUCCEEDED over two attempts", async ({
  page,
}) => {
  const seed = fixture();
  const api = await Api.as("member_a");
  const job = await api.submit(
    seed.tenants.a,
    cpuSpec(seed.artifacts.a_input.artifact_id, 1601, { iterations: 150_000_000, checkpoint_interval_seconds: 5 }),
  );
  await page.goto(jobPath(seed.tenants.a, job.job_id));
  await expect(status(page)).toHaveText("Đang chạy", START);

  await page.getByRole("button", { name: "Tạm dừng" }).click();
  const pause = page.getByRole("dialog", { name: "Tạm dừng job?" });
  await pause.getByRole("button", { name: "Tạm dừng" }).click();
  await expect(pause).toBeHidden();
  await expect(status(page)).toHaveText(/Đang tạm dừng \(chờ checkpoint\)|Đã tạm dừng/);
  await expect(status(page)).toHaveText("Đã tạm dừng", START);

  const checkpointRows = page.locator("section[aria-labelledby='checkpoints-title'] tbody tr");
  await expect(checkpointRows.filter({ hasText: "Đã ghi" }).first()).toBeVisible();
  const attemptRows = page.locator("section[aria-labelledby='attempts-title'] tbody tr");
  await expect(attemptRows).toHaveCount(1);
  const pausedCheckpoints = await api.items<Checkpoint>(`/jobs/${job.job_id}/checkpoints?page_size=100`, seed.tenants.a);
  expect(pausedCheckpoints.some((checkpoint) => checkpoint.state === "COMMITTED")).toBe(true);

  await page.getByRole("button", { name: "Tiếp tục" }).click();
  const resume = page.getByRole("dialog", { name: "Tiếp tục job?" });
  await resume.getByRole("button", { name: "Tiếp tục" }).click();
  await expect(resume).toBeHidden();
  await expect(status(page)).toHaveText("Đang chạy", START);
  await expect(status(page)).toHaveText("Hoàn tất", FINISH);

  const attempts = await api.items<Attempt>(`/jobs/${job.job_id}/attempts?page_size=100`, seed.tenants.a);
  // The API lists attempts newest first; the table keeps that order.
  expect(attempts.map((attempt) => attempt.attempt_number)).toEqual([2, 1]);
  expect(attempts[0].state).toBe("SUCCEEDED");
  await page.getByRole("tab", { name: "Tiến trình" }).click();
  await expect(attemptRows).toHaveCount(attempts.length);
  await expect(attemptRows.nth(0).locator("td").nth(0)).toHaveText("#2");
  await expect(attemptRows.nth(0).locator("td").nth(1)).toHaveText("Hoàn tất");
  await expect(attemptRows.nth(1).locator("td").nth(0)).toHaveText("#1");
  await api.dispose();
});

test("17. cancel a RUNNING job: CANCELLING (waiting for stop) until the backend reports CANCELLED", async ({ page }) => {
  const seed = fixture();
  const labels = await recordStatusLabels(page);
  const api = await Api.as("member_a");
  const job = await api.submit(
    seed.tenants.a,
    cpuSpec(seed.artifacts.a_input.artifact_id, 1701, { iterations: 250_000_000, checkpoint_interval_seconds: 5 }),
  );
  await page.goto(jobPath(seed.tenants.a, job.job_id));
  await expect(status(page)).toHaveText("Đang chạy", START);

  await page.getByRole("button", { name: "Hủy job" }).click();
  const dialog = page.getByRole("dialog", { name: "Hủy job?" });
  await dialog.getByRole("button", { name: "Hủy job" }).click();
  await expect(dialog).toBeHidden();
  await expect(status(page)).toHaveText("Đã hủy", START);

  // The page showed CANCELLING from the cancel response on, and "Đã hủy" only once the
  // backend said CANCELLED: no label in between claims the job has stopped.
  const running = labels.indexOf("Đang chạy");
  const cancelling = labels.indexOf("Đang hủy (chờ xác nhận dừng)");
  expect(running).toBeGreaterThanOrEqual(0);
  expect(cancelling).toBeGreaterThan(running);
  expect(labels.slice(cancelling)).toEqual(["Đang hủy (chờ xác nhận dừng)", "Đã hủy"]);

  const { job: final } = await api.job(seed.tenants.a, job.job_id);
  expect(final.state).toBe("CANCELLED");
  const attempts = await api.items<Attempt>(`/jobs/${job.job_id}/attempts?page_size=100`, seed.tenants.a);
  expect(attempts.every((attempt) => ["CANCELLED", "SUCCEEDED", "FAILED", "LOST"].includes(attempt.state))).toBe(true);
  await api.dispose();
});

test("18. a FAILED job (runtime limit) is retried: new job links to the source and back; the source is unchanged", async ({
  page,
}) => {
  const seed = fixture();
  const api = await Api.as("member_a");
  const job = await api.submit(
    seed.tenants.a,
    cpuSpec(seed.artifacts.a_input.artifact_id, 1801, {
      iterations: 250_000_000,
      runtime_limit_seconds: 5,
      checkpoint_interval_seconds: 5,
    }),
  );
  await page.goto(jobPath(seed.tenants.a, job.job_id));
  await expect(status(page)).toHaveText("Thất bại", FINISH);
  const attempts = await api.items<Attempt>(`/jobs/${job.job_id}/attempts?page_size=100`, seed.tenants.a);
  expect(attempts.map((attempt) => [attempt.state, attempt.failure_class])).toEqual([["FAILED", "TIMEOUT"]]);
  await expect(page.locator("section[aria-labelledby='attempts-title'] tbody tr").first()).toContainText("Quá thời gian");
  const { job: failed } = await api.job(seed.tenants.a, job.job_id);

  await page.getByRole("button", { name: "Chạy lại" }).click();
  const dialog = page.getByRole("dialog", { name: "Chạy lại job?" });
  await expect(dialog.getByRole("radio", { name: "Chạy lại từ đầu" })).toBeChecked();
  await dialog.getByRole("button", { name: "Chạy lại" }).click();
  await expect(page).not.toHaveURL(new RegExp(`/jobs/${job.job_id}$`));
  await expect(page).toHaveURL(/\/jobs\/[0-9a-f-]{36}$/);
  const retryId = page.url().split("/").pop()!;

  const { job: retry } = await api.job(seed.tenants.a, retryId);
  expect(retry.retry_of_job_id).toBe(job.job_id);
  const forward = page.locator(".summary dd").filter({ has: page.locator(`code[title="${job.job_id}"]`) });
  await expect(forward.getByRole("link")).toBeVisible();
  await forward.getByRole("link").click();

  await expect(page).toHaveURL(new RegExp(`/jobs/${job.job_id}$`));
  await expect(page.getByText("Đã chạy lại thành")).toBeVisible();
  const back = page.locator(".summary dd").filter({ has: page.locator(`code[title="${retryId}"]`) });
  await expect(back.getByRole("link")).toBeVisible();
  await expect(status(page)).toHaveText("Thất bại");

  const { job: source } = await api.job(seed.tenants.a, job.job_id);
  expect([source.state, source.version]).toEqual(["FAILED", failed.version]);
  await api.dispose();
});
