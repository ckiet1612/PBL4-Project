// /admin/*: guard, frame (title, audit note, grouped nav, mode banner) and the admin not-found page.
import { useMemo, useState } from "react";
import { Link, NavLink, Outlet } from "react-router";
import type { GlobalPolicy } from "../../api/types";
import { isSystemAdmin, useSession, useSessionContext } from "../../auth/session";
import { ADMIN_REQUIRED } from "./errors";
import { MODE_LABELS } from "./modes";
import { AdminContext } from "./shared";

const NAV_GROUPS = [
  {
    label: "Vận hành",
    items: [
      { to: "/admin", label: "Tổng quan", end: true },
      { to: "/admin/workers", label: "Worker" },
      { to: "/admin/jobs", label: "Hàng chờ" },
      { to: "/admin/recovery", label: "Khôi phục" },
    ],
  },
  {
    label: "Tổ chức",
    items: [
      { to: "/admin/tenants", label: "Tenant" },
      { to: "/admin/users", label: "User" },
    ],
  },
  { label: "Chính sách", items: [{ to: "/admin/policy", label: "Hệ thống" }] },
  {
    label: "Giám sát",
    items: [
      { to: "/admin/fairness", label: "Fairness" },
      { to: "/admin/audit", label: "Audit" },
    ],
  },
];

/** Display only: the backend answers 403 anyway. Non-admins never send a /v1/admin request. */
export function AdminGuard() {
  const session = useSession();
  if (!isSystemAdmin(session)) {
    return (
      <section className="page page-narrow">
        <h1>{ADMIN_REQUIRED}</h1>
        <p>Khu quản trị chỉ dành cho tài khoản có quyền quản trị hệ thống.</p>
        <p>
          <Link to="/">Về trang chủ</Link>
        </p>
      </section>
    );
  }
  return <AdminLayout />;
}

function AdminLayout() {
  const session = useSession();
  const { lastTenant } = useSessionContext();
  const [policy, setPolicy] = useState<GlobalPolicy | null>(null);
  const context = useMemo(() => ({ policy, rememberPolicy: setPolicy }), [policy]);
  const tenants = session.memberships.map((membership) => membership.tenant_id);
  const tenantHome = tenants.length === 0 ? null : lastTenant && tenants.includes(lastTenant) ? lastTenant : tenants[0];
  return (
    <div className="admin-area">
      <div className="admin-frame">
        <div>
          <p className="admin-title">Quản trị hệ thống</p>
          <p className="muted">Mọi thao tác và lượt xem trong khu này đều được ghi audit</p>
        </div>
        {tenantHome && (
          <Link className="button" to={`/t/${tenantHome}/jobs`}>
            Về khu tenant
          </Link>
        )}
      </div>
      <nav aria-label="Điều hướng quản trị" className="admin-nav">
        {NAV_GROUPS.map((group) => (
          <div key={group.label} className="admin-nav-group">
            <span className="admin-nav-label">{group.label}</span>
            <ul>
              {group.items.map((item) => (
                <li key={item.to}>
                  <NavLink to={item.to} end={item.end}>
                    {item.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>
      <div className="admin-content">
        {policy && policy.operational_mode !== "NORMAL" && (
          <div className="mode-banner" role="status">
            Hệ thống đang ở chế độ <strong>{MODE_LABELS[policy.operational_mode]}</strong>.{" "}
            <Link to="/admin/policy">Chính sách hệ thống</Link>
          </div>
        )}
        <AdminContext.Provider value={context}>
          <Outlet />
        </AdminContext.Provider>
      </div>
    </div>
  );
}

export function AdminNotFoundPage() {
  return (
    <section className="page page-narrow">
      <h1>Không tìm thấy trang</h1>
      <p>
        <Link to="/admin">Về Tổng quan quản trị</Link>
      </p>
    </section>
  );
}
