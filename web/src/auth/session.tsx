// Browser session in memory only: CSRF lives in a ref, never in storage (docs/web-ui.md, Storage).
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router";
import { createApiClient, isAbortError } from "../api/client";
import { createEndpoints, type Endpoints } from "../api/endpoints";
import { isApiError } from "../api/errors";
import type { BrowserSession } from "../api/types";
import { forgetRetries } from "../features/jobs/retryLinks";
import { createCsrfRefresher } from "./csrfRefresh";

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
  /** Shown once after a CSRF refresh found another user's session (B18-R14). */
  sessionNotice: string | null;
  dismissSessionNotice(): void;
}

export const SESSION_SWITCHED_NOTICE = "Phiên đăng nhập đã đổi sang tài khoản khác";

const SessionContext = createContext<SessionContextValue | null>(null);

function withoutCsrf(session: BrowserSession): SessionInfo {
  const { csrf_token: _csrf, ...rest } = session;
  return rest;
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<SessionState>({ status: "loading" });
  const [lastTenant, setLastTenantState] = useState<string | null>(null);
  const [bootAttempt, setBootAttempt] = useState(0);
  const [sessionNotice, setSessionNotice] = useState<string | null>(null);
  const csrfRef = useRef<string | null>(null);
  /** User of the session this tab is showing; a refresh that returns another user is a switch. */
  const userRef = useRef<string | null>(null);
  const navigate = useNavigate();
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;

  const authenticate = useCallback((session: BrowserSession) => {
    csrfRef.current = session.csrf_token;
    userRef.current = session.user_id;
    setState({ status: "authenticated", session: withoutCsrf(session) });
  }, []);

  const [api] = useState<Endpoints>(() => {
    const expire = () => {
      csrfRef.current = null;
      userRef.current = null;
      forgetRetries();
      setState((current) => (current.status === "authenticated" ? { status: "anonymous", reason: "expired" } : current));
    };
    const endpoints: Endpoints = createEndpoints(
      createApiClient({
        csrfToken: () => csrfRef.current,
        sessionUser: () => userRef.current,
        refreshCsrf: createCsrfRefresher({
          readSession: async () => (await endpoints.getSession()).data,
          currentUser: () => userRef.current,
          adopt: authenticate,
          switched() {
            // Another account signed in from another tab: drop everything of the old one; its
            // mutations are never resent with the new user's CSRF token.
            forgetRetries();
            setLastTenantState(null);
            setSessionNotice(SESSION_SWITCHED_NOTICE);
            navigateRef.current("/", { replace: true });
          },
        }),
        onAuthenticationRequired: expire,
      }),
    );
    return endpoints;
  });

  useEffect(() => {
    const controller = new AbortController();
    api
      .getSession(controller.signal)
      .then((response) => authenticate(response.data))
      .catch((error: unknown) => {
        if (isAbortError(error)) return;
        csrfRef.current = null;
        userRef.current = null;
        if (isApiError(error, "authentication_required")) setState({ status: "anonymous", reason: "boot" });
        else setState({ status: "error", error });
      });
    return () => controller.abort();
  }, [api, authenticate, bootAttempt]);

  const login = useCallback(
    async (username: string, password: string) => {
      await api.login({ username, password });
      // The session read is the one source of CSRF and memberships, as after a reload.
      const response = await api.getSession();
      setSessionNotice(null);
      authenticate(response.data);
    },
    [api, authenticate],
  );

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch (error) {
      if (!isApiError(error, "authentication_required")) throw error;
    }
    csrfRef.current = null;
    userRef.current = null;
    forgetRetries();
    setLastTenantState(null);
    setSessionNotice(null);
    setState({ status: "anonymous", reason: "logout" });
  }, [api]);

  const retry = useCallback(() => {
    setState({ status: "loading" });
    setBootAttempt((n) => n + 1);
  }, []);

  const dismissSessionNotice = useCallback(() => setSessionNotice(null), []);
  const value = useMemo<SessionContextValue>(
    () => ({
      state,
      api,
      login,
      logout,
      retry,
      lastTenant,
      setLastTenant: setLastTenantState,
      sessionNotice,
      dismissSessionNotice,
    }),
    [state, api, login, logout, retry, lastTenant, sessionNotice, dismissSessionNotice],
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
