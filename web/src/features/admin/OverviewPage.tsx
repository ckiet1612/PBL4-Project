// /admin — is the system healthy, what needs attention. Five reads (= five audit rows) per open.
import { Link } from "react-router";
import { PAGE_SIZE } from "../../api/limits";
import { useApi } from "../../auth/session";
import { EmptyState, ShortId, Time } from "../../components/bits";
import { capacityRows, summarizeAllocations, type AllocationSummary } from "./actions";
import { recoveryEventLabel } from "./labels";
import { MODE_LABELS } from "./modes";
import { attentionItems } from "./overview";
import { defaultRange } from "./ranges";
import { AdminErrorPanel, RefreshBar, useAdminContext, useAdminLoad } from "./shared";
import { CapacityTable } from "./workers/CapacityTable";
import { AdminStateBadge, HealthBadge } from "./workers/badges";

export function OverviewPage() {
  const api = useApi().admin;
  const { rememberPolicy } = useAdminContext();
  const overview = useAdminLoad(async (signal) => {
    const range = defaultRange();
    const [policy, workers, held, quarantined, recovery] = await Promise.all([
      api.getPolicy(signal),
      api.listWorkers(null, signal, PAGE_SIZE.overviewWorkers),
      api.listAllocations("HELD", signal),
      api.listAllocations("QUARANTINED", signal),
      api.listRecoveryEvents({ ...range, cursor: null }, signal, PAGE_SIZE.overviewRecovery),
    ]);
    rememberPolicy(policy.data);
    return { policy: policy.data, workers: workers.data, held: held.data, quarantined: quarantined.data, recovery: recovery.data };
  }, []);

  const data = overview.data;
  const totalQuarantined: AllocationSummary | null = data
    ? {
        count: data.quarantined.items.length,
        cpu_millis: 0,
        memory_bytes: 0,
        gpu_count: 0,
        incomplete: data.quarantined.page.next_cursor !== null,
      }
    : null;
  const attention =
    data && totalQuarantined
      ? attentionItems({
          mode: data.policy.operational_mode,
          workers: data.workers.items,
          workersIncomplete: data.workers.page.next_cursor !== null,
          quarantined: totalQuarantined,
          recoveryEvents: data.recovery.items.length,
          recoveryIncomplete: data.recovery.page.next_cursor !== null,
        })
      : [];

  return (
    <section className="page">
      <div className="page-header">
        <h1>Tổng quan</h1>
        <RefreshBar loads={[overview]} />
      </div>
      {overview.error !== null && <AdminErrorPanel error={overview.error} onRetry={overview.refresh} />}
      {overview.loading && data === null && <p className="status-line">Đang tải…</p>}
      {data && (
        <>
          <section className="form-group" aria-labelledby="attention-title">
            <h2 id="attention-title">Cần chú ý</h2>
            {attention.length === 0 ? (
              <p>Không có gì cần chú ý.</p>
            ) : (
              <ul>
                {attention.map((item) => (
                  <li key={item.text}>
                    <Link to={item.to}>{item.text}</Link>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section aria-labelledby="system-title">
            <h2 id="system-title">Hệ thống</h2>
            <dl className="key-values">
              <dt>Chế độ vận hành</dt>
              <dd>{MODE_LABELS[data.policy.operational_mode]}</dd>
              <dt>Giới hạn job tồn đọng toàn hệ thống</dt>
              <dd>{data.policy.global_outstanding_limit.toLocaleString("vi-VN")}</dd>
              <dt>Phân bổ đang giữ</dt>
              <dd>
                {data.held.items.length}
                {data.held.page.next_cursor !== null ? "+" : ""} HELD · {data.quarantined.items.length}
                {data.quarantined.page.next_cursor !== null ? "+" : ""} QUARANTINED
              </dd>
            </dl>
            <p>
              <Link to="/admin/policy">Chính sách hệ thống</Link>
            </p>
          </section>

          <section aria-labelledby="workers-title" className="stack-tight">
            <h2 id="workers-title">Worker</h2>
            {data.workers.items.length === 0 ? (
              <EmptyState title="Chưa có worker nào đăng ký">
                <p>Khởi động worker cục bộ; worker sẽ hiện ở đây sau lần đăng ký đầu tiên.</p>
              </EmptyState>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Worker</th>
                      <th scope="col">Sức khỏe</th>
                      <th scope="col">Trạng thái quản trị</th>
                      <th scope="col" className="col-optional">
                        Heartbeat gần nhất
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.workers.items.map((worker) => (
                      <tr key={worker.worker_id}>
                        <td>
                          <ShortId id={worker.worker_id} to={`/admin/workers/${worker.worker_id}`} copyLabel="Sao chép worker ID" />
                        </td>
                        <td>
                          <HealthBadge health={worker.health} />
                        </td>
                        <td>
                          <AdminStateBadge state={worker.admin_state} />
                        </td>
                        <td className="col-optional">
                          <Time iso={worker.last_heartbeat_at} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {data.workers.page.next_cursor !== null && (
              <p>
                <Link to="/admin/workers">Xem tất cả worker</Link>
              </p>
            )}
            {data.workers.items.length === 1 && (
              <CapacityTable
                caption="Sức chứa"
                rows={capacityRows(
                  data.workers.items[0].inventory?.allocatable ?? null,
                  summarizeAllocations(data.held, data.workers.items[0].worker_id),
                  summarizeAllocations(data.quarantined, data.workers.items[0].worker_id),
                )}
              />
            )}
          </section>

          <section aria-labelledby="recovery-title" className="stack-tight">
            <h2 id="recovery-title">Sự kiện khôi phục 24 giờ qua</h2>
            {data.recovery.items.length === 0 ? (
              <p className="muted">Không có sự kiện khôi phục trong 24 giờ qua.</p>
            ) : (
              <ul>
                {data.recovery.items.map((event) => (
                  <li key={event.event_id}>
                    <Time iso={event.created_at} /> · {recoveryEventLabel(event.type)}
                    {event.job_id && (
                      <>
                        {" "}
                        · <ShortId id={event.job_id} to={`/admin/jobs/${event.job_id}`} copyLabel="Sao chép job ID" />
                      </>
                    )}
                  </li>
                ))}
              </ul>
            )}
            <p>
              <Link to="/admin/recovery">Xem tất cả sự kiện khôi phục</Link>
            </p>
          </section>
        </>
      )}
    </section>
  );
}
