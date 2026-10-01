// Route guards. They decide what to render, never what the backend allows.
import { createContext, useContext, useEffect } from "react";
import { Link, Navigate, Outlet, useLocation, useParams } from "react-router";
import type { Role } from "../api/types";
import { ErrorPanel } from "../components/ErrorPanel";
import { useSession, useSessionContext } from "./session";

export function RequireSession() {
  const { state, retry } = useSessionContext();
  const location = useLocation();
  if (state.status === "loading") return <p className="status-line">Đang tải…</p>;
  if (state.status === "error") {
    return (
      <main className="page page-narrow">
        <ErrorPanel error={state.error} onRetry={retry} />
      </main>
    );
  }
  if (state.status === "anonymous") {
    if (state.reason === "logout") return <Navigate to="/login" replace />;
    const next = `${location.pathname}${location.search}${location.hash}`;
    const query = new URLSearchParams({ next });
    if (state.reason === "expired") query.set("expired", "1");
    return <Navigate to={`/login?${query.toString()}`} replace />;
  }
  return <Outlet />;
}

interface TenantContextValue {
  tenantId: string;
  role: Role;
}

const TenantContext = createContext<TenantContextValue | null>(null);

/** `/t/:tenantId/*`: the tenant comes from the URL (UX-A01) and must be one of the memberships. */
export function TenantScope() {
  const { tenantId = "" } = useParams();
  const session = useSession();
  const { setLastTenant } = useSessionContext();
  const membership = session.memberships.find((m) => m.tenant_id === tenantId);
  useEffect(() => {
    if (membership) setLastTenant(membership.tenant_id);
  }, [membership, setLastTenant]);
  if (!membership) return <NoTenantAccess />;
  return (
    <TenantContext.Provider value={{ tenantId: membership.tenant_id, role: membership.role }}>
      <Outlet />
    </TenantContext.Provider>
  );
}

export function useTenant(): TenantContextValue {
  const value = useContext(TenantContext);
  if (value === null) throw new Error("useTenant outside TenantScope");
  return value;
}

function NoTenantAccess() {
  return (
    <section className="page page-narrow">
      <h1>Không có quyền truy cập tenant này</h1>
      <p>Tài khoản của bạn không thuộc tenant trên đường dẫn.</p>
      <p>
        <Link to="/">Về tenant mặc định</Link>
      </p>
    </section>
  );
}
