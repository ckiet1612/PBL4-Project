// Header, global nav, tenant picker and account menu for every signed-in page.
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useMatch, useNavigate } from "react-router";
import { useSession, useSessionContext, type SessionInfo } from "../auth/session";
import { ErrorPanel } from "../components/ErrorPanel";
import { shortId } from "../components/format";
import { ROLE_LABELS } from "./labels";

/** Tenant on the URL, else the one used last in this SPA lifetime, else the first membership. */
export function useCurrentTenant(): string | null {
  const session = useSession();
  const match = useMatch("/t/:tenantId/*");
  const { lastTenant } = useSessionContext();
  const known = (id: string | null | undefined) => (id && session.memberships.some((m) => m.tenant_id === id) ? id : null);
  return known(match?.params.tenantId) ?? known(lastTenant) ?? session.memberships[0]?.tenant_id ?? null;
}

export function AppLayout() {
  const session = useSession();
  const tenantId = useCurrentTenant();
  return (
    <div className="app">
      <header className="app-header">
        <Link to="/" className="brand">
          Nexa
        </Link>
        {tenantId && <GlobalNav tenantId={tenantId} />}
        <div className="header-tools">
          <TenantPicker session={session} current={tenantId} />
          <AccountMenu session={session} tenantId={tenantId} />
        </div>
      </header>
      <main className="app-main">
        <Outlet />
      </main>
    </div>
  );
}

/** `adminSlot` is where B18 adds its area; B17 renders nothing there. */
function GlobalNav({ tenantId, adminSlot }: { tenantId: string; adminSlot?: ReactNode }) {
  return (
    <nav aria-label="Điều hướng chính" className="global-nav">
      <ul>
        <li>
          <NavLink to={`/t/${tenantId}/jobs`}>Jobs</NavLink>
        </li>
        <li>
          <NavLink to={`/t/${tenantId}/data`}>Dữ liệu</NavLink>
        </li>
        {adminSlot}
      </ul>
    </nav>
  );
}

function TenantPicker({ session, current }: { session: SessionInfo; current: string | null }) {
  const navigate = useNavigate();
  const inData = useMatch("/t/:tenantId/data/*") !== null;
  const id = useId();
  if (session.memberships.length < 2 || current === null) return null;
  // Switching keeps the area (Jobs/Dữ liệu) and drops filters and cursors of the old tenant.
  return (
    <div className="tenant-picker">
      <label htmlFor={id}>Tenant</label>
      <select id={id} value={current} onChange={(event) => navigate(`/t/${event.target.value}/${inData ? "data" : "jobs"}`)}>
        {session.memberships.map((membership) => (
          <option key={membership.tenant_id} value={membership.tenant_id}>
            {`${shortId(membership.tenant_id)} · ${ROLE_LABELS[membership.role]}`}
          </option>
        ))}
      </select>
    </div>
  );
}

function AccountMenu({ session, tenantId }: { session: SessionInfo; tenantId: string | null }) {
  const { logout } = useSessionContext();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuId = useId();
  const role = session.memberships.find((m) => m.tenant_id === tenantId)?.role ?? null;

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    };
    const onPointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [open]);

  const signOut = async () => {
    setBusy(true);
    setError(null);
    try {
      await logout();
    } catch (caught) {
      setError(caught);
      setBusy(false);
    }
  };

  return (
    <div className="account-menu" ref={rootRef}>
      <button
        ref={buttonRef}
        type="button"
        aria-expanded={open}
        aria-controls={menuId}
        onClick={() => setOpen((value) => !value)}
      >
        Tài khoản
      </button>
      {open && (
        <div id={menuId} className="menu-panel">
          <dl className="menu-facts">
            <dt>Vai trò</dt>
            <dd>{role ? ROLE_LABELS[role] : "Chưa thuộc tenant"}</dd>
            {session.system_roles.includes("SYSTEM_ADMIN") && (
              <>
                <dt>Hệ thống</dt>
                <dd>Quản trị viên hệ thống</dd>
              </>
            )}
            <dt>User ID</dt>
            <dd>
              <code title={session.user_id}>{shortId(session.user_id)}</code>
            </dd>
          </dl>
          <ul className="menu-actions">
            <li>
              <Link to="/account/tokens" onClick={() => setOpen(false)}>
                Token CLI
              </Link>
            </li>
            <li>
              <button type="button" onClick={signOut} disabled={busy}>
                {busy ? "Đang đăng xuất…" : "Đăng xuất"}
              </button>
            </li>
          </ul>
          {error !== null && <ErrorPanel error={error} />}
        </div>
      )}
    </div>
  );
}
