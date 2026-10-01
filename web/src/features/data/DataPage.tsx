// /t/:tenantId/data — artifacts of the tenant, kind filter on the URL, upload dialog, downloads.
import { useEffect, useId, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { isApiError, isBadCursorError } from "../../api/errors";
import type { Artifact, ArtifactKind, UserArtifactKind } from "../../api/types";
import { ARTIFACT_KIND_LABELS } from "../../app/labels";
import { useTenant } from "../../auth/guards";
import { useApi } from "../../auth/session";
import { EmptyState, Notice, ShortId, Time } from "../../components/bits";
import { Dialog } from "../../components/Dialog";
import { DownloadButton } from "../../components/DownloadButton";
import { ErrorPanel } from "../../components/ErrorPanel";
import { formatBytes, shortChecksum } from "../../components/format";
import { back, EMPTY_TRAIL, forward, type PageTrail } from "../../components/pageTrail";
import { Pager } from "../../components/Pager";
import { usePolled } from "../../components/usePolled";
import { UploadPanel } from "./UploadPanel";

const KINDS = Object.keys(ARTIFACT_KIND_LABELS) as ArtifactKind[];
const USER_KINDS: UserArtifactKind[] = ["INPUT", "DATASET", "MODEL"];
const JOB_INPUT_KINDS = new Set<ArtifactKind>(["INPUT", "DATASET"]);

export function DataPage() {
  const { tenantId } = useTenant();
  const api = useApi();
  const [params, setParams] = useSearchParams();
  const rawKind = params.get("kind");
  const kind = rawKind && (KINDS as string[]).includes(rawKind) ? (rawKind as ArtifactKind) : null;
  const cursor = params.get("cursor");
  const filterKey = `${tenantId}|${kind}`;
  const [trailState, setTrailState] = useState<{ key: string; trail: PageTrail }>({ key: filterKey, trail: EMPTY_TRAIL });
  const trail = trailState.key === filterKey ? trailState.trail : EMPTY_TRAIL;
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploaded, setUploaded] = useState<Artifact | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const kindId = useId();

  const artifacts = usePolled(
    { load: async (signal) => (await api.listArtifacts(tenantId, kind, cursor, signal)).data, schedule: null },
    [tenantId, kind, cursor],
  );

  useEffect(() => {
    if (isBadCursorError(artifacts.error, cursor)) {
      setNotice("Vị trí trang không còn hợp lệ, đã quay về trang đầu");
      setTrailState({ key: filterKey, trail: EMPTY_TRAIL });
      const next = new URLSearchParams(params);
      next.delete("cursor");
      setParams(next, { replace: true });
    }
  }, [artifacts.error, cursor, filterKey, params, setParams]);

  const goTo = (target: string | null, nextTrail: PageTrail) => {
    setTrailState({ key: filterKey, trail: nextTrail });
    const next = new URLSearchParams(params);
    if (target) next.set("cursor", target);
    else next.delete("cursor");
    setNotice(null);
    setParams(next);
  };
  const setKind = (value: string) => {
    const next = new URLSearchParams();
    if (value) next.set("kind", value);
    setNotice(null);
    setParams(next);
  };

  const page = artifacts.data;
  const showError = artifacts.error !== null && !isBadCursorError(artifacts.error, cursor);
  return (
    <section className="page">
      <div className="page-header">
        <h1>Dữ liệu</h1>
        <div className="actions">
          <button type="button" onClick={artifacts.refresh}>
            Làm mới
          </button>
          <button type="button" className="primary" onClick={() => setUploadOpen(true)}>
            Tải lên tệp
          </button>
        </div>
      </div>

      {uploaded && (
        <Notice onDismiss={() => setUploaded(null)}>
          Đã tải lên <code title={uploaded.artifact_id}>{uploaded.artifact_id}</code>.{" "}
          {JOB_INPUT_KINDS.has(uploaded.kind) && (
            <Link to={`/t/${tenantId}/jobs/new?input=${encodeURIComponent(uploaded.artifact_id)}`}>
              Tạo job với tệp này
            </Link>
          )}
        </Notice>
      )}
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}

      <form className="filter-bar" onSubmit={(event) => event.preventDefault()} aria-label="Bộ lọc dữ liệu">
        <div className="field">
          <label htmlFor={kindId}>Loại</label>
          <select id={kindId} value={kind ?? ""} onChange={(event) => setKind(event.target.value)}>
            <option value="">Tất cả</option>
            {KINDS.map((value) => (
              <option key={value} value={value}>
                {ARTIFACT_KIND_LABELS[value]}
              </option>
            ))}
          </select>
        </div>
      </form>

      {showError && (
        <ErrorPanel
          error={artifacts.error}
          onRetry={artifacts.refresh}
          title={isApiError(artifacts.error, "permission_denied") ? "Bạn không có quyền xem dữ liệu của tenant này" : undefined}
        />
      )}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">ID</th>
              <th scope="col">Loại</th>
              <th scope="col" className="col-optional">
                Media type
              </th>
              <th scope="col">Kích thước</th>
              <th scope="col" className="col-optional">
                Trạng thái
              </th>
              <th scope="col" className="col-optional">
                Checksum
              </th>
              <th scope="col" className="col-optional">
                Tạo lúc
              </th>
              <th scope="col">
                <span className="visually-hidden">Thao tác</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {artifacts.loading && page === null && (
              <tr>
                <td colSpan={8}>Đang tải…</td>
              </tr>
            )}
            {page?.items.map((artifact) => (
              <tr key={artifact.artifact_id}>
                <td>
                  <ShortId id={artifact.artifact_id} copyLabel="Sao chép artifact ID" />
                </td>
                <td>{ARTIFACT_KIND_LABELS[artifact.kind]}</td>
                <td className="col-optional">
                  <code>{artifact.media_type}</code>
                </td>
                <td>{formatBytes(artifact.size_bytes)}</td>
                <td className="col-optional">Đã lưu</td>
                <td className="col-optional">
                  <code title={artifact.checksum}>{shortChecksum(artifact.checksum)}</code>
                </td>
                <td className="col-optional">
                  <Time iso={artifact.created_at} />
                </td>
                <td>
                  <span className="inline-actions">
                    <DownloadButton
                      tenantId={tenantId}
                      target={{
                        artifactId: artifact.artifact_id,
                        checksum: artifact.checksum,
                        sizeBytes: artifact.size_bytes,
                        mediaType: artifact.media_type,
                      }}
                    />
                    {JOB_INPUT_KINDS.has(artifact.kind) && (
                      <Link
                        to={`/t/${tenantId}/jobs/new?input=${encodeURIComponent(artifact.artifact_id)}`}
                        aria-label={`Tạo job với tệp ${artifact.artifact_id}`}
                      >
                        Tạo job
                      </Link>
                    )}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {page && page.items.length === 0 && (kind !== null || cursor !== null) && (
        <EmptyState title="Không có dữ liệu khớp bộ lọc">
          <button type="button" onClick={() => setKind("")}>
            Xóa bộ lọc
          </button>
        </EmptyState>
      )}
      {page && page.items.length === 0 && kind === null && cursor === null && (
        <EmptyState title="Chưa có dữ liệu. Tải lên tệp đầu vào để tạo job">
          <button type="button" className="primary" onClick={() => setUploadOpen(true)}>
            Tải lên tệp
          </button>
        </EmptyState>
      )}

      <Pager
        hasPrevious={cursor !== null}
        nextCursor={page?.page.next_cursor ?? null}
        onFirst={() => goTo(null, EMPTY_TRAIL)}
        onPrevious={() => {
          const step = back(trail);
          goTo(step.target, step.trail);
        }}
        onNext={(next) => goTo(next, forward(trail, cursor))}
      />

      <Dialog open={uploadOpen} title="Tải lên tệp" onClose={() => setUploadOpen(false)} busy={uploadBusy}>
        <UploadPanel
          tenantId={tenantId}
          kinds={USER_KINDS}
          onCancel={() => setUploadOpen(false)}
          onBusyChange={setUploadBusy}
          onUploaded={(artifact) => {
            setUploadOpen(false);
            setUploaded(artifact);
            artifacts.refresh();
          }}
        />
      </Dialog>
    </section>
  );
}
