# State machine contract

Dẫn xuất từ [PLAN](../../PLAN.md) §3, §8–§9 và [domain model](domain-model.md). Mỗi hàng là một transition linearized tại PostgreSQL commit. Actor/API response có thể nhận lỗi trước commit; external side effect không được chạy trong transaction.

## Job state, desired state và waiting reason

`Job.state` dùng đúng enum: `QUEUED`, `DISPATCHING`, `RUNNING`, `PAUSING`, `PAUSED`, `RECOVERING`, `RETRY_WAIT`, `CANCELLING`, `SUCCEEDED`, `FAILED`, `CANCELLED`. Terminal là ba state cuối. `desired_state` là `RUNNING`, `PAUSED` hoặc `CANCELLED`. `waiting_reason` là derived nullable reason, không phải state; ví dụ `waiting_for_worker`, `waiting_for_capacity`, `waiting_for_quota`, `waiting_for_reservation`, `waiting_for_retry`, `waiting_for_compatibility`.

### Job transitions

| From | Operation/event | Actor | Guard | To | Atomic effects | Response/error |
|---|---|---|---|---|---|---|
| none | submit | user/CLI | auth/ownership; committed input; valid template/spec; total capacity/quota feasible; counters/rate/queue available; new idempotency scope | `QUEUED` | Create Job, LogicalSession, immutable JobSpec, counters, event seq 1, idempotency response; version 1, fence 0, desired RUNNING | `202`; replay same response; `409/422/429/503` per contract |
| `QUEUED` | scheduler reserve | coordinator leader | eligible ≥120 s; selected tenant by fairness; oldest feasible; no active reservation | `QUEUED` | Create singleton Reservation; event; version +1; waiting reason reservation | Internal decision; conflict loses/retries |
| `QUEUED` | dispatch commit | coordinator leader | desired RUNNING, or desired PAUSED with persisted `CHECKPOINT_FOR_PAUSE` recovery intent; eligible; reservation/order valid; leader/policy/job version live; capacity/quota/concurrency/GPU recheck; no authorized attempt | `DISPATCHING` | Coordinator selects concrete compatible GPU UUIDs, increments fence and generates the immutable startup nonce under dispatch locks; create Attempt `CREATED`, Allocation `HELD`, Lease, ledger segment; increment active counters; clear/consume reservation; event; version +1 | Worker offer appears only after commit; reconciliation can return the startup identity even if no claim succeeds |
| `DISPATCHING` | claim acknowledgment | worker | exact incarnation/attempt/allocation/fence/lease; lease live; desired RUNNING or exact `CHECKPOINT_FOR_PAUSE` intent | `DISPATCHING` | Attempt `CREATED→CLAIMED`; callback receipt plus immutable ExecutionContext snapshot containing the dispatch-committed startup nonce; no job version change | Same callback replays original context; stale `409` |
| `DISPATCHING` | start commit | worker | Attempt CLAIMED; exact authority/startup nonce/executor sequence; within startup limit; desired RUNNING; exact container identity unique | `RUNNING` | Attempt `CLAIMED→STARTING→RUNNING`; bind container; set lease expiry from commit DB time to `db_now + 45s`; store callback receipt/original StartResponse; event; version +1 | Same callback returns original expiry and never extends lease; worker applies deadline only after timely ack; stale/late `409` |
| `DISPATCHING` | checkpoint-for-pause start commit | worker | Attempt CLAIMED; exact authority/startup nonce/executor sequence; desired PAUSED; persisted `CHECKPOINT_FOR_PAUSE`; exact container identity unique | `PAUSING` | Attempt `CLAIMED→STARTING→RUNNING`; bind container; set lease expiry; retain recovery intent and desired PAUSED; event; version +1 | Runner may only restore/start enough to produce checkpoint then stop; no normal result completion |
| `RUNNING/PAUSING` | progress/renew | worker | exact authority; lease live; desired/intent consistent; progress sequence monotonic per Attempt | same | Renew lease using DB time; update complete progress snapshot; optional bounded event; no job version for heartbeat-only update | `200`; runner deadline changes only after timely valid ack; stale/expired `409`, dependency `503` |
| `RUNNING` | complete result | worker | exact authority; lease live; desired RUNNING; manifest/provenance/blob committed; no result; nonterminal | `SUCCEEDED` | Insert unique Result, Attempt `SUCCEEDED`, terminal event, decrement outstanding only; allocation-active counter and ledger continue until verified release; version +1 | Ack after commit; cancel winner makes stale `409` |
| `RUNNING` | pause request | user/admin | checkpointable; desired RUNNING; fresh If-Match/idempotency | `PAUSING` | desired PAUSED; event/audit; version +1; command visible to renew/poll | `202`; invalid capability `422`; repeat replay same response |
| `PAUSING` | pause checkpoint committed | worker | exact authority; desired PAUSED; checkpoint fenced/committed | `PAUSING` | Record checkpoint and stop request/event; no PAUSED yet | `201` checkpoint; still PAUSING |
| `PAUSING` | verified cleanup | worker/reconciler | desired PAUSED; committed compatible checkpoint; `ContainerStoppedProof` or valid `NoContainerProof`; allocation release valid | `PAUSED` | Attempt `CANCELLED` with pause reason; release allocation only now; decrement active counter/close ledger; keep outstanding; clear recovery intent; event; version +1 | Cleanup `200`; job becomes PAUSED |
| `PAUSING` | pause fails but attempt remains live | worker/coordinator | desired PAUSED; checkpoint failed; lease/authority/container healthy; policy allows abort pause | `RUNNING` | desired RUNNING; event with reason; version +1 | User sees RUNNING and failure event |
| `PAUSING` | lease/process fault | reaper | lease expired/lost under DB time; pause checkpoint may or may not be committed | `RECOVERING` | revoke lease; fence +1; Attempt LOST; allocation QUARANTINED; desired remains PAUSED; event; version +1 | No release; recovery waits cleanup |
| `PAUSED` | resume | user/admin | desired PAUSED; compatible committed checkpoint or restart-safe input; fresh If-Match/idempotency | `QUEUED` | desired RUNNING; no infra retry increment; event; version +1; outstanding unchanged | `202`; incompatible `422` |
| `DISPATCHING/RUNNING` | lease expiry, worker/container loss, infrastructure failure | reaper/coordinator | DB time past expiry or verified infra failure; nonterminal; one row-lock winner | `RECOVERING` | revoke lease; fence +1; Attempt LOST/FAILED; Allocation QUARANTINED; event; version +1; no counter/resource release | Internal transition; stale callbacks rejected |
| `DISPATCHING/RUNNING/PAUSING` | classified attempt failure | worker/coordinator | exact live authority and typed observation: exact container identity after create or tombstoned `NoContainerProof` before create; classified `INFRASTRUCTURE/TIMEOUT/OOM/INVALID_INPUT/INCOMPATIBLE/INTERNAL`; no terminal job; one row-lock winner | `RECOVERING` | Commit failure class/safe reason/typed observation; revoke lease; fence +1; Attempt `STOPPING`; Allocation QUARANTINED until cleanup; mark retry eligibility; event/callback receipt; version +1 | `workerFailAttempt` ack/internal transition; same callback replays, stale later publish rejected |
| `RECOVERING` | cleanup verified and retry available | reconciler | `ContainerStoppedProof` or `NoContainerProof` accepted; allocation released; retry count <2; desired RUNNING; checkpoint selection/restart-safe policy succeeds | `RETRY_WAIT` | release allocation, decrement active counter/close ledger; retry count +1; create RetrySchedule/backoff; event; version +1 | Next ready time persisted |
| `RECOVERING` | cleanup verified while desired PAUSED with checkpoint | reconciler | cleanup proof accepted; compatible committed checkpoint | `PAUSED` | release allocation/decrement active; clear recovery intent; no retry consumed; event; version +1 | Paused after recovery |
| `RECOVERING` | cleanup verified while desired PAUSED without checkpoint | reconciler | cleanup proof accepted; no compatible checkpoint; immutable template says restart-safe; retry count <2 | `RETRY_WAIT` | Release allocation/decrement active/close ledger; retry count +1; persist `CHECKPOINT_FOR_PAUSE` intent and RetrySchedule/backoff; desired remains PAUSED; event/version +1 | Next attempt may run only to create a checkpoint; this infrastructure retry is consumed exactly once |
| `RECOVERING` | cleanup verified, no safe recovery/budget | reconciler | cleanup proof accepted; desired not CANCELLED; no compatible checkpoint and not restart-safe, or required infrastructure retry budget exhausted | `FAILED` | release allocation/decrement active/outstanding; clear recovery intent; terminal reason/event; version +1 | Terminal immutable, including pause-crash without a safe checkpoint path |
| `RECOVERING` | cleanup verified after non-retryable failure | reconciler | proof union accepted; committed failure class is `TIMEOUT/OOM/INVALID_INPUT/INCOMPATIBLE/INTERNAL`; desired not CANCELLED | `FAILED` | Attempt `FAILED`; release allocation; decrement allocation-active and outstanding exactly once; close ledger; terminal reason/event; version +1 | Terminal immutable; manual retry may create a new job |
| `RECOVERING` | host unresponsive | reconciler | container absence cannot be proven | `RECOVERING` | Keep QUARANTINED allocation/counters/charge; event rate-limited; no dispatch using capacity | No timeout-based release |
| `RETRY_WAIT` | backoff due | coordinator | DB time ≥ ready_at; desired RUNNING, or desired PAUSED with `CHECKPOINT_FOR_PAUSE`; capability remains feasible | `QUEUED` | close retry schedule; ready sequence/event; version +1 | Eligible scheduling resumes under the persisted execution intent |
| `RETRY_WAIT` | capability/policy changes | coordinator/admin | desired/intent permits retry but temporarily not compatible/quota-eligible | `RETRY_WAIT` | waiting reason update/event if material; no hidden state | Remains visible/blocked |
| `QUEUED/RETRY_WAIT/PAUSED` | cancel request | user/admin | nonterminal; fresh If-Match/idempotency | `CANCELLED` | desired CANCELLED; remove reservation/retry; decrement outstanding; terminal event/audit; version +1 | `202`; no container cleanup needed |
| `DISPATCHING/RUNNING/PAUSING/RECOVERING` | cancel request | user/admin | nonterminal; fresh If-Match/idempotency | `CANCELLING` | desired CANCELLED; revoke/fence when authority exists; event/audit; version +1; allocation held/quarantined | `202`; later completion rejected |
| `CANCELLING` | cleanup verified | worker/reconciler | proof union accepted for exact startup/container identity; allocation identity matches | `CANCELLED` | release allocation; decrement active/outstanding; close ledger; Attempt CANCELLED; terminal event; version +1 | Cleanup ack; terminal immutable |
| `CANCELLING` | host unresponsive | reconciler | stop not proven | `CANCELLING` | Allocation/counters/charge retained; reconciliation continues | UI must not claim stopped |
| `FAILED` | manual retry | user/admin | source FAILED; fresh If-Match/idempotency; original immutable spec/input still valid; optional inherited checkpoint same tenant + compatible; admission succeeds | source stays `FAILED`; new job `QUEUED` | New Job/LogicalSession/JobSpec reference, `retry_of_job_id`, counters/event/idempotency; source unchanged | `202` new Job; `409/422/429/503` as submit |
| any terminal | any transition except manual retry query | any | terminal immutable | unchanged | Optional audit for rejected privileged request; no counter/event mutation for ordinary invalid call | `409 state_conflict`; idempotent replay of the original winning request returns original response |

Successful result recognition and allocation release are separate commits because container cleanup is an external fact. `SUCCEEDED` may temporarily retain a HELD/QUARANTINED allocation until cleanup; this remains charged but cannot change the unique final result.

## Invalid and repeated controls

| Request | Deterministic behavior |
|---|---|
| Same idempotency key + same payload | Return stored status/body/ETag before If-Match; no new event/version/counter |
| Same key + different payload | `409 idempotency_conflict` |
| Missing/stale If-Match on a new request | `428` / `412`; no state change |
| Pause non-checkpointable or terminal job | `422 infeasible_request` for capability; otherwise `409 state_conflict` |
| Resume anything except PAUSED | `409 state_conflict` |
| Manual retry anything except FAILED | `409 state_conflict` |
| Cancel already terminal | `409 state_conflict`, except replay of committed cancel returns original response |
| Duplicate worker callback same ID/hash | Return original acknowledgment; do not increment event/counter/version |
| Duplicate callback ID different hash | `409 idempotency_conflict` |
| Stale leader/incarnation/fence/lease/desired state | `409 stale_authority`; no metadata recognition |

## Attempt state machine

| From | Event | Guard | To | Atomic effect |
|---|---|---|---|---|
| none | dispatch commit | job QUEUED and all allocation rechecks pass | `CREATED` | Attempt, Allocation and Lease created with same job fence |
| `CREATED` | claim | exact offer/incarnation/fence/live lease and desired/intent | `CLAIMED` | Callback receipt, claim timestamp and immutable ExecutionContext snapshot |
| `CLAIMED` | start requested | no known container/tombstone; exact startup nonce; desired RUNNING or `CHECKPOINT_FOR_PAUSE` | `STARTING` | Startup deadline fixed at dispatch/claim contract; no Docker call in DB transaction |
| `STARTING` | container identity report | exact runtime identity; within 30 s | `RUNNING` | Bind immutable container identity, job RUNNING event |
| `CLAIMED/STARTING` | pre-create failure | exact authority; executor tombstone and `NoContainerProof` for startup nonce; no create in flight/container | `STOPPING` | Commit classified failure, revoke/fence and quarantine allocation; delayed start with older/equal executor sequence is stale |
| `RUNNING` | checkpoint reservation | exact authority; interval/pause request; no conflicting reservation | `CHECKPOINTING` | Server-generated checkpoint ID and job-locked sequence reserved; no committed Checkpoint yet; allocation unchanged |
| `CHECKPOINTING` | publish accepted | durable files + manifest valid + live authority; reserved identity/sequence match; inference current chunks insert-or-verify and prior chunks match recognized rows | `RUNNING` or `STOPPING` | Checkpoint COMMITTED with complete reference graph and any new immutable RecognizedChunk rows; desired PAUSED moves to STOPPING, otherwise RUNNING; repeated callback returns same ack |
| `RUNNING` | result reservation | exact authority; desired RUNNING; no Result/reservation conflict | `RUNNING` | Server-generated result ID reserved; no terminal fact/counter change |
| `RUNNING/CHECKPOINTING` | stop requested | desired PAUSED/CANCELLED, runtime expiry or failure | `STOPPING` | Stop intent/reason; runner grace deadline; allocation held |
| active | completion accepted | live publish authority and unique result | `SUCCEEDED` | Result/job terminal transaction; cleanup still required |
| `RUNNING/CHECKPOINTING/STARTING/CLAIMED` | worker failure callback | classified failure with exact live authority, typed container/no-container observation and safe reason code | `STOPPING` | Commit callback receipt and typed observations; job enters RECOVERING, lease is revoked/fenced and allocation quarantined until cleanup; failure class determines retry eligibility |
| `STOPPING` | verified cleanup after failure | exact `ContainerStoppedProof` or `NoContainerProof`; committed failure class | `FAILED` | Release-once transaction finalizes Attempt and corresponding Job retry/terminal transition |
| active | lease expired/stale process | reaper row-lock winner | `LOST` | Revoke lease, fence job, quarantine allocation |
| active/STOPPING | cancel/pause cleanup | correct stop proof | `CANCELLED` | Reason distinguishes user cancel vs checkpointed pause |
| terminal | any callback | callback replay only or stale reject | unchanged | No second transition |

Application `TIMEOUT`, `OOM`, `INVALID_INPUT` do not auto retry same spec. `INFRASTRUCTURE` may use remaining automatic budget. Failure class is committed, not inferred from client text.

## Allocation state machine

| From | Event | Guard | To | Effects/race behavior |
|---|---|---|---|---|
| none | dispatch commit | capacity/quota/GPU UUID locked and available | `HELD` | Count capacity/quota; open ledger segment; partial uniqueness blocks oversubscription |
| `HELD` | lease revoke, uncertain process, disable | container stop not yet proven | `QUARANTINED` | Continue capacity/quota/resource-time charge; never schedulable |
| `HELD` | cleanup after normal terminal | exact identity and stopped/absent proof | `RELEASED` | Close ledger, decrement active allocation counters, release GPU uniqueness |
| `QUARANTINED` | verified cleanup/reconcile | same worker/incarnation/attempt/allocation/container identity; absence proven | `RELEASED` | Exactly-once release under row lock; duplicate report returns prior result |
| `HELD/QUARANTINED` | unverified/mismatched cleanup | identity mismatch or container still present | unchanged | `409/202`; no resource release |
| `RELEASED` | duplicate cleanup/reaper | same identity | `RELEASED` | Idempotent acknowledgment; no double decrement/ledger charge |

## Worker state and readiness

Worker administrative state (`ENABLED/DRAINING/DISABLED`) is orthogonal to observed health (`STARTING/READY/SUSPECT/UNAVAILABLE`). After bootstrap provides worker identity/credential, each new process holds the local singleton and calls `workerCreateIncarnation`; the server locks Worker, generates the ID, increments sequence, ends the prior incarnation and makes the new one current in `STARTING` atomically. Same idempotency key/process nonce replays; older-incarnation heartbeat/renew/publish callbacks are stale. The process inventories the host, enumerates containers and either adopts an exact still-live identity through `workerAdoptAttempt` or stops/cleans it. Only then may the same incarnation become READY. Heartbeat every 5 s; DB-time age ≥15 s gives SUSPECT and ≥30 s UNAVAILABLE. Health loss fences/reconciles attempts but never alone releases allocation.

Admin drain commits `ENABLED→DRAINING`; disable commits any state to `DISABLED` and fences/stops active work without releasing allocation early. Enable/undrain commits `DRAINING/DISABLED→ENABLED` only after the current incarnation is READY, reconciliation complete and DB/storage/capability checks pass. It does not revive fenced attempts or release quarantine.

| Admin operation | New allocation | Existing attempt | Readiness |
|---|---|---|---|
| ENABLED | Allowed only when health READY | Renew/publish per authority | Reconcile prerequisite |
| DRAINING | Forbidden | Continue until terminal/control | READY may remain while draining |
| DISABLED | Forbidden | Revoke/fence and request stop; quarantine until cleanup | Cannot advertise dispatch READY |

Deployment operational mode is versioned in global policy and independent of worker state:

| From | Admin transition/guard | To | Effect |
|---|---|---|---|
| `NORMAL` | authenticated/versioned maintenance request | `ADMISSION_OFF` | Reject new job/sweep admission and new dispatch; running/control/checkpoint/cleanup continue |
| `ADMISSION_OFF` | all containers stopped, worker/allocation reconciliation complete, no unreleased allocation | `WRITE_FROZEN` | Commit freeze marker/audit; freeze domain/config writes while retaining only the explicit recovery-security exceptions below |
| `WRITE_FROZEN` | paired DB/blob restore verified against release/schema/manifest; worker still non-dispatching | `ADMISSION_OFF` | Existing enabled SA authenticates or uses a still-valid token; commit restore verification/audit and mode version; readiness/reconciliation writes may resume but admission stays off |
| `ADMISSION_OFF` | DB/schema/storage/capability healthy; worker reconciled and enabled/READY | `NORMAL` | Reopen admission and dispatch |

Invalid skips such as `NORMAL→WRITE_FROZEN` or reopening directly from `WRITE_FROZEN` return `409 state_conflict`; stale policy version returns `412`.

While `WRITE_FROZEN`, permitted writes are limited to existing-enabled-SA browser session establish/revoke plus login-rate metadata, append-only audit for allowed admin reads, stored idempotent replay with no domain mutation, and the guarded versioned recovery transition. Token/password/grant/membership/bootstrap changes remain forbidden, so these exceptions cannot mint durable authority or alter workload/artifact/allocation/ledger facts.

## Counters, event sequence, version và fence

- Submit/new manual retry: outstanding +1 at global/tenant/user and event sequence starts 1.
- Dispatch: active-attempt counters +1; fence +1; allocation and ledger begin atomically.
- Recovery fencing: fence +1 but counters/allocation stay held until cleanup.
- Normal success/failure/cancel decrements outstanding exactly once at terminal transition; active counters decrement at verified release, not process report.
- Pause release decrements active only; paused remains outstanding. Resume does not increment outstanding.
- Each material job transition increments version and appends exactly one next event sequence in the same transaction. Callback replay does neither.
- Job fence never decreases or resets across coordinator restart/recovery/resume. Manual retry has a new job fence domain starting at 0.
