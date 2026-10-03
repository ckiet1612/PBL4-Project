// /admin/jobs/:jobId — the Job object only, no controls (UX-A24). One read per open.
import { Link, useParams } from "react-router";
import { isApiError } from "../../../api/errors";
import { waitingReasonLabel } from "../../../app/labels";
import { useApi, useSession } from "../../../auth/session";
import { Breadcrumb, ShortId, StatusBadge, Time } from "../../../components/bits";
import { formatBytes, formatCores, shortId } from "../../../components/format";
import { AdminErrorPanel, RefreshBar, useAdminLoad } from "../shared";

export function AdminJobPage() {
  const { jobId = "" } = useParams();
  const api = useApi().admin;
  const session = useSession();
  const job = useAdminLoad(async (signal) => (await api.getJob(jobId, signal)).data, [jobId]);
  const data = job.data;
  const member = data !== null && session.memberships.some((item) => item.tenant_id === data.tenant_id);
  const notFound = isApiError(job.error, "resource_not_found");

  return (
    <section className="page">
      <Breadcrumb items={[{ label: "Hàng chờ", to: "/admin/jobs" }, { label: `Job ${shortId(jobId)}` }]} />
      <div className="page-header">
        <h1>
          Job <ShortId id={jobId} copyLabel="Sao chép job ID" />
        </h1>
        <RefreshBar loads={[job]} />
      </div>
      <p className="notice">Khu quản trị chỉ xem. Điều khiển job cần là thành viên của tenant.</p>
      {job.error !== null && (
        <AdminErrorPanel
          error={job.error}
          onRetry={notFound ? undefined : job.refresh}
          title={notFound ? "Không tìm thấy job" : undefined}
        />
      )}
      {job.loading && data === null && <p className="status-line">Đang tải…</p>}
      {data && (
        <>
          {member && (
            <p>
              <Link to={`/t/${data.tenant_id}/jobs/${data.job_id}`}>Mở trong khu tenant</Link>
            </p>
          )}
          <dl className="key-values">
            <dt>Trạng thái</dt>
            <dd>
              <StatusBadge state={data.state} />
              {data.waiting_reason && <span className="muted"> {waitingReasonLabel(data.waiting_reason)}</span>}
            </dd>
            <dt>Trạng thái mong muốn</dt>
            <dd>{data.desired_state}</dd>
            <dt>Tenant</dt>
            <dd>
              <ShortId id={data.tenant_id} to={`/admin/tenants/${data.tenant_id}`} copyLabel="Sao chép tenant ID" />
            </dd>
            <dt>User</dt>
            <dd>
              <ShortId id={data.user_id} to={`/admin/users/${data.user_id}`} copyLabel="Sao chép user ID" />
            </dd>
            <dt>Template</dt>
            <dd>
              {data.spec.template_id}@{data.spec.template_version}
            </dd>
            <dt>Tài nguyên yêu cầu</dt>
            <dd>
              {formatCores(data.spec.resources.cpu_millis)}, {formatBytes(data.spec.resources.memory_bytes)} RAM
              {data.spec.resources.gpu_count > 0 ? `, ${data.spec.resources.gpu_count} GPU` : ""}
            </dd>
            <dt>Ưu tiên</dt>
            <dd>{data.spec.priority}</dd>
            <dt>Giới hạn thời gian chạy</dt>
            <dd>{data.spec.runtime_limit_seconds} giây</dd>
            <dt>Lần chạy lại</dt>
            <dd>
              {data.retry_count}/{data.max_retries}
            </dd>
            {data.retry_of_job_id && (
              <>
                <dt>Chạy lại từ job</dt>
                <dd>
                  <ShortId id={data.retry_of_job_id} to={`/admin/jobs/${data.retry_of_job_id}`} copyLabel="Sao chép job ID" />
                </dd>
              </>
            )}
            <dt>Session</dt>
            <dd>
              <ShortId id={data.session_id} copyLabel="Sao chép session ID" />
            </dd>
            <dt>Job fence</dt>
            <dd>{data.job_fence}</dd>
            <dt>Phiên bản</dt>
            <dd>v{data.version}</dd>
            <dt>Tạo lúc</dt>
            <dd>
              <Time iso={data.created_at} />
            </dd>
            <dt>Cập nhật lúc</dt>
            <dd>
              <Time iso={data.updated_at} />
            </dd>
          </dl>
        </>
      )}
    </section>
  );
}
