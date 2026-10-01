// The only HTTP client of the Web UI. Every call goes to the same-origin REST /v1 that the CLI
// uses; the backend decides authorization and state. The client never logs bodies or headers.
import type { components } from "./generated";
import { REQUEST_TIMEOUT_MS } from "./limits";

export type ContractErrorCode = components["schemas"]["ErrorCode"];
/** Contract codes plus the three failures that never reach the server's envelope. */
export type ClientErrorCode = ContractErrorCode | "network_error" | "timeout" | "unexpected_response";

export type HttpMethod = "GET" | "POST" | "PATCH" | "DELETE";

export interface ApiRequest {
  method?: HttpMethod;
  /** Path under /v1, starting with "/". */
  path: string;
  /** Tenant-scoped routes: the tenant from the URL, sent as X-Nexa-Tenant-Id. */
  tenantId?: string;
  query?: Record<string, string | number | null | undefined>;
  json?: unknown;
  body?: Blob;
  headers?: Record<string, string>;
  idempotencyKey?: string;
  ifMatch?: string;
  signal?: AbortSignal;
  timeoutMs?: number;
  expect?: "json" | "blob";
  /** Session probe or login: a 401 is an answer, not an expired session. */
  authProbe?: boolean;
}

export interface ApiResponse<T> {
  status: number;
  data: T;
  etag: string | null;
  requestId: string | null;
  headers: Headers;
}

export interface ApiErrorInit {
  status: number;
  code: ClientErrorCode;
  requestId: string | null;
  serverMessage: string | null;
  reason: string | null;
  retryAfterSeconds: number | null;
}

export class ApiError extends Error implements ApiErrorInit {
  readonly status: number;
  readonly code: ClientErrorCode;
  readonly requestId: string | null;
  readonly serverMessage: string | null;
  readonly reason: string | null;
  readonly retryAfterSeconds: number | null;

  constructor(init: ApiErrorInit) {
    super(init.code);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.requestId = init.requestId;
    this.serverMessage = init.serverMessage;
    this.reason = init.reason;
    this.retryAfterSeconds = init.retryAfterSeconds;
  }
}

export interface ClientHooks {
  /** CSRF token of the current browser session, held in memory only. */
  csrfToken(): string | null;
  /** Re-read GET /v1/auth/session after invalid_csrf; resolves to the new token or null. */
  refreshCsrf(): Promise<string | null>;
  /** Any 401 (or a second invalid_csrf): drop the in-memory session and go to login. */
  onAuthenticationRequired(): void;
}

export interface ApiClient {
  request<T>(request: ApiRequest): Promise<ApiResponse<T>>;
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/** Retry-After as delta-seconds or HTTP-date; null when absent or unparseable. */
export function parseRetryAfter(value: string | null, nowMs: number): number | null {
  if (value === null) return null;
  const trimmed = value.trim();
  if (/^\d+$/.test(trimmed)) return Number(trimmed);
  const date = Date.parse(trimmed);
  if (Number.isNaN(date) || !/[a-z]/i.test(trimmed)) return null;
  return Math.max(0, Math.ceil((date - nowMs) / 1000));
}

function buildUrl(path: string, query: ApiRequest["query"]): string {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(query ?? {})) {
    if (value === null || value === undefined || value === "") continue;
    params.append(name, String(value));
  }
  const search = params.toString();
  return `/v1${path}${search ? `?${search}` : ""}`;
}

function isEnvelope(value: unknown): value is components["schemas"]["ErrorResponse"] {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.code === "string" && typeof candidate.message === "string";
}

async function errorFrom(response: Response): Promise<ApiError> {
  const requestIdHeader = response.headers.get("X-Request-Id");
  const retryAfterSeconds = parseRetryAfter(response.headers.get("Retry-After"), Date.now());
  let parsed: unknown = null;
  try {
    parsed = await response.json();
  } catch {
    parsed = null;
  }
  if (!isEnvelope(parsed)) {
    return new ApiError({
      status: response.status,
      code: "unexpected_response",
      requestId: requestIdHeader,
      serverMessage: null,
      reason: null,
      retryAfterSeconds,
    });
  }
  return new ApiError({
    status: response.status,
    code: parsed.code,
    requestId: parsed.request_id ?? requestIdHeader,
    serverMessage: parsed.message,
    reason: parsed.reason ?? null,
    retryAfterSeconds,
  });
}

export function createApiClient(hooks: ClientHooks, fetchImpl: typeof fetch = fetch): ApiClient {
  async function send<T>(request: ApiRequest, csrf: string | null): Promise<ApiResponse<T>> {
    const method = request.method ?? "GET";
    const headers = new Headers({ Accept: "application/json" });
    if (request.tenantId) headers.set("X-Nexa-Tenant-Id", request.tenantId);
    if (method !== "GET" && csrf) headers.set("X-CSRF-Token", csrf);
    if (request.idempotencyKey) headers.set("Idempotency-Key", request.idempotencyKey);
    if (request.ifMatch) headers.set("If-Match", request.ifMatch);
    let body: BodyInit | undefined;
    if (request.body !== undefined) {
      headers.set("Content-Type", "application/octet-stream");
      body = request.body;
    } else if (request.json !== undefined) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(request.json);
    }
    for (const [name, value] of Object.entries(request.headers ?? {})) headers.set(name, value);

    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort(new DOMException("Request timed out", "TimeoutError"));
    }, request.timeoutMs ?? REQUEST_TIMEOUT_MS);
    const forwardAbort = () => controller.abort(request.signal?.reason);
    if (request.signal?.aborted) forwardAbort();
    request.signal?.addEventListener("abort", forwardAbort, { once: true });

    let response: Response;
    try {
      response = await fetchImpl(buildUrl(request.path, request.query), {
        method,
        headers,
        body,
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal,
      });
    } catch (error) {
      if (timedOut) {
        throw new ApiError({ status: 0, code: "timeout", requestId: null, serverMessage: null, reason: null, retryAfterSeconds: null });
      }
      if (request.signal?.aborted) throw new DOMException("Request aborted", "AbortError");
      if (isAbortError(error)) throw error;
      throw new ApiError({ status: 0, code: "network_error", requestId: null, serverMessage: null, reason: null, retryAfterSeconds: null });
    } finally {
      clearTimeout(timer);
      request.signal?.removeEventListener("abort", forwardAbort);
    }

    if (!response.ok) throw await errorFrom(response);
    const meta = {
      status: response.status,
      etag: response.headers.get("ETag"),
      requestId: response.headers.get("X-Request-Id"),
      headers: response.headers,
    };
    if (response.status === 204) return { ...meta, data: null as T };
    if (request.expect === "blob") return { ...meta, data: (await response.blob()) as T };
    try {
      return { ...meta, data: (await response.json()) as T };
    } catch {
      throw new ApiError({
        status: response.status,
        code: "unexpected_response",
        requestId: meta.requestId,
        serverMessage: null,
        reason: null,
        retryAfterSeconds: null,
      });
    }
  }

  return {
    async request<T>(request: ApiRequest): Promise<ApiResponse<T>> {
      try {
        return await send<T>(request, hooks.csrfToken());
      } catch (error) {
        if (!(error instanceof ApiError)) throw error;
        if (error.code === "authentication_required" && !request.authProbe) {
          hooks.onAuthenticationRequired();
        }
        if (error.code !== "invalid_csrf" || request.authProbe) throw error;
        // One session refresh, then the same request (same key and body) once more.
        const refreshed = await hooks.refreshCsrf();
        if (refreshed === null) {
          hooks.onAuthenticationRequired();
          throw error;
        }
        try {
          return await send<T>(request, refreshed);
        } catch (second) {
          if (second instanceof ApiError && (second.code === "invalid_csrf" || second.code === "authentication_required")) {
            hooks.onAuthenticationRequired();
          }
          throw second;
        }
      }
    },
  };
}
