import { randomBytes, randomUUID } from "node:crypto";

import type { APIResponse, Browser, BrowserContext, Page } from "@playwright/test";

import { fixture, type UserKey } from "./fixture";
import { Api, contextAs, expect, test as base, watch } from "./support";

// B18 admin specs. On top of the B17 guard (CSP, page errors, page_size), every admin test
// fails if a /v1/admin request carried X-Nexa-Tenant-Id or if a page of the app left anything
// in web storage (scenario 12). Storage is checked when a context closes (specs close their
// own contexts) and again at teardown for contexts still open; a test that checked no app
// page fails, so the guard can never pass vacuously (B18-RV02).

export interface AdminCall {
  method: string;
  path: string;
  search: URLSearchParams;
  tenantHeader: string | undefined;
  idempotencyKey: string | undefined;
  ifMatch: string | undefined;
}

const recorders: AdminCall[][] = [];

/** Records every /v1/admin request a context sends (UI and page-context fetch). */
export function recordAdmin(context: BrowserContext): AdminCall[] {
  const calls: AdminCall[] = [];
  recorders.push(calls);
  context.on("request", (request) => {
    const url = new URL(request.url());
    if (!url.pathname.startsWith("/v1/admin")) return;
    const headers = request.headers();
    calls.push({
      method: request.method(),
      path: url.pathname,
      search: url.searchParams,
      tenantHeader: headers["x-nexa-tenant-id"],
      idempotencyKey: headers["idempotency-key"],
      ifMatch: headers["if-match"],
    });
  });
  return calls;
}

export async function expectNoBrowserStorage(page: Page): Promise<void> {
  const stored = await page.evaluate(async () => ({
    local: localStorage.length,
    session: sessionStorage.length,
    indexed: (await indexedDB.databases()).length,
    documentCookie: document.cookie,
  }));
  expect(stored).toEqual({ local: 0, session: 0, indexed: 0, documentCookie: "" });
}

const contexts: BrowserContext[] = [];
const storageChecks = { pages: 0, contexts: 0 };

/**
 * Web storage of one context: sessionStorage/localStorage/IndexedDB/document.cookie on every
 * open app page, then the context's storage state (localStorage and IndexedDB per origin,
 * cookies other than the HttpOnly session cookie), which also covers closed pages.
 */
async function expectContextStorageEmpty(context: BrowserContext): Promise<void> {
  const origin = new URL(fixture().base_url).origin;
  for (const open of context.pages()) {
    if (open.isClosed() || !open.url().startsWith(origin)) continue;
    await expectNoBrowserStorage(open);
    storageChecks.pages += 1;
  }
  // The snapshot carries localStorage plus the requested IndexedDB/OPFS lists per origin (the
  // typings only declare localStorage): any non-empty list is stored data.
  const state = await context.storageState({ indexedDB: true, opfs: true });
  expect(
    state.origins.filter((entry) =>
      Object.entries(entry).some(([key, value]) => key !== "origin" && Array.isArray(value) && value.length > 0),
    ),
    "origins with localStorage or IndexedDB",
  ).toEqual([]);
  expect(
    state.cookies.filter((cookie) => !cookie.httpOnly).map((cookie) => cookie.name),
    "cookies readable by scripts",
  ).toEqual([]);
  storageChecks.contexts += 1;
}

const checkedContexts = new WeakSet<BrowserContext>();

async function checkOnce(context: BrowserContext): Promise<void> {
  if (checkedContexts.has(context)) return;
  checkedContexts.add(context);
  await expectContextStorageEmpty(context);
}

/** Checks storage before a spec-owned context closes; a context is checked once. */
function guardStorage(context: BrowserContext): BrowserContext {
  contexts.push(context);
  const close = context.close.bind(context);
  context.close = async (options) => {
    await checkOnce(context);
    return close(options);
  };
  return context;
}

/** Negative control for the guard itself (admin/01-access): storage left behind must fail close(). */
export function storageGuardChecks(): { pages: number; contexts: number } {
  return { ...storageChecks };
}

export const test = base.extend<{ adminGuard: void }>({
  adminGuard: [
    async ({ context }, use, testInfo) => {
      recorders.length = 0;
      contexts.length = 0;
      storageChecks.pages = 0;
      storageChecks.contexts = 0;
      // The default context stays owned by Playwright (closed after this fixture).
      contexts.push(context);
      recordAdmin(context);
      await use();
      for (const calls of recorders) {
        expect(
          calls.filter((call) => call.tenantHeader !== undefined).map((call) => call.path),
          "admin requests with X-Nexa-Tenant-Id",
        ).toEqual([]);
      }
      // Contexts the spec left open (including the default one) are checked here.
      for (const open of contexts) await checkOnce(open);
      console.log(
        `[storage-guard] ${testInfo.titlePath.slice(1).join(" > ")}: pages=${storageChecks.pages} contexts=${storageChecks.contexts}`,
      );
      expect(storageChecks.pages, "app pages whose storage was checked").toBeGreaterThan(0);
    },
    { auto: true },
  ],
});

export { expect };

/** A stored-session context whose /v1/admin requests are recorded. */
export async function adminContextAs(
  browser: Browser,
  user: UserKey,
): Promise<{ context: BrowserContext; calls: AdminCall[] }> {
  const context = guardStorage(await contextAs(browser, user));
  return { context, calls: recordAdmin(context) };
}

/** A context without a session (login through the form), watched like the others. */
export async function anonymousContext(browser: Browser): Promise<BrowserContext> {
  const context = guardStorage(await browser.newContext());
  await watch(context);
  recordAdmin(context);
  return context;
}

/** Unique, lowercase, slug-safe name for objects a spec creates. */
export function uniqueName(prefix: string): string {
  return `${prefix}-${randomUUID().slice(0, 8)}`;
}

/** A password that lives only in the spec's memory; never printed. */
export function newPassword(): string {
  return randomBytes(24).toString("base64url");
}

/** Login form with explicit credentials (users created by a spec). */
export async function signInAs(page: Page, username: string, password: string): Promise<void> {
  await page.getByLabel("Tên đăng nhập").fill(username);
  await page.getByLabel("Mật khẩu").fill(password);
  await page.getByRole("button", { name: "Đăng nhập" }).click();
}

/** The admin REST client used for setup and cross-checks (no tenant header). */
export class AdminApi {
  private constructor(readonly api: Api) {}

  static async as(user: UserKey = "admin"): Promise<AdminApi> {
    return new AdminApi(await Api.as(user));
  }

  async read<T>(path: string): Promise<{ body: T; etag: string }> {
    const response = await this.api.get(`/admin${path}`, null);
    expect(response.status(), `${path}: ${await response.text()}`).toBe(200);
    return { body: (await response.json()) as T, etag: response.headers()["etag"] };
  }

  async create<T>(path: string, data: unknown, status = 201): Promise<T> {
    const response = await this.api.post(`/admin${path}`, null, data);
    expect(response.status(), `${path}: ${await response.text()}`).toBe(status);
    return (await response.json()) as T;
  }

  patch(path: string, data: unknown, ifMatch: string): Promise<APIResponse> {
    return this.api.http.patch(`/v1/admin${path}`, {
      headers: this.api.headers(null, { "If-Match": ifMatch }),
      data,
    });
  }

  async tenant(slug: string): Promise<{ tenant_id: string; slug: string; enabled: boolean; version: number }> {
    return this.create("/tenants", { slug, display_name: slug });
  }

  /** Every tenant whose slug starts with `prefix` (all pages). */
  async tenantsWithPrefix(prefix: string): Promise<{ tenant_id: string; slug: string }[]> {
    const found: { tenant_id: string; slug: string }[] = [];
    let cursor: string | null = null;
    do {
      const query: string = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
      const { body } = await this.read<{
        items: { tenant_id: string; slug: string }[];
        page: { next_cursor: string | null };
      }>(`/tenants?page_size=100${query}`);
      found.push(...body.items.filter((item) => item.slug.startsWith(prefix)));
      cursor = body.page.next_cursor;
    } while (cursor !== null);
    return found;
  }

  dispose(): Promise<void> {
    return this.api.dispose();
  }
}
