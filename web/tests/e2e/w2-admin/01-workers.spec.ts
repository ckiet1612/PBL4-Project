import { execFileSync } from "node:child_process";

import type { Page, Response } from "@playwright/test";

import { fixture } from "../fixture";
import { Api, cpuSpec, type Job } from "../support";
import { AdminApi, adminContextAs, expect, test } from "../admin-support";

// Scenarios 14–17 on a W2 stack: one real worker (Docker), coordinator and cpu-iterative image.
// The tests share the worker and run in file order; each one leaves it ENABLED and READY.

const START = { timeout: 120_000 };
const FINISH = { timeout: 240_000 };
const LONG = 250_000_000; // ≈ 45 s of compute on this host (B15/B17 W2 measurement).

interface Allocation {
  allocation_id: string;
  job_id: string;
  worker_id: string;
  state: "HELD" | "QUARANTINED" | "RELEASED";
}

interface AdminJob extends Job {
  waiting_reason: string | null;
}

/** HELD or QUARANTINED allocations on the stack's worker (first page; the stack has few). */
async function unreleased(admin: AdminApi, state: "HELD" | "QUARANTINED"): Promise<Allocation[]> {
  const { body } = await admin.read<{ items: Allocation[] }>(`/allocations?state=${state}&page_size=100`);
  return body.items.filter((item) => item.worker_id === fixture().worker_id);
}

async function waitState(api: Api, tenantId: string, jobId: string, state: string, options = START): Promise<void> {
  await expect.poll(async () => (await api.job(tenantId, jobId)).job.state, { ...options, intervals: [1000] }).toBe(state);
}

function workerContainer(): string {
  return `nexa_b17_worker_${fixture().run_id}`;
}

/** Opens the worker detail through the list, as an admin would. */
async function openWorker(page: Page): Promise<void> {
  const { worker_id } = fixture();
  await page.goto("/admin/workers");
  await expect(page.getByRole("heading", { level: 1, name: "Worker" })).toBeVisible();
  await page.locator("tbody tr").filter({ has: page.locator(`code[title="${worker_id}"]`) }).getByRole("link").click();
  await expect(page).toHaveURL(new RegExp(`/admin/workers/${worker_id}$`));
  await expect(page.getByRole("heading", { level: 1, name: /^Worker/ })).toBeVisible();
}

function badges(page: Page) {
  const values = page.locator("dl.key-values").first();
  return {
    health: values.locator("dt:has-text('Sức khỏe') + dd .badge"),
    admin: values.locator("dt:has-text('Trạng thái quản trị') + dd .badge"),
  };
}

function allocationRow(page: Page, jobId: string) {
  return page
    .getByRole("region", { name: "Phân bổ chưa trả" })
    .locator("tbody tr")
    .filter({ has: page.locator(`code[title="${jobId}"]`) });
}

/** Opens the action dialog and gives the reason; does not confirm. */
async function openAction(page: Page, label: string, reason: string): Promise<void> {
  await page.getByRole("region", { name: "Thao tác" }).getByRole("button", { name: label }).click();
  await page.getByRole("dialog", { name: `${label}?` }).getByLabel("Lý do").fill(reason);
}

/** Confirms the open action dialog; returns the action's response. */
async function confirmAction(page: Page, label: string): Promise<Response> {
  const answer = page.waitForResponse(
    (response) => response.request().method() === "POST" && /\/v1\/admin\/workers\/[^/]+\/(drain|disable|enable)$/.test(response.url()),
  );
  await page.getByRole("dialog", { name: `${label}?` }).getByRole("button", { name: label }).click();
  return answer;
}

/** Opens the action dialog, gives the reason and confirms; returns the action's response. */
async function act(page: Page, label: string, reason: string): Promise<Response> {
  await openAction(page, label, reason);
  return confirmAction(page, label);
}

const CONFLICT = /^Đối tượng vừa được thay đổi \(v\d+ → v\d+\)\. Kiểm tra rồi gửi lại\.$/;

/** A refusal the page may meet on the way: exact status, error code and (for 409) server message. */
interface Refusal {
  status: number;
  code: string;
  messages?: readonly string[];
}

/** A heartbeat that changes the worker's health moves its version (B18-R22a). */
const STALE_ETAG: Refusal = { status: 412, code: "version_conflict" };
/**
 * Enable rechecks READY against the latest heartbeat (admin_workers._require_enable_ready); after
 * an allocation change or a disable the next 5 s heartbeat round must pass first.
 */
const ENABLE_NOT_YET: Refusal = {
  status: 409,
  code: "state_conflict",
  messages: [
    "The worker is not READY",
    "The latest worker heartbeat did not pass the READY checks",
    "The current worker incarnation is not reconciled",
  ],
};

/**
 * Sends a worker action through the dialog until the server accepts it, as an admin following
 * the page would. Only the listed refusals are tolerated (status, code and message); any other
 * answer fails the test. On 412 the page re-reads the worker and keeps the dialog with its
 * reason and the explanation (B18-RV05); confirming again is a new intent with the new ETag.
 * On a tolerated 409 the dialog shows the server's reason and is dismissed, then reopened.
 * Every refusal is recorded as an annotation.
 */
async function actUntilAccepted(
  page: Page,
  label: string,
  reason: string,
  accepted: number,
  tolerated: readonly Refusal[],
  { dialogOpen = false } = {},
): Promise<string[]> {
  const refusals: string[] = [];
  let unexpected: string | null = null;
  let open = dialogOpen;
  const dialog = page.getByRole("dialog", { name: `${label}?` });
  await expect(async () => {
    if (unexpected !== null) return;
    const response = open ? await confirmAction(page, label) : await act(page, label, reason);
    open = true;
    const status = response.status();
    if (status === accepted) {
      await expect(dialog).toHaveCount(0);
      open = false;
      return;
    }
    const { code, message } = (await response.json()) as { code: string; message: string };
    refusals.push(`${status} ${code} ${message}`);
    const allowed = tolerated.some(
      (refusal) => refusal.status === status && refusal.code === code && (refusal.messages?.includes(message) ?? true),
    );
    if (!allowed) {
      unexpected = `${status} ${code} ${message} (after ${JSON.stringify(refusals.slice(0, -1))})`;
      return;
    }
    if (status === 412) {
      await expect(dialog.getByText(CONFLICT)).toBeVisible();
      await expect(dialog.getByLabel("Lý do")).toHaveValue(reason);
    } else {
      await expect(dialog.locator(".error-panel").getByText(`Chi tiết từ máy chủ: ${message}`)).toBeVisible();
      await dialog.getByRole("button", { name: "Bỏ qua" }).click();
      await expect(dialog).toHaveCount(0);
      open = false;
    }
    expect(status).toBe(accepted);
  }).toPass({ intervals: [5_000], timeout: 90_000 });
  expect(unexpected).toBeNull();
  test.info().annotations.push({ type: `${label} "${reason}" refusals before ${accepted}`, description: JSON.stringify(refusals) });
  return refusals;
}

const drainAccepted = (page: Page, reason: string) => actUntilAccepted(page, "Ngừng nhận job", reason, 202, [STALE_ETAG]);
const enableAccepted = (page: Page, reason: string) =>
  actUntilAccepted(page, "Bật lại", reason, 200, [ENABLE_NOT_YET, STALE_ETAG]);

async function workerHealth(admin: AdminApi): Promise<string> {
  return (await admin.read<{ health: string }>(`/workers/${fixture().worker_id}`)).body.health;
}

async function waitWorkerReady(admin: AdminApi): Promise<void> {
  await expect.poll(() => workerHealth(admin), { ...START, intervals: [1000] }).toBe("READY");
}

const transition = (page: Page) => page.locator("main p[role=status]").filter({ hasNotText: "Hệ thống đang ở chế độ" });

test("14–15. worker inventory and capacity, a RUNNING job's HELD allocation, drain waits for it, enable dispatches the queued job", async ({
  browser,
}) => {
  const seed = fixture();
  const admin = await AdminApi.as();
  const memberA = await Api.as("member_a");
  const memberB = await Api.as("member_b");
  await waitWorkerReady(admin);

  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  await openWorker(page);

  // 14: an idle READY/ENABLED worker with its inventory and nothing held.
  await expect(badges(page).health).toHaveText("Sẵn sàng");
  await expect(badges(page).admin).toHaveText("Đang bật");
  const inventory = page.getByRole("region", { name: "Inventory" });
  await expect(inventory).toBeVisible();
  await expect(inventory.getByText("Worker chưa gửi inventory.")).toHaveCount(0);
  const capacity = page.locator("table").filter({ has: page.locator("caption", { hasText: "Sức chứa" }) });
  const heldCpu = capacity.getByRole("row", { name: /^Đang giữ/ }).locator("td").first();
  await expect(capacity.getByRole("row", { name: /^Có thể cấp phát/ }).locator("td").first()).not.toHaveText("—");
  await expect(heldCpu).toHaveText(/^0(\s|$)/);
  await expect(transition(page)).toHaveCount(0);

  // Job A runs: after a refresh the page shows its HELD allocation (no list polling, A11).
  const jobA = await memberA.submit(seed.tenants.a, cpuSpec(seed.artifacts.a_input.artifact_id, 1401, { iterations: LONG }));
  await waitState(memberA, seed.tenants.a, jobA.job_id, "RUNNING");
  await page.getByRole("button", { name: "Làm mới" }).click();
  await expect(heldCpu).toHaveText(/^1(\s|$)/);
  await expect(allocationRow(page, jobA.job_id)).toContainText("Đang giữ");
  const runningHealth = await workerHealth(admin);
  test.info().annotations.push({ type: "worker health while job A runs (API)", description: runningHealth });
  await expect(badges(page).admin).toHaveText("Đang bật");

  // 15: drain with a reason. New work stops at once; job A keeps running.
  await drainAccepted(page, "b18 e2e drain");
  await expect(page.getByText('Đã gửi yêu cầu "Ngừng nhận job". Trạng thái được cập nhật theo dữ liệu máy chủ.')).toBeVisible();
  await expect(badges(page).admin).toHaveText("Ngừng nhận job");
  await expect(transition(page)).toHaveText("Đang chờ 1 job đang chạy kết thúc");
  await expect(page.getByText("Đang tự cập nhật")).toBeVisible();

  const jobB = await memberB.submit(seed.tenants.b, cpuSpec(seed.artifacts.b_input.artifact_id, 1501));
  let waiting: string | null = null;
  await expect
    .poll(
      async () => {
        const { body } = await admin.read<AdminJob>(`/jobs/${jobB.job_id}`);
        waiting = body.state === "QUEUED" ? body.waiting_reason : null;
        return waiting;
      },
      { ...START, intervals: [1000] },
    )
    .not.toBeNull();
  test.info().annotations.push({ type: "waiting_reason (job B while draining)", description: String(waiting) });

  // While A holds its allocation the page must not claim the drain is complete.
  expect((await memberA.job(seed.tenants.a, jobA.job_id)).job.state).toBe("RUNNING");
  expect((await unreleased(admin, "HELD")).map((item) => item.job_id)).toEqual([jobA.job_id]);
  await expect(transition(page)).toHaveClass(/pending-note/);
  await expect(page.getByText("Đã ngừng nhận job; không còn job đang chạy")).toHaveCount(0);

  await expect(transition(page)).toHaveText("Đã ngừng nhận job; không còn job đang chạy", FINISH);
  await expect(transition(page)).toHaveClass(/status-line/);
  expect(await unreleased(admin, "HELD")).toEqual([]);
  expect((await memberA.job(seed.tenants.a, jobA.job_id)).job.state).toBe("SUCCEEDED");
  expect((await memberB.job(seed.tenants.b, jobB.job_id)).job.state).toBe("QUEUED");
  await expect(page.getByText("Không có phân bổ HELD hoặc QUARANTINED trên worker này.")).toBeVisible();

  // Enable: the worker takes job B.
  await enableAccepted(page, "b18 e2e enable after drain");
  await expect(badges(page).admin).toHaveText("Đang bật");
  await expect(transition(page)).toHaveText("Worker sẵn sàng nhận job");
  await waitState(memberB, seed.tenants.b, jobB.job_id, "SUCCEEDED", FINISH);

  const posts = calls.filter((call) => call.method === "POST");
  expect(posts.map((call) => call.path.split("/").pop()).join(" ")).toMatch(/^(drain )+enable( enable)*$/);
  expect(posts.every((call) => call.ifMatch?.startsWith('"v') && call.idempotencyKey)).toBe(true);
  await Promise.all([admin.dispose(), memberA.dispose(), memberB.dispose(), context.close()]);
});

test("16. disable fences a RUNNING job; enable is refused (409) while its allocation is QUARANTINED and the paused worker has not cleaned up; the job recovers", async ({
  browser,
}) => {
  const seed = fixture();
  const admin = await AdminApi.as();
  const member = await Api.as("member_a");
  const job = await member.submit(seed.tenants.a, cpuSpec(seed.artifacts.a_input.artifact_id, 1601, { iterations: LONG }));
  await waitState(member, seed.tenants.a, job.job_id, "RUNNING");

  const { context } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  await openWorker(page);
  await expect(allocationRow(page, job.job_id)).toContainText("Đang giữ");

  // The worker cleans up within one renew round (5 s). Pausing its container (the harness's
  // own nexa_b17_worker_*) keeps the QUARANTINED window open long enough to try an enable.
  execFileSync("docker", ["pause", workerContainer()]);
  let paused = true;
  try {
    const disabled = await act(page, "Tắt worker", "b18 e2e disable");
    expect(disabled.status()).toBe(202);
    await expect(badges(page).admin).toHaveText("Đã tắt");
    await expect(allocationRow(page, job.job_id)).toContainText("Chờ xác nhận dọn dẹp (QUARANTINED)");
    await expect(transition(page)).toHaveText("Đang chờ worker xác nhận dọn dẹp (còn 1 phân bổ QUARANTINED)");
    expect((await unreleased(admin, "QUARANTINED")).map((item) => item.job_id)).toEqual([job.job_id]);
    await expect(page.getByRole("main")).not.toContainText(/đã dừng|đã dọn|dọn dẹp xong/i);
    await expect(
      page.getByText("Còn 1 phân bổ chờ worker xác nhận dọn dẹp; máy chủ sẽ từ chối bật lại cho tới khi dọn xong").first(),
    ).toBeVisible();

    // Enable while QUARANTINED: the server refuses and the dialog says why. The refusal names the
    // first READY recheck that fails (B18-RV07): with the worker paused that is the heartbeat or
    // reconciliation check, before the quarantine check is reached; which one is recorded.
    const refused = await act(page, "Bật lại", "b18 e2e enable too early");
    expect(refused.status()).toBe(409);
    const detail = (await refused.json()) as { code: string; message: string };
    expect(detail.code).toBe("state_conflict");
    expect([...ENABLE_NOT_YET.messages!, "The worker has no fresh heartbeat", "Quarantined allocations still await verified cleanup"]).toContain(
      detail.message,
    );
    test.info().annotations.push({ type: "enable while QUARANTINED → 409 (first failing recheck)", description: detail.message });
    const dialog = page.getByRole("dialog", { name: "Bật lại?" });
    await expect(dialog.locator(".error-panel").getByText(`Chi tiết từ máy chủ: ${detail.message}`)).toBeVisible();
    await dialog.getByRole("button", { name: "Bỏ qua" }).click();
    await expect(badges(page).admin).toHaveText("Đã tắt");
    expect((await unreleased(admin, "QUARANTINED")).map((item) => item.job_id)).toEqual([job.job_id]);
  } finally {
    if (paused) execFileSync("docker", ["unpause", workerContainer()]);
    paused = false;
  }

  // The worker stops the container, reports the cleanup, the allocation is RELEASED.
  await expect(transition(page)).toHaveText("Worker đã xác nhận dọn dẹp xong", FINISH);
  expect(await unreleased(admin, "QUARANTINED")).toEqual([]);
  expect(await unreleased(admin, "HELD")).toEqual([]);
  await expect(page.getByText("Không có phân bổ HELD hoặc QUARANTINED trên worker này.")).toBeVisible();

  await enableAccepted(page, "b18 e2e enable after cleanup");
  await expect(badges(page).admin).toHaveText("Đang bật");
  await expect(transition(page)).toHaveText("Worker sẵn sàng nhận job", FINISH);

  // A new attempt of the same job runs to the end.
  await waitState(member, seed.tenants.a, job.job_id, "SUCCEEDED", FINISH);
  const attempts = await member.items<{ attempt_number: number; failure_class: string | null }>(
    `/jobs/${job.job_id}/attempts`,
    seed.tenants.a,
  );
  expect(attempts.length).toBeGreaterThanOrEqual(2);
  expect(attempts.find((attempt) => attempt.attempt_number === 1)?.failure_class).toBe("INFRASTRUCTURE");

  // Recovery shows the fence of this job; audit shows the three worker actions with their reasons.
  await page.getByRole("navigation", { name: "Điều hướng quản trị" }).getByRole("link", { name: "Khôi phục" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Khôi phục" })).toBeVisible();
  const fenced = page.locator("tbody tr").filter({ has: page.locator(`code[title="${job.job_id}"]`) });
  await expect(fenced.filter({ hasText: "Lần chạy bị thu hồi quyền (fence)" })).toHaveCount(1);
  await expect(fenced.filter({ hasText: "Lần chạy bị thu hồi quyền (fence)" })).toContainText("WORKER_DISABLED");

  for (const [action, reasons] of [
    ["admin.worker.drain", ["b18 e2e drain"]],
    ["admin.worker.disable", ["b18 e2e disable"]],
    ["admin.worker.enable", ["b18 e2e enable after drain", "b18 e2e enable after cleanup"]],
  ] as const) {
    await page.goto(`/admin/audit?action=${action}`);
    await expect(page.getByRole("heading", { level: 1, name: "Audit" })).toBeVisible();
    await expect
      .poll(() => page.locator("tbody tr").evaluateAll((rows) => rows.map((row) => row.children[5]?.textContent?.trim() ?? "")))
      .toEqual(expect.arrayContaining([...reasons]));
    // The refused enable changed nothing, so it left no audit row.
    if (action === "admin.worker.enable") {
      await expect(page.locator("tbody tr").filter({ hasText: "b18 e2e enable too early" })).toHaveCount(0);
    }
  }
  await Promise.all([admin.dispose(), member.dispose(), context.close()]);
});

test("17. worker action 412: a second admin acts on a stale page and is told to re-check; a still valid action keeps its dialog and reason", async ({
  browser,
}) => {
  const admin = await AdminApi.as();
  await waitWorkerReady(admin);
  // This scenario disables an idle worker. A finished job's allocation stays HELD until the
  // worker confirms its container cleanup; a disable before that would fence it to QUARANTINED
  // (scenario 16's subject). Record what is still held, then wait until nothing is.
  const atStart = [...(await unreleased(admin, "HELD")), ...(await unreleased(admin, "QUARANTINED"))];
  test.info().annotations.push({
    type: "unreleased allocations on the worker at the start",
    description: JSON.stringify(atStart.map((item) => item.state)),
  });
  await expect
    .poll(async () => (await unreleased(admin, "HELD")).length + (await unreleased(admin, "QUARANTINED")).length, {
      ...START,
      intervals: [1000],
    })
    .toBe(0);
  await admin.dispose();
  const one = await adminContextAs(browser, "admin");
  const two = await adminContextAs(browser, "admin2");
  const first = await one.context.newPage();
  const second = await two.context.newPage();
  await openWorker(first);
  await openWorker(second);
  const version = Number((await second.locator("dt:has-text('Phiên bản') + dd").textContent())?.slice(1));

  await drainAccepted(first, "b18 e2e drain by admin one");
  await expect(badges(first).admin).toHaveText("Ngừng nhận job");

  const stale = await act(second, "Ngừng nhận job", "b18 e2e drain by admin two");
  expect(stale.status()).toBe(412);
  // The page re-read the worker: v<sent> → v<current>, at least one higher (a heartbeat that
  // changes health also moves the version). The text names the version the page now shows.
  await expect(second.getByText(CONFLICT)).toBeVisible();
  const shown = Number((await second.locator("dt:has-text('Phiên bản') + dd").textContent())?.slice(1));
  expect(shown).toBeGreaterThan(version);
  await expect(second.getByText(`Đối tượng vừa được thay đổi (v${version} → v${shown}). Kiểm tra rồi gửi lại.`)).toBeVisible();
  await expect(badges(second).admin).toHaveText("Ngừng nhận job");
  await expect(second.getByRole("dialog")).toHaveCount(0);
  const sent = two.calls.filter((call) => call.method === "POST");
  expect(sent.map((call) => call.ifMatch)).toEqual([`"v${version}"`]);

  // B18-RV05: admin two opens disable with a reason, then admin one enables. The confirm meets
  // 412; disable still applies, so the dialog stays with the reason and the explanation, and
  // confirming again (a new intent with the re-read ETag) disables the worker.
  const disableReason = "b18 e2e disable by admin two";
  await openAction(second, "Tắt worker", disableReason);
  await enableAccepted(first, "b18 e2e enable by admin one");
  await expect(badges(first).admin).toHaveText("Đang bật");
  const conflicted = await confirmAction(second, "Tắt worker");
  expect(conflicted.status()).toBe(412);
  const disableDialog = second.getByRole("dialog", { name: "Tắt worker?" });
  await expect(disableDialog.getByText(CONFLICT)).toBeVisible();
  await expect(disableDialog.getByLabel("Lý do")).toHaveValue(disableReason);
  await actUntilAccepted(second, "Tắt worker", disableReason, 202, [STALE_ETAG], { dialogOpen: true });
  await expect(badges(second).admin).toHaveText("Đã tắt");
  const disables = two.calls.filter((call) => call.method === "POST" && call.path.endsWith("/disable"));
  expect(disables.length).toBeGreaterThanOrEqual(2);
  expect(new Set(disables.map((call) => call.idempotencyKey)).size).toBe(disables.length);
  expect(new Set(disables.map((call) => call.ifMatch)).size).toBe(disables.length);

  // Admin two enables from the fresh page and restores the worker.
  await enableAccepted(second, "b18 e2e enable by admin two");
  await expect(badges(second).admin).toHaveText("Đang bật");
  await expect(transition(second)).toHaveText("Worker sẵn sàng nhận job");
  await Promise.all([one.context.close(), two.context.close()]);
});
