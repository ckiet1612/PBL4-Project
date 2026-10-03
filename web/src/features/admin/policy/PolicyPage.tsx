// /admin/policy — global outstanding limit and operational mode: two forms, two intents (UX-A23).
// Mode choices mirror validate_mode_transition; the server decides (409 shows its reason).
import { useId, useRef, useState, type FormEvent } from "react";
import { isApiError } from "../../../api/errors";
import type { GlobalPolicy, OperationalMode } from "../../../api/types";
import { useApi } from "../../../auth/session";
import { Notice, Time } from "../../../components/bits";
import { ConfirmDialog } from "../../../components/Dialog";
import { conflictMessage, serverFieldMessage, validationField } from "../errors";
import { checkGlobalLimit } from "../forms";
import { AdminIntent, INTENT_BUSY } from "../intent";
import { MODE_LABELS, modeOptions, NOT_REOPENABLE_WARNING } from "../modes";
import { AdminErrorPanel, RefreshBar, useAdminContext, useAdminLoad, type AdminLoad } from "../shared";

interface PolicyView {
  policy: GlobalPolicy;
  etag: string;
}

export function PolicyPage() {
  const api = useApi().admin;
  const { rememberPolicy } = useAdminContext();
  const view = useAdminLoad(async (signal): Promise<PolicyView> => {
    const response = await api.getPolicy(signal);
    rememberPolicy(response.data);
    return { policy: response.data, etag: response.etag ?? `"v${response.data.version}"` };
  }, []);
  return (
    <section className="page page-narrow">
      <div className="page-header">
        <h1>Chính sách hệ thống</h1>
        <RefreshBar loads={[view]} />
      </div>
      {view.error !== null && <AdminErrorPanel error={view.error} onRetry={view.refresh} />}
      {view.loading && view.data === null && <p className="status-line">Đang tải…</p>}
      {view.data && <PolicyForms view={view} />}
    </section>
  );
}

function PolicyForms({ view }: { view: AdminLoad<PolicyView> }) {
  const api = useApi().admin;
  const { rememberPolicy } = useAdminContext();
  const { policy, etag } = view.data!;
  const limitIntent = useRef(new AdminIntent());
  const modeIntent = useRef(new AdminIntent());
  const ids = { limit: useId(), mode: useId() };
  const [limitText, setLimitText] = useState(String(policy.global_outstanding_limit));
  const [limitTouched, setLimitTouched] = useState(false);
  const [target, setTarget] = useState<OperationalMode | null>(null);
  const [confirmMode, setConfirmMode] = useState(false);
  const [busy, setBusy] = useState<"limit" | "mode" | null>(null);
  const [error, setError] = useState<{ form: "limit" | "mode"; error: unknown } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const frozen = policy.operational_mode === "WRITE_FROZEN";
  const limit = checkGlobalLimit(limitText);
  const limitServerError =
    error?.form === "limit" && validationField(error.error, ["global_outstanding_limit"] as const)
      ? serverFieldMessage(error.error)
      : null;
  const limitError = (limitTouched ? limit.error : null) ?? limitServerError;
  const options = modeOptions(policy.operational_mode);
  const option = options.find((item) => item.target === target) ?? null;

  const apply = (next: GlobalPolicy, nextEtag: string | null) => {
    view.set({ policy: next, etag: nextEtag ?? `"v${next.version}"` });
    rememberPolicy(next);
  };

  const send = async (form: "limit" | "mode", body: { global_outstanding_limit?: number; operational_mode?: OperationalMode }) => {
    setBusy(form);
    setError(null);
    setNotice(null);
    const intent = form === "limit" ? limitIntent.current : modeIntent.current;
    try {
      const result = await intent.send(JSON.stringify(body), etag, (options) =>
        api.updatePolicy(body, { idempotencyKey: options.idempotencyKey, ifMatch: options.ifMatch ?? "" }),
      );
      if (result === INTENT_BUSY) return;
      apply(result.data, result.etag);
      if (form === "limit") {
        setLimitText(String(result.data.global_outstanding_limit));
        setLimitTouched(false);
        setNotice(`Đã lưu giới hạn toàn cục: ${result.data.global_outstanding_limit} job.`);
      } else {
        setTarget(null);
        setNotice(`Hệ thống đã chuyển sang chế độ ${MODE_LABELS[result.data.operational_mode]}.`);
      }
    } catch (caught) {
      if (isApiError(caught, "version_conflict")) {
        try {
          const latest = await api.getPolicy();
          apply(latest.data, latest.etag);
          setNotice(conflictMessage(policy.version, latest.data.version));
        } catch (reloadError) {
          setError({ form, error: reloadError });
        }
      } else {
        setError({ form, error: caught });
      }
    } finally {
      setBusy(null);
    }
  };

  const submitLimit = (event: FormEvent) => {
    event.preventDefault();
    setLimitTouched(true);
    if (limit.value === null || limit.value === policy.global_outstanding_limit) return;
    void send("limit", { global_outstanding_limit: limit.value });
  };

  const submitMode = (event: FormEvent) => {
    event.preventDefault();
    if (option) setConfirmMode(true);
  };

  return (
    <div className="stack">
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}
      <dl className="key-values">
        <dt>Chế độ vận hành</dt>
        <dd>
          <strong>{MODE_LABELS[policy.operational_mode]}</strong> ({policy.operational_mode})
        </dd>
        <dt>Giới hạn job tồn đọng toàn hệ thống</dt>
        <dd>{policy.global_outstanding_limit}</dd>
        <dt>Phiên bản</dt>
        <dd>v{policy.version}</dd>
        <dt>Cập nhật lúc</dt>
        <dd>
          <Time iso={policy.updated_at} />
        </dd>
      </dl>

      <form className="form form-group" onSubmit={submitLimit} aria-label="Giới hạn toàn cục" noValidate>
        <h2>Giới hạn toàn cục</h2>
        <div className="field">
          <label htmlFor={ids.limit}>Số job tồn đọng tối đa (mọi tenant)</label>
          <input
            id={ids.limit}
            inputMode="numeric"
            value={limitText}
            onChange={(event) => setLimitText(event.target.value)}
            disabled={frozen}
            aria-invalid={limitError !== null}
            aria-describedby={`${ids.limit}-help`}
          />
          <p id={`${ids.limit}-help`} className={limitError ? "field-error" : "help"}>
            {frozen
              ? "Đang khóa: chế độ Khóa ghi không cho thay đổi quản trị."
              : (limitError ?? "Số nguyên 1–1.000.000. Thấp hơn số job đang tồn đọng thì máy chủ từ chối.")}
          </p>
        </div>
        {error?.form === "limit" && limitServerError === null && <AdminErrorPanel error={error.error} />}
        <div className="form-actions">
          <button
            type="submit"
            className="primary"
            disabled={frozen || busy !== null || limitText.trim() === String(policy.global_outstanding_limit)}
          >
            {busy === "limit" ? "Đang lưu…" : "Lưu giới hạn"}
          </button>
        </div>
      </form>

      <form className="form form-group" onSubmit={submitMode} aria-label="Chế độ vận hành" noValidate>
        <h2>Chế độ vận hành</h2>
        <fieldset className="choice-list">
          <legend>Chuyển sang</legend>
          {options.map((item) => (
            <label key={item.target} className="checkbox">
              <input
                type="radio"
                name={ids.mode}
                value={item.target}
                checked={target === item.target}
                onChange={() => setTarget(item.target)}
              />
              <span>
                <strong>{MODE_LABELS[item.target]}</strong> — {item.consequence}
              </span>
            </label>
          ))}
        </fieldset>
        {options.some((item) => item.reopenWarning) && <p className="warning">{NOT_REOPENABLE_WARNING}</p>}
        {error?.form === "mode" && <AdminErrorPanel error={error.error} />}
        <div className="form-actions">
          <button type="submit" className="primary" disabled={busy !== null || option === null}>
            Chuyển chế độ
          </button>
        </div>
      </form>

      <ConfirmDialog
        open={confirmMode && option !== null}
        title={option ? `Chuyển sang chế độ ${MODE_LABELS[option.target]}?` : ""}
        confirmLabel="Chuyển chế độ"
        danger
        busy={busy === "mode"}
        onConfirm={async () => {
          if (option) await send("mode", { operational_mode: option.target });
          setConfirmMode(false);
        }}
        onCancel={() => setConfirmMode(false)}
      >
        {option && (
          <>
            <p>{option.consequence}</p>
            {option.note && (
              <p className="warning">
                <strong>{option.note}</strong>
              </p>
            )}
            {option.reopenWarning && <p className="warning">{NOT_REOPENABLE_WARNING}</p>}
          </>
        )}
      </ConfirmDialog>
    </div>
  );
}
