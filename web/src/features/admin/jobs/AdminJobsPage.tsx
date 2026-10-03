// /admin/jobs — cross-tenant queue, read-only (A3). Two reads per open: jobs + first 100 tenants
// for slugs and the tenant filter. No polling; filters and cursor on the URL.
import { useEffect, useId, useState, type FormEvent } from "react";
import { PAGE_SIZE } from "../../../api/limits";
import type { JobState, WaitingReason } from "../../../api/types";
import { JOB_STATE_LABELS, WAITING_REASON_LABELS, waitingReasonLabel } from "../../../app/labels";
import { useApi } from "../../../auth/session";
import { EmptyState, Notice, ShortId, StatusBadge, Time } from "../../../components/bits";
import { localInputToUtc, utcToLocalInput } from "../../../components/format";
import { validationField } from "../errors";
import { useCursorPaging } from "../paging";
import { AdminErrorPanel, RefreshBar, TenantName, UserName, useAdminLoad } from "../shared";

const STATES = Object.keys(JOB_STATE_LABELS) as JobState[];
const REASONS = Object.keys(WAITING_REASON_LABELS) as NonNullable<WaitingReason>[];
const FILTERS = ["tenant_id", "user_id", "state", "waiting_reason", "created_after"] as const;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function pick<T extends string>(value: string | null, allowed: readonly T[]): T | null {
  return value !== null && (allowed as readonly string[]).includes(value) ? (value as T) : null;
}

export function AdminJobsPage() {
  const api = useApi().admin;
  const paging = useCursorPaging(FILTERS);
  const { params, cursor, resetIfBadCursor } = paging;
  const filters = {
    tenant_id: params.get("tenant_id"),
    user_id: params.get("user_id"),
    state: pick(params.get("state"), STATES),
    waiting_reason: pick(params.get("waiting_reason"), REASONS),
    created_after: params.get("created_after"),
    cursor,
  };
  const filtered = FILTERS.some((name) => params.get(name) !== null);
  const ids = { tenant: useId(), user: useId(), state: useId(), reason: useId(), after: useId() };
  const [userDraft, setUserDraft] = useState(filters.user_id ?? "");
  const [userError, setUserError] = useState<string | null>(null);
  useEffect(() => setUserDraft(filters.user_id ?? ""), [filters.user_id]);

  const tenants = useAdminLoad(async (signal) => (await api.listTenants(null, signal, PAGE_SIZE.adminNames)).data, []);
  const jobs = useAdminLoad(async (signal) => (await api.listJobs(filters, signal)).data, [filters]);
  useEffect(() => resetIfBadCursor(jobs.error), [jobs.error, resetIfBadCursor]);
  const fieldInError = validationField(jobs.error, ["user_id", "tenant_id", "created_after"] as const);
  const error = paging.shownError(jobs.error);
  const tenantItems = tenants.data?.items ?? null;
  const page = jobs.data;

  const applyUser = (event: FormEvent) => {
    event.preventDefault();
    const value = userDraft.trim();
    if (value !== "" && !UUID.test(value)) {
      setUserError("User ID là UUID dạng 0190…-…");
      return;
    }
    setUserError(null);
    paging.setFilters({ user_id: value || null });
  };

  return (
    <section className="page">
      <div className="page-header">
        <h1>Hàng chờ</h1>
        <RefreshBar loads={[jobs, tenants]} />
      </div>
      <p className="muted">Mọi tenant, chỉ xem. Điều khiển job cần là thành viên của tenant.</p>
      {paging.notice && <Notice onDismiss={paging.clearNotice}>{paging.notice}</Notice>}

      <form className="filter-bar" onSubmit={applyUser} aria-label="Bộ lọc hàng chờ" noValidate>
        <div className="field">
          <label htmlFor={ids.tenant}>Tenant</label>
          <select
            id={ids.tenant}
            value={filters.tenant_id ?? ""}
            onChange={(event) => paging.setFilters({ tenant_id: event.target.value || null })}
          >
            <option value="">Tất cả</option>
            {filters.tenant_id && !tenantItems?.some((item) => item.tenant_id === filters.tenant_id) && (
              <option value={filters.tenant_id}>{filters.tenant_id}</option>
            )}
            {tenantItems?.map((tenant) => (
              <option key={tenant.tenant_id} value={tenant.tenant_id}>
                {tenant.slug}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={ids.state}>Trạng thái</label>
          <select
            id={ids.state}
            value={filters.state ?? ""}
            onChange={(event) => paging.setFilters({ state: event.target.value || null })}
          >
            <option value="">Tất cả</option>
            {STATES.map((value) => (
              <option key={value} value={value}>
                {JOB_STATE_LABELS[value]}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={ids.reason}>Lý do chờ</label>
          <select
            id={ids.reason}
            value={filters.waiting_reason ?? ""}
            onChange={(event) => paging.setFilters({ waiting_reason: event.target.value || null })}
          >
            <option value="">Tất cả</option>
            {REASONS.map((value) => (
              <option key={value} value={value}>
                {WAITING_REASON_LABELS[value]}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={ids.after}>Tạo sau (giờ địa phương)</label>
          <input
            id={ids.after}
            type="datetime-local"
            value={utcToLocalInput(filters.created_after)}
            onChange={(event) => paging.setFilters({ created_after: localInputToUtc(event.target.value) })}
            aria-invalid={fieldInError === "created_after"}
          />
        </div>
        <div className="field">
          <label htmlFor={ids.user}>User ID</label>
          <input
            id={ids.user}
            value={userDraft}
            onChange={(event) => setUserDraft(event.target.value)}
            aria-invalid={userError !== null || fieldInError === "user_id"}
            aria-describedby={userError ? `${ids.user}-error` : undefined}
            spellCheck={false}
          />
          {userError && (
            <p id={`${ids.user}-error`} className="field-error">
              {userError}
            </p>
          )}
        </div>
        <button type="submit">Lọc theo user</button>
        <button type="button" onClick={paging.clearFilters} disabled={!filtered && cursor === null}>
          Xóa bộ lọc
        </button>
      </form>
      {tenants.data?.page.next_cursor && (
        <p className="muted">Danh sách chọn tenant chỉ gồm {PAGE_SIZE.adminNames} tenant đầu tiên.</p>
      )}

      {error !== null && <AdminErrorPanel error={error} onRetry={jobs.refresh} />}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Job</th>
              <th scope="col">Tenant</th>
              <th scope="col">Trạng thái</th>
              <th scope="col" className="col-optional">
                Template
              </th>
              <th scope="col" className="col-optional">
                User
              </th>
              <th scope="col" className="col-optional">
                Tạo lúc
              </th>
            </tr>
          </thead>
          <tbody>
            {jobs.loading && page === null && (
              <tr>
                <td colSpan={6}>Đang tải…</td>
              </tr>
            )}
            {page?.items.map((job) => (
              <tr key={job.job_id}>
                <td>
                  <ShortId id={job.job_id} to={`/admin/jobs/${job.job_id}`} copyLabel="Sao chép job ID" />
                </td>
                <td>
                  <TenantName id={job.tenant_id} tenants={tenantItems} />
                </td>
                <td>
                  <StatusBadge state={job.state} />
                  {job.waiting_reason && <div className="muted">{waitingReasonLabel(job.waiting_reason)}</div>}
                </td>
                <td className="col-optional">
                  {job.spec.template_id}@{job.spec.template_version}
                </td>
                <td className="col-optional">
                  <UserName id={job.user_id} users={null} />
                </td>
                <td className="col-optional">
                  <Time iso={job.created_at} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {page && page.items.length === 0 && cursor === null && (
        <EmptyState title={filtered ? "Không có job khớp bộ lọc" : "Chưa có job nào"}>
          {filtered && (
            <button type="button" onClick={paging.clearFilters}>
              Xóa bộ lọc
            </button>
          )}
        </EmptyState>
      )}
      {paging.pager(page?.page.next_cursor ?? null)}
    </section>
  );
}
