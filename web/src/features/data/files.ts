// Upload/download helpers: checksum, strong ETag check and safe download names.
import type { TemplateId, UserArtifactKind } from "../../api/types";

export interface ArtifactRequirement {
  kind: UserArtifactKind;
  mediaType: string;
}

/**
 * Not on the wire (B17-R06): copied from src/nexa/domain/workload_adapters.py and used only to
 * filter the picker and preset uploads. The server still validates the reference at submit.
 */
export const TEMPLATE_ARTIFACTS: Record<TemplateId, { input: ArtifactRequirement; model: ArtifactRequirement | null }> = {
  "cpu-iterative": {
    input: { kind: "INPUT", mediaType: "application/vnd.nexa.cpu-iterative-input+json" },
    model: null,
  },
  "pytorch-cifar10-cnn": {
    input: { kind: "DATASET", mediaType: "application/vnd.apache.arrow.file" },
    model: null,
  },
  "batch-inference": {
    input: { kind: "DATASET", mediaType: "application/vnd.apache.arrow.file" },
    model: { kind: "MODEL", mediaType: "application/octet-stream" },
  },
};

/** Upload allowlist per kind (src/nexa/infrastructure/artifacts/policy.py). */
export const UPLOAD_MEDIA_TYPES: Record<UserArtifactKind, string[]> = {
  INPUT: ["application/vnd.nexa.cpu-iterative-input+json", "application/json"],
  DATASET: ["application/vnd.apache.arrow.file", "application/json"],
  MODEL: ["application/octet-stream"],
};

export async function sha256Checksum(blob: Blob): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  const hex = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `sha256:${hex}`;
}

/** Download ETag is the strong quoted checksum (`"sha256:<hex>"`). */
export function etagMatches(etag: string | null, checksum: string): boolean {
  return etag === `"${checksum}"`;
}

const MAX_NAME = 128;

export function safeFileName(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const base = raw.split(/[/\\]/).pop() ?? "";
  const cleaned = base
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, "")
    .replace(/[<>:"|?*]/g, "")
    .replace(/^[\s.]+|[\s.]+$/g, "")
    .slice(0, MAX_NAME);
  return cleaned === "" ? null : cleaned;
}

const EXTENSIONS: [RegExp, string][] = [
  [/ndjson|jsonl/, ".jsonl"],
  [/json$/, ".json"],
  [/parquet/, ".parquet"],
  [/arrow/, ".arrow"],
  [/^text\/plain/, ".txt"],
];

export function extensionFor(mediaType: string | null): string {
  if (!mediaType) return ".bin";
  const type = mediaType.split(";")[0].trim().toLowerCase();
  return EXTENSIONS.find(([pattern]) => pattern.test(type))?.[1] ?? ".bin";
}

export function downloadName(logicalName: string | null, artifactId: string, mediaType: string | null): string {
  return safeFileName(logicalName) ?? `${artifactId}${extensionFor(mediaType)}`;
}
