import { fixture } from "../fixture";
import { Api, cpuSpec } from "../support";
import { AdminApi, adminContextAs, expect, test } from "../admin-support";

// Scenarios 5 and 6: tenant policy (edit, 412 across two admins, 409 below a committed
// counter) and the global outstanding limit (edit, 412). Tenant B is this project's own: the
// w1-admin stack is not shared with the B17 regression run.

interface TenantPolicy {
  version: number;
  weight: number;
  outstanding_limit: number;
  resource_limit: { cpu_millis: number; memory_bytes: number; gpu_count: number };
}

test("5a. edit weight and resource limit; a stale second admin gets 412 with the server values side by side", async ({
  browser,
}) => {
  const seed = fixture();
  const admin = await AdminApi.as();
  const path = `/tenants/${seed.tenants.b}/policy`;
  const before = (await admin.read<TenantPolicy>(path)).body;

  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  await page.goto(`/admin/tenants/${seed.tenants.b}?tab=policy`);
  const form = page.getByRole("form", { name: "Chính sách tenant" });
  await expect(form.getByLabel("Trọng số")).toHaveValue(String(before.weight));

  const second = await adminContextAs(browser, "admin2");
  const stale = await second.context.newPage();
  await stale.goto(`/admin/tenants/${seed.tenants.b}?tab=policy`);
  const staleForm = stale.getByRole("form", { name: "Chính sách tenant" });
  await expect(staleForm.getByLabel("Trọng số")).toHaveValue(String(before.weight));

  await form.getByLabel("Trọng số").fill("3");
  await form.getByLabel("CPU (core)").fill("12");
  await form.getByRole("button", { name: "Lưu thay đổi" }).click();
  await expect(page.getByText(`Đã lưu chính sách (v${before.version + 1}).`)).toBeVisible();
  const patch = calls.filter((call) => call.method === "PATCH");
  expect(patch).toHaveLength(1);
  expect(patch[0].ifMatch).toBe(`"v${before.version}"`);
  const saved = (await admin.read<TenantPolicy>(path)).body;
  expect(saved).toMatchObject({ weight: 3, resource_limit: { ...before.resource_limit, cpu_millis: 12_000 } });

  await staleForm.getByLabel("RAM (GiB)").fill("8");
  await staleForm.getByRole("button", { name: "Lưu thay đổi" }).click();
  const conflict = stale.getByRole("alert", { name: "Xung đột phiên bản" });
  await expect(
    conflict.getByText(`Đối tượng vừa được thay đổi (v${before.version} → v${before.version + 1}). Kiểm tra rồi gửi lại.`),
  ).toBeVisible();
  await expect(conflict.getByRole("rowheader", { name: "Trọng số" })).toBeVisible();
  await conflict.getByRole("button", { name: "Dùng giá trị máy chủ" }).click();
  await expect(staleForm.getByLabel("Trọng số")).toHaveValue("3");
  await expect(staleForm.getByLabel("RAM (GiB)")).toHaveValue(String(before.resource_limit.memory_bytes / 1024 ** 3));
  expect((await admin.read<TenantPolicy>(path)).body).toEqual(saved);
  await admin.dispose();
  await second.context.close();
  await context.close();
});

test("5b. lowering the outstanding limit below committed QUEUED jobs is refused with the server reason; nothing changes", async ({
  browser,
}) => {
  const seed = fixture();
  const admin = await AdminApi.as();
  const member = await Api.as("member_b");
  const path = `/tenants/${seed.tenants.b}/policy`;
  // W1 has no worker: these stay QUEUED and hold the tenant's admission counter.
  for (const n of [1, 2, 3]) await member.submit(seed.tenants.b, cpuSpec(seed.artifacts.b_input.artifact_id, 500 + n));
  const outstanding = (await member.allJobs(seed.tenants.b)).filter((job) => job.state === "QUEUED").length;
  expect(outstanding).toBeGreaterThanOrEqual(3);
  const before = (await admin.read<TenantPolicy>(path)).body;

  const { context } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  await page.goto(`/admin/tenants/${seed.tenants.b}?tab=policy`);
  const form = page.getByRole("form", { name: "Chính sách tenant" });
  await form.getByLabel("Job tồn đọng tối đa (tenant)").fill(String(outstanding - 1));
  await form.getByRole("button", { name: "Lưu thay đổi" }).click();
  const panel = form.locator(".error-panel");
  await expect(panel).toBeVisible();
  await expect(panel.getByText("Chi tiết từ máy chủ: Tenant policy is below a committed admission counter")).toBeVisible();
  await expect(form.getByText("Hãy ngừng nhận job (drain) hoặc chờ job đang chạy kết thúc rồi thử lại")).toBeVisible();
  // The draft stays for the admin to correct.
  await expect(form.getByLabel("Job tồn đọng tối đa (tenant)")).toHaveValue(String(outstanding - 1));
  expect((await admin.read<TenantPolicy>(path)).body).toEqual(before);
  await member.dispose();
  await admin.dispose();
  await context.close();
});

test("6. global outstanding limit: edit, then a stale second admin gets 412 and the first value stays", async ({
  browser,
}) => {
  const admin = await AdminApi.as();
  const before = (await admin.read<{ version: number; global_outstanding_limit: number }>("/policy")).body;
  const next = before.global_outstanding_limit + 7;

  const { context, calls } = await adminContextAs(browser, "admin");
  const page = await context.newPage();
  await page.goto("/admin/policy");
  const limitForm = page.getByRole("form", { name: "Giới hạn toàn cục" });
  await expect(limitForm.getByLabel("Số job tồn đọng tối đa (mọi tenant)")).toHaveValue(
    String(before.global_outstanding_limit),
  );

  const second = await adminContextAs(browser, "admin2");
  const stale = await second.context.newPage();
  await stale.goto("/admin/policy");
  const staleForm = stale.getByRole("form", { name: "Giới hạn toàn cục" });
  await expect(staleForm.getByLabel("Số job tồn đọng tối đa (mọi tenant)")).toHaveValue(
    String(before.global_outstanding_limit),
  );

  await limitForm.getByLabel("Số job tồn đọng tối đa (mọi tenant)").fill(String(next));
  await limitForm.getByRole("button", { name: "Lưu giới hạn" }).click();
  await expect(page.getByText(`Đã lưu giới hạn toàn cục: ${next} job.`)).toBeVisible();
  const patch = calls.filter((call) => call.method === "PATCH" && call.path === "/v1/admin/policy");
  expect(patch.map((call) => call.ifMatch)).toEqual([`"v${before.version}"`]);

  await staleForm.getByLabel("Số job tồn đọng tối đa (mọi tenant)").fill(String(next + 1));
  await staleForm.getByRole("button", { name: "Lưu giới hạn" }).click();
  await expect(
    stale.getByText(`Đối tượng vừa được thay đổi (v${before.version} → v${before.version + 1}). Kiểm tra rồi gửi lại.`),
  ).toBeVisible();
  const after = (await admin.read<{ version: number; global_outstanding_limit: number }>("/policy")).body;
  expect(after).toMatchObject({ version: before.version + 1, global_outstanding_limit: next });
  await admin.dispose();
  await second.context.close();
  await context.close();
});
