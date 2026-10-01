import { readFileSync } from "node:fs";

import { defineConfig, devices } from "@playwright/test";

// The real-stack harness (scripts/b17_e2e_stack.py) writes a 0600 fixture outside the repo and
// passes its path in NEXA_B17_FIXTURE. Without it only `--list` is meaningful.
function baseURL(): string | undefined {
  const path = process.env.NEXA_B17_FIXTURE;
  if (!path) return undefined;
  return (JSON.parse(readFileSync(path, "utf8")) as { base_url: string }).base_url;
}

export default defineConfig({
  testDir: "tests/e2e",
  outputDir: "test-results",
  // Evidence runs must not hide flakes behind retries.
  retries: 0,
  // One worker: specs share one seeded stack and the per-user login rate limit.
  workers: 1,
  fullyParallel: false,
  forbidOnly: true,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  globalSetup: "./tests/e2e/global-setup.ts",
  use: {
    baseURL: baseURL(),
    // Caddy serves `tls internal` with skip_install_trust: its CA is never added to a trust
    // store, so the test browser accepts that one local certificate instead.
    ignoreHTTPSErrors: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    locale: "vi-VN",
    timezoneId: "Asia/Ho_Chi_Minh",
  },
  projects: [
    {
      name: "w1-desktop",
      testDir: "tests/e2e/w1",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
    {
      // Leaves the stack in ADMISSION_OFF (B17-R18), so the harness runs it on a stack of its own.
      name: "w1-admission",
      testDir: "tests/e2e/admission",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
    {
      name: "w1-mobile",
      testDir: "tests/e2e/mobile",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 390, height: 844 },
        isMobile: true,
        hasTouch: true,
      },
    },
    {
      name: "w2",
      testDir: "tests/e2e/w2",
      timeout: 300_000,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
  ],
});
