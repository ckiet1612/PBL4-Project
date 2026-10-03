// /admin/workers/:workerId — health, inventory, capacity, held allocations; drain/disable/enable (A6).
// Reads worker + first HELD + first QUARANTINED page (3 audit rows). Auto-update only while a
// transition is pending (transitionPoll.ts); "done" texts only when the API data shows it.
import { useEffect, useId, useRef, useState } from "react";
import { useParams } from "react-router";
import { isApiError } from "../../../api/errors";
import { REASON_MAX_LENGTH } from "../../../api/limits";
import type { AllocationPage, Worker } from "../../../api/types";
import { useApi } from "../../../auth/session";
import { Breadcrumb, Notice, ShortId, Time } from "../../../components/bits";
import { ConfirmDialog } from "../../../components/Dialog";
import { formatBytes, formatCores } from "../../../components/format";
import {
  capacityRows,
  enableBlockedNote,
  summarizeAllocations,
  transitionStatus,
  WORKER_ACTION_CONSEQUENCES,
  WORKER_ACTION_LABELS,
  workerActions,
  type WorkerAction,
} from "../actions";
import { conflictMessage } from "../errors";
import { INTENT_BUSY, IntentSlot } from "../intent";
import { ALLOCATION_STATE_LABELS } from "../labels";
import { AdminErrorPanel, RefreshBar, TenantName, useAdminLoad } from "../shared";
import { startTransitionPoll } from "../transitionPoll";
import { AdminStateBadge, HealthBadge } from "./badges";
import { CapacityTable } from "./CapacityTable";

interface WorkerView {
  worker: Worker;
  etag: string;
  held: AllocationPage;
  quarantined: AllocationPage;
  /** False after an action until the next read: the pages above predate it. */
  allocationsAfterAction: boolean;
}

const ACTIONS: WorkerAction[] = ["drain", "disable", "enable"];

export function WorkerDetailPage() {
  const { workerId = "" } = useParams();
  const api = useApi().admin;
  const read = async (signal?: AbortSignal): Promise<WorkerView> => {
    const [worker, held, quarantined] = await Promise.all([
      api.getWorker(workerId, signal),
      api.listAllocations("HELD", signal),
      api.listAllocations("QUARANTINED", signal),
    ]);
    return {
      worker: worker.data,
      etag: worker.etag ?? `"v${worker.data.version}"`,
      held: held.data,
      quarantined: quarantined.data,
      allocationsAfterAction: true,
    };
  };
  const view = useAdminLoad(read, [workerId]);
  const readRef = useRef(read);
  readRef.current = read;

  const [lastAction, setLastAction] = useState<WorkerAction | null>(null);
  const [action, setAction] = useState<WorkerAction | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<unknown>(null);
  const [actionConflict, setActionConflict] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);
  const [pollError, setPollError] = useState<unknown>(null);
  const [pollRound, setPollRound] = useState(0);
  // Page-level slot: cancel and reopen the same action after a timeout restores the reason, so
  // confirming resends the same Idempotency-Key (B18-RV10).
  const slot = useRef(new IntentSlot<{ action: WorkerAction; reason: string }>());
  const reasonId = useId();

  const data = view.data;
  const held = data ? summarizeAllocations(data.held, workerId) : null;
  const quarantined = data ? summarizeAllocations(data.quarantined, workerId) : null;
  const status =
    data && held && quarantined
      ? transitionStatus(data.worker, held, quarantined, lastAction, data.allocationsAfterAction)
      : null;
  const waiting = status?.phase === "waiting";

  useEffect(() => {
    if (!waiting || expired) return;
    setPollError(null);
    const handle = startTransitionPoll({
      async run(signal) {
        const next = await readRef.current(signal);
        view.set(next);
        setPollError(null);
        const nextStatus = transitionStatus(
          next.worker,
          summarizeAllocations(next.held, workerId),
          summarizeAllocations(next.quarantined, workerId),
          lastAction,
        );
        return nextStatus.phase === "waiting" ? "unchanged" : "done";
      },
      onExpire: () => setExpired(true),
      onError: setPollError,
    });
    return () => handle.stop();
    // view.set is a stable state update; restart only when waiting/expiry/round changes.
  }, [waiting, expired, pollRound, workerId, lastAction]);

  const refresh = () => {
    setExpired(false);
    setPollRound((n) => n + 1);
    view.refresh();
  };

  const trimmed = reason.trim();
  const reasonError =
    trimmed.length === 0 ? "Nhập lý do" : trimmed.length > REASON_MAX_LENGTH ? `Tối đa ${REASON_MAX_LENGTH} ký tự` : null;

  const open = (next: WorkerAction) => {
    const reopened = slot.current.reopen;
    setAction(next);
    setReason(reopened?.action === next ? reopened.reason : "");
    setActionError(null);
    setActionConflict(null);
  };
  const close = () => {
    if (!busy) setAction(null);
  };

  const confirm = async () => {
    if (action === null || data === null || reasonError) return;
    setBusy(true);
    setActionError(null);
    setActionConflict(null);
    slot.current.sent = { action, reason };
    try {
      const result = await slot.current.intent.send(
        JSON.stringify({ workerId, action, reason: trimmed }),
        data.etag,
        (options) => api.workerAction(workerId, action, { reason: trimmed }, { ...options, ifMatch: options.ifMatch ?? "" }),
      );
      if (result === INTENT_BUSY) return;
      setAction(null);
      setLastAction(action);
      setExpired(false);
      setPollRound((n) => n + 1);
      view.set({
        ...data,
        worker: result.data,
        etag: result.etag ?? `"v${result.data.version}"`,
        allocationsAfterAction: false,
      });
      setNotice(`Đã gửi yêu cầu "${WORKER_ACTION_LABELS[action]}". Trạng thái được cập nhật theo dữ liệu máy chủ.`);
    } catch (caught) {
      if (isApiError(caught, "version_conflict")) {
        // 412: re-read, explain and keep the dialog with its reason (B18-RV05) while the action
        // still applies to the new state; the next confirm is a new intent with the new ETag.
        try {
          const latest = await readRef.current();
          view.set(latest);
          const message = conflictMessage(data.worker.version, latest.worker.version);
          if (workerActions(latest.worker.admin_state)[action]) {
            setActionConflict(message);
          } else {
            // The action no longer applies (its button is gone): close, explain on the page.
            setAction(null);
            setNotice(message);
          }
        } catch {
          setActionConflict("Worker vừa được thay đổi. Bấm Làm mới, kiểm tra rồi gửi lại.");
        }
      } else {
        setActionError(caught);
      }
    } finally {
      setBusy(false);
    }
  };

  const visible = data ? workerActions(data.worker.admin_state) : null;
  const blocked = data && quarantined ? enableBlockedNote(data.worker.admin_state, quarantined) : null;
  const inventory = data?.worker.inventory ?? null;
  const allocations = data
    ? [...data.held.items, ...data.quarantined.items].filter((item) => item.worker_id === workerId)
    : [];

  return (
    <section className="page">
      <Breadcrumb items={[{ label: "Worker", to: "/admin/workers" }, { label: workerId.slice(-8) }]} />
      <div className="page-header">
        <h1>
          Worker <ShortId id={workerId} copyLabel="Sao chép worker ID" />
        </h1>
        <RefreshBar
          loads={[{ ...view, refresh }]}
          status={
            waiting && !expired ? (
              <span className="muted">Đang tự cập nhật</span>
            ) : expired ? (
              <span className="warning">Đã dừng tự cập nhật, bấm Làm mới</span>
            ) : null
          }
        />
      </div>
      {view.error !== null && (
        <AdminErrorPanel
          error={view.error}
          onRetry={refresh}
          title={isApiError(view.error, "resource_not_found") ? "Không tìm thấy worker" : undefined}
        />
      )}
      {view.loading && data === null && <p className="status-line">Đang tải…</p>}
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}
      {data && status && (
        <>
          <dl className="key-values">
            <dt>Sức khỏe</dt>
            <dd>
              <HealthBadge health={data.worker.health} />
            </dd>
            <dt>Trạng thái quản trị</dt>
            <dd>
              <AdminStateBadge state={data.worker.admin_state} />
            </dd>
            <dt>Heartbeat gần nhất</dt>
            <dd>
              <Time iso={data.worker.last_heartbeat_at} />
            </dd>
            <dt>Sẵn sàng từ</dt>
            <dd>
              <Time iso={data.worker.ready_at} />
            </dd>
            <dt>Incarnation</dt>
            <dd>
              {data.worker.current_incarnation_id ? (
                <ShortId id={data.worker.current_incarnation_id} copyLabel="Sao chép incarnation ID" />
              ) : (
                <span className="muted">—</span>
              )}
            </dd>
            <dt>Phiên bản</dt>
            <dd>v{data.worker.version}</dd>
          </dl>

          {status.text && (
            <p className={status.phase === "waiting" ? "pending-note" : "status-line"} role="status">
              {status.text}
            </p>
          )}
          {pollError !== null && <AdminErrorPanel error={pollError} onRetry={refresh} />}

          <section className="form-group" aria-labelledby="worker-actions-title">
            <h2 id="worker-actions-title">Thao tác</h2>
            {blocked && <p className="warning">{blocked}</p>}
            <div className="actions">
              {ACTIONS.filter((item) => visible?.[item]).map((item) => (
                <button
                  key={item}
                  type="button"
                  className={item === "disable" ? "danger" : item === "enable" ? "primary" : undefined}
                  onClick={() => open(item)}
                  disabled={busy}
                >
                  {WORKER_ACTION_LABELS[item]}
                </button>
              ))}
            </div>
            <p className="muted">Máy chủ quyết định thao tác có hợp lệ không; lý do từ chối được hiện tại đây.</p>
          </section>

          <CapacityTable
            caption="Sức chứa"
            rows={capacityRows(inventory?.allocatable ?? null, held!, quarantined!)}
          />

          <section aria-labelledby="allocations-title" className="stack-tight">
            <h2 id="allocations-title">Phân bổ chưa trả</h2>
            {allocations.length === 0 ? (
              <p className="muted">Không có phân bổ HELD hoặc QUARANTINED trên worker này.</p>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Job</th>
                      <th scope="col">Trạng thái</th>
                      <th scope="col">Tenant</th>
                      <th scope="col">Tài nguyên</th>
                      <th scope="col" className="col-optional">
                        Giữ từ
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {allocations.map((allocation) => (
                      <tr key={allocation.allocation_id}>
                        <td>
                          <ShortId id={allocation.job_id} to={`/admin/jobs/${allocation.job_id}`} copyLabel="Sao chép job ID" />
                        </td>
                        <td>{ALLOCATION_STATE_LABELS[allocation.state]}</td>
                        <td>
                          <TenantName id={allocation.tenant_id} tenants={null} />
                        </td>
                        <td>
                          {formatCores(allocation.resources.cpu_millis)} · {formatBytes(allocation.resources.memory_bytes)}
                          {allocation.resources.gpu_count > 0 ? ` · ${allocation.resources.gpu_count} GPU` : ""}
                        </td>
                        <td className="col-optional">
                          <Time iso={allocation.held_at} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section aria-labelledby="inventory-title" className="stack-tight">
            <h2 id="inventory-title">Inventory</h2>
            {inventory === null ? (
              <p className="muted">Worker chưa gửi inventory.</p>
            ) : (
              <dl className="key-values">
                <dt>Kiến trúc</dt>
                <dd>{inventory.architecture}</dd>
                <dt>CPU / RAM của máy</dt>
                <dd>
                  {formatCores(inventory.host_cpu_millis)} · {formatBytes(inventory.host_memory_bytes)}
                </dd>
                <dt>Runtime</dt>
                <dd>
                  Docker {inventory.runtime.docker_version} · {inventory.runtime.oci_runtime}{" "}
                  {inventory.runtime.oci_runtime_version} · kernel {inventory.runtime.kernel_release} · seccomp{" "}
                  {inventory.runtime.seccomp_available ? "có" : "không"}
                </dd>
                <dt>Adapter</dt>
                <dd>
                  {inventory.adapters.map((item) => `${item.adapter_id} ${item.adapter_version}`).join(", ") || "—"}
                </dd>
                <dt>Framework</dt>
                <dd>
                  {inventory.frameworks
                    .map((item) => `${item.framework} ${item.framework_version} (${item.device})`)
                    .join(", ") || "—"}
                </dd>
                <dt>Image</dt>
                <dd>
                  {inventory.images.length === 0
                    ? "—"
                    : inventory.images.map((item) => (
                        <div key={item.image_digest}>
                          <code title={item.image_digest}>{item.image_digest.slice(0, 19)}…</code> {item.architecture}
                          {item.verified ? "" : " (chưa xác minh)"}
                        </div>
                      ))}
                </dd>
                <dt>GPU</dt>
                <dd>
                  {inventory.gpu_devices.length === 0
                    ? "Không có"
                    : inventory.gpu_devices
                        .map((gpu) => `${gpu.model} (${formatBytes(gpu.memory_bytes)})${gpu.healthy ? "" : " — không khỏe"}`)
                        .join(", ")}
                </dd>
                <dt>Phát hiện lúc</dt>
                <dd>
                  <Time iso={inventory.discovered_at} />
                </dd>
              </dl>
            )}
          </section>
        </>
      )}

      {action && (
        <ConfirmDialog
          open
          title={`${WORKER_ACTION_LABELS[action]}?`}
          confirmLabel={WORKER_ACTION_LABELS[action]}
          danger={action === "disable"}
          busy={busy}
          confirmDisabled={reasonError !== null}
          onConfirm={confirm}
          onCancel={close}
        >
          <p>{WORKER_ACTION_CONSEQUENCES[action]}</p>
          {action === "enable" && blocked && <p className="warning">{blocked}</p>}
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
            <p id={`${reasonId}-help`} className={reasonError && reason !== "" ? "field-error" : "help"}>
              {reasonError && reason !== "" ? reasonError : `Bắt buộc, 1–${REASON_MAX_LENGTH} ký tự, ghi vào audit.`}
            </p>
          </div>
          {actionConflict !== null && <Notice onDismiss={() => setActionConflict(null)}>{actionConflict}</Notice>}
          {actionError !== null && <AdminErrorPanel error={actionError} />}
        </ConfirmDialog>
      )}
    </section>
  );
}
