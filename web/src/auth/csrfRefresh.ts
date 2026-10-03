// CSRF refresh after invalid_csrf, bound to the user a request was sent as (B18-R14, B18-RV03).
import { SESSION_SWITCHED } from "../api/client";
import type { BrowserSession } from "../api/types";

export interface CsrfRefresherDeps {
  /** GET /v1/auth/session; null when there is no session (or it cannot be read). */
  readSession(): Promise<BrowserSession | null>;
  /** User the tab currently shows; a session of another user is a switch. */
  currentUser(): string | null;
  /** Take over the session read (token and user) as the tab's own. */
  adopt(session: BrowserSession): void;
  /** Called once per switch: drop the old user's state and tell the new user. */
  switched(session: BrowserSession): void;
}

/**
 * Concurrent refreshes share one session read. A request is resent only when it was sent
 * as a known user and the session read belongs to that same user; otherwise the mutation
 * stays unsent (SESSION_SWITCHED), so it can never run under another account's cookie.
 */
export function createCsrfRefresher(deps: CsrfRefresherDeps) {
  let inflight: Promise<BrowserSession | null> | null = null;

  function read(): Promise<BrowserSession | null> {
    inflight ??= deps
      .readSession()
      .catch(() => null)
      .finally(() => {
        inflight = null;
      });
    return inflight;
  }

  return async function refreshCsrf(sentAs: string | null): Promise<string | null | typeof SESSION_SWITCHED> {
    const session = await read();
    if (session === null) return null;
    if (sentAs !== null && sentAs === session.user_id) {
      deps.adopt(session);
      return session.csrf_token;
    }
    const shown = deps.currentUser();
    if (shown !== null && shown !== session.user_id) {
      // The first waiter to see the new user performs the switch; later ones only refuse.
      deps.adopt(session);
      deps.switched(session);
    }
    // sentAs null (no session when sent) or a stale user: refuse, never adopt silently.
    return SESSION_SWITCHED;
  };
}
