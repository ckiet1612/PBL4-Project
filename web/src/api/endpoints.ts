// Typed wrappers for the /v1 routes the user UI calls. No admin routes.
import type { ApiClient, ApiResponse } from "./client";
import { PAGE_SIZE, TRANSFER_TIMEOUT_MS } from "./limits";
import type {
  Artifact,
  ArtifactKind,
  ArtifactPage,
  AttemptPage,
  BrowserSession,
  CheckpointPage,
  ControlRequest,
  EventPage,
  Job,
  JobPage,
  JobSpec,
  JobState,
  ProgressRecord,
  ResultRecord,
  RetryRequest,
  Sweep,
  SweepSubmitRequest,
  Template,
  TokenCreateRequest,
  TokenCreated,
  TokenPage,
  UserArtifactKind,
} from "./types";

export interface MutationOptions {
  idempotencyKey: string;
  signal?: AbortSignal;
}

/** Controls and retry: If-Match is required by type, so 428 precondition_required cannot happen. */
export interface GuardedMutationOptions extends MutationOptions {
  ifMatch: string;
}

export type ControlAction = "cancel" | "pause" | "resume";

export interface JobFilters {
  state: JobState | null;
  template_id: string | null;
  created_after: string | null;
  cursor: string | null;
}

export interface UploadMetadata {
  kind: UserArtifactKind;
  mediaType: string;
  checksum: string;
}

function requireIfMatch(ifMatch: string): string {
  if (!ifMatch) throw new Error("If-Match is required for job controls");
  return ifMatch;
}

export function createEndpoints(client: ApiClient) {
  return {
    getSession: (signal?: AbortSignal) =>
      client.request<BrowserSession>({ path: "/auth/session", authProbe: true, signal }),
    login: (body: { username: string; password: string }) =>
      client.request<BrowserSession>({ method: "POST", path: "/auth/login", json: body, authProbe: true }),
    logout: () => client.request<null>({ method: "POST", path: "/auth/logout", authProbe: true }),

    listTokens: (cursor: string | null, signal?: AbortSignal) =>
      client.request<TokenPage>({ path: "/tokens", query: { page_size: PAGE_SIZE.tokens, cursor }, signal }),
    createToken: (body: TokenCreateRequest, options: MutationOptions) =>
      client.request<TokenCreated>({ method: "POST", path: "/tokens", json: body, ...options }),
    revokeToken: (tokenId: string, options: MutationOptions) =>
      client.request<null>({ method: "DELETE", path: `/tokens/${encodeURIComponent(tokenId)}`, ...options }),

    listTemplates: (tenantId: string, signal?: AbortSignal) =>
      client.request<Template[]>({ path: "/templates", tenantId, query: { enabled: "true" }, signal }),

    listArtifacts: (tenantId: string, kind: ArtifactKind | null, cursor: string | null, signal?: AbortSignal) =>
      client.request<ArtifactPage>({
        path: "/artifacts",
        tenantId,
        query: { page_size: PAGE_SIZE.artifacts, kind, cursor },
        signal,
      }),
    getArtifact: (tenantId: string, artifactId: string, signal?: AbortSignal) =>
      client.request<Artifact>({ path: `/artifacts/${encodeURIComponent(artifactId)}`, tenantId, signal }),
    uploadArtifact: (tenantId: string, file: Blob, meta: UploadMetadata, options: MutationOptions) =>
      client.request<Artifact>({
        method: "POST",
        path: "/artifacts",
        tenantId,
        body: file,
        headers: {
          "X-Artifact-Kind": meta.kind,
          "X-Artifact-Media-Type": meta.mediaType,
          "X-Artifact-Size": String(file.size),
          "X-Artifact-Checksum": meta.checksum,
        },
        timeoutMs: TRANSFER_TIMEOUT_MS,
        ...options,
      }),
    downloadArtifact: (tenantId: string, artifactId: string, signal?: AbortSignal) =>
      client.request<Blob>({
        path: `/artifacts/${encodeURIComponent(artifactId)}/content`,
        tenantId,
        expect: "blob",
        timeoutMs: TRANSFER_TIMEOUT_MS,
        signal,
      }),

    listJobs: (tenantId: string, filters: JobFilters, signal?: AbortSignal) =>
      client.request<JobPage>({
        path: "/jobs",
        tenantId,
        query: { page_size: PAGE_SIZE.jobs, ...filters },
        signal,
      }),
    submitJob: (tenantId: string, spec: JobSpec, options: MutationOptions) =>
      client.request<Job>({ method: "POST", path: "/jobs", tenantId, json: { spec }, ...options }),
    getJob: (tenantId: string, jobId: string, signal?: AbortSignal) =>
      client.request<Job>({ path: `/jobs/${encodeURIComponent(jobId)}`, tenantId, signal }),
    getProgress: (tenantId: string, jobId: string, signal?: AbortSignal) =>
      client.request<ProgressRecord>({ path: `/jobs/${encodeURIComponent(jobId)}/progress`, tenantId, signal }),
    listAttempts: (tenantId: string, jobId: string, signal?: AbortSignal) =>
      client.request<AttemptPage>({
        path: `/jobs/${encodeURIComponent(jobId)}/attempts`,
        tenantId,
        query: { page_size: PAGE_SIZE.attempts },
        signal,
      }),
    listCheckpoints: (tenantId: string, jobId: string, signal?: AbortSignal) =>
      client.request<CheckpointPage>({
        path: `/jobs/${encodeURIComponent(jobId)}/checkpoints`,
        tenantId,
        query: { page_size: PAGE_SIZE.checkpoints },
        signal,
      }),
    listEvents: (tenantId: string, jobId: string, afterSequence: number | null, signal?: AbortSignal) =>
      client.request<EventPage>({
        path: `/jobs/${encodeURIComponent(jobId)}/events`,
        tenantId,
        query: { page_size: PAGE_SIZE.events, after_sequence: afterSequence },
        signal,
      }),
    getResult: (tenantId: string, jobId: string, signal?: AbortSignal) =>
      client.request<ResultRecord>({ path: `/jobs/${encodeURIComponent(jobId)}/result`, tenantId, signal }),
    controlJob: (
      tenantId: string,
      jobId: string,
      action: ControlAction,
      body: ControlRequest,
      options: GuardedMutationOptions,
    ): Promise<ApiResponse<Job>> =>
      Promise.resolve().then(() =>
        client.request<Job>({
          method: "POST",
          path: `/jobs/${encodeURIComponent(jobId)}/${action}`,
          tenantId,
          json: body,
          ...options,
          ifMatch: requireIfMatch(options.ifMatch),
        }),
      ),
    retryJob: (
      tenantId: string,
      jobId: string,
      body: RetryRequest,
      options: GuardedMutationOptions,
    ): Promise<ApiResponse<Job>> =>
      Promise.resolve().then(() =>
        client.request<Job>({
          method: "POST",
          path: `/jobs/${encodeURIComponent(jobId)}/retry`,
          tenantId,
          json: body,
          ...options,
          ifMatch: requireIfMatch(options.ifMatch),
        }),
      ),

    submitSweep: (tenantId: string, body: SweepSubmitRequest, options: MutationOptions) =>
      client.request<Sweep>({ method: "POST", path: "/sweeps", tenantId, json: body, ...options }),
    getSweep: (tenantId: string, sweepId: string, cursor: string | null, signal?: AbortSignal) =>
      client.request<Sweep>({
        path: `/sweeps/${encodeURIComponent(sweepId)}`,
        tenantId,
        query: { page_size: PAGE_SIZE.sweepChildren, cursor },
        signal,
      }),
  };
}

export type Endpoints = ReturnType<typeof createEndpoints>;
