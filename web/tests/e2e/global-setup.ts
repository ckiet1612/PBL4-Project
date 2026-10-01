import { request } from "@playwright/test";

import { fixture, SESSION_USERS, storageState } from "./fixture";

// One login per stored user for the whole run: specs reuse the session cookie instead of
// spending the per-user login rate limit. CSRF is not stored; the app reads it from
// GET /v1/auth/session into memory, exactly as after a page reload.
export default async function globalSetup(): Promise<void> {
  const seed = fixture();
  for (const user of SESSION_USERS) {
    const context = await request.newContext({
      baseURL: seed.base_url,
      ignoreHTTPSErrors: true,
      extraHTTPHeaders: { Origin: seed.base_url },
    });
    const { username, password } = seed.users[user];
    const response = await context.post("/v1/auth/login", { data: { username, password } });
    if (response.status() !== 200) {
      throw new Error(`global setup: login for ${user} returned HTTP ${response.status()}`);
    }
    await context.storageState({ path: storageState(user) });
    await context.dispose();
  }
}
