// Job detail polling (DETAIL_POLL): job + progress/attempts/checkpoints + new events + result.
// Stops after the run that saw a terminal state, so the last attempt and the result are loaded.
import { useRef } from "react";
import { ApiError } from "../../api/client";
import type { Endpoints } from "../../api/endpoints";
import { PAGE_SIZE } from "../../api/limits";
import { DETAIL_POLL } from "../../api/polling";
import type {
  Attempt,
  CheckpointRecord,
  Job,
  JobEvent,
  ManifestFile,
  ProgressRecord,
  ResultRecord,
} from "../../api/types";
import { usePolled } from "../../components/usePolled";
import { etagMatches } from "../data/files";
import { isTerminal } from "./actions";

export interface Section<T> {
  value: T | null;
  error: unknown;
}

export interface ResultView {
  record: ResultRecord;
  files: ManifestFile[];
  metrics: [string, string][];
}

export interface JobDetail {
  job: Job;
  etag: string;
  progress: Section<ProgressRecord>;
  attempts: Section<Attempt[]>;
  checkpoints: Section<CheckpointRecord[]>;
  events: JobEvent[];
  eventsError: unknown;
  /** Events up to job.event_sequence are not all loaded yet (the API has no cursor for events). */
  moreEvents: boolean;
  result: Section<ResultView>;
}

class ManifestError extends Error {}

export function jobEtag(etag: string | null, job: Job): string {
  // Contract ETag for jobs is the strong version `"v<version>"` (openapi.yaml ETag).
  return etag ?? `"v${job.version}"`;
}

function metricText(value: unknown): string {
  if (value === null) return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function parseManifest(text: string): { files: ManifestFile[]; metrics: [string, string][] } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new ManifestError("manifest is not JSON");
  }
  if (typeof parsed !== "object" || parsed === null) throw new ManifestError("manifest is not an object");
  const record = parsed as { files?: unknown; metrics?: unknown };
  if (!Array.isArray(record.files)) throw new ManifestError("manifest has no files");
  const files = record.files.filter(
    (file): file is ManifestFile =>
      typeof file === "object" &&
      file !== null &&
      typeof (file as ManifestFile).artifact_id === "string" &&
      typeof (file as ManifestFile).logical_name === "string" &&
      typeof (file as ManifestFile).media_type === "string" &&
      typeof (file as ManifestFile).size_bytes === "number" &&
      typeof (file as ManifestFile).checksum === "string",
  );
  const metrics =
    typeof record.metrics === "object" && record.metrics !== null
      ? Object.entries(record.metrics as Record<string, unknown>).map(([key, value]): [string, string] => [key, metricText(value)])
      : [];
  return { files, metrics };
}

async function settle<T>(promise: Promise<T>, previous: T | null): Promise<Section<T>> {
  try {
    return { value: await promise, error: null };
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    return { value: previous, error };
  }
}

export function isManifestError(error: unknown): boolean {
  return error instanceof ManifestError;
}

export function useJobDetail(api: Endpoints, tenantId: string, jobId: string) {
  const previous = useRef<JobDetail | null>(null);
  const identity = `${tenantId}/${jobId}`;
  const identityRef = useRef(identity);
  if (identityRef.current !== identity) {
    identityRef.current = identity;
    previous.current = null;
  }

  return usePolled<JobDetail>(
    {
      schedule: DETAIL_POLL,
      async load(signal) {
        const last = previous.current;
        const jobResponse = await api.getJob(tenantId, jobId, signal);
        const job = jobResponse.data;

        const [progress, attempts, checkpoints] = await Promise.all([
          settle(api.getProgress(tenantId, jobId, signal).then((r) => r.data), last?.progress.value ?? null),
          settle(api.listAttempts(tenantId, jobId, signal).then((r) => r.data.items), last?.attempts.value ?? null),
          settle(api.listCheckpoints(tenantId, jobId, signal).then((r) => r.data.items), last?.checkpoints.value ?? null),
        ]);

        let events = last?.events ?? [];
        let moreEvents = last?.moreEvents ?? false;
        let eventsError: unknown = null;
        const maxSequence = events.length > 0 ? events[events.length - 1].sequence : 0;
        if (last === null || job.event_sequence > maxSequence) {
          try {
            const page = (await api.listEvents(tenantId, jobId, maxSequence, signal)).data;
            const seen = new Set(events.map((event) => event.event_id));
            events = [...events, ...page.items.filter((event) => !seen.has(event.event_id))];
            const loaded = events.length > 0 ? events[events.length - 1].sequence : 0;
            moreEvents = page.items.length >= PAGE_SIZE.events && loaded < job.event_sequence;
          } catch (error) {
            if (error instanceof DOMException && error.name === "AbortError") throw error;
            eventsError = error;
          }
        }

        let result: Section<ResultView> = last?.result ?? { value: null, error: null };
        if (job.state === "SUCCEEDED" && result.value === null) {
          result = await settle(loadResult(api, tenantId, jobId, signal), null);
        }

        const detail: JobDetail = {
          job,
          etag: jobEtag(jobResponse.etag, job),
          progress,
          attempts,
          checkpoints,
          events,
          eventsError,
          moreEvents,
          result,
        };
        previous.current = detail;
        return detail;
      },
      fingerprint: (detail) =>
        [
          detail.job.version,
          detail.progress.value?.available ? detail.progress.value.progress_sequence : 0,
          detail.attempts.value?.map((a) => `${a.attempt_id}:${a.state}`).join(",") ?? "",
          detail.checkpoints.value?.length ?? 0,
          detail.events.length,
        ].join("|"),
      // A terminal job with more than one page of events loads the rest via refresh().
      done: (detail) => isTerminal(detail.job.state),
    },
    [tenantId, jobId],
  );
}

async function loadResult(api: Endpoints, tenantId: string, jobId: string, signal: AbortSignal): Promise<ResultView> {
  const record = (await api.getResult(tenantId, jobId, signal)).data;
  const manifest = await api.downloadArtifact(tenantId, record.manifest_artifact_id, signal);
  if (!etagMatches(manifest.etag, record.manifest_checksum)) throw new ManifestError("manifest checksum mismatch");
  const { files, metrics } = parseManifest(await manifest.data.text());
  return { record, files, metrics };
}

export function isResultPending(error: unknown): boolean {
  return error instanceof ApiError && error.code === "state_conflict";
}
