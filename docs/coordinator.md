# B11 coordinator and CPU execution

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
for measured limits and open scale/cadence work. GPU, checkpoint restore,
cancellation/reaper automation, bare-Linux portability and release acceptance
remain later gates.
