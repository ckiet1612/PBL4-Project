// Picker for the data a template needs: artifacts of the required kind, filtered by media type
// on the client (B17-R06), more pages only on request, upload inline without leaving the form.
import { useEffect, useId, useState } from "react";
import { isApiError } from "../../api/errors";
import type { Artifact } from "../../api/types";
import { useApi } from "../../auth/session";
import { ErrorPanel } from "../../components/ErrorPanel";
import { formatBytes, formatTime, shortId } from "../../components/format";
import type { ArtifactRequirement } from "../data/files";
import { UploadPanel } from "../data/UploadPanel";

interface ArtifactPickerProps {
  tenantId: string;
  label: string;
  requirement: ArtifactRequirement;
  value: string | null;
  onChange(artifactId: string | null): void;
  error?: string;
  /** Focus target for the error summary. */
  fieldId: string;
}

function optionLabel(artifact: Artifact): string {
  return `${shortId(artifact.artifact_id)} · ${formatBytes(artifact.size_bytes)} · ${formatTime(artifact.created_at)}`;
}

function fits(artifact: Artifact, requirement: ArtifactRequirement): boolean {
  return artifact.kind === requirement.kind && artifact.media_type === requirement.mediaType;
}

export function ArtifactPicker({ tenantId, label, requirement, value, onChange, error, fieldId }: ArtifactPickerProps) {
  const api = useApi();
  const helpId = useId();
  const [items, setItems] = useState<Artifact[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [selected, setSelected] = useState<{ id: string; artifact: Artifact | null; error: unknown } | null>(null);
  const [uploading, setUploading] = useState(false);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setItems([]);
    setNext(null);
    setLoading(true);
    setLoadError(null);
    api.listArtifacts(tenantId, requirement.kind, null, controller.signal).then(
      (response) => {
        setItems(response.data.items);
        setNext(response.data.page.next_cursor);
        setLoading(false);
      },
      (caught: unknown) => {
        if (controller.signal.aborted) return;
        setLoadError(caught);
        setLoading(false);
      },
    );
    return () => controller.abort();
  }, [api, tenantId, requirement.kind, reload]);

  // A value from ?input= or an earlier page: read it so it can be shown and checked.
  useEffect(() => {
    if (value === null || items.some((artifact) => artifact.artifact_id === value)) {
      setSelected(null);
      return;
    }
    if (selected?.id === value) return;
    const controller = new AbortController();
    api.getArtifact(tenantId, value, controller.signal).then(
      (response) => setSelected({ id: value, artifact: response.data, error: null }),
      (caught: unknown) => {
        if (!controller.signal.aborted) setSelected({ id: value, artifact: null, error: caught });
      },
    );
    return () => controller.abort();
  }, [api, tenantId, value, items, selected]);

  const loadMore = async () => {
    if (next === null) return;
    setLoading(true);
    try {
      const response = await api.listArtifacts(tenantId, requirement.kind, next);
      setItems((current) => [...current, ...response.data.items]);
      setNext(response.data.page.next_cursor);
    } catch (caught) {
      setLoadError(caught);
    } finally {
      setLoading(false);
    }
  };

  const matching = items.filter((artifact) => fits(artifact, requirement));
  const selectedArtifact = selected?.artifact ?? null;
  const mismatch = selectedArtifact !== null && !fits(selectedArtifact, requirement);
  const options = selectedArtifact && !mismatch && !matching.includes(selectedArtifact) ? [selectedArtifact, ...matching] : matching;
  const selectError =
    error ??
    (mismatch
      ? "Tệp đã chọn không phù hợp với template này"
      : selected?.error
        ? isApiError(selected.error, "resource_not_found")
          ? "Không tìm thấy tệp đã chọn trong tenant này"
          : "Không đọc được tệp đã chọn"
        : undefined);

  return (
    <div className="artifact-picker">
      <div className="field">
        <label htmlFor={fieldId}>{label}</label>
        <select
          id={fieldId}
          value={value ?? ""}
          onChange={(event) => onChange(event.target.value || null)}
          aria-invalid={selectError !== undefined}
          aria-describedby={helpId}
        >
          <option value="">{matching.length === 0 && !loading ? "Chưa có tệp phù hợp" : "Chọn tệp"}</option>
          {mismatch && selectedArtifact && (
            <option value={selectedArtifact.artifact_id}>{optionLabel(selectedArtifact)} (không phù hợp)</option>
          )}
          {options.map((artifact) => (
            <option key={artifact.artifact_id} value={artifact.artifact_id}>
              {optionLabel(artifact)}
            </option>
          ))}
        </select>
        <p id={helpId} className={selectError ? "field-error" : "help"}>
          {selectError ?? `${requirement.kind} · ${requirement.mediaType}`}
        </p>
      </div>
      {loading && <p className="muted">Đang tải danh sách tệp…</p>}
      {loadError !== null && <ErrorPanel error={loadError} onRetry={() => setReload((n) => n + 1)} />}
      <div className="actions">
        {next !== null && (
          <button type="button" onClick={loadMore} disabled={loading}>
            Xem thêm tệp
          </button>
        )}
        {!uploading && (
          <button type="button" onClick={() => setUploading(true)}>
            Tải lên tệp mới
          </button>
        )}
      </div>
      {uploading && (
        <UploadPanel
          tenantId={tenantId}
          kinds={[requirement.kind]}
          mediaType={requirement.mediaType}
          onCancel={() => setUploading(false)}
          onUploaded={(artifact) => {
            setItems((current) => [artifact, ...current.filter((a) => a.artifact_id !== artifact.artifact_id)]);
            setUploading(false);
            onChange(artifact.artifact_id);
          }}
        />
      )}
    </div>
  );
}
