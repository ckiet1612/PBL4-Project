// fetch → Blob → verify strong ETag and SHA-256 → object URL → <a download> (6.A).
import { useState } from "react";
import { BROWSER_TRANSFER_LIMIT_BYTES } from "../api/limits";
import { useApi } from "../auth/session";
import { downloadName, etagMatches, sha256Checksum } from "../features/data/files";
import { CliHint } from "./CliHint";
import { ErrorPanel } from "./ErrorPanel";

export interface DownloadTarget {
  artifactId: string;
  /** Unknown (e.g. input referenced by a spec): read the artifact metadata first. */
  checksum?: string;
  sizeBytes?: number;
  mediaType?: string | null;
  logicalName?: string | null;
}

class ChecksumMismatch extends Error {}

interface DownloadButtonProps {
  tenantId: string;
  target: DownloadTarget;
  label?: string;
}

export function DownloadButton({ tenantId, target, label = "Tải xuống" }: DownloadButtonProps) {
  const api = useApi();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [mismatch, setMismatch] = useState(false);
  const [tooLarge, setTooLarge] = useState(target.sizeBytes !== undefined && target.sizeBytes > BROWSER_TRANSFER_LIMIT_BYTES);
  // The CLI command uses the ID-based name: always shell-safe, no quoting needed.
  const cliFileName = downloadName(null, target.artifactId, target.mediaType ?? null);

  const download = async () => {
    setBusy(true);
    setError(null);
    setMismatch(false);
    try {
      let checksum = target.checksum;
      let mediaType = target.mediaType ?? null;
      if (checksum === undefined || target.sizeBytes === undefined) {
        const meta = (await api.getArtifact(tenantId, target.artifactId)).data;
        if (meta.size_bytes > BROWSER_TRANSFER_LIMIT_BYTES) {
          setTooLarge(true);
          return;
        }
        checksum = meta.checksum;
        mediaType = mediaType ?? meta.media_type;
      }
      const response = await api.downloadArtifact(tenantId, target.artifactId);
      const blob = response.data;
      if (!etagMatches(response.etag, checksum) || (await sha256Checksum(blob)) !== checksum) {
        throw new ChecksumMismatch();
      }
      const name = downloadName(
        target.logicalName ?? null,
        target.artifactId,
        response.headers.get("X-Artifact-Media-Type") ?? mediaType,
      );
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = name;
      anchor.rel = "noopener";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (caught) {
      if (caught instanceof ChecksumMismatch) setMismatch(true);
      else setError(caught);
    } finally {
      setBusy(false);
    }
  };

  if (tooLarge) {
    return (
      <CliHint
        text="Tệp lớn hơn 256 MiB: tải bằng CLI."
        command={`nexa artifact download ${target.artifactId} --output-file ${cliFileName} --tenant ${tenantId}`}
      />
    );
  }
  return (
    <span className="download">
      <button type="button" onClick={download} disabled={busy} aria-label={`${label} ${target.logicalName ?? target.artifactId}`}>
        {busy ? "Đang tải…" : label}
      </button>
      {mismatch && (
        <span className="field-error" role="alert">
          Checksum của tệp tải về không khớp; tệp không được lưu. Thử lại.
        </span>
      )}
      {error !== null && <ErrorPanel error={error} />}
    </span>
  );
}
