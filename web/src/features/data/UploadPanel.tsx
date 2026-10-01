// Browser upload (6.A): size check → SHA-256 → POST /v1/artifacts with one key per chosen file.
// Files over the browser limit are never read; the CLI command is shown instead.
import { useEffect, useId, useRef, useState } from "react";
import { isApiError } from "../../api/errors";
import { IntentTracker, withInProgressRetry } from "../../api/idempotency";
import { BROWSER_TRANSFER_LIMIT_BYTES } from "../../api/limits";
import type { Artifact, UserArtifactKind } from "../../api/types";
import { ARTIFACT_KIND_LABELS } from "../../app/labels";
import { useApi } from "../../auth/session";
import { CliHint } from "../../components/CliHint";
import { ErrorPanel } from "../../components/ErrorPanel";
import { formatBytes } from "../../components/format";
import { sha256Checksum, UPLOAD_MEDIA_TYPES } from "./files";

interface UploadPanelProps {
  tenantId: string;
  /** One kind = fixed by the caller (e.g. the input a template needs). */
  kinds: UserArtifactKind[];
  mediaType?: string;
  onUploaded(artifact: Artifact): void;
  onCancel?(): void;
  /** Lets a surrounding dialog refuse Esc while hashing or uploading. */
  onBusyChange?(busy: boolean): void;
}

type Phase = "idle" | "hashing" | "uploading";

export function UploadPanel({
  tenantId,
  kinds,
  mediaType: presetMedia,
  onUploaded,
  onCancel,
  onBusyChange,
}: UploadPanelProps) {
  const api = useApi();
  const ids = { kind: useId(), media: useId(), file: useId() };
  const [kind, setKind] = useState<UserArtifactKind>(kinds[0]);
  const [mediaType, setMediaType] = useState(presetMedia ?? UPLOAD_MEDIA_TYPES[kinds[0]][0]);
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<unknown>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const tracker = useRef(new IntentTracker());
  const inFlight = useRef(false);
  const fileKey = useRef(0);

  const tooLarge = file !== null && file.size > BROWSER_TRANSFER_LIMIT_BYTES;
  const mediaOptions = UPLOAD_MEDIA_TYPES[kind];
  const busy = phase !== "idle";
  useEffect(() => onBusyChange?.(busy), [busy, onBusyChange]);

  const changeKind = (next: UserArtifactKind) => {
    setKind(next);
    setMediaType(UPLOAD_MEDIA_TYPES[next].includes(mediaType) ? mediaType : UPLOAD_MEDIA_TYPES[next][0]);
  };

  const submit = async () => {
    if (inFlight.current) return;
    if (file === null) {
      setFileError("Chọn một tệp");
      return;
    }
    if (tooLarge) return;
    inFlight.current = true;
    setError(null);
    try {
      setPhase("hashing");
      const checksum = await sha256Checksum(file);
      setPhase("uploading");
      // Same file + same metadata = same intent, so a resend after a network error is not a duplicate.
      const key = tracker.current.keyFor(JSON.stringify({ kind, mediaType, checksum, size: file.size, name: file.name }));
      try {
        const response = await withInProgressRetry(() =>
          api.uploadArtifact(tenantId, file, { kind, mediaType, checksum }, { idempotencyKey: key }),
        );
        tracker.current.settle();
        setFile(null);
        fileKey.current += 1;
        onUploaded(response.data);
      } catch (caught) {
        tracker.current.settle(caught);
        throw caught;
      }
    } catch (caught) {
      setError(caught);
    } finally {
      inFlight.current = false;
      setPhase("idle");
    }
  };

  return (
    // A group, not a <form>: the panel also sits inside the submit form.
    <div className="upload-panel" role="group" aria-label="Tải lên tệp">
      {kinds.length > 1 ? (
        <div className="field">
          <label htmlFor={ids.kind}>Loại dữ liệu</label>
          <select
            id={ids.kind}
            value={kind}
            onChange={(event) => changeKind(event.target.value as UserArtifactKind)}
            disabled={busy}
          >
            {kinds.map((value) => (
              <option key={value} value={value}>
                {ARTIFACT_KIND_LABELS[value]}
              </option>
            ))}
          </select>
        </div>
      ) : (
        <p className="muted">Loại dữ liệu: {ARTIFACT_KIND_LABELS[kind]}</p>
      )}
      <div className="field">
        <label htmlFor={ids.media}>Media type</label>
        <select id={ids.media} value={mediaType} onChange={(event) => setMediaType(event.target.value)} disabled={busy}>
          {!mediaOptions.includes(mediaType) && <option value={mediaType}>{mediaType}</option>}
          {mediaOptions.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor={ids.file}>Tệp</label>
        <input
          key={fileKey.current}
          id={ids.file}
          type="file"
          disabled={busy}
          aria-invalid={fileError !== null}
          aria-describedby={`${ids.file}-help`}
          onChange={(event) => {
            setFile(event.target.files?.[0] ?? null);
            setFileError(null);
            setError(null);
          }}
        />
        <p id={`${ids.file}-help`} className={fileError ? "field-error" : "help"}>
          {fileError ??
            (file
              ? `${formatBytes(file.size)}. Tối đa ${formatBytes(BROWSER_TRANSFER_LIMIT_BYTES)} qua trình duyệt.`
              : `Tối đa ${formatBytes(BROWSER_TRANSFER_LIMIT_BYTES)} qua trình duyệt.`)}
        </p>
      </div>
      {tooLarge && (
        <CliHint
          text={`Tệp lớn hơn ${formatBytes(BROWSER_TRANSFER_LIMIT_BYTES)}: tải lên bằng CLI (thay FILE bằng đường dẫn tệp).`}
          command={`nexa artifact upload FILE --kind ${kind} --media-type ${mediaType} --tenant ${tenantId}`}
        />
      )}
      {phase === "hashing" && <p role="status">Đang tính SHA-256…</p>}
      {phase === "uploading" && <p role="status">Đang tải lên…</p>}
      {error !== null && (
        <ErrorPanel
          error={error}
          title={isApiError(error, "permission_denied") ? "Không có quyền tải dữ liệu lên tenant này" : undefined}
        />
      )}
      <div className="actions">
        {onCancel && (
          <button type="button" onClick={onCancel} disabled={busy}>
            Bỏ qua
          </button>
        )}
        <button type="button" className="primary" onClick={submit} disabled={busy || tooLarge}>
          {phase === "idle" ? "Tải lên" : "Đang tải lên…"}
        </button>
      </div>
    </div>
  );
}
