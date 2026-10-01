import { fixture, storageState } from "../fixture";
import { Api, expect, fillCpu, openForm, test } from "../support";

// Scenario 6e runs on its own stack (project w1-admission). The API wires the fail-closed
// recovery proof provider, so ADMISSION_OFF → NORMAL is refused until readiness and worker
// reconciliation exist (B17-R18): the mode cannot be handed back to the other W1 specs.

test.use({ storageState: storageState("member_a") });

test("6e. ADMISSION_OFF set by the admin through the API: submit explains the pause and creates nothing", async ({
  page,
}) => {
  const seed = fixture();
  const admin = await Api.as("admin");
  const member = await Api.as("member_a");
  const before = (await member.allJobs(seed.tenants.a)).length;

  const off = await admin.requestOperationalMode("ADMISSION_OFF");
  expect(off.previous).toBe("NORMAL");
  expect(off.response.status(), await off.response.text()).toBe(200);

  await openForm(page, seed.tenants.a);
  await fillCpu(page, seed.artifacts.a_input.artifact_id, "605");
  await page.getByRole("button", { name: "Gửi job" }).click();
  await expect(page.getByText("Hệ thống đang tạm ngừng nhận job mới")).toBeVisible();
  await expect(page).toHaveURL(/\/jobs\/new$/);
  expect((await member.allJobs(seed.tenants.a)).length).toBe(before);

  // Returning to NORMAL through the API: refused by the backend today, recorded as B17-R18.
  const back = await admin.requestOperationalMode("NORMAL");
  expect(back.previous).toBe("ADMISSION_OFF");
  expect(back.response.status()).toBe(409);
  expect(((await back.response.json()) as { code: string; message: string })).toMatchObject({
    code: "state_conflict",
    message: "Readiness and worker reconciliation are required",
  });
  await admin.dispose();
  await member.dispose();
});
