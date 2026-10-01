import { createHash, randomUUID } from "node:crypto";

import {
  expect,
  request,
  test as base,
  type APIRequestContext,
  type APIResponse,
  type Browser,
  type BrowserContext,
  type Page,
} from "@playwright/test";

import { fixture, storageState, type UserKey } from "./fixture";

// Scenario 14 and the page_size rule hold for every spec: each watched context records CSP
// violations, uncaught page errors and every list request, and the auto fixture fails the test
// at teardown if any of them broke the rule. Other console errors (e.g. Chromium's "Failed to
// load resource" on an expected 404) are not failures.

interface Watch {
  csp: string[];
  pageErrors: string[];
  oversizedPages: string[];
}

const watches: Watch[] = [];

function recordCsp(watch: Watch, text: string) {
  if (/content security policy|securitypolicyviolation/i.test(text)) watch.csp.push(text);
}

/** Starts watching a context: CSP events, CSP console messages, page errors, page_size. */
export async function watch(context: BrowserContext): Promise<void> {
  const current: Watch = { csp: [], pageErrors: [], oversizedPages: [] };
  watches.push(current);
  await context.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) => {
      console.error(`securitypolicyviolation ${event.violatedDirective} ${event.blockedURI}`);
    });
  });
  context.on("console", (message) => recordCsp(current, message.text()));
  context.on("weberror", (error) => current.pageErrors.push(error.error().message));
  context.on("request", (req) => {
    const size = new URL(req.url()).searchParams.get("page_size");
    if (size !== null && !(Number(size) >= 1 && Number(size) <= 100)) current.oversizedPages.push(req.url());
  });
}

export const test = base.extend<{ guard: void }>({
  guard: [
    async ({ context }, use) => {
      watches.length = 0;
      await watch(context);
      await use();
      for (const seen of watches) {
        expect(seen.csp, "CSP violations").toEqual([]);
        expect(seen.pageErrors, "uncaught page errors").toEqual([]);
        expect(seen.oversizedPages, "list requests with page_size outside 1..100").toEqual([]);
      }
    },
    { auto: true },
  ],
});

export { expect };

/** A browser context signed in as a seeded user (stored session from global setup). */
export async function contextAs(browser: Browser, user: UserKey): Promise<BrowserContext> {
  const context = await browser.newContext({ storageState: storageState(user) });
  await watch(context);
  return context;
}

/** Real REST client for setup and cross-checks; the same cookie session as the stored user. */
export class Api {
  private constructor(
    readonly http: APIRequestContext,
    private readonly csrf: string,
  ) {}

  static async as(user: UserKey): Promise<Api> {
    const seed = fixture();
    const http = await request.newContext({
      baseURL: seed.base_url,
      ignoreHTTPSErrors: true,
      storageState: storageState(user),
      extraHTTPHeaders: { Origin: seed.base_url },
    });
    const session = await http.get("/v1/auth/session");
    expect(session.status(), `session of ${user}`).toBe(200);
    return new Api(http, ((await session.json()) as { csrf_token: string }).csrf_token);
  }

  headers(tenantId: string | null, extra: Record<string, string> = {}): Record<string, string> {
    return {
      "X-CSRF-Token": this.csrf,
      "Idempotency-Key": `e2e-${randomUUID()}`,
      ...(tenantId ? { "X-Nexa-Tenant-Id": tenantId } : {}),
      ...extra,
    };
  }

  get(path: string, tenantId: string | null): Promise<APIResponse> {
    return this.http.get(`/v1${path}`, { headers: tenantId ? { "X-Nexa-Tenant-Id": tenantId } : {} });
  }

  post(path: string, tenantId: string | null, data: unknown, extra: Record<string, string> = {}): Promise<APIResponse> {
    return this.http.post(`/v1${path}`, { headers: this.headers(tenantId, extra), data });
  }

  async items<T>(path: string, tenantId: string): Promise<T[]> {
    const response = await this.get(path, tenantId);
    expect(response.status(), await response.text()).toBe(200);
    return ((await response.json()) as { items: T[] }).items;
  }

  async result(tenantId: string, jobId: string): Promise<ResultRecord> {
    const response = await this.get(`/jobs/${jobId}/result`, tenantId);
    expect(response.status(), await response.text()).toBe(200);
    return (await response.json()) as ResultRecord;
  }

  async download(tenantId: string, artifactId: string): Promise<Buffer> {
    const response = await this.get(`/artifacts/${artifactId}/content`, tenantId);
    expect(response.status()).toBe(200);
    return response.body();
  }

  async job(tenantId: string, jobId: string): Promise<{ job: Job; etag: string }> {
    const response = await this.get(`/jobs/${jobId}`, tenantId);
    expect(response.status()).toBe(200);
    return { job: (await response.json()) as Job, etag: response.headers()["etag"] };
  }

  /** Every job of the tenant, all pages (page_size 100). */
  async allJobs(tenantId: string): Promise<Job[]> {
    const jobs: Job[] = [];
    let cursor: string | null = null;
    do {
      const query: string = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
      const response = await this.get(`/jobs?page_size=100${query}`, tenantId);
      expect(response.status()).toBe(200);
      const page = (await response.json()) as { items: Job[]; page: { next_cursor: string | null } };
      jobs.push(...page.items);
      cursor = page.page.next_cursor;
    } while (cursor !== null);
    return jobs;
  }

  async submit(tenantId: string, spec: Record<string, unknown>): Promise<Job> {
    const response = await this.post("/jobs", tenantId, { spec });
    expect(response.status(), await response.text()).toBe(202);
    return (await response.json()) as Job;
  }

  async cancel(tenantId: string, jobId: string): Promise<Job> {
    const { etag } = await this.job(tenantId, jobId);
    const response = await this.post(`/jobs/${jobId}/cancel`, tenantId, { reason: "e2e setup" }, { "If-Match": etag });
    expect(response.status(), await response.text()).toBe(202);
    return (await response.json()) as Job;
  }

  async upload(
    tenantId: string,
    content: Buffer,
    kind: string,
    mediaType: string,
  ): Promise<{ artifact_id: string; checksum: string }> {
    const checksum = `sha256:${createHash("sha256").update(content).digest("hex")}`;
    const response = await this.http.post("/v1/artifacts", {
      headers: this.headers(tenantId, {
        "X-Artifact-Checksum": checksum,
        "X-Artifact-Size": String(content.length),
        "X-Artifact-Kind": kind,
        "X-Artifact-Media-Type": mediaType,
        "Content-Type": "application/octet-stream",
      }),
      data: content,
    });
    expect(response.status(), await response.text()).toBe(201);
    return (await response.json()) as { artifact_id: string; checksum: string };
  }

  /** PATCH the GlobalPolicy operational mode (system admin only) with the current ETag. */
  async requestOperationalMode(mode: "NORMAL" | "ADMISSION_OFF"): Promise<{ previous: string; response: APIResponse }> {
    const current = await this.get("/admin/policy", null);
    expect(current.status()).toBe(200);
    const previous = ((await current.json()) as { operational_mode: string }).operational_mode;
    const response = await this.http.patch("/v1/admin/policy", {
      headers: this.headers(null, { "If-Match": current.headers()["etag"] }),
      data: { operational_mode: mode },
    });
    return { previous, response };
  }

  dispose(): Promise<void> {
    return this.http.dispose();
  }
}

export interface ResultRecord {
  result_id: string;
  attempt_id: string;
  manifest_artifact_id: string;
  manifest_checksum: string;
}

export interface Attempt {
  attempt_id: string;
  attempt_number: number;
  state: string;
  failure_class: string | null;
}

export interface Checkpoint {
  checkpoint_id: string;
  attempt_id: string;
  sequence: number;
  state: string;
}

export const sha256 = (content: Buffer): string => `sha256:${createHash("sha256").update(content).digest("hex")}`;

export interface Job {
  job_id: string;
  tenant_id: string;
  user_id: string;
  state: string;
  desired_state: string;
  waiting_reason: string | null;
  retry_of_job_id: string | null;
  version: number;
  spec: { template_id: string; [key: string]: unknown };
}

const GIB = 1024 ** 3;

/** Defaults of the submit form (UX-A04); they fit every v1 template. */
export interface CpuRun {
  iterations?: number;
  runtime_limit_seconds?: number;
  checkpoint_interval_seconds?: number;
}

export function cpuSpec(inputArtifactId: string, seed = 1, run: CpuRun = {}): Record<string, unknown> {
  return {
    template_id: "cpu-iterative",
    template_version: 1,
    input_artifact_id: inputArtifactId,
    resources: { cpu_millis: 1000, memory_bytes: GIB, gpu_count: 0 },
    priority: 1,
    runtime_limit_seconds: run.runtime_limit_seconds ?? 300,
    checkpoint_interval_seconds: run.checkpoint_interval_seconds ?? 30,
    parameters: { iterations: run.iterations ?? 10, seed, modulus: 1_000_003 },
  };
}

export function trainingSpec(datasetArtifactId: string, seed = 1): Record<string, unknown> {
  return {
    ...cpuSpec(datasetArtifactId),
    template_id: "pytorch-cifar10-cnn",
    parameters: { epochs: 1, batch_size: 32, learning_rate: 0.01, seed, subset_size: 100 },
  };
}

/** Reads the one page element path the app renders for a job link. */
export function jobPath(tenantId: string, jobId: string): string {
  return `/t/${tenantId}/jobs/${jobId}`;
}

/** Fills the login form; the password never leaves this function. */
export async function signIn(page: Page, user: UserKey): Promise<void> {
  const { username, password } = fixture().users[user];
  await page.getByLabel("Tên đăng nhập").fill(username);
  await page.getByLabel("Mật khẩu").fill(password);
  await page.getByRole("button", { name: "Đăng nhập" }).click();
}

/** No page-level horizontal scroll (responsive rule). */
export async function expectNoHorizontalScroll(page: Page): Promise<void> {
  const widths = await page.evaluate(() => ({
    scroll: document.scrollingElement?.scrollWidth ?? 0,
    viewport: window.innerWidth,
  }));
  expect(widths.scroll).toBeLessThanOrEqual(widths.viewport);
}

export async function openForm(page: Page, tenantId: string): Promise<void> {
  await page.goto(`/t/${tenantId}/jobs/new`);
  await expect(page.getByRole("heading", { level: 1, name: "Tạo job" })).toBeVisible();
}

/** The form preselects the first supported template in API order, so pick CPU explicitly. */
export async function chooseCpu(page: Page): Promise<void> {
  const cpu = page.getByRole("radio", { name: /CPU iterative/ });
  await cpu.check();
  await expect(cpu).toBeChecked();
}

export async function fillCpu(page: Page, inputArtifactId: string, seed: string, iterations = "10"): Promise<void> {
  await chooseCpu(page);
  await page.getByLabel("Dữ liệu đầu vào").selectOption(inputArtifactId);
  await page.getByLabel("iterations", { exact: true }).fill(iterations);
  await page.getByLabel("seed", { exact: true }).fill(seed);
  await page.getByLabel("modulus", { exact: true }).fill("1000003");
}
