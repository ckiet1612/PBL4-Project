// Cancel/pause/resume/retry: one dialog, one Idempotency-Key per confirmed intent, If-Match of
// the newest job representation, at most one mutation per job in this tab. No automatic resend.
import { useCallback, useId, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router";
import type { ControlAction } from "../../api/endpoints";
import { isApiError } from "../../api/errors";
import { IntentTracker, withInProgressRetry } from "../../api/idempotency";
import { REASON_MAX_LENGTH } from "../../api/limits";
import type { CheckpointRecord, Job } from "../../api/types";
import { JOB_STATE_LABELS } from "../../app/labels";
import { useApi } from "../../auth/session";
import { Time } from "../../components/bits";
import { ConfirmDialog } from "../../components/Dialog";
import { ErrorPanel } from "../../components/ErrorPanel";
import { rememberRetry } from "./retryLinks";
import { jobEtag } from "./useJobDetail";

export type JobAction = ControlAction | "retry";

const COPY: Record<JobAction, { title: string; confirm: string; reason: string; body: string; notice: string }> = {
  cancel: {
    title: "Hủy job?",
    confirm: "Hủy job",
    reason: "Hủy từ Web UI",
    body: "Job đang chạy sẽ được yêu cầu dừng; kết quả của lần chạy này không được công nhận; không hoàn tác được.",
    notice: "Đã gửi yêu cầu hủy.",
  },
  pause: {
    title: "Tạm dừng job?",
    confirm: "Tạm dừng",
    reason: "Tạm dừng từ Web UI",
    body: "Job sẽ ghi checkpoint rồi dừng. Có thể tiếp tục sau từ checkpoint đó.",
    notice: "Đã gửi yêu cầu tạm dừng.",
  },
  resume: {
    title: "Tiếp tục job?",
    confirm: "Tiếp tục",
    reason: "Tiếp tục từ Web UI",
    body: "Job sẽ được xếp lịch chạy lại từ checkpoint đã ghi.",
    notice: "Đã gửi yêu cầu tiếp tục.",
  },
  retry: {
    title: "Chạy lại job?",
    confirm: "Chạy lại",
    reason: "Chạy lại từ Web UI",
    body: "Tạo một job mới với cùng cấu hình; job hiện tại không thay đổi.",
    notice: "Đã tạo job chạy lại.",
  },
};

interface ControlsInput {
  tenantId: string;
  /** null while the job is still loading: nothing can be opened then. */
  job: Job | null;
  etag: string;
  checkpoints: CheckpointRecord[];
  ownedByOther: boolean;
  /** Newest representation from a 202 or from the reload after 412. */
  onJob(job: Job, etag: string): void;
  refresh(): void;
}

export interface JobControls {
  open(action: JobAction): void;
  busy: boolean;
  notice: string | null;
  clearNotice(): void;
  dialog: ReactNode;
}

export function useJobControls(input: ControlsInput): JobControls {
  const api = useApi();
  const navigate = useNavigate();
  const reasonId = useId();
  const [action, setAction] = useState<JobAction | null>(null);
  const [reason, setReason] = useState("");
  const [checkpointId, setCheckpointId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const tracker = useRef(new IntentTracker());
  const pending = useRef<{ key: string; ifMatch: string } | null>(null);
  const inFlight = useRef(false);
  const inputRef = useRef(input);
  inputRef.current = input;

  const open = useCallback((next: JobAction) => {
    setAction(next);
    setReason(COPY[next].reason);
    setCheckpointId(null);
    setError(null);
  }, []);
  const close = useCallback(() => {
    if (!inFlight.current) setAction(null);
  }, []);

  const trimmed = reason.trim();
  const reasonError =
    trimmed.length === 0 ? "Nhập lý do" : trimmed.length > REASON_MAX_LENGTH ? `Tối đa ${REASON_MAX_LENGTH} ký tự` : null;

  const confirm = async () => {
    const { tenantId, job, etag, onJob, refresh } = inputRef.current;
    if (action === null || job === null || inFlight.current || reasonError) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    const body = action === "retry" ? { reason: trimmed, checkpoint_id: checkpointId } : { reason: trimmed };
    const key = tracker.current.keyFor(JSON.stringify({ job: job.job_id, action, body }));
    // A resend of the same intent keeps the If-Match it was first sent with.
    if (pending.current?.key !== key) pending.current = { key, ifMatch: etag };
    const options = { idempotencyKey: key, ifMatch: pending.current.ifMatch };
    try {
      const response = await withInProgressRetry(() =>
        action === "retry"
          ? api.retryJob(tenantId, job.job_id, { reason: trimmed, checkpoint_id: checkpointId }, options)
          : api.controlJob(tenantId, job.job_id, action, { reason: trimmed }, options),
      );
      tracker.current.settle();
      setAction(null);
      if (action === "retry") {
        rememberRetry(tenantId, job.job_id, response.data.job_id);
        navigate(`/t/${tenantId}/jobs/${response.data.job_id}`, { state: { flash: COPY.retry.notice } });
        return;
      }
      onJob(response.data, jobEtag(response.etag, response.data));
      setNotice(`${COPY[action].notice} Trạng thái hiện tại: ${JOB_STATE_LABELS[response.data.state]}.`);
      refresh();
    } catch (caught) {
      tracker.current.settle(caught);
      if (isApiError(caught, "version_conflict")) {
        setAction(null);
        try {
          const latest = await api.getJob(tenantId, job.job_id);
          onJob(latest.data, jobEtag(latest.etag, latest.data));
          setNotice(`Job vừa thay đổi (trạng thái mới: ${JOB_STATE_LABELS[latest.data.state]}). Kiểm tra rồi thử lại.`);
        } catch {
          setNotice("Job vừa thay đổi. Kiểm tra rồi thử lại.");
        }
        refresh();
      } else if (isApiError(caught, "state_conflict")) {
        setError(caught);
        refresh();
      } else {
        setError(caught);
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  const copy = action ? COPY[action] : null;
  const committed = input.checkpoints.filter((checkpoint) => checkpoint.state === "COMMITTED");
  const dialog = copy && (
    <ConfirmDialog
      open={action !== null}
      title={copy.title}
      confirmLabel={copy.confirm}
      danger={action === "cancel"}
      busy={busy}
      confirmDisabled={reasonError !== null}
      onConfirm={confirm}
      onCancel={close}
    >
      <p>{copy.body}</p>
      {input.ownedByOther && <p className="warning">Job này do người khác tạo.</p>}
      {action === "retry" && (
        <fieldset className="choice-list">
          <legend>Chạy lại từ</legend>
          <label>
            <input type="radio" name="retry-from" checked={checkpointId === null} onChange={() => setCheckpointId(null)} />
            Chạy lại từ đầu
          </label>
          {committed.map((checkpoint) => (
            <label key={checkpoint.checkpoint_id}>
              <input
                type="radio"
                name="retry-from"
                checked={checkpointId === checkpoint.checkpoint_id}
                onChange={() => setCheckpointId(checkpoint.checkpoint_id)}
              />
              Checkpoint #{checkpoint.sequence} (<Time iso={checkpoint.created_at} />)
            </label>
          ))}
          {committed.length > 0 && (
            <p className="muted">Máy chủ kiểm tra checkpoint có dùng được không; nếu không sẽ báo lỗi.</p>
          )}
        </fieldset>
      )}
      <div className="field">
        <label htmlFor={reasonId}>Lý do</label>
        <input
          id={reasonId}
          value={reason}
          maxLength={REASON_MAX_LENGTH + 1}
          onChange={(event) => setReason(event.target.value)}
          aria-invalid={reasonError !== null}
          aria-describedby={`${reasonId}-help`}
        />
        <p id={`${reasonId}-help`} className={reasonError ? "field-error" : "help"}>
          {reasonError ?? `1–${REASON_MAX_LENGTH} ký tự, ghi vào lịch sử sự kiện.`}
        </p>
      </div>
      {error !== null && <ErrorPanel error={error} />}
    </ConfirmDialog>
  );

  return { open, busy, notice, clearNotice: () => setNotice(null), dialog };
}
