// /admin/workers — every registered worker (single-node: usually one). One read per open.
import { useEffect } from "react";
import { useApi } from "../../../auth/session";
import { EmptyState, Notice, ShortId, Time } from "../../../components/bits";
import { formatBytes, formatCores } from "../../../components/format";
import { useCursorPaging } from "../paging";
import { AdminErrorPanel, RefreshBar, useAdminLoad } from "../shared";
import { AdminStateBadge, HealthBadge } from "./badges";

export function WorkersPage() {
  const api = useApi().admin;
  const paging = useCursorPaging([]);
  const { cursor, resetIfBadCursor } = paging;
  const workers = useAdminLoad(async (signal) => (await api.listWorkers(cursor, signal)).data, [cursor]);
  useEffect(() => resetIfBadCursor(workers.error), [workers.error, resetIfBadCursor]);
  const page = workers.data;
  const error = paging.shownError(workers.error);

  return (
    <section className="page">
      <div className="page-header">
        <h1>Worker</h1>
        <RefreshBar loads={[workers]} />
      </div>
      {paging.notice && <Notice onDismiss={paging.clearNotice}>{paging.notice}</Notice>}
      {error !== null && <AdminErrorPanel error={error} onRetry={workers.refresh} />}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Worker</th>
              <th scope="col">Sức khỏe</th>
              <th scope="col">Trạng thái quản trị</th>
              <th scope="col" className="col-optional">
                Có thể cấp phát
              </th>
              <th scope="col" className="col-optional">
                Heartbeat gần nhất
              </th>
            </tr>
          </thead>
          <tbody>
            {workers.loading && page === null && (
              <tr>
                <td colSpan={5}>Đang tải…</td>
              </tr>
            )}
            {page?.items.map((worker) => (
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
                  {worker.inventory
                    ? `${formatCores(worker.inventory.allocatable.cpu_millis)} · ${formatBytes(worker.inventory.allocatable.memory_bytes)} · ${worker.inventory.allocatable.gpu_count} GPU`
                    : "Chưa gửi inventory"}
                </td>
                <td className="col-optional">
                  <Time iso={worker.last_heartbeat_at} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {page && page.items.length === 0 && cursor === null && (
        <EmptyState title="Chưa có worker nào đăng ký; khởi động worker cục bộ">
          <p>Worker hiện ở đây sau lần đăng ký đầu tiên với API.</p>
        </EmptyState>
      )}
      {paging.pager(page?.page.next_cursor ?? null)}
    </section>
  );
}
