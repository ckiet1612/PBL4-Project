# B11/B15/B16 coordinator and execution

The coordinator is a separate process. Run it with `NEXA_DATABASE_URL` after the
current Alembic schema is ready:

```sh
PYTHONPATH=src .venv/bin/python -m nexa.coordinator.main
```

It acquires the singleton PostgreSQL leadership row for 15 seconds and renews
it every 5 seconds. A lost lease stops all dispatch mutations. A separate
accounting heartbeat charges held allocations; each tick builds a B04 policy
snapshot and commits an allocation,
server startup nonce, attempt lease, authority grant, job fence and event in one
transaction. The worker sees the committed offer on `workerPollDispatch`; the
coordinator never opens the Docker socket.

The worker claims the offer, receives an immutable execution graph, downloads
only graph artifacts, starts the B09 CPU runner and renews its lease. Result
reservation IDs and artifact IDs are server generated. A runner finalized
manifest must be uploaded and bound before completion can recognize a `Result`.
`SUCCEEDED` and resource release are separate: allocation accounting remains
held until a matching `NoContainerProof` or exact `ContainerStoppedProof` is
committed by cleanup. Duplicate callback IDs replay their durable response.

For development Compose, set `NEXA_WORKER_STATE_ROOT` to an existing durable
absolute host directory visible at the same path inside the worker and to the
Docker daemon. This is required for the runner's exact input/launch-spec bind
mounts; the runner receives only the bound input and control directory, never
the worker credential or Docker socket. Set a verified `NEXA_CPU_IMAGE_REF` of
the form `registry/name@sha256:<digest>`. Then `docker compose up db api
coordinator worker caddy` uses the coordinator image without a Docker socket. API and worker logs must
not be used as correctness evidence; inspect job events, authority, allocations,
ledger segments and result metadata in PostgreSQL.

B13 extends this path with transactionally maintained queue heads and queued
submitter presence, an exact-Decimal tenant min-heap, bounded normal candidate
windows (16 plus one separately selected oldest candidate), and batched ledger
statements. `queue_heads` and `queue_submitters` are derived from Job rows and
backfilled by migrations 0008 and 0010; neither is an authority source. The
coordinator still re-reads the PostgreSQL snapshot under leadership, policy,
counter, worker and allocation locks before committing a proposal. A changed
proposal creates no allocation. Policy capacity, current worker inventory and
tenant enable mutations append `queue_eligibility_events` in their transaction
only when they change which queued Jobs are possible at all. These are
admin-rate changes. The coordinator replays at most 64 queued Jobs per tick,
preserving each mutation's DB-time eligibility boundary, and excludes a tenant
from dispatch while its events remain pending. `fairness_ledgers.eligibility_signature`
is a legacy column and no longer drives a per-tick queue reconciliation.
**Measured limit (B13-OBS-01; remediation measurement, criterion awaits owner
decision OD-2).** Pending events are picked least recently processed first, so
tenants replay page by page in turn and each tenant finishes near the end of the
whole replay. On L (VPS1, PostgreSQL 17, direct-DB harness) with 100 tenants ×
1,000 queued Jobs, a capability change that leaves every Job possible still
replays 1,600 pages. The first tenant dispatches after 1,502 ticks, an estimated
~376 s at the 250 ms tick floor, and the replay ends after 1,600 ticks (~457 s).
Disabling and re-enabling one tenant replays only that tenant, in 32 ticks
(~43 s). Other tenants keep dispatching from the second tick.
Raw data: `docs/evidence/raw/REM-B13-OBS-01-stall.json`.
Held-quota headroom is not stored on Jobs. Since migration 0017, `jobs.eligible_since`
is base eligibility only. Each allocation insert or state/size change updates the
tenant's CPU and memory staircase in `quota_headroom_steps`, under a per-tenant
advisory lock and in the allocation's transaction. For sizes in
`(previous upper_bound, upper_bound]` it records the DB time at which headroom
last rose to admit them (`NULL`: never blocked). The top step equals the policy
limit minus held resources. Dispatch and release therefore write one staircase
row per resource and no Job rows or events. `queue_request_sizes` is a histogram
of queued non-GPU Job sizes, maintained by Job and Spec triggers; a missing row
on decrement fails closed with `data_corrupted`. The snapshot intersects the CPU
and memory staircases, clipped to capacity and `limit - held`, into cells, and
keeps populated cells with one `EXISTS` probe against the histogram. A tenant
with no populated cell is not schedulable and is not queried. A single cell folds
its resume time into the tenant resume and uses the existing batched/resumed
paths. Several cells use the grouped path, one stream per submitter and cell,
still merged into the 16+1 window. The cell floor is one opaque predicate, so the
planner keeps the ordered index walk at 100k Jobs.
The ledger heartbeat is a separate coordinator thread and transaction, started
at most every 250 ms. It checks leadership without locking the leadership row,
locks only ledger and open-segment rows, reads the operating mode and then DB
time after those locks, and charges through that time. Snapshot and commit
transactions read ledgers without locks and lock them only at their end, so
ledger commits do not wait for eligibility replay or candidate reads. A caller
holding policy locks can find a heartbeat boundary later than its own DB time.
`account_locked` then refunds that overlap at the unchanged open-segment share
and keeps segment boundaries equal to allocation times. A boundary later than
DB time read after the ledger locks is still rejected as a clock regression.
A freeze charges under the same ledger locks, so the heartbeat stops writing
once `WRITE_FROZEN` commits. A heartbeat error ends leadership, as a tick error
does, and the next leader catches up from the committed boundary.
The replay writes changed ages in one PostgreSQL statement. The proposal
commit rechecks the snapshot without replaying a second event page. Migration
0014 queues a bounded rebuild of preexisting Jobs: currently feasible Jobs
keep a valid age or start at the migration boundary, while blocked Jobs lose
a stale age. Tenant disable/enable commits an eligibility event; disable also
invalidates an active reservation, and enable records a tenant resume boundary.
An active reservation remains intact while that tenant's event replay is
pending, then is revalidated against the authoritative Job state.
Active-counter transitions write the end of a tenant/user concurrency block to
`admission_counters.eligible_resumed_at` in the same transaction. A candidate's
effective age starts at the latest of its base eligibility and those two resume
times and its quota cell's resume time; dispatch no longer updates every queued
job of a saturated scope. A Job submitted under held-resource quota keeps its
submit time as base age, and the staircase delays its effective age to the
release that frees enough headroom.
Migrations 0015 and 0016 index tenant/priority age and
tenant/submitter/priority age probes. If queued submitters have different
resume times, snapshot retrieval reads indexed per-submitter streams, merges
their normal candidates to the tenant's 16-candidate window, and selects one
independent oldest candidate. Cursor continuation and wrap use the same
indexed streams. The PostgreSQL 1,000-Job mixed-submitter EXPLAIN regression
checks that no Job scan filters the full submitter queue. A resumed oldest
stream (`eligible_since <= resume`, ready order) first probes the age index for
the tenant's oldest queued Job at that priority; when every Job ages after the
resume, the stream is skipped instead of walking the tenant's queue. Coordinator
transactions run with `SET LOCAL jit = off`: the 100-tenant batched queue
statement crosses the JIT cost thresholds, and compilation took about 1.1 s per
execution against about 75 ms of execution. See
[B13 evidence](evidence/B13-production-fairness.md)
for measured limits and open scale/cadence work. GPU, cancellation/reaper
automation, bare-Linux portability and release acceptance remain later gates.

B14 adds the minimal retry promotion that CPU checkpoint recovery needs, and
nothing more. Cleanup (API side) opens a `retry_schedules` row only for an
`INFRASTRUCTURE` attempt failure with budget left, desired `RUNNING` and a
restorable Job (a committed non-corrupt checkpoint or template `restart_safe`);
see [database](database.md). The leader probes due schedules at most once per
second from the tick. The probe first reads, without any lock, the batch that
promotion would lock and applies the same block rules to it; when every due Job is
still blocked for the reason it already shows, the probe ends there, so a retry
that stays blocked for hours costs no decision lock per second (remediation
B14-K1, chờ Task Review; measurements in the
[remediation evidence](evidence/B01-B16-findings-remediation.md)). That read
decides nothing. Only a batch with something to write (a promotion, or a new block
reason to announce) continues: `promote_retries` then runs its own short
transaction under the leadership and policy locks, takes the GLOBAL admission counter, and locks at
most a bounded batch of due Jobs and their open schedules. A Job that is still
`RETRY_WAIT`, desired `RUNNING`, without a recovery intent and whose schedule
`ready_at` has passed in DB time moves to `QUEUED` with a fresh ready sequence,
closes its schedule and appends `RETRY_READY/BACKOFF_ELAPSED`, all by
compare-and-set on state/version. A Job that no current worker can host stays in
`RETRY_WAIT` with `RETRY_BLOCKED` and a waiting reason. The coordinator does not
choose the restore checkpoint: the claim of the next attempt verifies candidates
newest-first and records the choice in the attempt's immutable execution context.
Restore events (`CHECKPOINT_CORRUPT`, `CHECKPOINT_INCOMPATIBLE`,
`CHECKPOINT_RESTORE_SELECTED`, `CHECKPOINT_FALLBACK_TO_INPUT`,
`CHECKPOINT_RESTORE_UNAVAILABLE`) advance the Job event sequence with an audit row
but, like claim acknowledgment itself, never change the Job version.
See [B14 evidence](evidence/B14-cpu-checkpoint-restore.md).

## Capability eligibility (B16; đã triển khai, chờ Task Review)

Eligibility and the policy snapshot no longer compare `adapter_id` with
`cpu.iterative`. Both call `JobService.template_runs_on(template, inventory)`:

1. `workload_adapters.runtime_descriptor` resolves the template's adapter. A
   template family owned by one adapter never runs on another, and any other
   template ID keeps the B11 binding by adapter ID and version (B16-R11).
2. The template's capability requirement must name the adapter's framework
   (`NEXA_CPU` or `PYTORCH`).
3. The requirement must match the worker inventory: architecture, adapter ID and
   version, image digest, device, and framework and version.

A template with no registered adapter, or with a requirement that does not
match, is never offered to a worker. The job waits for a matching worker and
the scheduler does not skip it silently. GPU requests are still excluded
(ACC-06, no GPU provider).

The coordinator imports no ML framework. Sweep parents never receive an
allocation or a slot, because only accepted child Jobs enter the queue.

## B15 reaper, retry promotion and retention sweep

Every tick runs at most one probe per second, in the order lease reaper → retry
promotion → idempotency retention sweep, before the dispatch snapshot. Each step
re-checks leadership (`_leader`) and the current policy's operational mode inside
its own write transaction; under `WRITE_FROZEN` none of them writes. Under
`ADMISSION_OFF` these steps and the ledger heartbeat still run, but the dispatch
snapshot and its commit decide nothing unless the mode is `NORMAL`: no new offer
and no reservation change, while an offer committed earlier stays pollable
(B15-OBS-02). Each step
commits on its own: a failing step, or one lease that cannot be reaped (for
example its job row locked past `lock_timeout`), is logged
(`coordinator_reap_lease_failed`) and retried by the next probe; it neither
stops the other leases and steps nor costs leadership or dispatch (B15-R26).

**Lease reaper** (`coordinator/reaper.py`). An unlocked probe on
`ix_attempt_leases_active_expiry` returns at most 16 live leases whose
`expires_at <= clock_timestamp()` (DB time, never worker time). Each lease is then
handled in its own transaction with the locks leadership → policy → job → logical
session → attempt → lease → allocation → open authority grants, followed by a
compare-and-set recheck against a DB time read after those locks: a lease already
revoked or renewed past `now` is left alone, so a concurrent renewal, cancel,
failure callback or second reaper that committed first wins and a duplicate reaper
is a no-op.

- Job `DISPATCHING`/`RUNNING`/`PAUSING` whose fence equals the attempt's fence:
  the attempt becomes `LOST` with `INFRASTRUCTURE/LEASE_EXPIRED`, the lease and
  grants are revoked with `LEASE_EXPIRED`, the allocation becomes `QUARANTINED`,
  and the job moves to `RECOVERING` with `job_fence + 1` and one
  `ATTEMPT_LOST/LEASE_EXPIRED` event. The desired state is kept, so a pausing job
  recovers toward `PAUSED`.
- Any other job, or an attempt already superseded by a higher fence (for example
  a job that reached `SUCCEEDED` before its cleanup was acknowledged; cancel and
  worker disable revoke the lease themselves):
  only the leftover authority is revoked and the allocation quarantined, with one
  `LEASE_REVOKED/LEASE_EXPIRED` event; attempt state, fence and job version (ETag)
  stay unchanged, and an active result reservation of that attempt is abandoned,
  so the revoked authority can publish nothing.

The reaper never releases capacity. A `QUARANTINED` allocation stays inside
capacity/quota and keeps its ledger segment open, so it is charged until the
worker (or a new incarnation reconciling the old one) reports verified cleanup;
only then does cleanup release it with `ALLOCATION_RELEASED/VERIFIED_CLEANUP` and
resolve `RECOVERING`. An `INFRASTRUCTURE` failure with desired `PAUSED` and a
committed checkpoint becomes `PAUSED` at no retry cost. Otherwise an
`INFRASTRUCTURE` failure with retries left becomes `RETRY_WAIT` (backoff
`min(30, 2^(retry-1))` s plus up to 1 s jitter, one `retry_schedules` row) when the
template is `restart_safe`, or when a checkpoint exists and no pause is pending; a
pending pause adds `recovery_intent = CHECKPOINT_FOR_PAUSE`. Everything else ends
`FAILED`; a reaped attempt stays `LOST`. The worker side is described in
[worker agent](worker-agent.md#pause-cancel-and-runner-stop-reasons-b15).

**Retry promotion** (`coordinator/retry.py`). A `RETRY_WAIT` job is promotable in
exactly the two combinations that `ck_jobs_queued_dispatchable` accepts for
`QUEUED`: desired `RUNNING` without recovery intent, or desired `PAUSED` with
`recovery_intent = CHECKPOINT_FOR_PAUSE`. The second case is a job that lost its
attempt while pausing before any checkpoint of a restart-safe template; its next
attempt is dispatched with `execution_intent = CHECKPOINT_FOR_PAUSE`, enters
`PAUSING` at start and ends `PAUSED` once that checkpoint commits. Because the
CHECK makes `state = 'QUEUED'` the complete queue predicate, the B13 snapshot and
eligibility queries and the four partial queue indexes filter on state alone.

**Lock order against requests (B15-R32).** Every request transaction first
revalidates its principal: it locks the credential row and the user's `users` row
`FOR NO KEY UPDATE`, then takes policy, counter and job locks. The coordinator
takes policy and counter locks, then job rows; a job UPDATE that makes the job
queued (retry promotion) fires the B13 `queue_submitters` trigger, and that
insert's foreign key takes `KEY SHARE` on the submitter's `users` row while the
coordinator still holds the job. `KEY SHARE` does not conflict with
`FOR NO KEY UPDATE`, so the coordinator never waits on a request's principal lock
and the cycle `users` → job → `users` cannot form (before, the principal was locked
`FOR UPDATE`: deadlock or the 1 s coordinator `lock_timeout`). No request or admin
path changes `users.user_id`, so every `users` lock is `FOR NO KEY UPDATE`; it
still excludes a second request of the same principal and every user update, so
an administrator disabling a user waits for the user's running request. Test:
`tests/integration/test_rem_b15_r32_submitter_lock.py`.

**Idempotency retention sweep** (`coordinator/retention.py`). One transaction
locks at most 100 `COMPLETED` records of `submitJob`, `cancelJob`, `pauseJob`,
`resumeJob` and `retryFailedJob` whose `expires_at` is before the transaction's DB
`now()` (`FOR UPDATE OF idempotency_records SKIP LOCKED`), then, under leadership,
deletes those whose job is terminal. The query walks the partial index
`ix_idempotency_records_b15_sweep` in expiry order and reads each candidate's job
state by primary key, so its cost does not grow with the number of queued jobs
(B15-R18). Terminal paths raise a job's records to `terminal_at + retention`, so
a record of a live job, a `PENDING` record or a record of any other operation is
never deleted. A walked record that is not due yet (its job is live, or it has no
job) is deferred instead: its `expires_at` is raised to `now()` + 1 day, so the
walk leaves it and no tick reads it again before then (remediation B15-R18
residual, chờ Task Review). Deferral only raises `expires_at` and is not a deletion
rule: the retention rule of [contracts](contracts.md#idempotency) is unchanged,
because the terminal path's `greatest(expires_at, terminal_at + retention)` still
sets the exact retention and 1 day is shorter than the 30-day minimum. Expired
`submitSweep` records follow the same walk once the job-request batch leaves room
(B16-R05): an unfinished sweep is deferred by 1 day; a finished one is deleted when
no accepted child still has its own `submitJob` record, and otherwise deferred to
that record's latest expiry. A deferral is a write, so it needs leadership and is
skipped under `WRITE_FROZEN` like the delete. See
[B15 evidence](evidence/B15-control-recovery.md) and the
[remediation evidence](evidence/B01-B16-findings-remediation.md).

## Fairness report (B18; đã triển khai, chờ Task Review)

`adminQueryFairness` reads the ledger the coordinator writes; it never changes
scheduling, ledger state or the fairness score, and it is not the scheduler's input.

- Parameters: `from`/`to` RFC 3339 with an offset, `to > from`, a range of at most
  31 days, `bucket_seconds` 1–86400 and at most 1000 buckets. Every violation is a
  400 `validation_failed`; the operation documents no 422.
- Buckets are half-open `[from + k·b, min(from + (k+1)·b, to))`, so the last bucket
  may be shorter. A segment contributes its overlap with each bucket; an open
  segment (`ended_at IS NULL`) counts up to the statement's DB time. Every instant
  (segment start/end, bucket bounds, statement time) is floored to the whole
  millisecond before the overlap is taken, the same `epoch_ms` the accounting tick
  charges with, so the normalized service of closed segments inside the range equals
  their summed `charged_amount` (B18-R26); a sub-millisecond segment contributes
  nothing, as it is never charged. Per tenant and
  bucket: dominant resource time = Σ share × overlap; normalized service = Σ share ×
  overlap / weight; allocation occupancy = Σ overlap; weight = DRT / normalized
  service, or the latest overlapping segment's weight when that is zero. Buckets
  without an overlapping segment are omitted; a report that would hold more than
  1000 tenant buckets is a 400 asking for a narrower query.
- The sums use the exact decimal ledger values inside PostgreSQL and are rounded to
  JSON numbers only on the wire. `fairness_report.aggregate_fairness` is the
  reference model the SQL is tested against (Hypothesis).
- The segment filter is one range overlap, `tstzrange(started_at, ended_at, '[)') &&
  [from, to)`, served by the GiST index `ix_allocation_ledger_segments_period`
  (migration 0023). The read sets `jit = off` locally: the bucket series makes the
  plan cost unestimable and JIT compilation dominated the statement. The read takes
  no row lock and writes only its audit record (`admin.fairness.query`). Plans and
  timings are in [B18 evidence](evidence/B18-web-ui-admin.md).
