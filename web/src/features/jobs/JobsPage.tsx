// /t/:tenantId/jobs — filters on the URL, cursor trail in memory, first page polls (LIST_POLL).
import { useEffect, useId, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { isApiError, isBadCursorError } from "../../api/errors";
import { LIST_POLL } from "../../api/polling";
import type { JobState, Template } from "../../api/types";
import { JOB_STATE_LABELS, waitingReasonLabel } from "../../app/labels";
import { useTenant } from "../../auth/guards";
import { useApi, useSession } from "../../auth/session";
import { EmptyState, Notice, ShortId, StatusBadge, Time } from "../../components/bits";
import { ErrorPanel } from "../../components/ErrorPanel";
import { localInputToUtc, shortId, utcToLocalInput } from "../../components/format";
import { back, EMPTY_TRAIL, forward, type PageTrail } from "../../components/pageTrail";
import { Pager } from "../../components/Pager";
import { usePolled } from "../../components/usePolled";

const STATES = Object.keys(JOB_STATE_LABELS) as JobState[];

function templateName(templates: Template[] | null, templateId: string, version: number): string {
  const template = templates?.find((t) => t.template_id === templateId);
  return `${template?.display_name ?? templateId} · v${version}`;
}

export function JobsPage() {
  const { tenantId } = useTenant();
  const session = useSession();
  const api = useApi();
  const [params, setParams] = useSearchParams();
  const rawState = params.get("state");
  const state = rawState && (STATES as string[]).includes(rawState) ? (rawState as JobState) : null;
  const templateId = params.get("template");
  const after = params.get("after");
  const cursor = params.get("cursor");
  const filtered = state !== null || templateId !== null || after !== null;
  const filterKey = `${tenantId}|${state}|${templateId}|${after}`;
  const [trailState, setTrailState] = useState<{ key: string; trail: PageTrail }>({ key: filterKey, trail: EMPTY_TRAIL });
  const trail = trailState.key === filterKey ? trailState.trail : EMPTY_TRAIL;
  const [notice, setNotice] = useState<string | null>(null);
  const ids = { state: useId(), template: useId(), after: useId() };

  const templates = usePolled({ load: async (signal) => (await api.listTemplates(tenantId, signal)).data, schedule: null }, [
    tenantId,
  ]);
  const jobs = usePolled(
    {
      load: async (signal) =>
        (await api.listJobs(tenantId, { state, template_id: templateId, created_after: after, cursor }, signal)).data,
      schedule: cursor === null ? LIST_POLL : null,
      fingerprint: (page) => `${page.items.map((job) => `${job.job_id}:${job.version}`).join(",")}|${page.page.next_cursor}`,
    },
    [tenantId, state, templateId, after, cursor],
  );

  useEffect(() => {
    if (isBadCursorError(jobs.error, cursor)) {
      setNotice("Vị trí trang không còn hợp lệ, đã quay về trang đầu");
      setTrailState({ key: filterKey, trail: EMPTY_TRAIL });
      const next = new URLSearchParams(params);
      next.delete("cursor");
      setParams(next, { replace: true });
    }
  }, [jobs.error, cursor, filterKey, params, setParams]);

  const setFilter = (name: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) next.set(name, value);
    else next.delete(name);
    next.delete("cursor");
    setNotice(null);
    setParams(next);
  };
  const goTo = (target: string | null, nextTrail: PageTrail) => {
    setTrailState({ key: filterKey, trail: nextTrail });
    const next = new URLSearchParams(params);
    if (target) next.set("cursor", target);
    else next.delete("cursor");
    setNotice(null);
    setParams(next);
  };

  const page = jobs.data;
  const showError = jobs.error !== null && !isBadCursorError(jobs.error, cursor);
  return (
    <section className="page">
      <div className="page-header">
        <h1>Jobs</h1>
        <div className="actions">
          <button type="button" onClick={jobs.refresh}>
            Làm mới
          </button>
          <Link className="button primary" to={`/t/${tenantId}/jobs/new`}>
            Tạo job
          </Link>
        </div>
      </div>

      <form className="filter-bar" onSubmit={(event) => event.preventDefault()} aria-label="Bộ lọc job">
        <div className="field">
          <label htmlFor={ids.state}>Trạng thái</label>
          <select id={ids.state} value={state ?? ""} onChange={(event) => setFilter("state", event.target.value || null)}>
            <option value="">Tất cả</option>
            {STATES.map((value) => (
              <option key={value} value={value}>
                {JOB_STATE_LABELS[value]}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={ids.template}>Template</label>
          <select
            id={ids.template}
            value={templateId ?? ""}
            onChange={(event) => setFilter("template", event.target.value || null)}
          >
            <option value="">Tất cả</option>
            {templateId && !templates.data?.some((t) => t.template_id === templateId) && (
              <option value={templateId}>{templateId}</option>
            )}
            {templates.data?.map((template) => (
              <option key={`${template.template_id}@${template.version}`} value={template.template_id}>
                {template.display_name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={ids.after}>Tạo sau (giờ địa phương)</label>
          <input
            id={ids.after}
            type="datetime-local"
            value={utcToLocalInput(after)}
            onChange={(event) => setFilter("after", localInputToUtc(event.target.value))}
          />
        </div>
        <button type="button" onClick={() => setParams(new URLSearchParams())} disabled={!filtered && cursor === null}>
          Xóa bộ lọc
        </button>
      </form>

      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}
      {showError && (
        <ErrorPanel
          error={jobs.error}
          onRetry={jobs.refresh}
          title={isApiError(jobs.error, "permission_denied") ? "Bạn không có quyền xem job của tenant này" : undefined}
        />
      )}

      <p className="muted">Mới nhất trước{cursor === null ? " · trang đầu tự làm mới" : ""}</p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Job</th>
              <th scope="col">Trạng thái</th>
              <th scope="col">Template</th>
              <th scope="col" className="col-optional">
                Người tạo
              </th>
              <th scope="col" className="col-optional">
                Tạo lúc
              </th>
            </tr>
          </thead>
          <tbody>
            {jobs.loading && page === null && (
              <tr>
                <td colSpan={5}>Đang tải…</td>
              </tr>
            )}
            {page?.items.map((job) => (
              <tr key={job.job_id}>
                <td>
                  <ShortId id={job.job_id} to={`/t/${tenantId}/jobs/${job.job_id}`} copyLabel="Sao chép job ID" />
                  {job.retry_of_job_id && <div className="muted">Chạy lại của {shortId(job.retry_of_job_id)}</div>}
                </td>
                <td>
                  <StatusBadge state={job.state} />
                  {waitingReasonLabel(job.waiting_reason) && (
                    <div className="muted">{waitingReasonLabel(job.waiting_reason)}</div>
                  )}
                </td>
                <td>
                  <Link to={`/t/${tenantId}/jobs/${job.job_id}`}>
                    {templateName(templates.data, job.spec.template_id, job.spec.template_version)}
                  </Link>
                </td>
                <td className="col-optional">
                  {job.user_id === session.user_id ? "Bạn" : <code title={job.user_id}>{shortId(job.user_id)}</code>}
                </td>
                <td className="col-optional">
                  <Time iso={job.created_at} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {page && page.items.length === 0 && (filtered || cursor !== null) && (
        <EmptyState title="Không có job khớp bộ lọc">
          <button type="button" onClick={() => setParams(new URLSearchParams())}>
            Xóa bộ lọc
          </button>
        </EmptyState>
      )}
      {page && page.items.length === 0 && !filtered && cursor === null && (
        <EmptyState title="Tenant chưa có job nào">
          <ol>
            <li>
              Chuẩn bị dữ liệu đầu vào ở trang <Link to={`/t/${tenantId}/data`}>Dữ liệu</Link>.
            </li>
            <li>
              <Link to={`/t/${tenantId}/jobs/new`}>Tạo job</Link> với tệp đó.
            </li>
          </ol>
        </EmptyState>
      )}

      <Pager
        hasPrevious={cursor !== null}
        nextCursor={page?.page.next_cursor ?? null}
        onFirst={() => goTo(null, EMPTY_TRAIL)}
        onPrevious={() => {
          const step = back(trail);
          goTo(step.target, step.trail);
        }}
        onNext={(next) => goTo(next, forward(trail, cursor))}
      />
    </section>
  );
}
