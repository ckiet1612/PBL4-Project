// "/" redirect, the no-tenant explanation and the not-found page.
import { Link, Navigate } from "react-router";
import { isSystemAdmin, useSession, useSessionContext } from "../auth/session";

export function HomeRedirect() {
  const session = useSession();
  const { lastTenant } = useSessionContext();
  const tenants = session.memberships.map((m) => m.tenant_id);
  if (tenants.length === 0) return <NoTenantPage />;
  const target = lastTenant && tenants.includes(lastTenant) ? lastTenant : tenants[0];
  return <Navigate to={`/t/${target}/jobs`} replace />;
}

function NoTenantPage() {
  const session = useSession();
  return (
    <section className="page page-narrow">
      <h1>Chưa thuộc tenant nào</h1>
      <p>Tài khoản chưa thuộc tenant nào. Liên hệ quản trị viên để được thêm vào tenant.</p>
      {isSystemAdmin(session) && (
        <p>
          <Link to="/admin">Mở khu quản trị</Link>
        </p>
      )}
      <p>
        Token CLI và đăng xuất nằm trong menu <strong>Tài khoản</strong>.
      </p>
    </section>
  );
}

export function NotFoundPage() {
  return (
    <section className="page page-narrow">
      <h1>Không tìm thấy trang</h1>
      <p>
        <Link to="/">Về Jobs</Link>
      </p>
    </section>
  );
}
