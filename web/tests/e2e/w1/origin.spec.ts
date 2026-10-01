import { request } from "@playwright/test";

import { fixture } from "../fixture";
import { expect, test } from "../support";

// The B17 Caddy origin (deploy/web/Caddyfile): document headers, cache rules, closed paths.

const CSP =
  "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; " +
  "frame-ancestors 'none'; base-uri 'none'; form-action 'self'";

test("origin: document headers, hashed assets, no inline script, docs and internal routes closed", async () => {
  const http = await request.newContext({
    baseURL: fixture().base_url,
    ignoreHTTPSErrors: true,
    storageState: { cookies: [], origins: [] },
  });

  for (const path of ["/", "/t/some-tenant/jobs"]) {
    const page = await http.get(path);
    expect(page.status()).toBe(200);
    const headers = page.headers();
    expect(headers["content-security-policy"]).toBe(CSP);
    expect(headers["x-content-type-options"]).toBe("nosniff");
    expect(headers["referrer-policy"]).toBe("same-origin");
    expect(headers["cache-control"]).toBe("no-cache");
    expect(headers["server"]).toBeUndefined();
  }

  const html = await (await http.get("/")).text();
  const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)];
  expect(scripts.length).toBeGreaterThan(0);
  for (const [, attributes, body] of scripts) {
    expect(attributes).toMatch(/\bsrc="\/assets\/[^"]+\.js"/);
    expect(body.trim()).toBe("");
  }
  expect(html).not.toMatch(/\sstyle="/);

  const asset = /src="(\/assets\/[^"]+\.js)"/.exec(html)![1];
  const script = await http.get(asset);
  expect(script.status()).toBe(200);
  expect(script.headers()["cache-control"]).toBe("public, max-age=31536000, immutable");
  expect((await http.get("/assets/missing-0000.js")).status()).toBe(404);

  for (const path of ["/docs", "/redoc", "/openapi.json"]) expect((await http.get(path)).status()).toBe(404);
  for (const path of ["/v1/internal/admin-bootstrap", "/v1/internal/worker-bootstrap"]) {
    const closed = await http.post(path, { data: {} });
    expect(closed.status()).toBe(404);
    expect(await closed.text()).toBe("");
  }
  await http.dispose();
});
