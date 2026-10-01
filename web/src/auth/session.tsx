// Browser session in memory only: CSRF lives in a ref, never in storage (docs/web-ui.md, Storage).
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createApiClient, isAbortError } from "../api/client";
import { createEndpoints, type Endpoints } from "../api/endpoints";
import { isApiError } from "../api/errors";
import type { BrowserSession } from "../api/types";
import { forgetRetries } from "../features/jobs/retryLinks";

export type SessionInfo = Omit<BrowserSession, "csrf_token">;

/** boot: no session at page load; expired: a call returned 401; logout: the user signed out. */
export type AnonymousReason = "boot" | "expired" | "logout";

export type SessionState =
  | { status: "loading" }
  | { status: "anonymous"; reason: AnonymousReason }
  | { status: "authenticated"; session: SessionInfo }
  | { status: "error"; error: unknown };

interface SessionContextValue {
  state: SessionState;
  api: Endpoints;
  login(username: string, password: string): Promise<void>;
  logout(): Promise<void>;
  retry(): void;
  /** Tenant used most recently in this SPA lifetime (memory only, UX-A02). */
  lastTenant: string | null;
  setLastTenant(tenantId: string): void;
}

const SessionContext = createContext<SessionContextValue | null>(null);

function withoutCsrf(session: BrowserSession): SessionInfo {
  const { csrf_token: _csrf, ...rest } = session;
  return rest;
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<SessionState>({ status: "loading" });
  const [lastTenant, setLastTenantState] = useState<string | null>(null);
  const [bootAttempt, setBootAttempt] = useState(0);
  const csrfRef = useRef<string | null>(null);

  const [api] = useState<Endpoints>(() => {
    const expire = () => {
      csrfRef.current = null;
      forgetRetries();
      setState((current) => (current.status === "authenticated" ? { status: "anonymous", reason: "expired" } : current));
    };
    const endpoints: Endpoints = createEndpoints(
      createApiClient({
        csrfToken: () => csrfRef.current,
        async refreshCsrf() {
          try {
            const response = await endpoints.getSession();
            csrfRef.current = response.data.csrf_token;
            setState({ status: "authenticated", session: withoutCsrf(response.data) });
            return response.data.csrf_token;
          } catch {
            return null;
          }
        },
        onAuthenticationRequired: expire,
      }),
    );
    return endpoints;
  });

  useEffect(() => {
    const controller = new AbortController();
    api
      .getSession(controller.signal)
      .then((response) => {
        csrfRef.current = response.data.csrf_token;
        setState({ status: "authenticated", session: withoutCsrf(response.data) });
      })
      .catch((error: unknown) => {
        if (isAbortError(error)) return;
        csrfRef.current = null;
        if (isApiError(error, "authentication_required")) setState({ status: "anonymous", reason: "boot" });
        else setState({ status: "error", error });
      });
    return () => controller.abort();
  }, [api, bootAttempt]);

  const login = useCallback(
    async (username: string, password: string) => {
      await api.login({ username, password });
      // The session read is the one source of CSRF and memberships, as after a reload.
      const response = await api.getSession();
      csrfRef.current = response.data.csrf_token;
      setState({ status: "authenticated", session: withoutCsrf(response.data) });
    },
    [api],
  );

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch (error) {
      if (!isApiError(error, "authentication_required")) throw error;
    }
    csrfRef.current = null;
    forgetRetries();
    setLastTenantState(null);
    setState({ status: "anonymous", reason: "logout" });
  }, [api]);

  const retry = useCallback(() => {
    setState({ status: "loading" });
    setBootAttempt((n) => n + 1);
  }, []);

  const value = useMemo<SessionContextValue>(
    () => ({ state, api, login, logout, retry, lastTenant, setLastTenant: setLastTenantState }),
    [state, api, login, logout, retry, lastTenant],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSessionContext(): SessionContextValue {
  const value = useContext(SessionContext);
  if (value === null) throw new Error("useSessionContext outside SessionProvider");
  return value;
}

export function useApi(): Endpoints {
  return useSessionContext().api;
}

/** Only below RequireSession. */
export function useSession(): SessionInfo {
  const { state } = useSessionContext();
  if (state.status !== "authenticated") throw new Error("useSession without an authenticated session");
  return state.session;
}

export function isSystemAdmin(session: SessionInfo): boolean {
  return session.system_roles.includes("SYSTEM_ADMIN");
}
