# Domain and persistence contract

Dẫn xuất từ [PLAN](../../PLAN.md) §2–§5, §7–§10 và [contract index](../contracts.md). Đây là logical model B01; B05 sở hữu kiểu SQL/DDL cụ thể nhưng phải thực thi các key, unique, ownership, CAS và index group dưới đây.

## Nguyên tắc

- PostgreSQL là authority cho identity, authorization, state, queue, allocation, lease, counter, ledger, idempotency, audit/event và artifact metadata. Filesystem chỉ giữ blob bất biến đã commit.
- Mọi entity tenant-scoped có `tenant_id`; reference tenant-scoped dùng composite ownership `(tenant_id, referenced_id)` hoặc constraint tương đương, không chỉ kiểm tra trong UI.
- UUIDv7 do server sinh; timestamp từ DB khi quyết định lease/linearization. Mọi mutable aggregate có `version bigint >=1` dùng CAS/row lock.
- `BrowserSession` là credential đăng nhập. `LogicalSession` là identity thực thi 1:1 với job. Hai entity không có FK hoặc state machine chung.
- Soft-delete không dùng cho resource correctness. Revocation/disable là state có audit; blob chỉ bị GC theo reference contract.

## Identity và authorization

| Entity | Fields bắt buộc | Key/quan hệ | Constraint và lifecycle |
|---|---|---|---|
| `Tenant` | `tenant_id`, `slug`, `display_name`, `enabled`, `version`, timestamps | PK `tenant_id`; unique case-folded `slug` | Không xóa khi còn reference; disable chặn admission mới, không đổi ownership cũ |
| `User` | `user_id`, normalized `username`, `display_name`, `password_hash`, `enabled`, `version`, timestamps | PK; unique case-folded username | Argon2id hash; disable revoke session/token trong cùng administrative workflow |
| `MembershipSet` | `tenant_id`, `version`, timestamps | PK/FK tenant; exactly one row created at Tenant version 1 | Concurrency aggregate for the whole membership collection; list ETag exists even when empty; every create/update/delete increments once under this row lock |
| `Membership` | `tenant_id`, `user_id`, `role`, timestamps | PK/unique `(tenant_id,user_id)`; FK tenant/user | Role chỉ `MEMBER/TENANT_ADMIN`; mutation CAS uses MembershipSet version, not a nonexistent/new member row; không thể cấp quyền global qua tenant membership |
| `SystemRoleGrant` | `user_id`, `role`, `version`, granted actor/time, revoked time | PK/unique `(user_id,role)`; FK user | V1 chỉ có `SYSTEM_ADMIN`; bootstrap tạo grant đầu tiên, mutation sau đó dùng user version/If-Match và audit; không được revoke/disable system admin enabled cuối cùng |
| `BrowserSession` | `browser_session_id`, `user_id`, `secret_hash`, `csrf_secret_hash`, `expires_at`, `revoked_at`, `last_seen_at`, timestamps | PK; FK user; unique secret hash | Opaque cookie; expiry/revoke authoritative. Không phải logical execution session |
| `CliToken` | `token_id`, `user_id`, `token_hash`, `name`, scopes, `expires_at`, `revoked_at`, timestamps | PK; unique token hash; FK user | Raw token trả một lần; scope không thể vượt quyền hiện tại; membership vẫn recheck mỗi request |
| `WorkerCredential` | `credential_id`, `worker_id`, `credential_hash`, scopes, `expires_at`, `revoked_at`, timestamps | PK; unique hash; FK worker | Chỉ identity local; rotate không đổi `worker_id`; workload không nhận credential |

## Template, input và immutable job specification

| Entity | Fields bắt buộc | Key/quan hệ | Constraint và lifecycle |
|---|---|---|---|
| `Template` | `template_id`, `current_version`, display metadata, `enabled` | PK stable string | Chỉ admin quản lý; ID thuộc bốn template B01 |
| `TemplateVersion` | `template_id`, `version`, parameter schema, resource/capability bounds, adapter ID/version, image digest, checkpoint flags, timestamps | PK `(template_id,version)` | Immutable khi có job tham chiếu; image là digest, không tag |
| `Artifact` | `artifact_id`, `tenant_id`, kind, declared media type, size, checksum, `blob_key`, state, version, timestamps | PK; unique `(tenant_id,checksum,kind,media_type)` có thể dedup metadata; blob key nội bộ | Client không gửi/nhận path. Transport luôn octet-stream; media type là metadata đã allowlist-validate và nằm trong idempotency hash. Chỉ `COMMITTED` được tham chiếu; blob immutable |
| `JobSpec` | `job_id`, canonical JSON, `spec_checksum`, template/version, input/model artifact IDs, resource vector, priority, runtime/checkpoint limits | PK/FK job; composite tenant FKs tới artifact | Immutable sau submit; canonical hash theo contract index; digest/parameters không sửa qua retry |

`TemplateVersion` và `JobSpec` giữ snapshot đủ để replay/restore dù catalog về sau disable hoặc có version mới. Disable template chặn job mới, không vô hiệu job đã accepted nếu image/adapter/capability vẫn tương thích.

## Job, logical session, attempt và lineage

| Entity | Fields bắt buộc | Key/quan hệ | Constraint và lifecycle |
|---|---|---|---|
| `Job` | `job_id`, `tenant_id`, submitter, state, desired state, waiting reason, optional recovery intent, `version`, `job_fence`, event sequence, retry count/max, `retry_of_job_id`, timestamps | PK; FK tenant/user; optional same-tenant self-FK retry source | Terminal immutable; recovery intent is null or `CHECKPOINT_FOR_PAUSE`; fence monotonic; exactly one JobSpec and LogicalSession |
| `LogicalSession` | `session_id`, `tenant_id`, `job_id`, created timestamp | PK; unique `(tenant_id,job_id)` and `(tenant_id,session_id)` | Không có mutable state; API derives state từ Job |
| `Attempt` | `attempt_id`, tenant/job, attempt number, state, execution intent, worker/incarnation/allocation/lease, `job_fence`, startup nonce, failure class/reason, progress sequence/snapshot, start/end timestamps | PK; unique `(job_id,attempt_number)`; unique startup nonce | Startup nonce is server-generated and committed with dispatch before any claim, then replayed unchanged in ExecutionContext/reconciliation. Partial unique: tối đa một attempt ở state authorized `CLAIMED/STARTING/RUNNING/CHECKPOINTING/STOPPING` mỗi job; progress monotonic within this attempt only |
| `RetrySchedule` | job, retry number, `ready_at`, jitter sample, reason | PK `(job_id,retry_number)` | Chỉ automatic infrastructure recovery; tối đa 2 sau attempt đầu; resume/manual retry không dùng row này |
| `SweepParent` | `sweep_id`, tenant/user, base spec hash, idempotency scope, child/accepted/rejected counts, timestamps | PK; no allocation FK | Tracking group, không phải job và không giữ execution slot |
| `SweepChild` | sweep, child index, parameter hash, accepted job ID hoặc rejected error snapshot | PK `(sweep_id,child_index)`; unique `(sweep_id,parameter_hash)`; same-tenant job FK | Tối đa 100; immutable outcome; replay dùng mapping này |

`retry_of_job_id` chỉ dùng manual retry từ `FAILED`. Automatic recovery và resume tạo Attempt mới trong cùng Job/LogicalSession. Checkpoint inheritance dùng `CheckpointReference`, không đổi ownership/blob metadata.

## Worker, leadership, allocation và lease

| Entity | Fields bắt buộc | Key/quan hệ | Constraint và lifecycle |
|---|---|---|---|
| `Worker` | `worker_id`, admin state, health, current incarnation, inventory version, last heartbeat/ready, version | PK; v1 có đúng một configured local identity | `DRAINING/DISABLED` chặn allocation mới; READY chỉ sau reconcile |
| `WorkerIncarnation` | server-generated incarnation ID, worker, sequence, process-start nonce, process start, reconcile/ready timestamps, ended timestamp | PK; unique `(worker_id,sequence)` and `(worker_id,process_start_nonce)`; one current | Tạo dưới Worker lock qua idempotent operation; sequence tăng đơn điệu; singleton lock local; incarnation cũ không renew/publish |
| `WorkerInventory` | worker/incarnation, architecture, host and allocatable CPU/RAM, Docker/OCI/cgroups/kernel/seccomp, exact adapter/image/framework/device/CUDA capabilities, checksum, observed time | PK by inventory version | Closed discovered declaration, không client-controlled; missing matching capability means unsupported; reserve áp dụng trước allocatable |
| `GpuDevice` | worker/inventory version, GPU UUID, model, bytes, compute capability, driver/API version, health | PK `(worker_id,gpu_uuid,inventory_version)` | Allocation chỉ theo UUID discovered healthy and compatible; coordinator chooses deterministic free UUID under locks |
| `CoordinatorLeadership` | singleton key, holder, epoch, lease expiry, renew timestamp | Singleton PK | Epoch tăng khi acquire sau expiry; DB time; không nằm trong attempt renew authority |
| `Allocation` | allocation, tenant/job/attempt/worker, resource vector, GPU UUIDs, state, held/quarantine/release times, release reason | PK; unique attempt; composite owner FKs | Partial unique GPU UUID trên allocation chưa `RELEASED`; HELD và QUARANTINED đều charge/capacity/quota |
| `AttemptLease` | lease, attempt, worker/incarnation, job fence, issued/expiry/revoked times, revoke reason | PK; unique active lease per attempt | Expiry theo DB time; revoke không release allocation |
| `AttemptAuthorityGrant` | attempt, worker/incarnation, allocation, lease, job fence, predecessor grant nullable, grant/adopt callback, granted/ended timestamps | PK `(attempt_id,worker_incarnation_id)`; immutable predecessor chain; exact composite FKs | Dispatch creates the first grant; successful same-Attempt adoption appends one successor while preserving attempt/allocation/lease/fence. Current grant alone may renew/publish. A completed artifact-upload replay from a predecessor is allowed only after current-Authority authentication and exact immutable upload-metadata match; lineage never grants new upload/publish authority to an old incarnation |
| `ExecutorAttemptRecord` | attempt/allocation, startup nonce, operation sequence, state (`PREPARED/CREATE_IN_FLIGHT/CREATED/TOMBSTONED`), container identity nullable, tombstone sequence, inspection checksum, durable runner receive/control sequences with payload hashes/acks, pending checkpoint binding, pending result completion-token/message-sequence/callback/reservation binding, staged/final-descriptor hashes and upload keys, committed Artifact bindings, binding-set checksum, timestamps | PK attempt; unique startup nonce; one pending checkpoint and one pending result binding/attempt; unique staged name/logical name/artifact ID within each binding set | Local durable executor record guarded by per-attempt lock. Reconciliation may initialize it directly as `TOMBSTONED` at operation/tombstone sequence 1 for a server-corroborated revoked `UNCLAIMED` attempt; no Docker create occurs. Otherwise tombstone rejects delayed create/start with older/equal sequence and supports no-container proof. Runner replay state makes lost IPC/HTTP acknowledgments reuse the same sequence, payload and reservation. Each file, auxiliary-manifest and final-manifest committed binding is persisted only from an exact `workerUploadAttemptArtifact` response; file bindings are replayed to runner before final manifest creation, while the final binding supplies the publish/complete manifest artifact ID. Same-Attempt adoption preserves these bindings and compares them with transferred server reservations before continuation; ordinary revoke/cleanup abandons them and cannot authorize publish |
| `ContainerIdentity` | attempt/allocation, startup nonce, executor create sequence, container ID, runtime identity digest, created/stopped/verified times | PK `(attempt_id,container_id)` | Cleanup phải match cả ID và digest; absence uses corroborated executor tombstone/inspection, không tin process-exit report đơn lẻ |

Ba miền không thay thế nhau: coordinator `epoch` bảo vệ quyết định scheduling; worker `incarnation_id` bảo vệ callback từ agent; `job_fence` bảo vệ execution authority của job. Attempt khỏe không mất quyền chỉ vì coordinator epoch đổi.

`ExecutionContext` returned at claim is a deterministic immutable projection under the same Job/Attempt/Allocation locks: logical session, JobSpec, referenced TemplateVersion snapshot, adapter/image digest, Allocation including concrete GPU UUID, input/model/dataset Artifact descriptors, selected committed restore checkpoint/manifest/file descriptors, execution intent and the startup nonce already committed at dispatch. Callback acknowledgment stores the exact response snapshot for replay. Worker content authorization traverses only these persisted reference edges for the exact current Authority; worker never queries PostgreSQL directly or uses tenant routes.

## Policy, admission và fairness

| Entity | Fields bắt buộc | Key/quan hệ | Constraint và lifecycle |
|---|---|---|---|
| `PolicyVersion` | global version, `global_outstanding_limit`, operational mode, created actor/time | PK version; one current pointer | Immutable version; default global limit 100000 and mode NORMAL; guarded NORMAL/ADMISSION_OFF/WRITE_FROZEN transitions control admission, dispatch and maintenance freeze; frozen mode permits only existing-SA session/rate metadata, append-only read audit, domain-free replay and guarded recovery transition |
| `TenantPolicy` | tenant, version, weight, resource/outstanding/concurrency limits, tenant submit rate/burst, per-user submit rate/burst, timestamps | PK `(tenant_id,version)`; one current pointer | Defaults tenant 5/s burst 20, user 2/s burst 10; weight >0; update dưới held allocation/counter bị từ chối |
| `AdmissionCounter` | scope type/id, outstanding, active attempts, version, updated time | PK `(scope_type,scope_id)` | Global/tenant/user rows khóa theo thứ tự; paused vẫn outstanding |
| `RateBucket` | scope, tokens fixed-point, capacity, refill rate, last DB time, version | PK scope | Durable token bucket; replay không consume |
| `FairnessLedger` | tenant, virtual score fixed-point, accounted-through DB time, active flag, version | PK tenant | Charge held allocation dominant share/weight; tick ≤1 s; never reset on restart |
| `AllocationLedgerSegment` | allocation, tenant, start/end DB time, dominant share, weight, charged amount | PK segment | Append/close transactionally at allocation/policy/release boundaries; restart reconciliation prevents double charge |
| `Reservation` | singleton local slot, tenant/job, eligibility since, created/invalidated times/reason, policy version | Singleton unique where active | Tối đa một active; selected tenant vẫn theo weighted score; invalidation audited |
| `QueueHead` | tenant/priority, candidate job, ready sequence, eligibility/retry/aging keys | Rebuildable projection/index-backed | Không authoritative; reconcile from Job/Policy/Ledger |

Virtual score uses fixed-point decimal/numeric with deterministic rounding selected by B05 and property-tested against rational reference. B05 may choose storage precision but cannot change ordering/tie-break semantics.

## Event, audit và idempotency

| Entity | Fields bắt buộc | Key/quan hệ | Constraint và lifecycle |
|---|---|---|---|
| `Event` | event ID, tenant/job optional, per-job sequence, type, reason, actor, safe metadata, timestamp | PK; unique `(job_id,sequence)` | Append in same transaction as transition; no secret/input/checkpoint bytes |
| `AuditRecord` | audit ID, actor, tenant, action, target, before/after version, reason, timestamp | PK | Same transaction as privileged mutation; append-only |
| `IdempotencyRecord` | context namespace, principal, operation, key, request hash, normalized upload metadata and original Authority when applicable, `PENDING/COMPLETED`, secret-free response status/body/header snapshot, resource ID, expiry, timestamps | unique scope tuple | Public tenant UUID, `GLOBAL`, `BOOTSTRAP` or `WORKER:<worker_id>` namespace; worker upload tenant is resolved from exact live Authority before lookup. Auth/current credential and required authority are rechecked before replay. Exact hash normally must match; the only Authority-hash exception is completed same-Attempt artifact upload after successful adoption: current Authority must descend from stored Authority and attempt/allocation/lease/fence plus immutable upload metadata must match exactly, while key/response/resource remain unchanged. Completed replay precedes If-Match; one-time secret rows store only locator/delivery metadata; active resource extends retention |
| `CallbackReceipt` | worker, operation, callback ID, payload hash, acknowledgment snapshot, timestamp | unique `(worker_id,operation,callback_id)` | Same-payload replay returns same ack; different payload conflict |

## Checkpoint, result, log và references

| Entity | Fields bắt buộc | Key/quan hệ | Constraint và lifecycle |
|---|---|---|---|
| `CheckpointReservation` | server checkpoint ID, tenant/job/attempt/authority, reserved sequence, callback, state, reserved time | PK checkpoint ID; unique callback; unique `(job_id,sequence)` across reservations; at most one `RESERVED`/attempt | Reserved under Job lock from a monotonic never-reused counter. Successful same-Attempt adoption atomically rebinds Authority while preserving ID/sequence/callback; deterministic publish rejection marks `REJECTED`, and an authority-revoking failure/control/reaper transaction marks `ABANDONED`. Gaps are never visible to restore and identity cannot be repurposed; only exact publish may change to `COMMITTED` |
| `Checkpoint` | checkpoint, tenant/job/attempt, sequence, manifest artifact/checksum, provenance/compatibility summary, state, created time | PK/FK committed reservation; unique `(job_id,sequence)` | Only fenced `COMMITTED` restorable; retain newest two committed minimum |
| `CheckpointReference` | tenant, source checkpoint, target job, reason, created time | PK; unique `(target_job_id,source_checkpoint_id)` | Same-tenant; manual retry only after compatibility validation; source blob stays referenced |
| `ResultReservation` | server result ID, tenant/job/attempt/authority, callback, state, superseded time, reserved time | PK result ID; unique callback; at most one ACTIVE reservation/job | Replayable identity preparation; same live Authority cannot create a second reservation. Successful same-Attempt adoption atomically rebinds Authority while preserving ID/callback. After any other authority revoke and before Result commit, only a later Attempt may atomically mark the old reservation ABANDONED and create a new active ID. Abandoned ID cannot be reused or committed |
| `Result` | result, tenant/job/attempt, manifest artifact/checksum, created time | PK/FK committed reservation; unique `job_id` | Fenced final publish; immutable; one recognized final result |
| `RecognizedChunk` | tenant/job/session, chunk ID/range, exact result artifact/checksum, source attempt/fence, recognized time | PK/unique `(job_id,chunk_id)`; composite FKs to job/session/source attempt/artifact | Created only by fenced checkpoint/result publish for a current-Authority chunk using insert-or-verify; exact duplicates reuse, conflicts reject; prior attempts may be carried forward only through an existing exact row and can never be invented/retargeted |
| `LogSegment` | tenant/job/attempt, byte start/end, artifact/checksum, truncated flag, created time | PK; unique `(attempt_id,start_offset)` | Bounded total; no path exposed; immutable committed segment |
| `ArtifactReference` | artifact, tenant, owner type/id, purpose, logical name, created time | Composite PK; unique `(owner_type,owner_id,purpose,logical_name)` | GC reachability authority; composite same-tenant constraint; direct checkpoint/result files bind current-attempt artifacts, while chunk-manifest edges may bind immutable `RecognizedChunk` artifacts from prior attempts of the same job/session with exact source fence |
| `UploadSession` | upload ID, tenant/attempt optional, expected size/checksum, staged key, bytes received, expiry, state | PK | Active staging protection; interrupted upload expires; not an Artifact until durable commit |

Manifest structure is canonical in [workload schema](schemas/workload-manifests.schema.json). Metadata transaction never points to a blob before checksum/fsync/rename/directory fsync has completed.

## Authoritative, derived và cache

| Data | Class | Rebuild/reconcile rule |
|---|---|---|
| Job/attempt/allocation/lease/policy/counter/ledger/idempotency/event/audit/artifact metadata | PostgreSQL authoritative | Never infer correctness from cache/metrics/container list alone |
| Immutable input/checkpoint/result/log bytes | Durable filesystem authoritative bytes, PostgreSQL authoritative references | Checksum both sides; orphan is unreferenced bytes, missing referenced blob is readiness/recovery failure |
| Session displayed state, waiting reason, fairness aggregate, worker health | Derived/materialized | Recompute from authoritative rows and DB time; waiting reason is not job state |
| Queue head, min-heap, scheduler candidate page, dashboard cache | Rebuildable cache | Discard on restart/mismatch and rebuild through bounded indexed scans |
| Prometheus metrics and JSON logs | Observability only | Never drive transition, lease expiry, ownership, quota or release |

## Required uniqueness/check groups

1. Composite tenant ownership on every job/session/attempt/checkpoint/result/artifact/sweep reference.
2. One logical session/job, one JobSpec/job, one recognized Result/job, one active authorized Attempt/job.
3. One allocation/attempt; one active lease/attempt; no GPU UUID on two unreleased allocations.
4. Monotonic `(job_id,event_sequence)`, attempt number, job fence, worker incarnation sequence, executor operation/tombstone sequence, policy version and checkpoint reservation sequence.
5. Idempotency and callback unique scopes exactly as defined above.
6. Resource/count checks non-negative; allocation resource vector equals authorized request; released timestamp iff state RELEASED.
7. One MembershipSet/tenant with version starting 1; membership mutation and audit use the same collection-version transaction.
8. Terminal Job cannot update state/desired state/spec; retry source and sweep links must be same tenant.

## Index groups B05 must provide

- Queue partial indexes on `state`, `tenant_id`, effective priority inputs, `ready_sequence`, `retry_ready_at`, eligible-since; separate oldest-eligible lookup for reservation.
- Allocation partial indexes for unreleased tenant/worker usage and GPU UUID exclusivity.
- Lease indexes on active expiry; worker heartbeat/admin/health; retry schedule ready time.
- Job/event keyset indexes matching API ordering; artifact/checkpoint/result ownership/download lookup.
- Idempotency scope/expiry and callback receipt unique indexes; rate/counter/ledger rows by lock order.
- Audit/recovery time/action indexes and bounded aggregate ledger indexes. No per-job metric label index is implied.

Exact PostgreSQL names, partitioning and numeric precision are B05 implementation details; query plans must later satisfy ACC-11 without weakening this model.
