// /t/:tenantId/sweeps/:sweepId — per-child admission outcomes. Not on the nav (no list API, B17-R10).
import { useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router";
import { ERROR_MESSAGES, isApiError } from "../../api/errors";
import { useTenant } from "../../auth/guards";
import { useApi } from "../../auth/session";
import { Breadcrumb, Notice, ShortId, Time } from "../../components/bits";
import { ErrorPanel } from "../../components/ErrorPanel";
import { shortChecksum, shortId } from "../../components/format";
import { back, EMPTY_TRAIL, forward, type PageTrail } from "../../components/pageTrail";
import { Pager } from "../../components/Pager";
import { usePolled } from "../../components/usePolled";

export function SweepPage() {
  const { tenantId } = useTenant();
  const { sweepId = "" } = useParams();
  const api = useApi();
  const location = useLocation();
  const navigate = useNavigate();
  const [cursor, setCursor] = useState<string | null>(null);
  const [trail, setTrail] = useState<PageTrail>(EMPTY_TRAIL);
  const flash = (location.state as { flash?: string } | null)?.flash ?? null;
  const sweep = usePolled(
    { load: async (signal) => (await api.getSweep(tenantId, sweepId, cursor, signal)).data, schedule: null },
    [tenantId, sweepId, cursor],
  );
  const crumbs = [{ label: "Jobs", to: `/t/${tenantId}/jobs` }, { label: `Sweep ${shortId(sweepId)}` }];
  const data = sweep.data;
  const goTo = (target: string | null, nextTrail: PageTrail) => {
    setTrail(nextTrail);
    setCursor(target);
  };

  return (
    <section className="page">
      <Breadcrumb items={crumbs} />
      <h1>Kết quả sweep</h1>
      {flash && (
        <Notice onDismiss={() => navigate(location.pathname, { replace: true, state: null })}>{flash}</Notice>
      )}
      {sweep.error !== null && (
        <ErrorPanel
          error={sweep.error}
          onRetry={sweep.refresh}
          title={
            isApiError(sweep.error, "resource_not_found", "permission_denied")
              ? "Không tìm thấy sweep hoặc bạn không có quyền xem"
              : undefined
          }
        />
      )}
      {data === null && sweep.error === null && <p>Đang tải…</p>}
      {data && (
        <>
          <dl className="key-values">
            <dt>Sweep</dt>
            <dd>
              <ShortId id={data.sweep_id} copyLabel="Sao chép sweep ID" />
            </dd>
            <dt>Tạo lúc</dt>
            <dd>
              <Time iso={data.created_at} />
            </dd>
            <dt>Job con</dt>
            <dd>
              {data.child_count} tổng · {data.accepted_count} được nhận · {data.rejected_count} bị từ chối
            </dd>
          </dl>
          <p className="muted">Giá trị tham số của từng job con xem ở tab Cấu hình của job đó.</p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col" className="col-optional">
                    Mã tham số
                  </th>
                  <th scope="col">Kết quả</th>
                  <th scope="col">Job</th>
                </tr>
              </thead>
              <tbody>
                {data.children.map((child) => (
                  <tr key={child.child_index}>
                    <td>{child.child_index}</td>
                    <td className="col-optional">
                      <code title={child.parameter_hash}>{shortChecksum(child.parameter_hash)}</code>
                    </td>
                    <td>
                      {child.status === "ACCEPTED" ? (
                        <span className="badge tone-success">Được nhận</span>
                      ) : (
                        <>
                          <span className="badge tone-danger">Bị từ chối</span>
                          {child.error && (
                            <div className="muted">
                              {ERROR_MESSAGES[child.error.code]?.title ?? child.error.code}: {child.error.message}
                            </div>
                          )}
                        </>
                      )}
                    </td>
                    <td>
                      {child.job_id ? (
                        <ShortId id={child.job_id} to={`/t/${tenantId}/jobs/${child.job_id}`} copyLabel="Sao chép job ID" />
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager
            hasPrevious={cursor !== null}
            nextCursor={data.page.next_cursor}
            onFirst={() => goTo(null, EMPTY_TRAIL)}
            onPrevious={() => {
              const step = back(trail);
              goTo(step.target, step.trail);
            }}
            onNext={(next) => goTo(next, forward(trail, cursor))}
          />
        </>
      )}
    </section>
  );
}
