# Concurrency, recovery, authorization and failure contract

Dẫn xuất từ [PLAN](../../PLAN.md) §2–§6, §8–§10 và [invariants](../invariants.md). PostgreSQL commit là linearization point cho accepted control-plane facts. Docker/filesystem/network side effects không được chạy trong DB transaction.

## Transaction discipline

Isolation may use `READ COMMITTED` plus explicit row locks/CAS/unique constraints or stricter isolation. Implementation choice is B05/B11-owned, but observable behavior and lock order below are mandatory.

### Global lock order

Transactions acquire only needed groups, always in this order; multiple IDs within a group sort by canonical byte order:

1. Existing idempotency or callback-receipt scope row (or unique insert/savepoint for a new key).
2. Coordinator leadership singleton when the operation is leader-owned.
3. Global policy, tenant policy, then admission/rate counters: global → tenant → user.
4. Worker current-incarnation/inventory/admin-state row.
5. Job row(s), then logical session; multiple jobs sorted.
6. Attempt row(s), then active lease.
7. Allocation row(s), then GPU UUID lock/advisory key sorted.
8. Fairness ledger, allocation segment and singleton reservation.
9. Artifact/checkpoint/result/reference metadata.

Cleanup/reaper/control never begin by locking allocation before job. Sweep child admission processes each child through the same order and does not lock 100 jobs as one monolithic transaction. Deadlock/serialization abort retries the whole transaction at most three times with fresh bounded 10–50 ms jitter; persistent conflict returns `503 dependency_unavailable`, `Retry-After: 1`, and idempotency prevents duplicated effects.

### Linearization table

| Operation | Commit is official when | Protection | Atomic writes | External convergence |
|---|---|---|---|---|
| Login/token/bootstrap | credential hash/session row + audit committed | unique username/token hash, rate row, bootstrap state | credential metadata, revoke/create audit, secret-free idempotency locator | Session delivery follows normal response. Raw CLI/worker secret is never persisted: same-key replay returns `one_time_secret_unavailable`; CLI caller revokes unknown token, worker new-key retry rotates the unknown current credential |
| Worker incarnation | new current incarnation commit | worker credential, Worker row lock, idempotency key/process nonce, monotonic sequence | server-generated ID, sequence +1, prior-ended/current pointer, STARTING state, replay response | New process inventories/reconciles afterward; every old-incarnation callback is stale except restricted cleanup proof |
| Artifact upload | metadata/reference + idempotency committed **after** durable blob | upload ownership, checksum/size, blob key uniqueness | Artifact COMMITTED, reference, exact `201 Artifact` response snapshot | Pre-commit blob is orphan; GC after TTL/reference recheck. Response or replay is the sole committed binding source passed back to runner |
| Job submit | Job/Session/Spec/event/counters/idempotency commit | auth, input/template ownership, policy/rate/counter locks, queue cap | All listed rows; no accepted ID exists before commit | `202` response loss replays same Job |
| Sweep child | each child Job commit or durable rejected outcome commit | parent/index uniqueness + ordinary submit locks | Child mapping plus ordinary submit effects | Resume unfinished indexes after crash; no duplicate child |
| Policy/operational-mode update | new version/current pointer/audit commit | If-Match, current policy, held counter/allocation and mode-transition guards | immutable policy version, pointer, audit, idempotency | Scheduler reloads/rechecks; admission/dispatch closes or reopens only after committed mode |
| Dispatch | Attempt/Allocation/Lease/event/counters/ledger/fence/startup-nonce commit | live leader, policy/job version, job desired state, quota/capacity/GPU uniqueness | Entire authority grant and server-generated immutable startup nonce in one transaction | Worker poll sees offer later; reconciliation can recover startup identity before claim; no Docker call before/inside commit |
| Claim/start | claim receipt/context, then start transition/callback commit | exact worker incarnation/attempt/allocation/fence/lease, desired/intent, startup nonce/sequence, container uniqueness | claim snapshots ExecutionContext; start sets DB lease expiry to commit DB time +45 s and stores original ack | Worker materializes only graph-authorized artifacts. Same start callback replays original expiry; deadline is applied only after timely valid ack |
| Adopt | incarnation/authority-lineage/reservation transfer, renew and callback commit | current STARTING incarnation, exact prior live authority/container, DB-time lease, local binding compatibility | Under Job→Attempt/Lease→reservation locks append immutable prior→current Authority grant, replace incarnation on Attempt/Lease and each active same-Attempt reservation, extend expiry, store new Authority plus transferred reservation snapshot/ack | Same callback replays the full snapshot. Reserve-before-adopt is rebound; adopt-before-request makes old Authority stale. A stable descriptor-key retry may replay an original completed Artifact response through exact lineage/metadata verification without mutating or scanning upload receipts. Local mismatch/failure/expiry/late response requires fail or stop+cleanup before READY |
| Renew | new lease expiry/progress/callback ack commit | DB time, exact authority, desired/intent; before progress exact `0/null`, afterward per-attempt sequence ≥1 and complete snapshot | lease/receipt plus no progress change for `0/null`, or accepted complete ProgressSnapshot for monotonic sequence | Runner deadline remains the last acknowledged value until timely valid ack; same sequence requires byte-equivalent snapshot; response delay/replay cannot rebase it |
| Checkpoint reservation | reservation/callback commit | exact live authority, Job lock, next sequence | server checkpoint ID + sequence, reservation only | Response loss replays exact reservation; abandoned reservation is not a Checkpoint and never restorable |
| Checkpoint publish | Checkpoint/reference/recognized-chunk/event/callback commit | durable blob, exact reservation, ownership, runner-finalized manifest built from committed Artifact bindings, live authority, chunk insert-or-verify and sequence uniqueness | metadata only after blob durable; reservation becomes committed checkpoint; new current-attempt chunks become immutable recognized rows atomically | Rejected/lost publish leaves reservation uncommitted and existing artifact/orphan GC-safe |
| Result reservation/complete | active reservation commit, then Result + recognized-chunk/reference + job/attempt terminal + event/counters/callback commit | live authority, desired RUNNING, exact active reserved ID, runner-finalized manifest built from committed Artifact bindings, current-chunk insert-or-verify, exact prior-chunk rows, unique Result/job | Reservation alone is not recognized; a later Attempt may supersede an abandoned revoked-authority reservation; completion and terminal state are one commit | Response loss replays each callback; abandoned/superseded reservation is not a Result; container cleanup/release is later |
| Failure report | typed failure/reason + revoke/fence/quarantine/event/callback commit | live authority, class/observation consistency, exact container identity or valid startup tombstone/NoContainerProof | Attempt STOPPING, Job RECOVERING, lease revoked, fence incremented, allocation QUARANTINED | Executor rejects delayed start; cleanup separately proves no container or exact stopped container before release/retry/terminal resolution |
| Cancel/pause/resume | desired/state/event/audit/idempotency commit | auth, If-Match, row lock, terminal guard | control fact and version/counter when applicable | Worker observes on renew; stop/checkpoint outside transaction |
| Lease reaper | revoke/fence/job RECOVERING/allocation QUARANTINED/event commit | DB time, job/lease/allocation locks, callback/reaper CAS | No capacity release | Worker/reconciler stops exact container; host silence retains quarantine |
| Cleanup/release | allocation RELEASED/ledger close/counter changes/job continuation/event/callback commit | cleanup identity, proof union, startup/container rows, release-once guard | Exact-once release and subsequent recovery transition | Inspection/tombstone occurs before; mismatch or create-in-flight returns without release |
| GC | delete claim/token commit, filesystem delete, finalize metadata | no reference/active upload/restore, min age, token freshness | Claim prevents new reference or reference transaction wins | Crash before delete retries; after delete before finalize reconciles by stat/checksum |
| Backup/restore mode | versioned `NORMAL→ADMISSION_OFF→WRITE_FROZEN` marker/audit commits | admin auth/If-Match; drain/pause/container-stop and allocation reconciliation guards | Operational-mode policy version and freeze/restore audit | DB/blob snapshot outside transaction; verified restore returns to ADMISSION_OFF, then readiness/reconcile permits NORMAL |

## Side-effect crash boundaries

- **DB commit before side effect:** durable intent is retried/reconciled. Dispatch offer before container start and stop intent before Docker stop are safe examples.
- **Side effect before DB acknowledgment:** caller must inspect immutable identity and replay callback. Container start report and blob publish use idempotency/identity; no blind second start/rename.
- **Blob before metadata:** only orphan bytes; not downloadable/restorable. Blob commit order is bounded staging → checksum/size → file fsync → atomic rename → directory fsync → metadata transaction.
- **Metadata before blob is forbidden.** A missing referenced blob is a readiness/recovery incident, not a reason to pretend success.
- Metrics/log export failures never roll back or decide correctness.

## Race outcomes

| Race | Required winner/constraint | Losing operation |
|---|---|---|
| Concurrent same idempotency key/same hash | Unique scope; one inserts/locks PENDING and commits response | Wait ≤5 s then replay, or `409 idempotency_in_progress`; no counter/rate double-spend |
| Same key/different hash | Existing request hash | `409 idempotency_conflict` before mutation |
| Concurrent allocator for one job | Job row + one-authorized-attempt unique constraint + fence CAS | Reload/no decision; no second allocation |
| Allocators for same capacity/GPU | Counter/allocation locks and partial GPU uniqueness | Transaction conflict/retry; never oversubscribe |
| Stale coordinator | Leadership lease/epoch rechecked at dispatch commit | Cannot commit decision; healthy existing attempt remains valid |
| Old worker incarnation | Worker current incarnation and callback authority | Renew/publish rejected; cleanup may use restricted cleanup route for exact old identity |
| Delayed Docker create vs pre-create cancel/failure | Local per-attempt lock + monotonic executor sequence + startup tombstone, then API fence | Delayed prepare/start at older/equal sequence is rejected; no unmanaged container and release occurs once after `NoContainerProof` |
| Cancel vs complete | Same Job lock; first commit fixes desired/terminal state | If cancel first, completion stale; if result first, cancel sees immutable SUCCEEDED and `409` |
| Pause vs crash | Same Job/Attempt/Lease locks | Crash/reaper moves RECOVERING while desired PAUSED; PAUSED only after valid checkpoint + cleanup |
| Reaper vs renew | Lease row + DB time/CAS | Renew first extends lease; reaper rechecks. Reaper first revokes/fences; renew stale |
| Reaper vs complete/checkpoint | Job/Attempt/Lease lock + callback receipt | One commit wins; loser stale. Orphan blob is GC-safe |
| Duplicate cleanup/release | Allocation release-once guard + callback receipt | Returns prior ack; no double counter/ledger close |
| GC vs upload | Active UploadSession/age + GC claim token | Active/new upload prevents claim; expired staging can be removed |
| GC vs restore/reference | Artifact reference lock + GC claim | Reference transaction or GC claim wins; restore never opens an artifact committed as deleted |
| Manual retry duplicates | Source state + idempotency unique response | Same request returns same new Job; no second lineage |
| Sweep response loss | Parent/idempotency + child-index uniqueness | Replay returns same accepted/rejected mapping |

## Lease, deadline and cleanup

DB time alone decides attempt lease validity. Lease duration 45 s, renew interval 5 s. Before the **first** HTTP send for a start/adopt/renew callback ID, worker records immutable `first_send_monotonic` and candidate deadline `first_send_monotonic + 45s - 5s`. The worker does not send that candidate to the runner yet. Only after a valid API acknowledgment is received while local monotonic time is still before the candidate may worker send `SET_AUTHORITY_DEADLINE`; runner acknowledges that control sequence and retains the last acknowledged deadline otherwise.

Mandatory timelines:

1. Renew failure: prior runner deadline is `D0`; callback `C1` is first sent at `S1`, candidate `D1=S1+40s`; network/DB fails. No API acknowledgment and no runner update occur. Runner stops at `D0`.
2. Delayed response: `C2` first sent at `S2`, API commits, but response arrives at/after `D2=S2+40s`. Worker discards it for local continuation, sends no deadline update, and stops/reconciles; the DB lease fact cannot revive elapsed runner authority.
3. Same-callback replay: response to `C3` is lost. Retry uses the original `S3`; API returns the stored acknowledgment without a new lease extension. If received before `D3`, worker may apply exactly `D3`; it never computes from retry-send or receive time. If received after `D3`, it behaves as delayed response.

The same rule applies to start and adoption acknowledgments. An API commit may be authoritative in PostgreSQL while the local runner still stops conservatively; recovery reconciles that state without manufacturing extra runtime authority.

Heartbeat health (5/15/30 s) and lease are different signals. SUSPECT/UNAVAILABLE may trigger inspection/fencing policy, but only DB lease/revoke transition removes publish authority. Neither lease expiry, process-exit report, heartbeat loss nor coordinator change proves container stopped. Reaper revokes/fences and quarantines; exact cleanup/reconcile proof releases.

Cleanup endpoint accepts a revoked attempt's narrowly scoped identity/proof so a stale worker cannot publish/renew but can help free capacity. `ContainerStoppedProof` includes startup nonce/executor sequence, exact container ID/runtime digest, stop time, exit code and inspection checksum. `NoContainerProof` includes the dispatch-committed startup nonce, executor sequence, a tombstone sequence at or after it and inspection checksum proving the local attempt lock has no create in flight and Docker has no matching managed identity. For `claim_state=UNCLAIMED`, API additionally verifies no claim receipt, start receipt or container identity was ever recorded before accepting a reconciliation-initialized sequence-1 tombstone. If API cannot corroborate the startup/container record, returns `202` and retains quarantine.

Bootstrap/restart/pre-create walkthroughs:

- Fresh bootstrap: bootstrap returns only worker identity/credential → process takes local singleton and calls `workerCreateIncarnation` → server returns generated incarnation sequence 1 in STARTING → empty reconciliation plus valid closed inventory → heartbeat with `reconcile_complete=true` may reach READY.
- Restart: new process nonce creates sequence N+1 and atomically ends N → callbacks from N fail stale → exact live N container can continue only after `workerAdoptAttempt` commits new Authority for N+1 and its timely acknowledgment updates runner → every failed/expired adoption is stopped and cleaned before READY.
- Reservation/adoption race: if an old-incarnation reserve transaction commits first, adoption under the same ordered locks rebinds its unchanged ID/callback to N+1 and returns it; if adoption commits first, the old reserve is stale and N+1 may reserve normally. Lost adoption response replays the same transferred snapshot. The new process compares that snapshot with its durable runner record under the attempt lock; any ID/callback/sequence/hash mismatch forbids publish and triggers fail/stop/cleanup.
- Cancel before create after claim: cancel commit fences/quarantines first; executor then holds the attempt lock, writes tombstone sequence 8 after confirming no create in flight/container, and calls cleanup with `NoContainerProof`. Image-preparation failure while Authority is still live uses the same tombstone proof in `workerFailAttempt`, then cleanup. In both cases delayed start sequence ≤8 is rejected locally and any later API start is stale; release occurs once without a fabricated container ID.
- Dispatch → cancel-before-claim → reconciliation → release: dispatch atomically stores Attempt `CREATED`, Authority and startup nonce; cancel revokes/fences and quarantines before any claim receipt exists. Reconciliation returns that exact revoked Authority, nonce, `claim_state=UNCLAIMED` and null container. Under the local attempt lock the worker creates a durable sequence-1 tombstone without Docker create, inspects exact Nexa labels, and sends `NoContainerProof{startup_nonce,executor_operation_sequence:1,tombstone_sequence:1,...}`. API checks the nonce and that no claim/start/container record exists, then releases once. A delayed-claim is rejected by revoked authority/desired state, and the local tombstone rejects any delayed create/start. A repeated-cleanup callback replays the original release acknowledgment; the `RELEASED` row guard prevents a second ledger close or counter decrement.

## Capability, pause and reservation walkthroughs

Capability matching is exact against the closed WorkerInventory. A CPU inventory may declare `linux/amd64`, Docker/OCI/cgroups v2/seccomp, the required adapter and image, and `NEXA_CPU` framework/device with an empty GPU list. A CUDA inventory additionally declares the exact image architecture, PyTorch/CUDA runtime support and a healthy GPU with UUID, driver/API version and compute capability. Missing adapter/image/framework/device/CUDA/driver/GPU entry means unsupported. Dispatch recheck loses/blocks with `waiting_for_compatibility`; restore emits the typed incompatibility reason and never silently falls back CUDA to CPU.

Pause-crash outcomes are closed:

1. Crash after a compatible checkpoint commit: reaper enters RECOVERING and keeps allocation charged; verified cleanup releases it and transitions directly to PAUSED without consuming a retry.
2. Crash before the first checkpoint: after verified cleanup, if the immutable template is restart-safe and retry count is below 2, transaction releases the old allocation, increments infrastructure retry once, persists `CHECKPOINT_FOR_PAUSE`, keeps desired PAUSED and schedules backoff. The next claim is restricted to producing a checkpoint; after its checkpoint commit and cleanup the job becomes PAUSED. It cannot publish a final result while desired PAUSED.
3. No checkpoint plus unsafe restart or exhausted budget: verified cleanup releases/decrements active and outstanding exactly once and transitions FAILED. No RECOVERING dead end remains.

Reservation response-loss outcomes:

- Checkpoint 1 reserves server ID `C1`, sequence 1; replay returns `C1/1`; publish commits it. Checkpoint 2 similarly gets `C2/2`. An abandoned reservation consumes no committed/restorable sequence view, may leave a monotonic gap, and cannot be reused for different content.
- After reserving `C1/1`, worker sends `REQUEST_CHECKPOINT` with that exact callback/ID/sequence and a monotonic-nanosecond deadline. Runner closes checkpoint bytes and emits staged descriptors before any final manifest exists. For each descriptor, worker retries `workerUploadAttemptArtifact` with one deterministic idempotency key until it receives the original/replayed `201 Artifact`, durably stores that exact ID/kind/media type/size/checksum, and sends the binding to runner with the same IPC sequence/hash on retry. Only after all bindings are accepted does runner create the final manifest and emit `CHECKPOINT_READY` with its complete descriptor. Worker uploads those exact canonical bytes with the descriptor-derived key and persists the returned manifest Artifact before publish. Thus response loss/restart creates neither a second reservation/artifact nor a manifest with guessed IDs.
- After recovery, the new Attempt under the same Job locks the Job and receives a sequence greater than every earlier reservation; its manifest provenance carries the new attempt/fence but preserves the same logical session. Restore orders committed rows only, so gaps have no meaning.
- Result runner emits `RESULT_PREPARE(T1)` before manifest construction. Worker durably stores the completion-token/message-sequence/callback mapping before reserving server ID `R1`; HTTP response loss replays that callback and `R1`. Lost `PREPARE_RESULT(T1,R1)` acknowledgment retries the same control sequence/hash. Runner then emits at most 64 staged descriptors per `RESULT_FILE_BATCH`; worker uploads each with a stable idempotency key, persists the exact `201 Artifact` response and returns `CommittedArtifactBinding` batches. After every direct/chunk binding and optional committed chunk-output manifest binding is accepted, `FINALIZE_RESULT_MANIFEST` authorizes runner to build the canonical manifest; `RESULT_READY` echoes `T1`, callback and `R1` plus the complete final-manifest descriptor. Worker uploads it with the descriptor-derived key and persists the returned Artifact before completion. Only completion with active manifest `result_id=R1` can recognize the unique Result. If that Authority is otherwise revoked before completion, the later Attempt atomically abandons `R1` and reserves `R2`; `R1` can never commit. Reservation without completion is not downloadable/final and causes no terminal counter change.
- If the worker process restarts while `C1` or `R1` is active, successful same-Attempt adoption returns and rebinds that exact reservation; it never allocates `C2`/`R2`. Adoption also appends the immutable prior→current Authority grant. Therefore an upload committed under a predecessor incarnation but lost its response can be retried under current Authority with the same descriptor-derived key; after exact lineage, attempt/allocation/lease/fence and immutable metadata checks, server returns the original Artifact instead of allocating another ID. No unbounded receipt scan or rewrite occurs. The durable executor/runner record preserves control/message sequences and payload hashes across process restart. A conflicting local record makes adoption unusable and leads to fencing/cleanup instead of guessing which manifest may publish.

Membership concurrency uses one `MembershipSet(tenant_id,version)` aggregate created at tenant creation with version 1. Listing an empty set returns `ETag: "v1"`. First membership upsert with `If-Match: "v1"` commits the row, audit and set version 2 together. If two admins read `"v2"` and mutate concurrently, one lock winner commits version 3; the loser receives `412` after replay lookup and writes no membership/audit/version change. Delete follows the same collection version, not a member-row version.

## Authorization matrix

`SA` = user with an active global `SYSTEM_ADMIN` grant; `TA` = tenant admin for target tenant; `M` = member for target tenant; `W` = exact local worker credential; `B` = bootstrap operator secret. Every allowed cell still enforces token scope and CSRF for cookie mutations. `M/TA` additionally require active target-tenant membership; `SA` authorization uses the global grant and does not fabricate or require tenant membership.

| Surface | M | TA | SA | W | B |
|---|---:|---:|---:|---:|---:|
| Login/session, own token | own | own | own | no | no |
| Template catalog | yes | yes | only through a separate active tenant membership | no | no |
| Upload/list/download artifact | owned tenant/reference | owned tenant | only through a separate active tenant membership; global grant alone gives no artifact access | attempt-scoped upload/reference only | no |
| Submit/list/detail job/session/events/log/result on tenant routes | own tenant | own tenant | only through a separate active tenant membership | no general query | no |
| Cross-tenant admin queue/detail (`/admin/jobs`) | no | no | audited read | no | no |
| Cancel/pause/resume/manual retry | own submitted/tenant policy | tenant | only through a separate active tenant membership; no global-role impersonation | no | no |
| Sweep | tenant | tenant | only through a separate active tenant membership | no | no |
| Tenant/user/membership/global or tenant policy | no | no in v1 | yes | no | no |
| Worker/capacity/allocation/drain/disable/enable/fairness/recovery/audit | no | tenant-scoped fairness only when exposed by product; not global admin endpoints | yes | own protocol only | no |
| Incarnation/reconciliation/heartbeat/poll/claim/adopt/start/renew/execution-artifact/reservation/checkpoint/complete/fail/cleanup | no | no | no direct impersonation | exact identity/authority | no |
| Initial system-admin bootstrap | no | no | no normal session route | no | maintenance operation only while no enabled SA exists |
| Worker bootstrap | no | no | no normal session route | no | maintenance operation only |

Composite references validate target ownership in one transaction: job input/model artifact, session/job, attempt/job, checkpoint inheritance, result/download, sweep child and retry source. Object from another tenant returns `404`, preventing existence disclosure. SA operations are explicit `/admin`, require the active global grant plus admin scope and audit; v1 `/admin/jobs` is read-only, and tenant job control still requires a real target-tenant membership. UI role checks never replace backend checks.

### CLI scope-to-operation mapping

CLI scopes are exact and non-hierarchical; a write scope does not imply the corresponding read scope. The operation first checks the scope below, then independently checks active user, tenant context/membership/role, ownership, system grant, object state and idempotency/precondition rules.

| Scope | Authorized operation groups |
|---|---|
| any valid token | Current-principal/session introspection only; returns that token's own principal context |
| `jobs:read` | Tenant template catalog; job/list/detail; logical session; attempts/checkpoints/events/log/progress/result reads |
| `jobs:write` | Job submit, sweep submit, cancel, pause, resume and manual retry; does not grant artifact upload or read |
| `artifacts:read` | Tenant artifact list/metadata/download for references visible to the caller |
| `artifacts:write` | Tenant artifact upload only; attempt outputs remain worker-authority operations |
| `tokens:write` | List/create/revoke only the caller's CLI tokens; requested scopes must be a subset of caller authority and cannot create a global grant or tenant membership |
| `admin:read` | Audited `/admin` GET operations, requiring an active `SYSTEM_ADMIN` global grant |
| `admin:write` | Audited `/admin` mutations, requiring an active `SYSTEM_ADMIN` global grant; no tenant-route impersonation |

Browser-cookie calls have no token scope but still require the same role/membership/ownership checks and CSRF for mutation. Worker/bootstrap credentials never receive CLI scopes.

## Authentication/session security

- Password: Argon2id parameters are deployment configuration meeting then-current OWASP minimum at B06; config validation refuses weaker values. Password length 12–1024 here is an input bound, not a claim of entropy.
- Browser session default absolute TTL 12 h, idle TTL 2 h; rotation on login/privilege change; revoke server-side. Cookie `HttpOnly; Secure; SameSite=Lax; Path=/`; state mutation requires same-origin CSRF token and Origin/Host validation.
- CLI token TTL 5 min–30 days, least scopes, hash only, revoke/expiry each request. Worker credential separate, default TTL 24 h with rotation and exact worker binding.
- Bootstrap secret comes from Compose/CLI secret, minimum 32 random bytes, never a default, image, repository or log value. Both bootstrap operations are maintenance-network only and default to a configurable 15-minute window. Admin bootstrap is allowed only while no enabled `SYSTEM_ADMIN` exists; its first commit atomically creates the user/grant and permanently closes new admin bootstrap attempts, while completed same-key replay remains available. Worker bootstrap/rotation has a separate explicit window; successful delivery may close it early only after worker acknowledgment, and response-loss recovery within the window can use a new key to rotate the unknown credential. After that window, an operator must explicitly reopen worker rotation mode.
- The server generates a UUIDv7 request ID for every request and reflects it in JSON errors/`X-Request-Id`; clients cannot choose it. Logs redact Authorization, Cookie, CSRF, bootstrap secret and payloads.

## Configurable abuse/security limits

Names are contract keys; B02 chooses environment/config serialization without changing meaning. Defaults are conservative functional bounds, not benchmark results.

| Key | Type/default | Valid range / behavior | Rationale |
|---|---|---|---|
| `api_json_max_bytes` | int / 1 MiB | 64 KiB–16 MiB | Bound parse memory |
| `global_outstanding_limit` | int / 100000 | 1–1000000; versioned GlobalPolicy | Bound accepted nonterminal jobs across deployment |
| `artifact_max_file_bytes` | int / 10 GiB | 1 MiB–1 TiB | Bound each streamed blob |
| `tenant_artifact_quota_bytes` | int / 100 GiB | ≥artifact max | Bounded durable storage |
| `attempt_log_max_bytes` | int / 100 MiB | 1 MiB–10 GiB; truncate/fail policy explicit | Stop log flood |
| `log_read_max_bytes` | int / 1 MiB | 1 KiB–1 MiB | API response bound |
| `scratch_max_bytes` | int / 2 GiB | 64 MiB–host validated max; counts RAM | Container abuse bound |
| `container_pid_limit` | int / 512 | 32–32768 | Fork bound |
| `idempotency_pending_wait_milliseconds` | int / 5000 | 0–30000 | Bounded concurrent wait |
| `idempotency_terminal_retention_days` | int / 30 | ≥30 | PLAN minimum |
| `staging_ttl_seconds` | int / 86400 | 3600–604800 | Interrupted upload cleanup |
| `orphan_ttl_seconds` | int / 86400 | 3600–604800 and >max clock-skew policy | Safe GC delay |
| `storage_high_watermark_percent` | int / 85 | 50–95 | Reject before disk-full |
| `storage_critical_watermark_percent` | int / 95 | >high, ≤99 | Stop dispatch/upload, preserve recovery |
| `aggregate_max_range_days` | int / 31 | 1–366 | Bounded dashboards |
| `aggregate_max_buckets` | int / 1000 | 1–10000 | Bounded query/result |
| `login_rate_per_minute` | int / 10 per source+username | 1–120 | Brute-force/backpressure |

PLAN policy defaults are concrete in `GlobalPolicy`/`TenantPolicy`: 100000 global outstanding; 2000 tenant/user; 2 tenant/1 user attempts; tenant rate 5/s burst 20; user rate 2/s burst 10; CPU/RAM tenant limit 50% allocatable and GPU 1 when present. Global and tenant policy versions are rechecked at admission/dispatch; reductions below held counters/allocation are rejected.

## Failure scope

Guaranteed scope: process/container crash, temporary network loss and whole-server reboot while durable PostgreSQL and artifact storage remain intact. Metadata acknowledged in this scope targets RPO 0 under configured PostgreSQL/storage durability. Single server has no availability while off. Disk loss/corruption requires consistent backup; retry is not disaster recovery. Compute is at-least-once within retry budget, only one final result is recognized; no general exactly-once promise.

## Failure matrix

| Fault/injection point | Expected behavior | Durable state | Reconciliation | Evidence | INV / ACC | Task |
|---|---|---|---|---|---|---|
| Worker agent killed during compute | Runner stops by last acknowledged monotonic deadline if renew absent; lease reaper fences/quarantines; no stale publish | Job/attempt/lease/allocation/event survive | New server-generated incarnation; timely exact adoption or stop/cleanup, then restore/new attempt | Kill timeline, adoption deadline/overlap, old-incarnation rejection, checkpoint/result | INV-08–13,17 / ACC-13–16,20,22 | B10,B15,B22 |
| Workload container killed | Attempt failure/lease path; classify infrastructure unless OOM/timeout evidence | Accepted job/checkpoint/result catalog unchanged | Inspect identity, cleanup, recovery within budget | Container kill at compute/upload, event/allocation trace | INV-08,11,13 / ACC-14–16,22 | B09,B14,B15,B22 |
| Host reboot | No availability while down; old containers do not auto-restart; DB/blob durable | Queue/session/ledger/counter/metadata persist; held allocations not assumed free | Compose restarts control/worker; reconcile old identities before READY; resume valid jobs | Reboot report, accepted-ID and ledger reconciliation | INV-12–17 / ACC-16,20,31 | B10,B15,B21,B22 |
| API crash before mutation commit | No accepted fact/response; transaction rollback | No partial state/event/counter/idempotency | Client retries same key | Fault around commit, DB audit | INV-10 / ACC-07,08,20 | B08,B20,B22 |
| API crash after commit before response | Accepted fact durable | Completed idempotency response and all atomic effects | Replay same key returns original response | Response-loss test and accepted IDs | INV-10 / ACC-07,20 | B08,B20,B22 |
| Coordinator death/leader turnover | Scheduling pauses briefly; existing healthy attempts renew through API | Leadership epoch changes only; attempts retain worker/fence/lease authority | New leader rebuilds heap/ledger snapshot and schedules | Two-leader/old-leader race; renew across restart | INV-04,09 / ACC-08,12,20 | B11,B13,B22 |
| Old coordinator returns | Cannot dispatch with stale epoch/lease | No stale attempt/allocation commit | Reload or exit | Stale leader rejected under lock | INV-09 / ACC-12,31 | B11,B20,B22 |
| DB unavailable during submit/control | Fail closed; no `202` without commit; API `503` | Last commit authoritative | Retry after readiness; idempotency if uncertain | Partition around request/commit | INV-10,13 / ACC-06,20,21 | B08,B15,B22 |
| DB/network unavailable during renew | No acknowledged renewal reaches runner; it retains the prior deadline then stops; quarantine on expiry | Prior lease expiry remains unless an unseen DB commit occurred | Restore connectivity, replay same callback without rebasing, then reaper/cleanup/recovery | Failed/delayed/replayed renewal timelines | INV-12,13 / ACC-13,16,21 | B10,B15,B22 |
| Heartbeat delayed/partitioned | READY→SUSPECT→UNAVAILABLE at 15/30 s; no early capacity release | Worker health and allocations durable | Reconcile when worker returns; exact cleanup | Delayed heartbeat and no-reallocation proof | INV-02,12,13 / ACC-13,16,21 | B10,B15,B22 |
| Cancel commits before complete | Desired CANCELLED/fence wins; result publish rejected | Cancel event/idempotency, no Result | Stop/cleanup then CANCELLED | Concurrent timeline and unique result query | INV-10,11 / ACC-14,15,31 | B15,B20 |
| Complete commits before cancel | Unique result/SUCCEEDED wins; new cancel `409` | Terminal result/event/counters | Cleanup releases allocation only | Race test/replay | INV-10,11 / ACC-14,15 | B15,B20 |
| Pause request vs worker crash | Desired PAUSED persists; recovery does not claim PAUSED early | PAUSING/RECOVERING, checkpoint metadata only if committed, quarantine | Cleanup; existing checkpoint→PAUSED without retry; otherwise restart-safe `CHECKPOINT_FOR_PAUSE` consumes one infra retry; unsafe/exhausted→FAILED | Pause-crash before/after checkpoint with counters/retry trace | INV-11,13,15 / ACC-15,16,18 | B14,B15,B22 |
| Two reapers / reaper vs callback | Row lock/CAS and unique callback make one transition win | One fence/event/counter/result outcome | Loser reloads/replays/stale rejects | Concurrent DB test | INV-10,11,13 / ACC-08,14–16,31 | B15,B20 |
| Lease expiry during checkpoint upload | Blob may complete but metadata publish stale; allocation quarantine | No invalid Checkpoint; possible orphan blob | GC orphan after TTL; recover older checkpoint/input | Fault before/after blob and publish | INV-13–16 / ACC-17,18,23 | B14,B15,B20 |
| Timeout | Runner stops at 300 s max, grace 5 s then kill; no blind retry | Failure event, quarantine until cleanup | Cleanup then FAILED | Runtime bound measurement | INV-13,19 / ACC-22,25 | B09,B15,B20 |
| CPU/RAM/GPU OOM | Runtime enforces limit; classify OOM; no same-spec auto retry | Failure/event/allocation held to cleanup | Cleanup; user may manual retry new job | cgroup/GPU OOM report | INV-19 / ACC-22,25,34 | B09,B15,B23 |
| Log flood/PID/scratch abuse | Hard limit/truncate-stop policy, no control-plane exhaustion | Bounded log metadata/failure event | Cleanup/GC under reference rules | Deliberate abuse measurements | INV-16,18,19 / ACC-23,25 | B09,B19,B20 |
| Disk full before/during staging/fsync/rename | Upload/checkpoint fails; no partial metadata; dispatch/admission stops at watermark | Existing committed artifact/checkpoint untouched | Remove safe staging/orphan, restore capacity, readiness recheck | Fault at each durable-commit step | INV-14,16 / ACC-17,23,31 | B07,B14,B19,B22 |
| Crash after blob rename before metadata | Durable orphan only | No Artifact/Checkpoint/Result reference | Orphan GC after TTL and reference/active-upload recheck | Filesystem/DB audit | INV-14,16 / ACC-17,23 | B07,B14,B19 |
| Corrupt newest checkpoint | Checksum reject; older committed checkpoint tried first | Corrupt evidence/event; old reference retained | Restore older; restart input only if restart-safe | Corruption/fallback timeline | INV-15 / ACC-18,19,31 | B14,B16,B22 |
| GC concurrent with upload/restore | Lock/token rules preserve active/referenced bytes | Either reference or delete claim wins, never dangling successful restore | Reconcile claim/stat after crash | Concurrent GC tests/checksum | INV-16 / ACC-23 | B19,B20 |
| Full stack restart | State/counters/ledger persist; cache rebuilt; worker not READY before reconcile | PostgreSQL/blob remain authority | Schema/storage readiness, heap rebuild, container reconcile | Before/after reconciliation | INV-04,12,17 / ACC-08,11,16,20,21 | B13,B15,B21,B22 |
| Backup restore with missing/corrupt blob | Fail readiness/recovery for affected references; do not fabricate result | Restored metadata exposes inconsistency explicitly | Restore correct backup or mark incident; checksum all manifest refs | Backup/restore manifest and negative case | INV-15,17 / ACC-18,28,31,33 | B21,B22 |
| Relocation to smaller/incompatible host | Admission stays off; incompatible/unfit jobs blocked with reason, no auto resume | Job/session/checkpoint/ledger preserved | New inventory/incarnation, checksum/compatibility scan, resume only compatible | Two-host compatible and incompatible cases | INV-03,15,17 / ACC-28,32,33 | B21 |

## Backup and stopped relocation

Required order: commit global mode `NORMAL→ADMISSION_OFF` → drain or pause/checkpoint → verify all containers stopped and allocations reconciled → commit `ADMISSION_OFF→WRITE_FROZEN` → take PostgreSQL backup and artifact snapshot with shared manifest/checksums → keep old execution off → install same release → restore both → migrate only under maintenance rules → verify manifest and commit `WRITE_FROZEN→ADMISSION_OFF` → create new worker incarnation/inventory → reconcile/checksum/compatibility → enable worker and smoke → commit `ADMISSION_OFF→NORMAL`.

`WRITE_FROZEN` freezes workload, artifact, policy except guarded recovery, allocation, ledger, membership and credential mutations. To avoid recovery lockout and preserve audited reads, four narrow metadata writes remain legal: an existing enabled `SYSTEM_ADMIN` may establish/revoke a browser session (including the login rate counter); permitted admin reads may append immutable audit records; exact idempotent replays may return stored outcomes without domain mutation; and a versioned, guarded `WRITE_FROZEN→ADMISSION_OFF` transition may commit restore verification. Token creation/rotation, password/role/membership changes and all bootstrap enrollment remain forbidden. These exceptions cannot create a principal, grant privilege, publish a blob, change job authority or release capacity.

Backup manifest binds DB backup identity/time, artifact snapshot identity, contract/schema/release versions and checksums. A DB-only or blob-only copy is not a consistent backup. Upgrade/rollback after accepting new writes cannot promise lossless rollback to an old snapshot. Disk-loss recovery is outside retry/failure scope and depends on this backup evidence.
