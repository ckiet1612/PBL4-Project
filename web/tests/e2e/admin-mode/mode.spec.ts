import { fixture } from "../fixture";
import { Api, fillCpu, openForm } from "../support";
import type { Page, Response } from "@playwright/test";

import { AdminApi, adminContextAs, expect, test } from "../admin-support";

// Scenario 13 on its own stack (project w1-admin-mode): NORMAL → ADMISSION_OFF; this stack has
// no worker, so reopening NORMAL is refused by the readiness proof (B19-R02), and
// ADMISSION_OFF → WRITE_FROZEN is refused by the fail-closed freeze proof (B21). The frozen
// side runs on the w1-admin-frozen stack; the ready reopen runs on W2 (B19).

interface GlobalPolicy {
  version: number;
  operational_mode: string;
}

/** The next answer to `method path` (B18-RV08: the specs assert status and code, not only text). */
function nextAnswer(page: Page, method: string, path: string): Promise<Response> {
  return page.waitForResponse((response) => response.request().method() === method && new URL(response.url()).pathname === path);
}

async function expectRefused(answer: Promise<Response>, status: number, code: string, message?: string): Promise<void> {
  const response = await answer;
  expect(response.status()).toBe(status);
  const body = (await response.json()) as { code: string; message: string };
  expect(body.code).toBe(code);
  if (message !== undefined) expect(body.message).toBe(message);
}

test("13. NORMAL → ADMISSION_OFF through the UI pauses submit; WRITE_FROZEN and NORMAL are refused with the server reason", async ({
  browser,
}) => {
  const seed = fixture();
  const admin = await AdminApi.as();
  const before = (await admin.read<GlobalPolicy>("/policy")).body;
  expect(before.operational_mode).toBe("NORMAL");

  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  await page.goto("/admin/policy");
  const form = page.getByRole("form", { name: "Chế độ vận hành" });
  await form.getByRole("radio", { name: /^Ngừng nhận job —/ }).check();
  await form.getByRole("button", { name: "Chuyển chế độ" }).click();
  const confirm = page.getByRole("dialog", { name: "Chuyển sang chế độ Ngừng nhận job?" });
  await expect(confirm.getByText("Mở lại NORMAL cần cơ sở dữ liệu, lưu trữ và worker sẵn sàng.")).toBeVisible();
  const switched = nextAnswer(page, "PATCH", "/v1/admin/policy");
  await confirm.getByRole("button", { name: "Chuyển chế độ" }).click();
  expect((await switched).status()).toBe(200);
  await expect(page.getByText("Hệ thống đã chuyển sang chế độ Ngừng nhận job.")).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "Hệ thống đang ở chế độ" })).toContainText("Ngừng nhận job");
  const patches = calls.filter((call) => call.method === "PATCH" && call.path === "/v1/admin/policy");
  expect(patches.map((call) => call.ifMatch)).toEqual([`"v${before.version}"`]);
  expect((await admin.read<GlobalPolicy>("/policy")).body).toMatchObject({
    version: before.version + 1,
    operational_mode: "ADMISSION_OFF",
  });

  // A member's submit now explains the pause (B17 message) and creates nothing.
  const member = await Api.as("member_a");
  const jobsBefore = (await member.allJobs(seed.tenants.a)).length;
  // Guarded like the admin contexts (storage, CSP, page errors; B18-RV08).
  const { context: memberContext } = await adminContextAs(browser, "member_a");
  const memberPage = await memberContext.newPage();
  await openForm(memberPage, seed.tenants.a);
  await fillCpu(memberPage, seed.artifacts.a_input.artifact_id, "1301");
  const submitted = nextAnswer(memberPage, "POST", "/v1/jobs");
  await memberPage.getByRole("button", { name: "Gửi job" }).click();
  await expectRefused(submitted, 409, "state_conflict");
  await expect(memberPage.getByText("Hệ thống đang tạm ngừng nhận job mới")).toBeVisible();
  expect((await member.allJobs(seed.tenants.a)).length).toBe(jobsBefore);

  // ADMISSION_OFF → WRITE_FROZEN: refused while the freeze proof fails closed (B21);
  // ADMISSION_OFF → NORMAL: refused because no worker is READY on this stack (B19-R02).
  await expect(form.getByText("Bản hiện tại chưa hỗ trợ đóng băng/khôi phục qua API (B21).")).toBeVisible();
  for (const [label, title, reason, hint] of [
    [
      "Khóa ghi",
      "Chuyển sang chế độ Khóa ghi?",
      "Freeze requires stopped containers and reconciled unreleased allocations",
      "Bản hiện tại chưa hỗ trợ đóng băng/khôi phục qua API (B21).",
    ],
    [
      "Bình thường",
      "Chuyển sang chế độ Bình thường?",
      "Readiness and worker reconciliation are required",
      "Chỉ mở lại được khi cơ sở dữ liệu, lưu trữ và worker đều sẵn sàng. Nếu chưa, máy chủ từ chối và giữ nguyên chế độ.",
    ],
  ] as const) {
    await form.getByRole("radio", { name: new RegExp(`^${label} —`) }).check();
    await form.getByRole("button", { name: "Chuyển chế độ" }).click();
    const dialog = page.getByRole("dialog", { name: title });
    await expect(dialog.getByText(hint)).toBeVisible();
    const refused = nextAnswer(page, "PATCH", "/v1/admin/policy");
    await dialog.getByRole("button", { name: "Chuyển chế độ" }).click();
    await expectRefused(refused, 409, "state_conflict", reason);
    const panel = form.locator(".error-panel");
    await expect(panel.getByText(`Chi tiết từ máy chủ: ${reason}`)).toBeVisible();
    expect((await admin.read<GlobalPolicy>("/policy")).body).toMatchObject({
      version: before.version + 1,
      operational_mode: "ADMISSION_OFF",
    });
  }
  await expect(page.getByText("Hệ thống đã chuyển sang chế độ")).toHaveCount(0);

  await member.dispose();
  await memberContext.close();
  await admin.dispose();
  await context.close();
});
