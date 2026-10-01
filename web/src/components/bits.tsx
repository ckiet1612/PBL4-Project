// Small presentational pieces used on several pages. Visual style lives in CSS only.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";
import type { JobState } from "../api/types";
import { JOB_STATE_LABELS, STATE_TONE } from "../app/labels";
import { formatTime, shortId } from "./format";

export function StatusBadge({ state }: { state: JobState }) {
  return <span className={`badge tone-${STATE_TONE[state]}`}>{JOB_STATE_LABELS[state]}</span>;
}

export function Time({ iso }: { iso: string | null }) {
  if (!iso) return <span className="muted">—</span>;
  return (
    <time dateTime={iso} title={`${iso} (UTC)`}>
      {formatTime(iso)}
    </time>
  );
}

export function CopyButton({ value, label = "Sao chép" }: { value: string; label?: string }) {
  const [status, setStatus] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (timer.current !== null) clearTimeout(timer.current);
  }, []);
  const copy = () => {
    navigator.clipboard.writeText(value).then(
      () => setStatus("copied"),
      () => setStatus("failed"),
    );
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = setTimeout(() => setStatus("idle"), 3000);
  };
  return (
    <button type="button" className="button-link" onClick={copy} aria-label={label}>
      {status === "copied" ? "Đã sao chép" : status === "failed" ? "Không sao chép được" : "Sao chép"}
    </button>
  );
}

/** Rút gọn theo 8 ký tự cuối; ID đầy đủ nằm trong title và nút sao chép. */
export function ShortId({ id, to, copyLabel = "Sao chép ID" }: { id: string; to?: string; copyLabel?: string }) {
  const text = <code title={id}>{shortId(id)}</code>;
  return (
    <span className="short-id">
      {to ? <Link to={to}>{text}</Link> : text}
      <CopyButton value={id} label={copyLabel} />
    </span>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty-state">
      <p className="empty-title">{title}</p>
      {children}
    </div>
  );
}

export interface Crumb {
  label: string;
  to?: string;
}

export function Breadcrumb({ items }: { items: Crumb[] }) {
  return (
    <nav aria-label="Breadcrumb" className="breadcrumb">
      <ol>
        {items.map((item, index) => (
          <li key={`${item.label}-${index}`}>
            {item.to ? <Link to={item.to}>{item.label}</Link> : <span aria-current="page">{item.label}</span>}
          </li>
        ))}
      </ol>
    </nav>
  );
}

/** Short feedback right where the action happened; stays until replaced or dismissed. */
export function Notice({ children, onDismiss }: { children: ReactNode; onDismiss?(): void }) {
  return (
    <div className="notice" role="status">
      <span>{children}</span>
      {onDismiss && (
        <button type="button" className="button-link" onClick={onDismiss}>
          Đóng
        </button>
      )}
    </div>
  );
}
