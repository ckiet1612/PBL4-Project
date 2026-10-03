// Every API error on screen goes through describeError: Vietnamese text, request_id, no stack.
import { describeError, type ErrorView } from "../api/errors";
import { CopyButton } from "./bits";

interface ErrorPanelProps {
  error: unknown;
  /** Read actions only: re-run the same read. */
  onRetry?(): void;
  /** Replaces the mapped title where the page knows better (e.g. permission on a list). */
  title?: string;
  /** Area-specific mapping on top of describeError (admin: adminErrorView). */
  describe?(error: unknown): ErrorView;
}

export function ErrorPanel({ error, onRetry, title, describe = describeError }: ErrorPanelProps) {
  const view = describe(error);
  return (
    <div className="error-panel" role="alert">
      <p className="error-title">{title ?? view.title}</p>
      {view.hint && <p>{view.hint}</p>}
      {view.detail && <p className="muted">Chi tiết từ máy chủ: {view.detail}</p>}
      {view.retryAfterSeconds !== null && <p>Thử lại sau khoảng {view.retryAfterSeconds} giây.</p>}
      {view.requestId && (
        <p className="muted">
          Mã yêu cầu: <code>{view.requestId}</code> <CopyButton value={view.requestId} label="Sao chép mã yêu cầu" />
        </p>
      )}
      {onRetry && (
        <button type="button" onClick={onRetry}>
          Thử lại
        </button>
      )}
    </div>
  );
}
