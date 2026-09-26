# B13 production fairness, quota and ledger evidence

Status: **implemented and verified locally within the scope below; ready for
independent Task Review.** No B13 Task Review approval is claimed. This is the
uncommitted worktree on base HEAD `96c674a43b285275dfb30e1f87ff61fffac1397c`.
The user confirmed B01-B12 Task Review approval before this task.

This revision answers Task Review round 1 ("Không duyệt", blocking finding
B13-R13, default-quota eligibility replay). Every current P report was
regenerated on the fixed source. Reviewer check 1 is the PostgreSQL backlog
test (see Verification). Check 2 is under real-time fairness. Check 3 is under
queue ≥100,000. R12 remains open for the user's decision.

## Scope, layers and environment

| Layer | Evidence in this task | Boundary |
|---|---|---|
| D | B04 fixed five-seed, six-profile simulator comparison rerun against the current heap policy | Virtual time and simulated resources |
| P | PostgreSQL 17 migrations, guarded integration suite, production HTTP API prefill of 100,000 Jobs, queue microbenchmark with raw `EXPLAIN (ANALYZE, BUFFERS)`, committed-ledger cadence, real-time fairness and reservation traces, injected DB faults | Direct-DB fixtures are labelled; they are not API or worker acceptance |
| L | B11 two-tenant CPU vertical on Docker Desktop Linux VM | Not bare Linux, portability, sustained load or GPU |
| G | Not run | No GPU or CUDA claim |

Host: macOS 27.0 arm64 (`macOS-27.0-arm64-arm-64bit`). Docker Desktop, engine
29.5.3, `linux/arm64`, kernel `6.12.76-linuxkit`, 8 vCPU, 3.8 GiB VM memory.
PostgreSQL 17.11 (`server_version_num` 170011) image
`sha256:f4c66b820c6f974249089d3d16d86a3698eae11e8746eb6644b2271031e91232` in
two containers on loopback: shared cluster `nexa_b13_pg` port 15439 and an
isolated cluster `nexa_b13_pg_isolated` port 15440. This is a Docker Desktop
Linux VM, not a bare-Linux host.

## Implementation map

- `scheduler/policy.py`: the B04 policy only, weighted dominant resource-time
  plus hard quota, aging every 60 s up to 2 levels and one local reservation
  after 120 s. It uses an exact-Decimal tenant min-heap and does not import
  PyTorch.
- `coordinator/accounting.py`: aggregate-then-dominant charging of every
  unreleased allocation, including QUARANTINED, with batched ledger writes,
  exact Decimal and a durable monotonic virtual floor. `account_locked` refunds
  any heartbeat charge beyond a policy-locked caller's DB time. Real DB-time
  regression raises instead of charging backwards.
- `coordinator/service.py` and `runtime.py`: a separate `coordinator-accounting`
  thread runs `CoordinatorService.account(epoch)` at most every 250 ms, each in
  its own transaction. It performs an unlocked leadership/lease check, locks
  only ledger rows, skips `WRITE_FROZEN` and charges through DB time read after
  the locks. Any exception stops the coordinator loop (fail closed). The next
  leader catches up from the committed `accounted_through`. Decision ticks no
  longer determine ledger cadence. Every coordinator transaction sets
  `jit = off` (B13-R13).
- `coordinator/snapshot.py`: 16 normal candidates per tenant from indexed
  priority/age streams plus one independent oldest or protected-reservation
  candidate, a durable cursor with wrap and per-submitter age streams merged
  into one 16+1 window. `read_snapshot` performs no writes;
  `persist_fairness_locked` writes score, demand and floor. Commit re-reads the
  snapshot under the B11 lock and leadership rules. A changed proposal
  allocates nothing. A resumed oldest stream first reads the tenant's oldest
  queued age at that priority through the age index. It is skipped when no Job
  aged by the resume time (B13-R13).
- `coordinator/eligibility.py`: policy capacity, current worker inventory and
  tenant enable mutations record an eligibility event at DB time, and only when
  the set of possible queued Jobs changes. These are admin-rate changes. The
  coordinator replays at most 64 queued Jobs per tick, round-robin across
  tenants, and excludes tenants with pending events from dispatch. A protected
  reservation is held while its tenant still has pending replay. Since
  migration `0017`, allocation insert, release and resize no longer emit
  events or rewrite Job rows (B13-R13).
- Held-quota headroom (migration `0017`): `jobs.eligible_since` is base
  eligibility only. Each allocation change updates the tenant's CPU and memory
  staircase `quota_headroom_steps(tenant, resource, upper_bound, resumed_at)`
  under a per-tenant advisory lock, in the allocation's transaction. The top
  step is `limit - held`. `queue_request_sizes` is a histogram of queued
  non-GPU Job sizes, maintained by Job/Spec triggers; a missing row on
  decrement fails closed with `data_corrupted`. `coordinator/snapshot.py`
  intersects both staircases, clipped to capacity and `limit - held`, into
  cells. It keeps populated cells with one `EXISTS` probe against the
  histogram. The effective age is
  `greatest(base, tenant resume, user resume, CPU step, memory step)`. The
  cell floor is one opaque `CASE` predicate. Two plain range comparisons
  against the `VALUES` columns cut the row estimate about 9×. That flipped the
  100k plan to per-tenant bitmap scans plus sort and hit the 3 s statement
  timeout (see B13-R13).
- Migrations `20260925_0008` to `0017`, schema snapshots `schema_v7.py` to
  `schema_v14.py`: queue heads, queued-submitter presence, tenant/user
  concurrency resume times, retry-ready age boundaries, eligibility events,
  tenant enable boundaries, an upgrade-time queue rebuild, the
  tenant/priority and tenant/submitter/priority age probes, and the quota
  staircase/histogram. The legacy `eligibility_signature` column (`0009`)
  remains unused. Dropping it would change the schema of the frozen evidence
  DBs and add a migration without a behavioural gain, so it stays.
- `application/job_service.py` is unchanged from base HEAD. The earlier R05
  held-quota submit branch was reverted: the base age is the submit time, and
  the staircase delays the effective age. `application/policy_service.py`:
  freeze charges through its DB time under the ledger locks.
- `infrastructure/security.py`: base64url decoding rejects noncanonical
  encodings. This was found while running the full suite during B13: a
  one-character cursor tamper could decode to the same bytes. Its deterministic
  red test is `tests/security/test_primitives.py`. The change tightens
  validation only.

## Reproduction rules

- Use `NEXA_TEST_DATABASE_URL` only: `postgresql+psycopg`, loopback,
  PostgreSQL 17, database name prefix `nexa_b05_test_`. Harnesses reject a
  non-loopback URL, a wrong prefix or an exact `NEXA_DATABASE_URL` match. Never
  use `NEXA_DATABASE_URL` as a test target. Do not print passwords. The
  commands below write `<guarded URL>` in place of a credential-bearing URL.
- Destructive Pytest ran only on `nexa_b05_test_b13regression`. Benchmark DBs
  are separate. The API evidence DBs `nexa_b05_test_b13api_final`,
  `nexa_b05_test_b13api_frozen_0016` and `nexa_b05_test_b13api_frozen_0017`
  were never used as destructive targets. Every queue and cadence run used a
  `CREATE DATABASE ... TEMPLATE` copy.
- Copy chain for the current reports:
  - `…b13r13_base` is a copy of `…b13api_frozen_0017`. The pending run
    (`--prepare-api-worker`) inserted the direct-DB CPU worker there, which
    left 100 pending inventory events.
  - The pending-base queue runs, the default-quota run and all eight cadence
    runs used copies of `…b13r13_base`.
  - The drained steady and dispatch runs used copies of
    `…b13r13_cadence_long` after that run had replayed all 100 events.
  - After a run, the copied DB still holds that coordinator's 15 s
    leadership lease, so the next copy waits 20 s.
- `uv` is absent from the host PATH. The executable used is
  `/tmp/nexa-b12-uv-bootstrap/bin/uv` (0.9.27). `PYTHONPATH=src:.` is required
  because the synced `.venv` holds a non-editable package.
- No run used `SET enable_seqscan=off`.
- Every JSON report records base HEAD, the SHA-256 of
  `git diff --binary -- src/ migrations/ tests/ benchmarks/b13/ pyproject.toml uv.lock`,
  and hashes of untracked source files.
- `api_prefill` resumes from `<output>.accepted.jsonl` when that file exists.
  A fresh prefill needs a new output path.

```sh
U=/tmp/nexa-b12-uv-bootstrap/bin/uv
R=benchmarks/results/b13
PREFILL="--api-prefill-result $R/b13-api-prefill-100k-r13.json"
# Frozen-source API prefill on a new, empty DB migrated to head
NEXA_TEST_DATABASE_URL=<guarded URL>/nexa_b05_test_b13api_frozen_0017 PYTHONPATH=src:. \
  $U run --no-sync python -m benchmarks.b13.api_prefill \
  --tenants 100 --jobs-per-tenant 1000 --workers 8 --submit-concurrency 2 \
  --state-dir /tmp/b13-api-frozen-0017-state --output $R/b13-api-prefill-100k-r13.json
# Queue microbench, 100k (each on its own template copy, see the copy chain above)
NEXA_TEST_DATABASE_URL=<guarded URL>/<copy> PYTHONPATH=src:. \
  $U run --no-sync python -m benchmarks.b13.queue_microbench \
  --tenants 100 --jobs-per-tenant 1000 $PREFILL --output <report> \
  { --repetitions 3 --prepare-api-worker          # r13-pending (on …b13r13_base)
  | --repetitions 3                               # r13-drained-steady
  | --repetitions 3 --prepare-dispatch            # r13-drained-dispatch
  | --repetitions 10 --prepare-default-quota }    # r13-default-quota
# Committed-ledger cadence: 12 s windows with 2 s warm-up (add --mutation), or 720 s without warm-up
NEXA_TEST_DATABASE_URL=<guarded URL>/<copy> PYTHONPATH=src:. \
  $U run --no-sync python -m benchmarks.b13.ledger_cadence [--mutation] \
  [--seconds 720 --warmup-seconds 0] --output <report>
# Real-time fairness, one fresh DB migrated to head per run
NEXA_TEST_DATABASE_URL=<guarded URL>/nexa_b05_test_b13fairness_<run> PYTHONPATH=src:. \
  $U run --no-sync python -m benchmarks.b13.runtime_fairness \
  --jobs-per-tenant 1000 --warmup-seconds 10 --window-seconds 30 --windows 3 \
  --execution-seconds 1 --tenant-cpu-quota-millis {1500|6000} --weights {1,1,1|1,2,4} \
  --output $R/b13-runtime-fairness-<run>.json
# PostgreSQL traces (guarded regression DB)
B13_REALTIME_STARVATION=1 B13_RESERVATION_TIMELINE=<report> \
  $U run --no-sync pytest --run-postgres -q -p no:randomly \
  tests/integration/test_coordinator_b13.py::test_reservation_drains_fitting_arrivals_until_verified_cleanup_releases_capacity
B13_CONCURRENT_CLEANUP=1 [B13_RACE_TICK_DELAY_MS=250] B13_RESERVATION_TIMELINE=<report> \
  $U run --no-sync pytest --run-postgres -q -p no:randomly <same test>
B13_DB_FAULT_TIMELINE=<report> $U run --no-sync pytest --run-postgres -q -p no:randomly \
  tests/integration/test_coordinator_b13.py::test_ledger_db_fault_rolls_back_dispatch_and_new_process_catches_up
```

The `{a | b}` groups list alternative flags, one run each. New DBs (the prefill
DB and each fairness DB) were created empty and migrated to head through the
Alembic Python API with the explicit guarded URL. `NEXA_DATABASE_URL` was never
set.

## Current-source results

### Source provenance

Every current P report records tracked diff SHA-256
`8ebff3fc5382cad78719deaa2d5a6393f3107a44779bfef1a03d0a5f87c7873d` on base
HEAD `96c674a`, with identical hashes for all 28 untracked source, migration,
harness and test files. The worktree still matches that hash and all 28 file
hashes at handoff. The API prefill recorded the same provenance at start and
end.

The reservation and DB-fault traces are Pytest timelines and do not embed
provenance. They ran at 03:08 local time. The last source, migration, harness
or test edit was at 02:59, and no such file changed afterwards. Documentation
changes do not enter the hash.

### D: policy simulation (ACC-09, ACC-35)

The fixed B04 comparison was regenerated against the current policy: 180 runs,
six profiles × five seeds × six policies. Each comparison shares trace,
capacity and quota. All 20 valid fairness windows met weighted Jain `>=0.95`.
The minimum was `0.987613987` (`weighted-124`, seed 29). Every product-policy
row had zero invariant violations. The CSV is byte-identical to the approved
B04 CSV. Baselines are simulator-only.

### P: frozen-source API prefill of 100,000 Jobs (ACC-06, ACC-07, ACC-20, ACC-35)

`b13-api-prefill-100k-r13.json` ran on `nexa_b05_test_b13api_frozen_0017`, a
new DB migrated to head (`0017`). It was one uninterrupted pass from
2026-09-25 20:22:40 to 21:08:35 UTC on the final source. It submitted 100
tenants × 1,000 Jobs through the production HTTP routes using eight
independently authenticated submitters, each with a browser-issued CLI token.
Tenant, policy, artifact and Job writes all used API routes.

- Admin API settings: two concurrent request slots, tenant/user quota and rate
  policy, and a global outstanding cap of 101,000 at policy version 2. Product
  defaults are unchanged.
- The static CPU template is inserted directly because its admin route belongs
  to a later task.
- Outcome: 100,000 accepted, zero dependency retries, zero errors, and the
  same provenance at start and end.
- Request latency: median 52.9 ms, observed range 24.5–538.1 ms. This is not a
  B22 throughput rate.
- Accepted-ID journal: exactly 100,000 distinct idempotency keys and no key
  reused across principals.
- SQL reconciliation: 100,000 accepted IDs, 100,000 Jobs, each traceable to a
  Session and a Spec, and global outstanding 100,000.
- A new API process returned 200 sampled owned reads and 100 cross-tenant
  denials.

### P: queue ≥100,000 and query plans (ACC-11)

These runs used template copies of the API DB (copy chain under Reproduction
rules). Each run had one warm-up tick, then measured ticks, then raw
`EXPLAIN (ANALYZE, BUFFERS)` of the captured statements with `jit = off`, as in
coordinator transactions. Statements are replayed after the timed ticks, so
plan state can differ from timed state. Before and after each tick, a fixture
probe counts the `jobs` and `queue_eligibility_events` rows written, using
row `xmin`.

| Report (`b13-queue-api-100k-r13-*.json`) | State and path | Warm-up (ms) | Measured ticks (ms) | Median (ms) | Heartbeat (ms) | SQL / queue statements per tick | Rows written per tick |
|---|---|---|---|---|---|---|---|
| `drained-steady` | 99,994 queued, 6 held, 0 pending; `DrainForReservation` | 444.7 | 233.7 / 181.7 / 223.5 | 223.5 | 20.4–32.4 | 51 / 2 | 0 `jobs`, 0 events |
| `drained-dispatch` | same, plus `--prepare-dispatch` | 1,020.1 (CreateReservation) | Dispatch 427.2 / CreateReservation 448.8 / Dispatch 450.3 | 448.8 | 19.2–25.3 | 131–134 / 7 (Dispatch), 107 / 4 | 1 `jobs`, 0 events |
| `default-quota` | 100,000 queued; 2,000m pool; every tenant at the default 50% quota; fixture release after each Dispatch | 717.5 (CreateReservation) | 10 ticks, Dispatch and CreateReservation alternating, 444.7–527.0 | 478.6 | 11.8–20.9 | 113 / 7 (Dispatch), 93 / 4 | 1 `jobs`, 0 events |
| `pending` | 100,000 queued; 100 inventory events pending after the worker fixture | 296.2 | 134.3 / 129.7 / 210.3, all `NoDecision` | 134.3 | 13.9–18.7 | 46 / 2 | 64 `jobs`, 1 event completed |

Default quota at 100k (reviewer check 3 for B13-R13):

- The fixture reduces allocatable CPU to 2,000m. It sets every current tenant
  policy to 1,000m CPU and half the allocatable memory, and drains the
  admin-rate capacity events of that change before measuring: 1,596 replay
  pages. Each 1,000m dispatch exhausts the tenant's CPU headroom, and the
  fixture release after it restores the headroom. Both events are the quota
  crossings that R13 found.
- All 10 measured ticks wrote exactly one `jobs` row and zero eligibility
  events, with zero pending events. Each of the five fixture releases took
  26.2–33.0 ms and wrote one `jobs` row (the terminal transition) and zero
  events. The staircase after the release was
  `cpu [0, 1000]`, `memory [0, 6375342080, 6442450944]`.
- Before the fix, the same crossing appended one event per tenant crossing and
  replayed the tenant's queue at 64 Jobs per tick. The reviewer measured about
  27,108 `jobs` row updates in 90 s at 1,500m.

Plan findings for the `drained-*` and `default-quota` reports:

- No `Seq Scan` on `jobs`. Job index nodes (`ix_jobs_b13_head`,
  `ix_jobs_b13_priority_age`, `ix_jobs_oldest_eligible`) read at most 16 rows
  per loop. Steady filtered no Job rows; dispatch and default quota filtered at
  most one.
- The batched default-quota queue statement executed in 9.7 ms with 16,417
  shared hits and no reads. It walked `ix_jobs_b13_head` in 100 loops of at
  most 16 rows, and its oldest-age probes returned at most 1 row per loop.
- The Sort nodes are the per-tenant bounded windows of 16 rows over 93–100
  loops.
- In each dispatch report, two write replays could not run against the later
  state, and are stored as plain `EXPLAIN` with `"analyzed": false`. These are
  the segment `INSERT` and one `UPDATE`: the segment `UPDATE` in
  `drained-dispatch` and the Job `UPDATE` in `default-quota`. For
  `default-quota`, the working log showed a duplicate segment key and the
  terminal-Job immutability trigger. The reports do not store the error text.
  The plain Job `UPDATE` plan is an index scan.

The `pending` report is the cold/pending path. Each tick replays one 64-Job
page. That is the maximum `jobs` rows per loop (64), and it filtered none.
The tenants with pending events are excluded until their replay completes.
`b13-queue-api-100k-r13-steady.json` and `-dispatch.json` ran on the same
pending base, so they measured this path too: `NoDecision`, 64 `jobs` rows
and 1 event per tick. They are superseded by the `drained-*` reports.

Dispatch and default-quota preparation (aged fixture, reservation setup,
direct-DB CPU worker, capacity and quota changes, fixture releases) consists
of direct DB writes and is not a production API operation.

Behavioral PostgreSQL regressions cover the following:

- queue heads
- 17th-candidate cursor continuation
- promotion of aged low-priority Jobs outside the first 16
- retry-ready age boundaries
- bounded statement count for multi-tenant accounting
- skipping of blocked submitters
- per-submitter age ordering with cursor wrap
- a mixed-submitter EXPLAIN bound. It was red at 999, then 499, actual Job
  rows before migration `0016`, and green at the 32-row bound.

### P: committed ledger cadence ≤1 s (ACC-08)

`ledger_cadence.py` timestamps completed transactions that update
`fairness_ledgers`. It reports the largest commit gap, not
`accounted_through`. A new process waits for any previous leadership lease to
expire. The harness waits for leadership before warm-up and records the wait
as `leadership_wait_ms`.

All eight runs used the shared cluster (`nexa_b13_pg`, port 15439), each on
its own copy of `…b13r13_base`. That base had 100 inventory events pending
from the worker fixture, so every run starts with a queue replay in progress.

| Report (`b13-ledger-cadence-r13-*.json`) | Duration | Commits | Max gap (ms) | Median gap (ms) | Heartbeats (max ms) | Ticks (max ms) | Leadership wait (ms) |
|---|---|---|---|---|---|---|---|
| `steady-1` | 12 s + 2 s warm-up | 93 | 336.1 | 110.2 | 47 (223.5) | 46 `NoDecision` (310.6) | 99.6 |
| `steady-2` | same | 94 | 305.1 | 66.1 | 47 (162.8) | 47 `NoDecision` (319.2) | 130.5 |
| `steady-3` | same | 95 | 298.7 | 93.6 | 47 (69.1) | 48 `NoDecision` (232.3) | 107.0 |
| `mutation-1` | same, 2 mutations | 95 | 289.9 | 95.9 | 47 (125.7) | 48 `NoDecision` (246.9) | 166.8 |
| `mutation-2` | same | 94 | 353.5 | 71.1 | 47 (105.8) | 47 `NoDecision` (341.6) | 83.3 |
| `mutation-3` | same | 92 | 380.2 | 93.6 | 47 (197.6) | 45 `NoDecision` (479.2) | 335.7 |
| `long-720s` | 720 s, no warm-up | 5,412 | 498.1 | 129.0 | 2,802 (383.3; p99 262.6) | 2,597 (603.7) | 105.5 |
| `cold-720s` | 720 s, no warm-up | 5,436 | 476.2 | 121.8 | 2,805 (406.2; p99 260.5) | 2,631 `NoDecision` (510.8) | 76.1 |

Every run recorded zero errors, no commit gap above 600 ms and no tick above
1 s.

- `long-720s` is a 100-tenant rebuild followed by steady state. It completed
  all 100 pending events at 21:30:06 UTC, about 7 min 43 s into the window,
  leaving 99,994 QUEUED Jobs and 6 unreleased allocations. Its 2,597 ticks
  were 1,496 `NoDecision`, 1,088 `DrainForReservation`, 7 `CreateReservation`
  and 6 `Dispatch`.
- `cold-720s` doubles the rebuild. Before the run, one direct-DB fixture
  transaction added a capability to the current worker inventory, which
  emitted a second event for each of the 100 tenants (200 pending):

```sql
UPDATE worker_inventories i SET workload_capabilities = jsonb_set(
  i.workload_capabilities, '{frameworks}',
  (i.workload_capabilities->'frameworks') ||
  '[{"framework":"NEXA_B13_FIXTURE","framework_version":"0.0.0","device":"CPU"}]'::jsonb)
FROM workers w WHERE w.worker_id = i.worker_id
  AND i.inventory_version = w.current_inventory_version;
ANALYZE jobs; ANALYZE job_specs; ANALYZE queue_eligibility_events;
```

  In 720 s it completed 100 of the 200 events (the last at 21:42:39 UTC) and
  ended with 100 still pending and 100,000 QUEUED Jobs. Every tick was
  `NoDecision`, because tenants with pending events are excluded. Replay is
  bounded at 64 Jobs per tick, so a rebuild after an admin-rate change takes
  minutes at 100k. Ledger commits stayed ≤476.2 ms throughout.

Before the heartbeat, the equivalent 100-event rebuild on this cluster had a
2,069.9 ms maximum gap, because cadence was bounded by decision-tick latency.

These are healthy-DB cadence results on Docker Desktop. DB outage behaviour is
covered by the fault evidence below, not by a cadence claim.

### P: injected DB faults, restart and catch-up (ACC-08, ACC-20, ACC-21)

- `b13-db-ledger-fault-restart-r13.json`: a trigger rejected the ledger
  UPDATE with SQLSTATE `P0001` (`ProgrammingError`) during a dispatch tick.
  - Ledger score, `accounted_through`, version, the single held allocation,
    active counters and the event count were all unchanged.
  - A new coordinator process then dispatched and charged the held allocation
    from the committed boundary. Score rose from `0` to `0.279…`, and
    `accounted_through` advanced past the pre-fault boundary.
- `test_accounting_heartbeat_db_fault_rolls_back_and_next_heartbeat_catches_up`:
  a `BEFORE UPDATE` trigger makes `service.account(epoch)` raise `DBAPIError`.
  - The ledger score, `accounted_through` and version remain unchanged.
  - After the trigger is removed, the next heartbeat advances to its boundary,
    the score increases, and the ledger equals the segment sums.
  - This passed on its first run; it characterizes existing behaviour and was
    not a red-first test.
- Heartbeat unit and integration tests:
  - leadership required, `WRITE_FROZEN` skipped
  - ledger rows locked once without fairness-state writes
  - an exception sets `lost` and stops the loop
  - a heartbeat commits while a decision transaction is slow
  - exact refund of overlap under policy locks
  - real DB-time regression detected

### P: real-time weighted fairness and default quota (ACC-09, ACC-10, ACC-35)

Setup: one fresh migrated DB per run (`nexa_b05_test_b13fairness_<run>`), three
tenants with equal CPU cohorts of 1,000 queued Jobs each.

- Capacity: 3,000m allocatable CPU; two active slots per tenant and per user.
- Jobs: 1,000m CPU and 1 GiB each, completed by a direct-DB fixture after 1 s.
- Windows: 10 s warm-up, then three non-overlapping 30 s windows.
- Every tick is followed by a probe. An idle-fit tick is a `NoDecision` while
  at least 1,000m CPU is free and some tenant has a queued Job that fits its
  held-quota headroom.

Jain is computed from each allocation's held-time overlap with the window,
divided by weight. Virtual scores and completion counts are not used.

| Run | Tenant CPU quota | Weights | Jain per window | Decisions | Idle-fit ticks | Eligibility events | `jobs` rows per dispatch/completion |
|---|---|---|---|---|---|---|---|
| `quota1500_1` | 1,500m (default 50%) | 1:1:1 | 0.9995 / 0.9996 / 0.9995 | 228 Dispatch, 151 NoDecision | 0 | 0 | 1.503 |
| `quota1500_2` | 1,500m | 1:1:1 | 0.9994 / 0.9995 / 0.9994 | 220 Dispatch, 145 NoDecision | 0 | 0 | 1.503 |
| `quota1500_3` | 1,500m | 1:1:1 | 0.9994 / 0.9995 / 0.9995 | 226 Dispatch, 147 NoDecision | 0 | 0 | 1.503 |
| `control6000_1` | 6,000m | 1:2:4 | 0.9959 / 0.9977 / 0.9937 | 216 Dispatch, 146 NoDecision | 9 (`proposal_changed`) | 0 | 1.503 |
| `control6000_2` | 6,000m | 1:2:4 | 0.9845 / 0.9920 / 0.9966 | 216 Dispatch, 149 NoDecision | 11 (`proposal_changed`) | 0 | 1.503 |
| `control6000_3` | 6,000m | 1:2:4 | 0.9970 / 0.9952 / 0.9943 | 217 Dispatch, 149 NoDecision | 11 (`proposal_changed`) | 0 | 1.502 |
| `quota1500_w124` | 1,500m | 1:2:4 | 0.7903 / 0.7941 / 0.7915 | 227 Dispatch, 150 NoDecision | 0 | 0 | 1.503 |

Report files are `b13-runtime-fairness-<run>.json`.

- Default quota (reviewer check 2): every window of all three runs has Jain
  ≥0.9994. Every `NoDecision` is `no_eligible_candidate`, which means each tenant is at
  its quota. Dispatch counts match the 6,000m control. Before the R13 fix, the
  reviewer measured 15 Dispatch and 375 NoDecision in the same setup.
- Across all seven runs: zero eligibility events, zero pending events, zero
  errors, held CPU never above 3,000m, and every tenant kept ≥884 queued Jobs.
  About 1.5 `jobs` row updates per dispatch or completion: one per dispatch,
  and the fixture's completion transition.
- No tick left capacity idle because of replay. The 9–11 idle-fit ticks per
  control run are all `proposal_changed`: the commit transaction re-reads the
  snapshot, and a tenant order that differs from the proposal's creates no
  allocation. This is the B11 recheck, and each such tick is followed by a
  fresh decision.
- `quota1500_w124` is supplementary. At a 1,500m quota each tenant can hold
  only one 1,000m Job. The hard quota therefore equalizes held time regardless
  of weight. Equal shares under weights 1:2:4 give a weighted Jain of
  1.75²/(3 × 1.3125) ≈ 0.778. The run measures 0.79, as expected. Weighted
  shares apply only below hard quota, so this run is not a fairness gate.
- Completion is a scheduler fixture, not worker or Docker execution.

### P: aging, reservation, starvation and races (ACC-05, ACC-10, ACC-12)

- `b13-reservation-postgres-realtime-r13.json`, a fixed real-time
  workload:
  - A 3,000m allocation is held while a 4,000m Job becomes eligible. 24
    fitting small Jobs then arrive over 115.2 s and dispatch.
  - The trace first observes the reservation 121.3 s after the large Job
    became eligible.
  - A fitting arrival was drained. A fenced `NO_CONTAINER` failure plus the
    cleanup callback released the held allocation, and the protected Job
    dispatched.
  - The small Jobs queued before and after the protected dispatch both
    dispatched afterwards, leaving zero queued Jobs. The trace spans 122.1 s.
- `b13-reservation-postgres-timeline-r13.json`: the same steps with a
  DB-aged interval.
- `b13-reservation-race-parallel-r13.json`: a tick concurrent with
  cleanup returned `DrainForReservation` before cleanup committed.
- `b13-reservation-race-cleanup-first-r13.json`: a tick delayed 250 ms
  observed the committed cleanup and returned `Dispatch`.
- In both race traces the protected Job got exactly one allocation, and held
  CPU stayed within capacity.

Other PostgreSQL regressions cover the following:

- two simultaneous ticks at tenant active limit one produce one dispatch
- a quota reduction between proposal and commit allocates nothing
- a new high-priority Job invalidates a stale proposal
- the B11 leader-takeover tests still pass

These are focused schedules, not the B22 chaos matrix, and not a universal
wait bound.

### Verification commands on the final source

Run 2026-09-25 21:54–22:01 UTC on tracked diff `8ebff3fc…`, after all benchmarks.

| Command (with `U=/tmp/nexa-b12-uv-bootstrap/bin/uv`) | Result |
|---|---|
| `$U sync --frozen --all-groups --no-editable` | Audited 41 packages, exit 0 |
| `$U run --no-sync ruff check .` | All checks passed |
| `$U run --no-sync ruff format --check .` | 357 files already formatted |
| `PYTHONPATH=src:. $U run --no-sync pytest -q` | 652 passed, 310 skipped, 23.1 s. The two warnings are Starlette/anyio deprecations from locked dependencies. |
| `NEXA_TEST_DATABASE_URL=<guarded>/nexa_b05_test_b13regression PYTHONPATH=src:. $U run --no-sync pytest --run-postgres -q` | 947 passed, 15 skipped, 282.0 s. Skips are opt-in Docker/runtime-evidence tests. Includes clean upgrade, downgrade and re-upgrade to head `0017`, the populated `0012` → head and `0016` ↔ `0017` migration regressions, the R13 default-quota backlog test, the R05 boundary tests and schema reflection. |
| `NEXA_RUN_DOCKER=1 NEXA_B11_RUNTIME_EVIDENCE=1 NEXA_B09_IMAGE_REF=nexa/cpu-iterative@sha256:341d943487940cb67f9e5ef61c2334593d8eab99619fc0f977e4f28a6d0e8e9a NEXA_B11_WORKER_IMAGE=nexa/b11-local:review NEXA_TEST_DATABASE_URL=<guarded>/nexa_b05_test_b13docker PYTHONPATH=src:. $U run --no-sync pytest --run-postgres tests/docker/test_b11_vertical.py -q` | 2 passed, 104.3 s on Docker Desktop Linux VM |
| `pnpm --dir web install --frozen-lockfile`, `run typecheck`, `run build` | pass. B13 does not change `web/`. |
| `git diff --check` | clean |

Worker image `nexa/b11-local:review` has image ID
`sha256:dfc0b8e147681ecb956e6db257b200b9f154ee08a6ab6fdbb270f9951a8b7fd6`.
Its baked `nexa/worker/*` sources hash-match the working tree. The worker
imports only `nexa.worker.*` and `nexa.infrastructure.persistence.ids`, both
unchanged. The API and coordinator in this test run from the working tree.

## Historical and superseded evidence

The following raw files are kept but do not support the current claims:

- **Before the R13 fix (heartbeat-era source).** All `*-heartbeat*` reports
  (cadence, queue, runtime fairness, reservation traces and DB fault), the
  `b13-queue-api-100k-frozen-0016.json` pending run and the
  `b13-api-prefill-100k-frozen-0016.*` prefill. They measured source before
  migration `0017`. Their runtime fairness runs used a 6,000m quota, which
  did not exercise the default-quota path that R13 found.
- **Pending-base runs labelled `steady` and `dispatch`.**
  `b13-queue-api-100k-r13-steady.json` and `-dispatch.json` are final-source
  runs, but on the pending base. They measured the replay path (all
  `NoDecision`, 64 `jobs` rows per tick) rather than steady state or
  dispatch. The `drained-*` reports replace them.
- **R13 diagnostic kept as a published fail.**
  `b13-queue-api-100k-r13-default-quota-jit-diagnostic.json` (tracked diff
  `2e911e6c…`) was the first 100k default-quota measurement of the R13 fix.
  Writes were already bounded (one `jobs` row per tick and per release, zero
  events), but measured ticks took 2,800–3,124 ms (median 2,999 ms) because of
  JIT compilation. After `jit = off`, a working-log rerun (not kept) measured
  about 0.9–1.4 s ticks, which exposed the resumed oldest-stream walk. Both
  are fixed in the final source.
- **Before the heartbeat.**
  - `b13-ledger-cadence-frozen-0016-full-rebuild.json` failed ≤1 s on the
    shared cluster with a 2,069.9 ms maximum gap (SHA-256 `3f033066…`).
  - `b13-ledger-cadence-isolated-0016-long.json` failed with a 1,051.1 ms
    maximum gap (SHA-256 `a21c121d…`).
  - `b13-ledger-cadence-isolated-0016-steady-{1,2,3}.json`,
    `b13-ledger-cadence-final-*.json` and `b13-ledger-cadence-100k*.json`
    passed short windows, but cadence then depended on tick latency. These
    failures motivated R03's heartbeat.
- **Before migration `0016`, or on a non-frozen source.**
  - `b13-api-prefill-100k.json` (100,008 Jobs; 8 written under another
    principal).
  - `b13-api-prefill-100k-final.json` (100,000 Jobs over interrupted
    continuations).
  - `b13-api-prefill-100k-lock-timeout.json`, the smoke prefills,
    `b13-queue-micro-100k*.json`, and `b13-queue-api-100k-*` reports other
    than the `heartbeat-*` and `frozen-0016.json` files cited above.
  - `b13-runtime-weighted-fairness{,-2,-3}.json`,
    `b13-reservation-*` files without `heartbeat`, and
    `b13-db-ledger-fault-restart.json`.
- **Diagnostic failures kept as published fails.**
  - `b13-queue-api-100k-rebuild.json` and `-rebuild-v2.json` had 1.4–2.6 s
    ticks with 256-Job replay batches. Replay is now bounded at 64 Jobs.
- **Earlier simulation bundle.** `b13-policy-simulation.json.gz` and
  `b13-policy-comparison.{csv,svg}` are superseded by the `-current` files.

## Findings and closure

| ID | Finding | Closure evidence | Status |
|---|---|---|---|
| B13-R01 | A changed resource/capability signature ran tenant-wide eligibility UPDATEs inside `read_snapshot` | Mutation-time events, 64-Job bounded replay, interval tests and 100k cold rebuild | Closed locally |
| B13-R02 | Concurrency saturation cleared `eligible_since` of all queued Jobs; block/resume let an old Job reserve early | Durable resume boundaries; two red/green PostgreSQL cases | Closed locally |
| B13-R03 | Ledger commit cadence depended on decision-tick latency (2,069.9 ms and 1,051.1 ms maximum gaps) | Independent 250 ms accounting heartbeat. On the final source, six 12 s windows and two 720 s runs stay ≤498.1 ms on the shared cluster. Both 720 s runs include 100-tenant rebuilds, and the cold one has two events per tenant. Plus DB-fault catch-up tests. | Closed locally |
| B13-R04 | Starvation/reservation, heap rebuild and quota/cleanup/leader races lacked production evidence | Real-time trace, both cleanup/tick orders, quota revalidation, simultaneous ticks, B11 takeover | Closed locally; B22 chaos separate |
| B13-R05 | New Jobs under held resource quota started eligible age; block/unblock boundaries inexact | API/PostgreSQL submit test and backlog event tests | Closed locally |
| B13-R06 | Tenant disable/enable kept pre-disable age | Migration `0014` and regression | Closed locally |
| B13-R07 | Oldest query could return NULL base age; `GREATEST` masked NULL after resume | Both query paths exclude NULL; two regressions | Closed locally |
| B13-R08 | Upgrading a populated B12 queue left NULL/stale ages | Upgrade-time bounded rebuild; populated `0012` → head regression | Closed locally |
| B13-R09 | Pending replay hid the tenant window and invalidated a valid reservation | >512-Job regression retaining the reservation during replay | Closed locally |
| B13-R10 | Mixed-submitter age subquery read 999 of 1,000 Jobs | Migration `0016` per-submitter streams; EXPLAIN bound and ordering regressions; 100k current-source plans | Closed locally |
| B13-R11 | A ledger DB error must not commit partial accounting or dispatch; restart must catch up exactly | Tick and heartbeat fault tests; `b13-db-ledger-fault-restart-r13.json` | Closed locally; B22 outage matrix separate |
| B13-R13 | At the default tenant quota (50% of capacity), each allocation or release that crossed quota headroom appended per-Job eligibility events, and the replay excluded the tenant. The reviewer measured 15 Dispatch, 375 NoDecision and about 27,108 `jobs` row updates at 1,500m, against 220 Dispatch at 6,000m. | Migration `0017`: per-tenant headroom staircase and queued-size histogram; `eligible_since` holds base eligibility only; dispatch and release write no Job rows or events for quota. PostgreSQL tests: ≥1,000-Job backlog at 50% quota with bounded `jobs` writes per dispatch and release, next tick dispatches, R05 boundaries unchanged. Real-time default-quota runs (3 × 3 windows, Jain ≥0.9994, zero idle-fit ticks, zero events) and the 100k default-quota report (one `jobs` row and zero events per tick and per release; plans ≤16 rows per loop, no Job `Seq Scan`). Two sub-defects were found while measuring the fix at 100k and fixed test-first. (a) JIT compilation of the 100-tenant batched statement took about 1.1 s per execution, so coordinator transactions now run with `jit = off`. (b) A resumed oldest stream walked a tenant's whole priority segment when every Job aged after the resume, so an index-backed oldest-age probe now gates it. | Closed locally; awaiting re-review |
| B13-R12 | **Open, root cause in B05, outside B13 scope.** B05 Decimal SQL functions call each other unqualified. PostgreSQL 17 maintenance operations run under a restricted `search_path`, so evaluating expression index `nexa_decimal_zero_rank(virtual_score)` (`ix_fairness_ledgers_score`) fails with `function nexa_decimal_is_valid(text) does not exist`. Consequences: `ANALYZE fairness_ledgers` and autoanalyze of that table fail in every B13 DB (16,179 such errors in the shared cluster log), and `pg_restore` needed a clone-only function `search_path` workaround. B13's 250 ms heartbeat raises the update rate, so autoanalyze is attempted more often. | Not needed for any B13 gate: the table holds one row per tenant, and the captured accounting plans are indexed and bounded. Fix proposal: an additive migration that sets `search_path` on (or schema-qualifies) the B05 Decimal functions, plus a restore test. This needs the user's scope decision and belongs to B05/B21 ownership. | Open; reported, not fixed in B13 |

## Gate matrix for the B13 slice

Status covers the applicable B13 slice only, not the full release gate.

| Gate | B13 applicability | Status | Evidence |
|---|---|---|---|
| ACC-02 | Regression: B13 changes no authorization logic; base64url decoding tightened | `pass` | Guarded suite incl. auth/scope matrix; noncanonical-cursor red/green test; prefill with 8 scoped submitters |
| ACC-03 | Regression: tenant-scoped queue/ledger rows | `pass` | 100 cross-tenant denials in the final-source prefill; guarded ownership suites |
| ACC-04 | P capacity/unreleased allocation incl. quarantine | `pass` | B11/B13 PostgreSQL tests, race traces within capacity, Docker vertical. Real GPU UUID excluded. |
| ACC-05 | P hard quota/policy mutation | `pass` | Quota reduction between proposal/commit, simultaneous ticks at limit one, cleanup/tick orders, B06/B11 quota regressions |
| ACC-06 | P admission/backpressure with B13 eligible-age rule | `pass` | Final-source 100k API prefill, B08 `429`/`503`/`Retry-After`/`422` regressions, held-quota submit test |
| ACC-07 | P idempotent submit | `pass` | 100,000 completed `submitJob` records reconciled; B08 replay regressions |
| ACC-08 | P ledger/floor, ≤1 s committed cadence, restart without reset/double-charge | `pass` | Exact accounting/refund/floor tests. Cadence table above: 8 final-source runs, all ≤498.1 ms, no tick >1 s. Fault/catch-up tests. |
| ACC-09 | D weighted fairness; P coordinator | `pass` | D: 5 seeds, min Jain 0.9876. P at the default 50% quota: 3 runs × 3 windows, min Jain 0.9994, zero events. P 6,000m control at weights 1:2:4: 3 runs × 3 windows, min 0.9845. The supplementary 1:2:4 run at 1,500m is quota-equalized (0.79 ≈ expected 0.778) and is not a gate. L/G execution fairness not claimed. |
| ACC-10 | D aging/reservation; P fixed-workload starvation | `pass` | Real-time trace (reservation at 121.3 s, drain, verified cleanup, all Jobs dispatched); invalidation regressions; R05 block/unblock boundaries on the staircase. At the default quota no tick left capacity idle because of replay. No universal bound. |
| ACC-11 | P queue ≥100,000 | `pass` | Final-source reports on API-prefilled 100k: drained steady (median 223.5 ms), dispatch (448.8 ms), default quota (10 ticks, median 478.6 ms, one `jobs` row and zero events per decision and per release), pending replay (64 Jobs per tick). No Job `Seq Scan`; ≤16 rows per loop outside the 64-Job replay page. B22 read latency/sustained load separate. |
| ACC-12 | P leadership/dispatch | `pass` | Stale-proposal, simultaneous-tick and B11 takeover tests. Heartbeat requires live leadership and never calls Docker. |
| ACC-20 | P ledger/queue durability across coordinator restart | `pass` | Fault-restart catch-up trace; prefill accepted-ID reconciliation across a new API process. Full-host reboot not claimed. |
| ACC-21 | P DB-failure fail-closed for ledger/dispatch | `pass` | Injected `P0001` on tick and heartbeat: rollback, loop stops, catch-up. Network partition/readiness matrix belongs to B22. |
| ACC-28 | P additive migrations `0008`–`0017` | `pass` | Clean upgrade/downgrade/re-upgrade to head `0017`; populated `0012` → head and populated `0016` → `0017` → `0016` → `0017` with held allocations; reflection. R12 (B05 root cause) disclosed; B21 restore not claimed. |
| ACC-35 | D/P benchmark reproducibility | `pass` | ≥5 seeds; ≥3 runs after warm-up; raw JSON, scripts, commands, provenance, SHA-256 manifest; failures published |
| ACC-39 | Tooling for affected code | `pass` | Frozen sync, Ruff check/format, default and guarded suites, Docker vertical, UI typecheck/build. Playwright/image scan not affected by B13. |

Out of scope and not inferred: B14 checkpoint/restore, B15 controls, B21
backup/restore, B22 sustained load, soak and chaos, B23 real GPU, bare-Linux
portability and release acceptance.

## SHA-256 manifest (`benchmarks/results/b13/`)

Current evidence:

| File | SHA-256 |
|---|---|
| `b13-api-prefill-100k-r13.json` | `037d2d8a2bc0da8111cca2cef6daf6a371d19fd0e407cf673fc8f35e3200c3c3` |
| `b13-api-prefill-100k-r13.accepted.jsonl` | `b9a347e8928267f430cfc590d2670c28511bb2ca23e605639a489037bf005a4d` |
| `b13-queue-api-100k-r13-drained-steady.json` | `2aed9b4e63926d97a6df71393f240eb1dc5006c897479a89904e59b2067f0288` |
| `b13-queue-api-100k-r13-drained-dispatch.json` | `05fb61a9aa94bc42c5a706aee9cb26308be45087087c260b8eba8ce485377f75` |
| `b13-queue-api-100k-r13-default-quota.json` | `7d7a2ec4f4b1779bf4ab4b2e724cfb6bad02020f2d69521532cd246c8da23d98` |
| `b13-queue-api-100k-r13-pending.json` | `67d3e7684932d0a763c696d5330a9459bb2c7603fea3c68351fb6fdb0b29adac` |
| `b13-ledger-cadence-r13-steady-1.json` | `6972c929a63b9ea343fa83715566e547571c159599f585dbe78946b36a9d3bf0` |
| `b13-ledger-cadence-r13-steady-2.json` | `d96219943d5bdaece5216ac03c620713d3f9b5248b125102347dd7c8e1009a8b` |
| `b13-ledger-cadence-r13-steady-3.json` | `4dfee2bc958df9d8fda8d28d1f15c3633496b110bdd17ffcb9835b6bd0e2b242` |
| `b13-ledger-cadence-r13-mutation-1.json` | `b1d1edb4af7e8a6de041b4f8e3ff51616e1069628232ed0c88f093ffab20e464` |
| `b13-ledger-cadence-r13-mutation-2.json` | `d5cf6e6dcfa2f49380f421bcf34cbc3a91e98844ce8dc00345afdde1c9441032` |
| `b13-ledger-cadence-r13-mutation-3.json` | `4b4b5a457d43502ead62dc2c3ceb276828bd62ebb7e41195a3bad0ad86971700` |
| `b13-ledger-cadence-r13-long-720s.json` | `50b7f988d0bd73954d1c08d4d600e3fceeabe5fc112db709dae549a906fd0550` |
| `b13-ledger-cadence-r13-cold-720s.json` | `9700ba3fdac40ece4685f587f0a2bca894a6d989f9426eb53bce7c696306b434` |
| `b13-db-ledger-fault-restart-r13.json` | `69dbc1b530c64195f8edf8d085343c0090958df26f7c517ed7bd9aa879c76f6e` |
| `b13-runtime-fairness-quota1500_1.json` | `bc1af2fa68271cb532dca8d6b63bb560d26e74d1db9dc40f256f1ffdbc589f27` |
| `b13-runtime-fairness-quota1500_2.json` | `fd12eb0c07145896c9a6a918da35cc68a1c667d28e4ce899fe6792a0ec675b1b` |
| `b13-runtime-fairness-quota1500_3.json` | `60c071eb2bfb1299624fb225d152e3a40fd6a3b10bbf485465f08dc4ade2fe05` |
| `b13-runtime-fairness-control6000_1.json` | `101bcfe75f2f0d150b6a0f4963d531e8eaca93b3ac18227261d08667a39280b6` |
| `b13-runtime-fairness-control6000_2.json` | `be0f541ce0bbb62c4f01d566673857cfbd651fd02cdd9e7cc49c6c04f768f0f6` |
| `b13-runtime-fairness-control6000_3.json` | `f1ae82fa04d57d7690a045d39059b292d419bd4a50e412e12d4d529fed3769af` |
| `b13-runtime-fairness-quota1500_w124.json` | `26a248dd46c2cf67b66036a669bad1af23b5a4a33ba1587911ce42102d9528b1` |
| `b13-reservation-postgres-realtime-r13.json` | `00079927aeb2868fb5ddbe89ca8bceea62493bd9a9f28421c5af31449d583ec6` |
| `b13-reservation-postgres-timeline-r13.json` | `8928ef2921cfacad95ef61e60d7fd18adf9d483a4b6ecd31e1eccfee5e03591c` |
| `b13-reservation-race-parallel-r13.json` | `ecb5bbae892e325739e7d92614647362e06a9c155af92ae665a789c12b3e3425` |
| `b13-reservation-race-cleanup-first-r13.json` | `e3a8a12a86ac7e9d179eeda2675ed07ae2a6aa7c728ce4181505e1dc4aacfba5` |
| `b13-policy-simulation-current.json.gz` | `01547f6ad5a6b8867d272acd0cce29a6f8db40785a829a67283d5667b430d110` |
| `b13-policy-comparison-current.csv` | `4e644f193dbfefd9b32af1ae77bc14ff3cb98e8f5923d91965128df610c5eeb0` |
| `b13-policy-comparison-current.svg` | `94f4e315f911dccbc18dc72faf60ad6a95a1c2b0bbc3ea8ce71071cd3a24da96` |

Superseded files cited above:

| File | SHA-256 |
|---|---|
| `b13-queue-api-100k-r13-steady.json` | `4fc46abeec4f593e079aada27e0e2989221d16cfa36b8d7854e11161948d87f1` |
| `b13-queue-api-100k-r13-dispatch.json` | `d5a22a639a5e0d11749e27785870b9a61fa4539763834fe7d0f7d4524167e6f5` |
| `b13-queue-api-100k-r13-default-quota-jit-diagnostic.json` | `1321cf7d802872f94bf42e640872678032d9a86ec3c29b24ee0cea84e3e34a30` |
| `b13-api-prefill-100k-frozen-0016.json` | `1c40d2fe509e055a07be9127111a2e61bd6c375e7ed03c2922e7308e7ddd3a18` |
| `b13-api-prefill-100k-frozen-0016.accepted.jsonl` | `2bbf575da03d80bb73d78298894230b6d7d57747c292fbb2b044da7218d491ae` |
| `b13-queue-api-100k-frozen-0016.json` | `293f97a8b8ca2b277f381b2a88a5643fe9867735b1a553a89521bddd882caa78` |
| `b13-queue-api-100k-heartbeat-steady.json` | `776a1267fc2dad030b95a84d13fa8dccde9cfdd8f8dee9a7b92bc5d2583a37fe` |
| `b13-queue-api-100k-heartbeat-dispatch.json` | `c14c2417c5d6d26138640bfcf64ffe6e323355cf4f748342eefd1a4fb4185c05` |
| `b13-ledger-cadence-heartbeat-steady-1.json` | `3fec1b3c11523ff8ec821b2dd59fe1ace123e4dedb040c6c62f61b81bbe8bfa2` |
| `b13-ledger-cadence-heartbeat-steady-2.json` | `8032098bc9d21984430ae4dbd1037ad59037798bdcdf4ac0b17a33cb92a4d61c` |
| `b13-ledger-cadence-heartbeat-steady-3.json` | `e035dff54e6372b3e78a8d87ae8dc441a5e766f070ca2cb9450bf0bbe5cb548c` |
| `b13-ledger-cadence-heartbeat-mutation-1.json` | `d24a28de4be0c0e6ba49dc059f4a1e1d5782ec222d8044116e6d04cfe2c0f36b` |
| `b13-ledger-cadence-heartbeat-mutation-2.json` | `55ce2ae9e749d00a484703cd78213bba572458390facb536a7451aed75989a79` |
| `b13-ledger-cadence-heartbeat-mutation-3.json` | `d1fae15807c66beb0182b16a4471bd80254fb193f7a36089fd810b5e936405e3` |
| `b13-ledger-cadence-heartbeat-long-720s.json` | `a34c05f985abb7cee973c81fcc3f3fcc89a14f1f7f9a699a3eb1b25b189cda81` |
| `b13-ledger-cadence-heartbeat-cold-shared-720s.json` | `62f0ef30d5e6f7e97a461fd7f866391e62f00d826f34243897f02cf7cecc4319` |
| `b13-db-ledger-fault-restart-heartbeat.json` | `fec4e0568a12dae8e307e6f6dd62ef25c3795a74b4ec58089c0496188909de72` |
| `b13-runtime-weighted-fairness-heartbeat-1.json` | `89c34035ecda75f08750b3a2c6750d4f0aafe9c152adc8249695392b4f538332` |
| `b13-runtime-weighted-fairness-heartbeat-2.json` | `fc42304d1ca9856a667209b486b18912396ca85d1832afae203faaa6a9fcea70` |
| `b13-runtime-weighted-fairness-heartbeat-3.json` | `224fc4baf0dd5901f0557660340abab43a6c2de65e6bbb72b3c72fa49a18b709` |
| `b13-reservation-postgres-realtime-heartbeat.json` | `3c706fc8e82ea9837ec263c2cb289c4ba93f8d9d7dcbdea16d5887005687bed8` |
| `b13-reservation-postgres-timeline-heartbeat.json` | `9b9696473479738fa8bfd659d9039e240f05077d6d37a4755f34ab7c61ae88f8` |
| `b13-reservation-race-parallel-heartbeat.json` | `318003326433f2d052e8e6bb3cd66e152bb3b32aebca81dd7524196d1098198b` |
| `b13-reservation-race-cleanup-first-heartbeat.json` | `933d796d8aa7649b3930536154be9598fd334981c29f5f2ae7b55be08bb886f8` |

Recheck with `cd benchmarks/results/b13 && shasum -a 256 <file>`.
