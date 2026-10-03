// Typed wrappers for the /v1 routes the UI calls. Admin routes (B18) are under `admin` and never
// send a tenant header.
import type { ApiClient, ApiResponse } from "./client";
import { PAGE_SIZE, TRANSFER_TIMEOUT_MS } from "./limits";
import type {
  AllocationPage,
  AllocationState,
  Artifact,
  ArtifactKind,
  ArtifactPage,
  AttemptPage,
  AuditPage,
  BrowserSession,
  CheckpointPage,
  ControlRequest,
  EventPage,
  FairnessReport,
  GlobalPolicy,
  GlobalPolicyUpdate,
  Job,
  JobPage,
  JobSpec,
  JobState,
  Membership,
  MembershipPage,
  MembershipWriteRequest,
  ProgressRecord,
  ResultRecord,
  RetryRequest,
  Sweep,
  SweepSubmitRequest,
  Template,
  Tenant,
  TenantCreateRequest,
  TenantPage,
  TenantPolicy,
  TenantPolicyUpdate,
  TenantUpdateRequest,
  TokenCreateRequest,
  TokenCreated,
  TokenPage,
  User,
  UserArtifactKind,
  UserCreateRequest,
  UserPage,
  UserUpdateRequest,
  WaitingReason,
  Worker,
  WorkerPage,
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

export interface AdminJobFilters {
  tenant_id: string | null;
  user_id: string | null;
  state: JobState | null;
  waiting_reason: NonNullable<WaitingReason> | null;
  created_after: string | null;
  cursor: string | null;
}

export interface FairnessQuery {
  from: string;
  to: string;
  bucket_seconds: number;
  tenant_id: string | null;
}

export interface RangeQuery {
  from: string;
  to: string;
  cursor: string | null;
}

export type WorkerActionName = "drain" | "disable" | "enable";

function requireIfMatch(ifMatch: string): string {
  if (!ifMatch) throw new Error("If-Match is required for guarded mutations");
  return ifMatch;
}

/** Guarded write: If-Match checked before anything is sent (no 428 from the UI). */
function guarded<T>(client: ApiClient, request: Parameters<ApiClient["request"]>[0], options: GuardedMutationOptions) {
  return Promise.resolve().then(() =>
    client.request<T>({ ...request, ...options, ifMatch: requireIfMatch(options.ifMatch) }),
  );
}

const id = encodeURIComponent;

/** /v1/admin/*: SYSTEM_ADMIN only; every request, reads included, writes an audit row. */
function createAdminEndpoints(client: ApiClient) {
  return {
    getPolicy: (signal?: AbortSignal) => client.request<GlobalPolicy>({ path: "/admin/policy", signal }),
    updatePolicy: (body: GlobalPolicyUpdate, options: GuardedMutationOptions) =>
      guarded<GlobalPolicy>(client, { method: "PATCH", path: "/admin/policy", json: body }, options),

    listWorkers: (cursor: string | null, signal?: AbortSignal, pageSize: number = PAGE_SIZE.admin) =>
      client.request<WorkerPage>({ path: "/admin/workers", query: { page_size: pageSize, cursor }, signal }),
    getWorker: (workerId: string, signal?: AbortSignal) =>
      client.request<Worker>({ path: `/admin/workers/${id(workerId)}`, signal }),
    workerAction: (
      workerId: string,
      action: WorkerActionName,
      body: { reason: string },
      options: GuardedMutationOptions,
    ): Promise<ApiResponse<Worker>> =>
      guarded<Worker>(client, { method: "POST", path: `/admin/workers/${id(workerId)}/${action}`, json: body }, options),
    listAllocations: (state: AllocationState, signal?: AbortSignal) =>
      client.request<AllocationPage>({
        path: "/admin/allocations",
        query: { page_size: PAGE_SIZE.allocations, state },
        signal,
      }),

    listJobs: (filters: AdminJobFilters, signal?: AbortSignal) =>
      client.request<JobPage>({ path: "/admin/jobs", query: { page_size: PAGE_SIZE.admin, ...filters }, signal }),
    getJob: (jobId: string, signal?: AbortSignal) => client.request<Job>({ path: `/admin/jobs/${id(jobId)}`, signal }),

    listTenants: (cursor: string | null, signal?: AbortSignal, pageSize: number = PAGE_SIZE.admin) =>
      client.request<TenantPage>({ path: "/admin/tenants", query: { page_size: pageSize, cursor }, signal }),
    getTenant: (tenantId: string, signal?: AbortSignal) =>
      client.request<Tenant>({ path: `/admin/tenants/${id(tenantId)}`, signal }),
    createTenant: (body: TenantCreateRequest, options: MutationOptions) =>
      client.request<Tenant>({ method: "POST", path: "/admin/tenants", json: body, ...options }),
    updateTenant: (tenantId: string, body: TenantUpdateRequest, options: GuardedMutationOptions) =>
      guarded<Tenant>(client, { method: "PATCH", path: `/admin/tenants/${id(tenantId)}`, json: body }, options),
    getTenantPolicy: (tenantId: string, signal?: AbortSignal) =>
      client.request<TenantPolicy>({ path: `/admin/tenants/${id(tenantId)}/policy`, signal }),
    updateTenantPolicy: (tenantId: string, body: TenantPolicyUpdate, options: GuardedMutationOptions) =>
      guarded<TenantPolicy>(
        client,
        { method: "PATCH", path: `/admin/tenants/${id(tenantId)}/policy`, json: body },
        options,
      ),

    /** ETag = MembershipSet version of the tenant: the If-Match for add/change/remove. */
    listMemberships: (tenantId: string, cursor: string | null, signal?: AbortSignal) =>
      client.request<MembershipPage>({
        path: `/admin/tenants/${id(tenantId)}/memberships`,
        query: { page_size: PAGE_SIZE.admin, cursor },
        signal,
      }),
    upsertMembership: (tenantId: string, body: MembershipWriteRequest, options: GuardedMutationOptions) =>
      guarded<Membership>(
        client,
        { method: "POST", path: `/admin/tenants/${id(tenantId)}/memberships`, json: body },
        options,
      ),
    deleteMembership: (tenantId: string, userId: string, options: GuardedMutationOptions) =>
      guarded<null>(
        client,
        { method: "DELETE", path: `/admin/tenants/${id(tenantId)}/memberships/${id(userId)}` },
        options,
      ),

    listUsers: (cursor: string | null, signal?: AbortSignal, pageSize: number = PAGE_SIZE.admin) =>
      client.request<UserPage>({ path: "/admin/users", query: { page_size: pageSize, cursor }, signal }),
    getUser: (userId: string, signal?: AbortSignal) => client.request<User>({ path: `/admin/users/${id(userId)}`, signal }),
    createUser: (body: UserCreateRequest, options: MutationOptions) =>
      client.request<User>({ method: "POST", path: "/admin/users", json: body, ...options }),
    updateUser: (userId: string, body: UserUpdateRequest, options: GuardedMutationOptions) =>
      guarded<User>(client, { method: "PATCH", path: `/admin/users/${id(userId)}`, json: body }, options),

    queryFairness: (query: FairnessQuery, signal?: AbortSignal) =>
      client.request<FairnessReport>({ path: "/admin/fairness", query: { ...query }, signal }),
    listRecoveryEvents: (query: RangeQuery, signal?: AbortSignal, pageSize: number = PAGE_SIZE.admin) =>
      client.request<EventPage>({ path: "/admin/recovery-events", query: { page_size: pageSize, ...query }, signal }),
    listAudit: (query: RangeQuery & { action: string | null }, signal?: AbortSignal) =>
      client.request<AuditPage>({ path: "/admin/audit", query: { page_size: PAGE_SIZE.admin, ...query }, signal }),
  };
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

    admin: createAdminEndpoints(client),
  };
}

export type Endpoints = ReturnType<typeof createEndpoints>;
