// /t/:tenantId/jobs/:jobId — header → state → primary actions → summary → tabs → metadata → danger zone.
import { useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router";
import { isApiError } from "../../api/errors";
import type { Attempt, CheckpointRecord, Job, JobEvent, ProgressRecord } from "../../api/types";
import {
  ACTOR_LABELS,
  ATTEMPT_STATE_LABELS,
  CHECKPOINT_STATE_LABELS,
  FAILURE_CLASS_LABELS,
  waitingReasonLabel,
} from "../../app/labels";
import { useTenant } from "../../auth/guards";
import { useApi, useSession } from "../../auth/session";
import { Breadcrumb, CopyButton, EmptyState, Notice, ShortId, StatusBadge, Time } from "../../components/bits";
import { DownloadButton } from "../../components/DownloadButton";
import { ErrorPanel } from "../../components/ErrorPanel";
import { formatBytes, formatCores, shortChecksum, shortId } from "../../components/format";
import { Tabs } from "../../components/Tabs";
import { usePolled } from "../../components/usePolled";
import { availableActions, canControl, DETAIL_TABS, defaultTab, pendingNote, type DetailTab } from "./actions";
import { isManifestError, isResultPending, useJobDetail, type JobDetail, type Section } from "./useJobDetail";
import { retriesOf } from "./retryLinks";
import { useJobControls } from "./useJobControls";

const TAB_LABELS: Record<DetailTab, string> = {
  progress: "Tiến trình",
  result: "Kết quả",
  events: "Sự kiện",
  logs: "Log",
  config: "Cấu hình",
};

export function JobDetailPage() {
  const { tenantId, role } = useTenant();
  const { jobId = "" } = useParams();
  const session = useSession();
  const api = useApi();
  const location = useLocation();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const detail = useJobDetail(api, tenantId, jobId);
  const templates = usePolled({ load: async (signal) => (await api.listTemplates(tenantId, signal)).data, schedule: null }, [
    tenantId,
  ]);
  // A 202 or a reload after 412 may be newer than the last poll; the newer version wins.
  const [override, setOverride] = useState<{ jobId: string; job: Job; etag: string } | null>(null);
  const flash = (location.state as { flash?: string } | null)?.flash ?? null;

  const polledJob = detail.data?.job ?? null;
  const useOverride =
    override !== null && override.jobId === jobId && (polledJob === null || override.job.version > polledJob.version);
  const job = useOverride ? override.job : polledJob;
  const etag = useOverride ? override.etag : (detail.data?.etag ?? "");

  const controls = useJobControls({
    tenantId,
    job,
    etag,
    checkpoints: detail.data?.checkpoints.value ?? [],
    ownedByOther: job !== null && job.user_id !== session.user_id,
    onJob: (next, nextEtag) => setOverride({ jobId, job: next, etag: nextEtag }),
    refresh: detail.refresh,
  });

  const crumbs = [{ label: "Jobs", to: `/t/${tenantId}/jobs` }, { label: `Job ${shortId(jobId)}` }];

  if (job === null || detail.data === null) {
    return (
      <section className="page">
        <Breadcrumb items={crumbs} />
        {detail.error !== null ? (
          <ErrorPanel
            error={detail.error}
            onRetry={detail.refresh}
            title={
              isApiError(detail.error, "resource_not_found", "permission_denied")
                ? "Không tìm thấy job hoặc bạn không có quyền xem"
                : undefined
            }
          />
        ) : (
          <p>Đang tải…</p>
        )}
      </section>
    );
  }

  const data = detail.data;
  const template = templates.data?.find(
    (t) => t.template_id === job.spec.template_id && t.version === job.spec.template_version,
  );
  // Unknown checkpointability (templates not loaded or template disabled): let the server decide.
  const actions = availableActions(job, template?.checkpointable ?? true);
  const allowed = canControl(job, session, role);
  const rawTab = params.get("tab");
  const tab = rawTab && (DETAIL_TABS as string[]).includes(rawTab) ? (rawTab as DetailTab) : defaultTab(job.state);
  const note = pendingNote(job);
  const waiting = waitingReasonLabel(job.waiting_reason);
  const anyPrimary = actions.pause !== "hidden" || actions.resume || actions.retry;
  const selectTab = (next: DetailTab) => {
    const nextParams = new URLSearchParams(params);
    nextParams.set("tab", next);
    setParams(nextParams, { replace: true });
  };

  return (
    <section className="page">
      <Breadcrumb items={crumbs} />
      <div className="page-header">
        <div>
          <h1>{template?.display_name ?? job.spec.template_id}</h1>
          <p className="muted">
            Job <ShortId id={job.job_id} copyLabel="Sao chép job ID" />
          </p>
        </div>
        <div className="status-block">
          <StatusBadge state={job.state} />
          {note && <p className="pending-note">{note}</p>}
          {waiting && <p className="muted">{waiting}</p>}
        </div>
      </div>

      {flash && (
        <Notice onDismiss={() => navigate(`${location.pathname}${location.search}`, { replace: true, state: null })}>
          {flash}
        </Notice>
      )}
      {controls.notice && <Notice onDismiss={controls.clearNotice}>{controls.notice}</Notice>}

      {allowed ? (
        anyPrimary && (
          <div className="actions primary-actions">
            {actions.pause === "enabled" && (
              <button type="button" onClick={() => controls.open("pause")} disabled={controls.busy}>
                Tạm dừng
              </button>
            )}
            {actions.pause === "disabled" && (
              <span className="disabled-action">
                <button type="button" disabled aria-describedby="pause-reason">
                  Tạm dừng
                </button>
                <span id="pause-reason" className="muted">
                  Template này không hỗ trợ tạm dừng
                </span>
              </span>
            )}
            {actions.resume && (
              <button type="button" className="primary" onClick={() => controls.open("resume")} disabled={controls.busy}>
                Tiếp tục
              </button>
            )}
            {actions.retry && (
              <button type="button" className="primary" onClick={() => controls.open("retry")} disabled={controls.busy}>
                Chạy lại
              </button>
            )}
          </div>
        )
      ) : (
        <p className="muted">Chỉ người tạo job hoặc quản trị viên tenant được điều khiển job này</p>
      )}

      <Summary job={job} progress={data.progress} tenantId={tenantId} ownUserId={session.user_id} />

      <Tabs label="Chi tiết job" tabs={DETAIL_TABS.map((id) => ({ id, label: TAB_LABELS[id] }))} active={tab} onSelect={selectTab}>
        {tab === "progress" && <ProgressTab detail={data} />}
        {tab === "result" && <ResultTab job={job} result={data.result} tenantId={tenantId} onRetry={detail.refresh} />}
        {tab === "events" && <EventsTab detail={data} onMore={detail.refresh} />}
        {tab === "logs" && (
          <EmptyState title="Hệ thống chưa hỗ trợ xem log của job">
            <p className="muted">Theo dõi tiến trình ở tab Tiến trình và Sự kiện.</p>
          </EmptyState>
        )}
        {tab === "config" && <ConfigTab job={job} tenantId={tenantId} />}
      </Tabs>

      <details className="metadata">
        <summary>Metadata kỹ thuật</summary>
        <dl className="key-values">
          <dt>Job ID</dt>
          <dd>
            <code>{job.job_id}</code> <CopyButton value={job.job_id} label="Sao chép job ID" />
          </dd>
          <dt>Session ID</dt>
          <dd>
            <code>{job.session_id}</code>
          </dd>
          <dt>Phiên bản / ETag</dt>
          <dd>
            {job.version} / <code>{etag}</code>
          </dd>
          <dt>Job fence</dt>
          <dd>{job.job_fence}</dd>
          <dt>Sự kiện mới nhất</dt>
          <dd>#{job.event_sequence}</dd>
        </dl>
      </details>

      {allowed && actions.cancel && (
        <section className="danger-zone" aria-labelledby="danger-zone-title">
          <h2 id="danger-zone-title">Hành động nguy hiểm</h2>
          <p>Hủy job không hoàn tác được.</p>
          <button type="button" className="danger" onClick={() => controls.open("cancel")} disabled={controls.busy}>
            Hủy job
          </button>
        </section>
      )}
      {controls.dialog}
    </section>
  );
}

function Summary({
  job,
  progress,
  tenantId,
  ownUserId,
}: {
  job: Job;
  progress: Section<ProgressRecord>;
  tenantId: string;
  ownUserId: string;
}) {
  const spec = job.spec;
  return (
    <section className="summary" aria-label="Tóm tắt">
      <dl className="key-values">
        <dt>Tạo lúc</dt>
        <dd>
          <Time iso={job.created_at} />
        </dd>
        <dt>Cập nhật lúc</dt>
        <dd>
          <Time iso={job.updated_at} />
        </dd>
        <dt>Người tạo</dt>
        <dd>{job.user_id === ownUserId ? "Bạn" : <code title={job.user_id}>{shortId(job.user_id)}</code>}</dd>
        <dt>Template</dt>
        <dd>
          <code>
            {spec.template_id}@{spec.template_version}
          </code>
        </dd>
        <dt>Tài nguyên</dt>
        <dd>
          {formatCores(spec.resources.cpu_millis)}, {formatBytes(spec.resources.memory_bytes)} RAM
        </dd>
        <dt>Giới hạn thời gian</dt>
        <dd>{spec.runtime_limit_seconds} giây</dd>
        <dt>Số lần thử lại tự động</dt>
        <dd>
          {job.retry_count}/{job.max_retries}
        </dd>
        {job.retry_of_job_id && (
          <>
            <dt>Chạy lại của</dt>
            <dd>
              <ShortId id={job.retry_of_job_id} to={`/t/${tenantId}/jobs/${job.retry_of_job_id}`} copyLabel="Sao chép job ID gốc" />
            </dd>
          </>
        )}
        {retriesOf(tenantId, job.job_id).length > 0 && (
          <>
            <dt>Đã chạy lại thành</dt>
            <dd>
              <span className="inline-actions">
                {retriesOf(tenantId, job.job_id).map((retryId) => (
                  <ShortId key={retryId} id={retryId} to={`/t/${tenantId}/jobs/${retryId}`} copyLabel="Sao chép job ID mới" />
                ))}
              </span>
              <span className="help">Chỉ hiện các lần chạy lại tạo trong phiên trình duyệt này.</span>
            </dd>
          </>
        )}
        <dt>Tiến độ</dt>
        <dd>
          <ProgressLine progress={progress} />
        </dd>
      </dl>
    </section>
  );
}

function ProgressLine({ progress }: { progress: Section<ProgressRecord> }) {
  const value = progress.value;
  if (value === null) {
    return progress.error !== null ? <span className="field-error">Không đọc được tiến độ</span> : <span>Đang tải…</span>;
  }
  if (!value.available) return <span className="muted">Chưa có dữ liệu tiến độ</span>;
  const percent = Math.round(value.snapshot.fraction * 1000) / 10;
  return (
    <span className="progress-line">
      <progress max={1} value={value.snapshot.fraction} aria-label="Tiến độ" /> {percent}%
    </span>
  );
}

function attemptNumber(attempts: Attempt[] | null, attemptId: string): string {
  const attempt = attempts?.find((a) => a.attempt_id === attemptId);
  return attempt ? `#${attempt.attempt_number}` : shortId(attemptId);
}

function ProgressTab({ detail }: { detail: JobDetail }) {
  const progress = detail.progress.value;
  const attempts = detail.attempts.value;
  const checkpoints = detail.checkpoints.value;
  const restoreId = progress?.available ? progress.restore_checkpoint_id : null;
  return (
    <div className="stack">
      <section aria-labelledby="progress-title">
        <h2 id="progress-title">Tiến độ</h2>
        {detail.progress.error !== null && <ErrorPanel error={detail.progress.error} title="Không đọc được tiến độ" />}
        {progress && !progress.available && <p className="muted">Chưa có dữ liệu tiến độ</p>}
        {progress?.available && (
          <dl className="key-values">
            <dt>Hoàn thành</dt>
            <dd>
              <ProgressLine progress={detail.progress} />
            </dd>
            {progress.snapshot.step !== null && (
              <>
                <dt>Bước</dt>
                <dd>{progress.snapshot.step}</dd>
              </>
            )}
            {progress.snapshot.epoch !== null && (
              <>
                <dt>Epoch</dt>
                <dd>{progress.snapshot.epoch}</dd>
              </>
            )}
            {progress.snapshot.item_cursor !== null && (
              <>
                <dt>Vị trí xử lý</dt>
                <dd>{progress.snapshot.item_cursor}</dd>
              </>
            )}
            <dt>Báo cáo lúc</dt>
            <dd>
              <Time iso={progress.reported_at} /> · bởi lần chạy {attemptNumber(attempts, progress.attempt_id)}
            </dd>
          </dl>
        )}
      </section>

      <section aria-labelledby="attempts-title">
        <h2 id="attempts-title">Lần chạy</h2>
        {detail.attempts.error !== null && <ErrorPanel error={detail.attempts.error} title="Không đọc được lần chạy" />}
        {attempts && attempts.length === 0 && <p className="muted">Chưa có lần chạy nào</p>}
        {attempts && attempts.length > 0 && <AttemptsTable attempts={attempts} />}
      </section>

      <section aria-labelledby="checkpoints-title">
        <h2 id="checkpoints-title">Checkpoint</h2>
        {detail.checkpoints.error !== null && (
          <ErrorPanel error={detail.checkpoints.error} title="Không đọc được checkpoint" />
        )}
        {checkpoints && checkpoints.length === 0 && <p className="muted">Chưa có checkpoint nào</p>}
        {checkpoints && checkpoints.length > 0 && (
          <CheckpointsTable checkpoints={checkpoints} attempts={attempts} restoreId={restoreId} />
        )}
      </section>
    </div>
  );
}

function AttemptsTable({ attempts }: { attempts: Attempt[] }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">Lần</th>
            <th scope="col">Trạng thái</th>
            <th scope="col">Bắt đầu</th>
            <th scope="col">Kết thúc</th>
            <th scope="col">Lỗi</th>
            <th scope="col" className="col-optional">
              ID kỹ thuật
            </th>
          </tr>
        </thead>
        <tbody>
          {attempts.map((attempt) => (
            <tr key={attempt.attempt_id}>
              <td>#{attempt.attempt_number}</td>
              <td>{ATTEMPT_STATE_LABELS[attempt.state]}</td>
              <td>
                <Time iso={attempt.started_at} />
              </td>
              <td>
                <Time iso={attempt.ended_at} />
              </td>
              <td>{attempt.failure_class ? FAILURE_CLASS_LABELS[attempt.failure_class] : "—"}</td>
              <td className="col-optional">
                <details>
                  <summary>Xem</summary>
                  <dl className="key-values compact">
                    <dt>Attempt</dt>
                    <dd>
                      <code>{attempt.attempt_id}</code>
                    </dd>
                    <dt>Worker</dt>
                    <dd>
                      <code>{attempt.worker_id ?? "—"}</code>
                    </dd>
                    <dt>Fence</dt>
                    <dd>{attempt.job_fence}</dd>
                  </dl>
                </details>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CheckpointsTable({
  checkpoints,
  attempts,
  restoreId,
}: {
  checkpoints: CheckpointRecord[];
  attempts: Attempt[] | null;
  restoreId: string | null;
}) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">Số</th>
            <th scope="col">Trạng thái</th>
            <th scope="col">Thời điểm</th>
            <th scope="col">Lần chạy</th>
          </tr>
        </thead>
        <tbody>
          {checkpoints.map((checkpoint) => (
            <tr key={checkpoint.checkpoint_id}>
              <td>
                #{checkpoint.sequence}
                {checkpoint.checkpoint_id === restoreId && <span className="tag">Đang dùng để khôi phục</span>}
              </td>
              <td>{CHECKPOINT_STATE_LABELS[checkpoint.state]}</td>
              <td>
                <Time iso={checkpoint.created_at} />
              </td>
              <td>{attemptNumber(attempts, checkpoint.attempt_id)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ResultTab({
  job,
  result,
  tenantId,
  onRetry,
}: {
  job: Job;
  result: JobDetail["result"];
  tenantId: string;
  onRetry(): void;
}) {
  if (job.state === "CANCELLED") return <EmptyState title="Job đã hủy nên không có kết quả" />;
  if (job.state === "FAILED") return <EmptyState title="Job thất bại; xem Sự kiện và Lần chạy" />;
  if (job.state !== "SUCCEEDED") return <EmptyState title="Job chưa hoàn tất" />;
  if (result.error !== null && result.value === null) {
    if (isResultPending(result.error)) return <EmptyState title="Kết quả đang được ghi nhận, thử lại sau ít giây" />;
    if (isManifestError(result.error)) {
      return (
        <div className="error-panel" role="alert">
          <p className="error-title">Không đọc được danh sách tệp kết quả</p>
          <p>Manifest kết quả không khớp checksum hoặc sai định dạng. Thử lại; nếu vẫn lỗi, báo quản trị viên.</p>
          <button type="button" onClick={onRetry}>
            Thử lại
          </button>
        </div>
      );
    }
    return <ErrorPanel error={result.error} onRetry={onRetry} title="Không đọc được kết quả" />;
  }
  const view = result.value;
  if (view === null) return <p>Đang tải…</p>;
  return (
    <div className="stack">
      <section aria-labelledby="files-title">
        <h2 id="files-title">Tệp kết quả</h2>
        {view.files.length === 0 ? (
          <p className="muted">Kết quả không có tệp nào</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Tên</th>
                  <th scope="col" className="col-optional">
                    Media type
                  </th>
                  <th scope="col">Kích thước</th>
                  <th scope="col" className="col-optional">
                    Checksum
                  </th>
                  <th scope="col">
                    <span className="visually-hidden">Tải xuống</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {view.files.map((file) => (
                  <tr key={file.artifact_id}>
                    <td>{file.logical_name}</td>
                    <td className="col-optional">
                      <code>{file.media_type}</code>
                    </td>
                    <td>{formatBytes(file.size_bytes)}</td>
                    <td className="col-optional">
                      <code title={file.checksum}>{shortChecksum(file.checksum)}</code>
                    </td>
                    <td>
                      <DownloadButton
                        tenantId={tenantId}
                        target={{
                          artifactId: file.artifact_id,
                          checksum: file.checksum,
                          sizeBytes: file.size_bytes,
                          mediaType: file.media_type,
                          logicalName: file.logical_name,
                        }}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section aria-labelledby="metrics-title">
        <h2 id="metrics-title">Chỉ số</h2>
        {view.metrics.length === 0 ? (
          <p className="muted">Không có chỉ số</p>
        ) : (
          <dl className="key-values">
            {view.metrics.map(([key, value]) => (
              <div key={key} className="key-value-row">
                <dt>{key}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>
      <p className="muted">
        Kết quả do lần chạy {shortId(view.record.attempt_id)} tạo lúc <Time iso={view.record.created_at} />.
      </p>
    </div>
  );
}

function EventsTab({ detail, onMore }: { detail: JobDetail; onMore(): void }) {
  const events: JobEvent[] = detail.events;
  return (
    <div className="stack">
      {detail.eventsError !== null && (
        <ErrorPanel error={detail.eventsError} onRetry={onMore} title="Không đọc được sự kiện" />
      )}
      {events.length === 0 ? (
        <p className="muted">Chưa có sự kiện</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">Loại</th>
                <th scope="col">Lý do</th>
                <th scope="col" className="col-optional">
                  Tác nhân
                </th>
                <th scope="col">Thời điểm</th>
              </tr>
            </thead>
            <tbody>
              {events.map((event) => (
                <tr key={event.event_id}>
                  <td>{event.sequence}</td>
                  <td>
                    <code>{event.type}</code>
                  </td>
                  <td className="wrap">{event.reason}</td>
                  <td className="col-optional">{ACTOR_LABELS[event.actor_type]}</td>
                  <td>
                    <Time iso={event.created_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {detail.moreEvents && (
        <button type="button" onClick={onMore}>
          Tải thêm sự kiện
        </button>
      )}
    </div>
  );
}

function ConfigTab({ job, tenantId }: { job: Job; tenantId: string }) {
  const spec = job.spec;
  const dataLink = (artifactId: string, label: string) => (
    <span className="inline-actions">
      <ShortId id={artifactId} copyLabel={`Sao chép ID ${label}`} />
      <DownloadButton tenantId={tenantId} target={{ artifactId }} label={`Tải ${label}`} />
    </span>
  );
  return (
    <div className="stack">
      <dl className="key-values">
        <dt>Dữ liệu vào</dt>
        <dd>{dataLink(spec.input_artifact_id, "dữ liệu vào")}</dd>
        {spec.template_id === "batch-inference" && (
          <>
            <dt>Model</dt>
            <dd>{dataLink(spec.model_artifact_id, "model")}</dd>
          </>
        )}
        <dt>Priority</dt>
        <dd>{spec.priority}</dd>
        <dt>Chu kỳ checkpoint</dt>
        <dd>{spec.checkpoint_interval_seconds} giây</dd>
        <dt>Giới hạn thời gian</dt>
        <dd>{spec.runtime_limit_seconds} giây</dd>
        <dt>Spec checksum</dt>
        <dd>
          <code title={job.spec_checksum}>{shortChecksum(job.spec_checksum)}</code>{" "}
          <CopyButton value={job.spec_checksum} label="Sao chép spec checksum" />
        </dd>
      </dl>
      <h2>Tham số</h2>
      <dl className="key-values">
        {Object.entries(spec.parameters).map(([key, value]) => (
          <div key={key} className="key-value-row">
            <dt>
              <code>{key}</code>
            </dt>
            <dd>{String(value)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
