// /admin/recovery?from&to&cursor — recovery events of every tenant (1 read). Default last 24 h.
import { useEffect } from "react";
import { useApi } from "../../../auth/session";
import { EmptyState, Notice, ShortId, Time } from "../../../components/bits";
import { ACTOR_LABELS } from "../../../app/labels";
import { recoveryEventLabel } from "../labels";
import { useCursorPaging, useUrlRange } from "../paging";
import { checkRange } from "../ranges";
import { AdminErrorPanel, RangeForm, RefreshBar, useAdminLoad } from "../shared";

export function RecoveryPage() {
  const api = useApi().admin;
  const paging = useCursorPaging(["from", "to"]);
  const { cursor, resetIfBadCursor } = paging;
  const range = useUrlRange();
  const urlProblem = checkRange(range.from, range.to);
  const events = useAdminLoad(
    async (signal) => (await api.listRecoveryEvents({ ...range, cursor }, signal)).data,
    [range.from, range.to, cursor],
    urlProblem === null,
  );
  useEffect(() => resetIfBadCursor(events.error), [events.error, resetIfBadCursor]);
  const error = paging.shownError(events.error);
  const page = events.data;

  return (
    <section className="page">
      <div className="page-header">
        <h1>Khôi phục</h1>
        <RefreshBar loads={[events]} />
      </div>
      <p className="help">
        Sự kiện mất lần chạy, thu hồi lease, chọn checkpoint và chạy lại của mọi tenant. Mở job để xem thông tin job.
      </p>
      <RangeForm
        label="Khoảng sự kiện khôi phục"
        range={range}
        submitLabel="Xem"
        check={(draft) => {
          const message = checkRange(draft.from, draft.to);
          return message ? { field: "to", message } : null;
        }}
        onSubmit={(draft) => paging.setFilters({ from: draft.from, to: draft.to })}
      />
      {paging.notice && <Notice onDismiss={paging.clearNotice}>{paging.notice}</Notice>}
      {urlProblem !== null && (
        <p className="field-error" role="alert">
          {urlProblem}
        </p>
      )}
      {error !== null && <AdminErrorPanel error={error} onRetry={events.refresh} />}
      {page && page.items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Thời điểm</th>
                <th scope="col">Sự kiện</th>
                <th scope="col">Job</th>
                <th scope="col" className="col-optional">
                  Tenant
                </th>
                <th scope="col" className="col-optional">
                  Bởi
                </th>
                <th scope="col" className="col-optional">
                  Lý do
                </th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((event) => (
                <tr key={event.event_id}>
                  <td>
                    <Time iso={event.created_at} />
                  </td>
                  <td>{recoveryEventLabel(event.type)}</td>
                  <td>
                    {event.job_id ? (
                      <ShortId id={event.job_id} to={`/admin/jobs/${event.job_id}`} copyLabel="Sao chép job ID" />
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="col-optional">
                    <ShortId id={event.tenant_id} to={`/admin/tenants/${event.tenant_id}`} copyLabel="Sao chép tenant ID" />
                  </td>
                  <td className="col-optional">{ACTOR_LABELS[event.actor_type]}</td>
                  <td className="col-optional wrap">{event.reason ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {page && page.items.length === 0 && cursor === null && (
        <EmptyState title="Không có sự kiện khôi phục trong khoảng này" />
      )}
      {paging.pager(page?.page.next_cursor ?? null)}
    </section>
  );
}
