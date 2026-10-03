// /admin/fairness?from&to&bucket&tenant_id — dominant resource-time per tenant and bucket.
// Runs on open with the URL or the default range (24 h, 1 h buckets; UX-A19/A28): fairness +
// tenants (first 100, for slugs and the filter). Out-of-bounds queries are not sent.
import { useId, useState } from "react";
import { useSearchParams } from "react-router";
import { PAGE_SIZE } from "../../../api/limits";
import { useApi } from "../../../auth/session";
import { EmptyState, Time } from "../../../components/bits";
import {
  BUCKET_PRESETS,
  checkFairnessQuery,
  DEFAULT_BUCKET_SECONDS,
  FAIRNESS_EXPLANATION,
  sortBuckets,
  tenantTotals,
} from "../fairness";
import { useUrlRange } from "../paging";
import { AdminErrorPanel, RangeForm, RefreshBar, TenantName, useAdminLoad } from "../shared";
import { formatNumber } from "./format";

export function FairnessPage() {
  const api = useApi().admin;
  const [params, setParams] = useSearchParams();
  const range = useUrlRange();
  const bucketParam = Number(params.get("bucket") ?? DEFAULT_BUCKET_SECONDS);
  const tenantId = params.get("tenant_id");
  const urlProblem = checkFairnessQuery(range.from, range.to, bucketParam);
  const ids = { bucket: useId(), tenant: useId() };
  const [bucket, setBucket] = useState(String(bucketParam));
  const [tenantDraft, setTenantDraft] = useState(tenantId ?? "");

  const tenants = useAdminLoad(async (signal) => (await api.listTenants(null, signal, PAGE_SIZE.adminNames)).data, []);
  const report = useAdminLoad(
    async (signal) =>
      (
        await api.queryFairness(
          { from: range.from, to: range.to, bucket_seconds: bucketParam, tenant_id: tenantId },
          signal,
        )
      ).data,
    [range.from, range.to, bucketParam, tenantId],
    urlProblem === null,
  );
  const tenantItems = tenants.data?.items ?? null;
  const buckets = report.data ? sortBuckets(report.data.buckets) : null;
  const totals = report.data ? tenantTotals(report.data.buckets) : null;

  return (
    <section className="page">
      <div className="page-header">
        <h1>Fairness</h1>
        <RefreshBar loads={urlProblem === null ? [report, tenants] : [tenants]} />
      </div>
      <p className="help">{FAIRNESS_EXPLANATION}</p>
      <RangeForm
        label="Khoảng báo cáo fairness"
        range={range}
        submitLabel="Xem báo cáo"
        check={(draft) => checkFairnessQuery(draft.from, draft.to, Number(bucket))}
        onSubmit={(draft) =>
          setParams({
            from: draft.from,
            to: draft.to,
            bucket,
            ...(tenantDraft ? { tenant_id: tenantDraft } : {}),
          })
        }
      >
        <div className="field">
          <label htmlFor={ids.bucket}>Độ dài bucket</label>
          <select id={ids.bucket} value={bucket} onChange={(event) => setBucket(event.target.value)}>
            {!BUCKET_PRESETS.some((item) => String(item.seconds) === bucket) && <option value={bucket}>{bucket} giây</option>}
            {BUCKET_PRESETS.map((item) => (
              <option key={item.seconds} value={item.seconds}>
                {item.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={ids.tenant}>Tenant</label>
          <select id={ids.tenant} value={tenantDraft} onChange={(event) => setTenantDraft(event.target.value)}>
            <option value="">Tất cả</option>
            {tenantDraft && !tenantItems?.some((item) => item.tenant_id === tenantDraft) && (
              <option value={tenantDraft}>{tenantDraft}</option>
            )}
            {tenantItems?.map((tenant) => (
              <option key={tenant.tenant_id} value={tenant.tenant_id}>
                {tenant.slug}
              </option>
            ))}
          </select>
        </div>
      </RangeForm>
      {urlProblem !== null && (
        <p className="field-error" role="alert">
          Không gửi báo cáo: {urlProblem.message}
        </p>
      )}
      {report.error !== null && <AdminErrorPanel error={report.error} onRetry={report.refresh} />}
      {report.loading && report.data === null && <p className="status-line">Đang tải…</p>}
      {report.data && (
        <p className="muted">
          Từ <Time iso={report.data.from} /> đến <Time iso={report.data.to} />, bucket {report.data.bucket_seconds} giây.
        </p>
      )}
      {buckets && buckets.length === 0 && <EmptyState title="Không có phân bổ nào trong khoảng này" />}
      {totals && totals.length > 0 && (
        <div className="table-wrap">
          <table>
            <caption>Theo tenant — tổng trong báo cáo này</caption>
            <thead>
              <tr>
                <th scope="col">Tenant</th>
                <th scope="col">Thời gian tài nguyên trội (giây)</th>
                <th scope="col">Dịch vụ chuẩn hóa</th>
                <th scope="col" className="col-optional">
                  Thời gian chiếm dụng (giây)
                </th>
              </tr>
            </thead>
            <tbody>
              {totals.map((total) => (
                <tr key={total.tenant_id}>
                  <td>
                    <TenantName id={total.tenant_id} tenants={tenantItems} />
                  </td>
                  <td>{formatNumber(total.dominant_resource_time_seconds)}</td>
                  <td>{formatNumber(total.normalized_service)}</td>
                  <td className="col-optional">{formatNumber(total.allocation_occupancy_seconds)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {buckets && buckets.length > 0 && (
        <div className="table-wrap">
          <table>
            <caption>Theo bucket</caption>
            <thead>
              <tr>
                <th scope="col">Bắt đầu</th>
                <th scope="col">Tenant</th>
                <th scope="col">Trọng số</th>
                <th scope="col">Thời gian tài nguyên trội (giây)</th>
                <th scope="col">Dịch vụ chuẩn hóa</th>
                <th scope="col" className="col-optional">
                  Thời gian chiếm dụng (giây)
                </th>
              </tr>
            </thead>
            <tbody>
              {buckets.map((item) => (
                <tr key={`${item.start_at}|${item.tenant_id}`}>
                  <td>
                    <Time iso={item.start_at} />
                  </td>
                  <td>
                    <TenantName id={item.tenant_id} tenants={tenantItems} />
                  </td>
                  <td>{formatNumber(item.weight)}</td>
                  <td>{formatNumber(item.dominant_resource_time_seconds)}</td>
                  <td>{formatNumber(item.normalized_service)}</td>
                  <td className="col-optional">{formatNumber(item.allocation_occupancy_seconds)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
