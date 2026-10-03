// Tenant ?tab=policy (A7): every TenantPolicy field, PATCH only what changed, If-Match = ETag of
// the latest policy read. 412 → ConflictCompare against the reloaded policy; 409 → server reason
// + POLICY_CONFLICT_HINT.
import { useId, useRef, useState, type FormEvent } from "react";
import { isApiError } from "../../../api/errors";
import type { TenantPolicy } from "../../../api/types";
import { useApi } from "../../../auth/session";
import { Notice, Time } from "../../../components/bits";
import { conflictMessage, POLICY_CONFLICT_HINT, serverFieldMessage, validationField } from "../errors";
import { AdminIntent, INTENT_BUSY } from "../intent";
import {
  POLICY_FIELD_LABELS,
  policyDraft,
  policyUpdate,
  zeroResourceLimit,
  type PolicyDraft,
  type PolicyField,
} from "../policyForm";
import { AdminErrorPanel, RefreshBar, useAdminLoad, type AdminLoad } from "../shared";
import type { TenantView } from "../tenants/TenantDetailPage";
import { ConflictCompare } from "./ConflictCompare";

const GROUPS: { legend: string; help?: string; fields: PolicyField[] }[] = [
  {
    legend: "Phân chia công bằng",
    help: "Trọng số lớn hơn → tenant được phần thời gian tài nguyên lớn hơn khi tranh chấp.",
    fields: ["weight"],
  },
  {
    legend: "Hạn mức tài nguyên",
    help: "Tổng tài nguyên các phân bổ chưa trả của tenant; CPU theo core, RAM theo GiB.",
    fields: ["cpu", "memory", "gpu"],
  },
  {
    legend: "Hạn mức job",
    fields: ["outstanding_limit", "user_outstanding_limit", "concurrent_attempt_limit", "user_concurrent_attempt_limit"],
  },
  {
    legend: "Tốc độ gửi job",
    help: "Nhận 0,5 hoặc 0.5.",
    fields: ["submit_rate_per_second", "submit_burst", "user_submit_rate_per_second", "user_submit_burst"],
  },
];

const WIRE_FIELDS: Record<string, PolicyField> = {
  weight: "weight",
  outstanding_limit: "outstanding_limit",
  user_outstanding_limit: "user_outstanding_limit",
  concurrent_attempt_limit: "concurrent_attempt_limit",
  user_concurrent_attempt_limit: "user_concurrent_attempt_limit",
  submit_rate_per_second: "submit_rate_per_second",
  submit_burst: "submit_burst",
  user_submit_rate_per_second: "user_submit_rate_per_second",
  user_submit_burst: "user_submit_burst",
  resource_limit: "cpu",
};

interface PolicyView {
  policy: TenantPolicy;
  etag: string;
}

export function TenantPolicyTab({ tenantId, tenantLoad }: { tenantId: string; tenantLoad: AdminLoad<TenantView> }) {
  const api = useApi().admin;
  const read = async (signal?: AbortSignal): Promise<PolicyView> => {
    const response = await api.getTenantPolicy(tenantId, signal);
    return { policy: response.data, etag: response.etag ?? `"v${response.data.version}"` };
  };
  const view = useAdminLoad(read, [tenantId]);
  return (
    <div className="stack">
      <div className="page-header">
        <h2>Chính sách tenant</h2>
        <RefreshBar loads={[tenantLoad, view]} />
      </div>
      {view.error !== null && <AdminErrorPanel error={view.error} onRetry={view.refresh} />}
      {view.loading && view.data === null && <p className="status-line">Đang tải…</p>}
      {view.data && <PolicyForm key={view.loadedAt ?? 0} view={view} read={read} />}
    </div>
  );
}

function PolicyForm({ view, read }: { view: AdminLoad<PolicyView>; read(): Promise<PolicyView> }) {
  const api = useApi().admin;
  const { policy, etag } = view.data!;
  const intent = useRef(new AdminIntent());
  const formId = useId();
  const [base, setBase] = useState(policy);
  const [baseEtag, setBaseEtag] = useState(etag);
  const [draft, setDraft] = useState<PolicyDraft>(() => policyDraft(policy));
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [conflict, setConflict] = useState<{ server: PolicyDraft; message: string } | null>(null);
  const diff = policyUpdate(base, draft);
  const serverField = (() => {
    const wire = validationField(error, Object.keys(WIRE_FIELDS));
    return wire ? WIRE_FIELDS[wire] : null;
  })();

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (Object.keys(diff.errors).length > 0 || Object.keys(diff.update).length === 0) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await intent.current.send(JSON.stringify(diff.update), baseEtag, (options) =>
        api.updateTenantPolicy(base.tenant_id, diff.update, {
          idempotencyKey: options.idempotencyKey,
          ifMatch: options.ifMatch ?? "",
        }),
      );
      if (result === INTENT_BUSY) return;
      const nextEtag = result.etag ?? `"v${result.data.version}"`;
      setBase(result.data);
      setBaseEtag(nextEtag);
      setDraft(policyDraft(result.data));
      setConflict(null);
      setTouched(false);
      setNotice(`Đã lưu chính sách (v${result.data.version}).`);
    } catch (caught) {
      if (isApiError(caught, "version_conflict")) {
        try {
          const latest = await read();
          setBase(latest.policy);
          setBaseEtag(latest.etag);
          setConflict({
            server: policyDraft(latest.policy),
            message: conflictMessage(base.version, latest.policy.version),
          });
        } catch (reloadError) {
          setError(reloadError);
        }
      } else {
        setError(caught);
      }
    } finally {
      setBusy(false);
    }
  };

  const fieldError = (field: PolicyField) =>
    (touched || draft[field] !== policyDraft(base)[field] ? diff.errors[field] : undefined) ??
    (serverField === field ? (serverFieldMessage(error) ?? undefined) : undefined);

  return (
    <form className="form" onSubmit={submit} aria-label="Chính sách tenant" noValidate>
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}
      {zeroResourceLimit(base.resource_limit) && (
        <p className="warning">Tenant chưa chạy được job nào vì hạn mức tài nguyên bằng 0</p>
      )}
      <p className="muted">
        Phiên bản v{base.version} · cập nhật <Time iso={base.updated_at} />
      </p>
      {conflict && (
        <ConflictCompare
          server={conflict.server}
          draft={draft}
          message={conflict.message}
          onUseServer={() => {
            setDraft(conflict.server);
            setConflict(null);
          }}
        />
      )}
      {GROUPS.map((group) => (
        <fieldset key={group.legend} className="form-group">
          <legend>{group.legend}</legend>
          {group.help && <p className="help">{group.help}</p>}
          {group.fields.map((field) => {
            const id = `${formId}-${field}`;
            const message = fieldError(field);
            return (
              <div className="field" key={field}>
                <label htmlFor={id}>{POLICY_FIELD_LABELS[field]}</label>
                <input
                  id={id}
                  inputMode="decimal"
                  value={draft[field]}
                  onChange={(event) => setDraft({ ...draft, [field]: event.target.value })}
                  aria-invalid={message !== undefined}
                  aria-describedby={message ? `${id}-error` : undefined}
                />
                {message && (
                  <p id={`${id}-error`} className="field-error">
                    {message}
                  </p>
                )}
              </div>
            );
          })}
        </fieldset>
      ))}
      {error !== null && serverField === null && (
        <>
          <AdminErrorPanel error={error} />
          {isApiError(error, "state_conflict") && <p className="help">{POLICY_CONFLICT_HINT}</p>}
        </>
      )}
      <div className="form-actions">
        <button type="button" onClick={() => setDraft(policyDraft(base))} disabled={busy || !diff.changed}>
          Hoàn tác thay đổi
        </button>
        <button type="submit" className="primary" disabled={busy || !diff.changed}>
          {busy ? "Đang lưu…" : "Lưu thay đổi"}
        </button>
      </div>
    </form>
  );
}
