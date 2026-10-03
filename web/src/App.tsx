// Routes follow the sitemap in docs/web-ui.md §1.
import { BrowserRouter, Navigate, Route, Routes } from "react-router";
import { AppErrorBoundary } from "./app/AppErrorBoundary";
import { AppLayout } from "./app/AppLayout";
import { HomeRedirect, NotFoundPage } from "./app/SimplePages";
import { RequireSession, TenantScope } from "./auth/guards";
import { LoginPage } from "./auth/LoginPage";
import { SessionProvider } from "./auth/session";
import { TokensPage } from "./features/account/TokensPage";
import { AdminGuard, AdminNotFoundPage } from "./features/admin/AdminLayout";
import { AdminJobPage } from "./features/admin/jobs/AdminJobPage";
import { AdminJobsPage } from "./features/admin/jobs/AdminJobsPage";
import { AuditPage } from "./features/admin/monitor/AuditPage";
import { FairnessPage } from "./features/admin/monitor/FairnessPage";
import { RecoveryPage } from "./features/admin/monitor/RecoveryPage";
import { OverviewPage } from "./features/admin/OverviewPage";
import { PolicyPage } from "./features/admin/policy/PolicyPage";
import { TenantDetailPage } from "./features/admin/tenants/TenantDetailPage";
import { TenantsPage } from "./features/admin/tenants/TenantsPage";
import { UserDetailPage } from "./features/admin/users/UserDetailPage";
import { UsersPage } from "./features/admin/users/UsersPage";
import { WorkerDetailPage } from "./features/admin/workers/WorkerDetailPage";
import { WorkersPage } from "./features/admin/workers/WorkersPage";
import { DataPage } from "./features/data/DataPage";
import { JobDetailPage } from "./features/jobs/JobDetailPage";
import { JobsPage } from "./features/jobs/JobsPage";
import { SubmitPage } from "./features/submit/SubmitPage";
import { SweepPage } from "./features/submit/SweepPage";

export default function App() {
  return (
    <AppErrorBoundary>
      <BrowserRouter>
        <SessionProvider>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route element={<RequireSession />}>
              <Route element={<AppLayout />}>
                <Route index element={<HomeRedirect />} />
                <Route path="account/tokens" element={<TokensPage />} />
                <Route path="t/:tenantId" element={<TenantScope />}>
                  <Route index element={<Navigate to="jobs" replace />} />
                  <Route path="jobs" element={<JobsPage />} />
                  <Route path="jobs/new" element={<SubmitPage />} />
                  <Route path="jobs/:jobId" element={<JobDetailPage />} />
                  <Route path="sweeps/:sweepId" element={<SweepPage />} />
                  <Route path="data" element={<DataPage />} />
                  <Route path="*" element={<NotFoundPage />} />
                </Route>
                <Route path="admin" element={<AdminGuard />}>
                  <Route index element={<OverviewPage />} />
                  <Route path="workers" element={<WorkersPage />} />
                  <Route path="workers/:workerId" element={<WorkerDetailPage />} />
                  <Route path="jobs" element={<AdminJobsPage />} />
                  <Route path="jobs/:jobId" element={<AdminJobPage />} />
                  <Route path="tenants" element={<TenantsPage />} />
                  <Route path="tenants/:tenantId" element={<TenantDetailPage />} />
                  <Route path="users" element={<UsersPage />} />
                  <Route path="users/:userId" element={<UserDetailPage />} />
                  <Route path="policy" element={<PolicyPage />} />
                  <Route path="fairness" element={<FairnessPage />} />
                  <Route path="recovery" element={<RecoveryPage />} />
                  <Route path="audit" element={<AuditPage />} />
                  <Route path="*" element={<AdminNotFoundPage />} />
                </Route>
                <Route path="*" element={<NotFoundPage />} />
              </Route>
            </Route>
          </Routes>
        </SessionProvider>
      </BrowserRouter>
    </AppErrorBoundary>
  );
}
