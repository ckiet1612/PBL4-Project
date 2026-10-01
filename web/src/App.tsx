// Routes follow the sitemap in docs/web-ui.md §1.
import { BrowserRouter, Navigate, Route, Routes } from "react-router";
import { AppLayout } from "./app/AppLayout";
import { HomeRedirect, NotFoundPage } from "./app/SimplePages";
import { RequireSession, TenantScope } from "./auth/guards";
import { LoginPage } from "./auth/LoginPage";
import { SessionProvider } from "./auth/session";
import { TokensPage } from "./features/account/TokensPage";
import { DataPage } from "./features/data/DataPage";
import { JobDetailPage } from "./features/jobs/JobDetailPage";
import { JobsPage } from "./features/jobs/JobsPage";
import { SubmitPage } from "./features/submit/SubmitPage";
import { SweepPage } from "./features/submit/SweepPage";

export default function App() {
  return (
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
              <Route path="*" element={<NotFoundPage />} />
            </Route>
          </Route>
        </Routes>
      </SessionProvider>
    </BrowserRouter>
  );
}
