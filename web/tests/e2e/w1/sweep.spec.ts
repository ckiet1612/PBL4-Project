import { fixture, storageState } from "../fixture";
import { Api, expect, test } from "../support";

// Scenario 12: a 2×2 sweep of pytorch-cifar10-cnn gives four child jobs with working links.

test.use({ storageState: storageState("member_a") });

test("12. sweep 2×2 → sweep page lists 4 accepted children; each link opens its job", async ({ page }) => {
  const seed = fixture();
  await page.goto(`/t/${seed.tenants.a}/jobs/new?template=pytorch-cifar10-cnn`);
  await expect(page.getByRole("radio", { name: /PyTorch CIFAR-10 CNN training/ })).toBeChecked();
  await page.getByLabel("Dữ liệu đầu vào").selectOption(seed.artifacts.a_dataset.artifact_id);
  await page.getByLabel("learning_rate", { exact: true }).fill("0,01");
  await page.getByLabel("seed", { exact: true }).fill("1201");
  await page.getByLabel("subset_size", { exact: true }).fill("100");

  await page.getByText("5. Nâng cao").click();
  await page.getByLabel("Tạo nhiều job với các tổ hợp giá trị tham số").check();
  const names = page.getByLabel("Tham số", { exact: true });
  const values = page.getByLabel("Giá trị", { exact: true });
  await names.nth(0).selectOption("epochs");
  await values.nth(0).fill("1; 2");
  await page.getByRole("button", { name: "Thêm tham số" }).click();
  await names.nth(1).selectOption("batch_size");
  await values.nth(1).fill("16; 32");
  await expect(page.getByText("Sẽ tạo 4 job con")).toBeVisible();

  await page.getByRole("button", { name: "Gửi sweep (4 job)" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Kết quả sweep" })).toBeVisible();
  await expect(page.getByText("Đã gửi sweep: 4 job được nhận, 0 bị từ chối.")).toBeVisible();
  await expect(page.getByText("4 tổng · 4 được nhận · 0 bị từ chối")).toBeVisible();
  const links = page.locator("tbody tr td:last-child a");
  await expect(links).toHaveCount(4);
  const hrefs = await links.evaluateAll((anchors) => anchors.map((a) => a.getAttribute("href") ?? ""));
  const jobIds = hrefs.map((href) => href.split("/").pop()!);
  expect(new Set(jobIds).size).toBe(4);

  const api = await Api.as("member_a");
  const combos = new Set<string>();
  for (const jobId of jobIds) {
    const { job } = await api.job(seed.tenants.a, jobId);
    const params = job.spec.parameters as { epochs: number; batch_size: number };
    combos.add(`${params.epochs}x${params.batch_size}`);
  }
  expect(combos).toEqual(new Set(["1x16", "1x32", "2x16", "2x32"]));
  await api.dispose();

  await links.first().click();
  await expect(page).toHaveURL(new RegExp(`/jobs/${jobIds[0]}$`));
  await expect(page.getByRole("heading", { level: 1, name: "PyTorch CIFAR-10 CNN training (CPU)" })).toBeVisible();
  await page.goBack();
  await links.nth(3).click();
  await expect(page).toHaveURL(new RegExp(`/jobs/${jobIds[3]}$`));
  await expect(page.getByRole("heading", { level: 1, name: "PyTorch CIFAR-10 CNN training (CPU)" })).toBeVisible();
});
