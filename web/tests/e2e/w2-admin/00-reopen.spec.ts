import type { Page, Response } from "@playwright/test";

import { fixture } from "../fixture";
import { Api, fillCpu, openForm } from "../support";
import { AdminApi, adminContextAs, expect, test } from "../admin-support";

// B19 (B19-R02): NORMAL → ADMISSION_OFF → NORMAL through the UI on a W2 stack, where the
// readiness proof can pass (database, storage and a READY, reconciled worker). It runs first
// in w2-admin, before the drain/disable scenarios, and leaves the stack in NORMAL.

const START = { timeout: 120_000 };
const FINISH = { timeout: 240_000 };

interface GlobalPolicy {
  version: number;
  operational_mode: string;
}

interface Worker {
  worker_id: string;
  health: string;
  admin_state: string;
  last_heartbeat_at: string | null;
}

interface Allocation {
  job_id: string;
  released_at: string | null;
}

function nextAnswer(page: Page, method: string, path: string): Promise<Response> {
  return page.waitForResponse((response) => response.request().method() === method && new URL(response.url()).pathname === path);
}

async function switchMode(page: Page, label: string, title: string, hint: string): Promise<Response> {
  const form = page.getByRole("form", { name: "Chế độ vận hành" });
  await form.getByRole("radio", { name: new RegExp(`^${label} —`) }).check();
  await form.getByRole("button", { name: "Chuyển chế độ" }).click();
  const dialog = page.getByRole("dialog", { name: title });
  await expect(dialog.getByText(hint)).toBeVisible();
  const answer = nextAnswer(page, "PATCH", "/v1/admin/policy");
  await dialog.getByRole("button", { name: "Chuyển chế độ" }).click();
  return answer;
}

test("B19. NORMAL → ADMISSION_OFF → NORMAL through the UI with a ready worker; submit works again", async ({ browser }) => {
  const seed = fixture();
  const admin = await AdminApi.as();
  const before = (await admin.read<GlobalPolicy>("/policy")).body;
  expect(before.operational_mode).toBe("NORMAL");
  // The proof needs a READY, ENABLED worker; nothing held or quarantined (B18 lesson).
  await expect
    .poll(async () => {
      const { body } = await admin.read<{ items: Worker[] }>("/workers?page_size=10");
      return body.items.find((item) => item.worker_id === seed.worker_id)?.health;
    }, START)
    .toBe("READY");
  for (const state of ["HELD", "QUARANTINED"]) {
    const { body } = await admin.read<{ items: unknown[] }>(`/allocations?state=${state}&page_size=100`);
    expect(body.items).toHaveLength(0);
  }

  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  await page.goto("/admin/policy");
  const banner = page.getByRole("status").filter({ hasText: "Hệ thống đang ở chế độ" });

  const paused = await switchMode(
    page,
    "Ngừng nhận job",
    "Chuyển sang chế độ Ngừng nhận job?",
    "Mở lại NORMAL cần cơ sở dữ liệu, lưu trữ và worker sẵn sàng.",
  );
  expect(paused.status()).toBe(200);
  await expect(page.getByText("Hệ thống đã chuyển sang chế độ Ngừng nhận job.")).toBeVisible();
  await expect(banner).toContainText("Ngừng nhận job");

  const reopened = await switchMode(
    page,
    "Bình thường",
    "Chuyển sang chế độ Bình thường?",
    "Chỉ mở lại được khi cơ sở dữ liệu, lưu trữ và worker đều sẵn sàng. Nếu chưa, máy chủ từ chối và giữ nguyên chế độ.",
  );
  expect(reopened.status()).toBe(200);
  expect(((await reopened.json()) as GlobalPolicy).operational_mode).toBe("NORMAL");
  await expect(page.getByText("Hệ thống đã chuyển sang chế độ Bình thường.")).toBeVisible();
  await expect(banner).toHaveCount(0);
  const patches = calls.filter((call) => call.method === "PATCH" && call.path === "/v1/admin/policy");
  expect(patches.map((call) => call.ifMatch)).toEqual([`"v${before.version}"`, `"v${before.version + 1}"`]);
  expect((await admin.read<GlobalPolicy>("/policy")).body).toMatchObject({
    version: before.version + 2,
    operational_mode: "NORMAL",
  });

  // A member submits again through the form and the job runs to SUCCEEDED on the worker.
  const { context: memberContext } = await adminContextAs(browser, "member_a");
  const memberPage = await memberContext.newPage();
  await openForm(memberPage, seed.tenants.a);
  await fillCpu(memberPage, seed.artifacts.a_input.artifact_id, "1901");
  const submitted = nextAnswer(memberPage, "POST", "/v1/jobs");
  await memberPage.getByRole("button", { name: "Gửi job" }).click();
  expect((await submitted).status()).toBe(202);
  await expect(memberPage).toHaveURL(/\/jobs\/[0-9a-f-]{36}$/);
  const jobId = memberPage.url().split("/").pop()!;
  await expect(memberPage.locator(".status-block .badge")).toHaveText("Hoàn tất", FINISH);
  const member = await Api.as("member_a");
  expect((await member.job(seed.tenants.a, jobId)).job.state).toBe("SUCCEEDED");

  // Hand the next scenarios an idle READY worker. Releasing the allocation moves the
  // reconciliation snapshot, so the next heartbeat is STARTING until the worker reconciles
  // again; a READY heartbeat after the release means it already has.
  const released = async () => {
    const { body } = await admin.read<{ items: Allocation[] }>("/allocations?state=RELEASED&page_size=100");
    return body.items.find((item) => item.job_id === jobId)?.released_at ?? null;
  };
  await expect.poll(released, FINISH).not.toBeNull();
  const releasedAt = Date.parse((await released())!);
  await expect
    .poll(async () => {
      const { body } = await admin.read<{ items: Worker[] }>("/workers?page_size=10");
      const worker = body.items.find((item) => item.worker_id === seed.worker_id);
      return worker?.health === "READY" && Date.parse(worker.last_heartbeat_at ?? "") > releasedAt;
    }, START)
    .toBe(true);

  await member.dispose();
  await memberContext.close();
  await admin.dispose();
  await context.close();
});
