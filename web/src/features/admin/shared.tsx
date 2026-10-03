// Admin area plumbing. Every /v1/admin request writes an audit row, so reads load once per
// open/refresh (no polling, B18-R02) and show when they were taken.
import { createContext, useContext, useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router";
import { isAbortError } from "../../api/client";
import type { GlobalPolicy, Tenant, User } from "../../api/types";
import { ShortId, Time } from "../../components/bits";
import { ErrorPanel } from "../../components/ErrorPanel";
import { localInputToUtc, utcToLocalInput } from "../../components/format";
import { adminErrorView } from "./errors";
import { defaultRange, RANGE_PRESETS, type TimeRange } from "./ranges";

interface AdminContextValue {
  /** Latest global policy read on any admin page; drives the mode banner without extra calls. */
  policy: GlobalPolicy | null;
  rememberPolicy(policy: GlobalPolicy): void;
}

export const AdminContext = createContext<AdminContextValue | null>(null);

export function useAdminContext(): AdminContextValue {
  const value = useContext(AdminContext);
  if (value === null) throw new Error("useAdminContext outside AdminLayout");
  return value;
}

export interface AdminLoad<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  /** ms epoch of the last successful read. */
  loadedAt: number | null;
  refresh(): void;
  /** Newest representation from a mutation response or a 412 reload. */
  set(data: T): void;
}

/**
 * One read when `deps` change and on refresh(); never on a timer. A refresh keeps the shown data
 * until the new read arrives; a deps change clears it. `enabled = false` sends nothing.
 */
export function useAdminLoad<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  enabled = true,
): AdminLoad<T> {
  const [state, setState] = useState<{ data: T | null; error: unknown; loading: boolean; loadedAt: number | null }>({
    data: null,
    error: null,
    loading: enabled,
    loadedAt: null,
  });
  const [attempt, setAttempt] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  const key = JSON.stringify(deps);
  const keyRef = useRef(key);

  useEffect(() => {
    const same = keyRef.current === key;
    keyRef.current = key;
    if (!enabled) {
      setState({ data: null, error: null, loading: false, loadedAt: null });
      return;
    }
    const controller = new AbortController();
    setState((current) => ({
      data: same ? current.data : null,
      error: null,
      loading: true,
      loadedAt: same ? current.loadedAt : null,
    }));
    loadRef.current(controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setState({ data, error: null, loading: false, loadedAt: Date.now() });
      },
      (error: unknown) => {
        if (controller.signal.aborted || isAbortError(error)) return;
        setState((current) => ({ ...current, error, loading: false }));
      },
    );
    return () => controller.abort();
  }, [key, attempt, enabled]);

  return {
    ...state,
    refresh: () => setAttempt((n) => n + 1),
    set: (data) => setState((current) => ({ ...current, data })),
  };
}

/** "Cập nhật lúc …" + Làm mới for the reads of one page; `status` adds the auto-update state. */
export function RefreshBar({ loads, status }: { loads: AdminLoad<unknown>[]; status?: ReactNode }) {
  const loading = loads.some((load) => load.loading);
  const times = loads.map((load) => load.loadedAt);
  const loadedAt = times.every((time) => time !== null) ? Math.max(...(times as number[])) : null;
  return (
    <div className="refresh-bar">
      <span className="muted">
        {loadedAt !== null ? (
          <>
            Cập nhật lúc <Time iso={new Date(loadedAt).toISOString()} />
          </>
        ) : loading ? (
          "Đang tải…"
        ) : (
          "Chưa tải được dữ liệu"
        )}
      </span>
      {status}
      <button type="button" onClick={() => loads.forEach((load) => load.refresh())} disabled={loading}>
        Làm mới
      </button>
    </div>
  );
}

export function AdminErrorPanel({ error, onRetry, title }: { error: unknown; onRetry?(): void; title?: string }) {
  return <ErrorPanel error={error} onRetry={onRetry} title={title} describe={adminErrorView} />;
}

/** Tenant slug from a list already in memory (UX-A20); otherwise the short ID. */
export function TenantName({ id, tenants, link = true }: { id: string; tenants: Tenant[] | null | undefined; link?: boolean }) {
  const tenant = tenants?.find((item) => item.tenant_id === id);
  const to = link ? `/admin/tenants/${id}` : undefined;
  if (!tenant) return <ShortId id={id} to={to} copyLabel="Sao chép tenant ID" />;
  return (
    <span title={id}>
      {to ? <Link to={to}>{tenant.slug}</Link> : tenant.slug}
    </span>
  );
}

export function EnabledBadge({ enabled }: { enabled: boolean }) {
  return <span className={`badge tone-${enabled ? "success" : "danger"}`}>{enabled ? "Đang bật" : "Đã tắt"}</span>;
}

export function UserName({ id, users }: { id: string; users: User[] | null | undefined }) {
  const user = users?.find((item) => item.user_id === id);
  if (!user) return <ShortId id={id} to={`/admin/users/${id}`} copyLabel="Sao chép user ID" />;
  return <span title={id}>{user.username}</span>;
}

interface RangeFormProps {
  range: TimeRange;
  /** Error from the page's own check of the draft (null = ok). */
  check(draft: TimeRange): { field: string; message: string } | null;
  onSubmit(range: TimeRange): void;
  submitLabel: string;
  /** Extra fields (bucket, tenant, action) rendered before the button. */
  children?: ReactNode;
  label: string;
}

/** Local-time datetime inputs sent as RFC3339 UTC; presets are relative to now. */
export function RangeForm({ range, check, onSubmit, submitLabel, children, label }: RangeFormProps) {
  const [draft, setDraft] = useState({ from: utcToLocalInput(range.from), to: utcToLocalInput(range.to) });
  const [touched, setTouched] = useState(false);
  const ids = { from: useId(), to: useId() };
  useEffect(() => {
    setDraft({ from: utcToLocalInput(range.from), to: utcToLocalInput(range.to) });
  }, [range.from, range.to]);
  const value: TimeRange = { from: localInputToUtc(draft.from) ?? "", to: localInputToUtc(draft.to) ?? "" };
  const problem = check(value);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (problem === null) onSubmit(value);
  };
  const preset = (ms: number) => {
    const next = defaultRange(Date.now(), ms);
    setDraft({ from: utcToLocalInput(next.from), to: utcToLocalInput(next.to) });
  };
  const fieldError = (field: "from" | "to") =>
    touched && problem?.field === field ? (
      <p id={`${ids[field]}-error`} className="field-error">
        {problem.message}
      </p>
    ) : null;
  return (
    <form className="filter-bar" onSubmit={submit} aria-label={label} noValidate>
      <div className="field">
        <label htmlFor={ids.from}>Từ (giờ địa phương)</label>
        <input
          id={ids.from}
          type="datetime-local"
          value={draft.from}
          onChange={(event) => setDraft({ ...draft, from: event.target.value })}
          aria-invalid={touched && problem?.field === "from"}
          aria-describedby={touched && problem?.field === "from" ? `${ids.from}-error` : undefined}
        />
        {fieldError("from")}
      </div>
      <div className="field">
        <label htmlFor={ids.to}>Đến (giờ địa phương)</label>
        <input
          id={ids.to}
          type="datetime-local"
          value={draft.to}
          onChange={(event) => setDraft({ ...draft, to: event.target.value })}
          aria-invalid={touched && problem?.field === "to"}
          aria-describedby={touched && problem?.field === "to" ? `${ids.to}-error` : undefined}
        />
        {fieldError("to")}
      </div>
      <div className="field">
        <span className="label">Chọn nhanh</span>
        <div className="actions">
          {RANGE_PRESETS.map((item) => (
            <button key={item.ms} type="button" onClick={() => preset(item.ms)}>
              {item.label} gần nhất
            </button>
          ))}
        </div>
      </div>
      {children}
      <button type="submit" className="primary">
        {submitLabel}
      </button>
      {touched && problem !== null && problem.field !== "from" && problem.field !== "to" && (
        <p className="field-error" role="alert">
          {problem.message}
        </p>
      )}
    </form>
  );
}
