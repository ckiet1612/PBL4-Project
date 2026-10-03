// /admin/audit?from&to&action&cursor — audit records (1 read), including admin reads.
import { useEffect, useId, useState } from "react";
import type { AuditRecord } from "../../../api/types";
import { useApi } from "../../../auth/session";
import { EmptyState, Notice, ShortId, Time } from "../../../components/bits";
import { AUDIT_ACTION_LABELS, auditActionLabel } from "../labels";
import { useCursorPaging, useUrlRange } from "../paging";
import { checkRange } from "../ranges";
import { AdminErrorPanel, RangeForm, RefreshBar, useAdminLoad } from "../shared";

const ACTIONS = Object.keys(AUDIT_ACTION_LABELS).sort();
const ACTOR_TYPES: Record<AuditRecord["actor_type"], string> = {
  USER: "User",
  ADMIN: "Quản trị viên",
  WORKER: "Worker",
  COORDINATOR: "Coordinator",
  SYSTEM: "Hệ thống",
};

export function AuditPage() {
  const api = useApi().admin;
  const paging = useCursorPaging(["from", "to", "action"]);
  const { params, cursor, resetIfBadCursor } = paging;
  const range = useUrlRange();
  const action = params.get("action");
  const [actionDraft, setActionDraft] = useState(action ?? "");
  const actionId = useId();
  const urlProblem = checkRange(range.from, range.to);
  const records = useAdminLoad(
    async (signal) => (await api.listAudit({ ...range, action, cursor }, signal)).data,
    [range.from, range.to, action, cursor],
    urlProblem === null,
  );
  useEffect(() => resetIfBadCursor(records.error), [records.error, resetIfBadCursor]);
  const error = paging.shownError(records.error);
  const page = records.data;

  return (
    <section className="page">
      <div className="page-header">
        <h1>Audit</h1>
        <RefreshBar loads={[records]} />
      </div>
      <p className="notice">Danh sách gồm cả lượt xem của quản trị viên</p>
      <RangeForm
        label="Bộ lọc audit"
        range={range}
        submitLabel="Lọc"
        check={(draft) => {
          const message = checkRange(draft.from, draft.to);
          return message ? { field: "to", message } : null;
        }}
        onSubmit={(draft) => paging.setFilters({ from: draft.from, to: draft.to, action: actionDraft || null })}
      >
        <div className="field">
          <label htmlFor={actionId}>Hành động</label>
          <select id={actionId} value={actionDraft} onChange={(event) => setActionDraft(event.target.value)}>
            <option value="">Tất cả</option>
            {actionDraft && !ACTIONS.includes(actionDraft) && <option value={actionDraft}>{actionDraft}</option>}
            {ACTIONS.map((value) => (
              <option key={value} value={value}>
                {auditActionLabel(value)} ({value})
              </option>
            ))}
          </select>
        </div>
      </RangeForm>
      {paging.notice && <Notice onDismiss={paging.clearNotice}>{paging.notice}</Notice>}
      {urlProblem !== null && (
        <p className="field-error" role="alert">
          {urlProblem}
        </p>
      )}
      {error !== null && <AdminErrorPanel error={error} onRetry={records.refresh} />}
      {page && page.items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Thời điểm</th>
                <th scope="col">Hành động</th>
                <th scope="col">Bởi</th>
                <th scope="col" className="col-optional">
                  Đối tượng
                </th>
                <th scope="col" className="col-optional">
                  Tenant
                </th>
                <th scope="col" className="col-optional">
                  Lý do
                </th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((record) => (
                <tr key={record.audit_id}>
                  <td>
                    <Time iso={record.created_at} />
                  </td>
                  <td title={record.action}>{auditActionLabel(record.action)}</td>
                  <td>
                    {ACTOR_TYPES[record.actor_type]}{" "}
                    <ShortId id={record.actor_id} copyLabel="Sao chép ID người thực hiện" />
                  </td>
                  <td className="col-optional">
                    {record.target_type} <ShortId id={record.target_id} copyLabel="Sao chép ID đối tượng" />
                  </td>
                  <td className="col-optional">
                    {record.tenant_id ? (
                      <ShortId id={record.tenant_id} to={`/admin/tenants/${record.tenant_id}`} copyLabel="Sao chép tenant ID" />
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="col-optional wrap">{record.reason || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {page && page.items.length === 0 && cursor === null && (
        <EmptyState title="Không có bản ghi audit khớp bộ lọc" />
      )}
      {paging.pager(page?.page.next_cursor ?? null)}
    </section>
  );
}
