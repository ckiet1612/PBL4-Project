# B13 Production Fairness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make B04 fairness, admission, durable accounting, and bounded queue retrieval work through the B11 production coordinator, with reproducible B13 evidence.

**Architecture:** Preserve the pure B04 policy and the B11 PostgreSQL transaction boundary. PostgreSQL owns queue, quota, ledger, reservation, and cursor facts; any heap is a disposable proposal cache. Each dispatch commit rechecks leader, policy, job, worker, allocation, counters, and reservation under the documented lock order.

**Tech Stack:** Python 3.12, SQLAlchemy 2, psycopg, PostgreSQL 17, Alembic, Pytest/Hypothesis, Docker CPU worker, Ruff.

**Spec:** `PLAN.md` sections 3-6, 9-10, 13-14; `docs/contracts/internal-interfaces.md`; `docs/contracts/concurrency-recovery.md`; the B13 task brief attached to this task.

## Global Constraints

- Work in the supplied checkout. Do not branch, create a worktree, commit, push, edit PLAN, install skills, deploy, publish, or launch another task.
- Preserve exact Decimal score/weight storage and B04 ordering. No independent scheduler, admission, or ledger subsystem.
- Keep `NEXA_TEST_DATABASE_URL` guarded to PostgreSQL 17 and a `nexa_b05_test_` database; never use `NEXA_DATABASE_URL` for destructive tests.
- Separate D policy, P API/PostgreSQL, L Docker execution, and G real GPU evidence. A missing mandatory B13 measurement remains `blocked` or `not-run`.
- User authorization to implement in this checkout overrides generic skill instructions to stop after planning, require a worktree/commit, or dispatch a reviewer subagent.

## Current Checkpoint (2026-09-25)

Implemented and verified locally: maintained heads/submitters, 16+1 indexed
candidate windows, exact-Decimal heap, batched ledger statements, concurrency
resume times, retry-ready age boundary, and API submit initialization under held
resource quota. The fixed B04 simulation, 100,008 accepted-ID API audit, 100k
drain/dispatch microbenchmarks, clean migration cycle, guarded PostgreSQL suite
and Docker CPU regression have raw evidence in
`docs/evidence/B13-production-fairness.md`.

Earlier checkpoint: resource/capability mutation reconciled queued rows widely,
and cold cadence, production starvation and race evidence were not yet measured.
The 100k authenticated write prefill began before the final submit-age change;
its accepted-ID audit remains valid, but it is not a single frozen-source run.

Current checkpoint: migration `0014` records tenant enable/disable boundaries
and starts bounded rebuild for preexisting queue rows. Candidate queries reject
NULL base age, 64-Job replay uses one PostgreSQL UPDATE, and commit revalidation
does not replay a second page. Focused PostgreSQL tests cover migration from a
populated `0012`, admin HTTP toggles, pending reservation and policy/dispatch
race. The API journal reconciles 100,000 accepted IDs, Jobs and global counter;
the final continuation added 2,870 writes with two concurrent requests. Three
steady, three cold-process and three mutation ledger windows on a 100k clone
all had maximum commit gaps below one second. Source-current indexed queue
steady/dispatch plans, the full PostgreSQL suite and Docker CPU vertical pass.
The full 100k cold eligibility replay completed in a 720-second window with
zero pending events, zero errors and a maximum committed-ledger gap of 846 ms.
Source-current event/head/candidate EXPLAIN plans were captured on the same
100k clone. Three independent PostgreSQL weighted runtime runs exceeded Jain
0.95, and a real-time 121-second reservation trace plus both cleanup/tick
commit orders were recorded. A fully frozen-source 100k API prefill and the
remaining affected DB fault evidence are required before B13 review handoff.

Latest continuation: a ledger UPDATE fault test now records atomic rollback and
exact new-process catch-up in `b13-db-ledger-fault-restart.json`. A new
1,000-Job, two-submitter EXPLAIN regression reproduced a 999-row scan after
different resume boundaries. Grouped indexed streams and migration `0016`
reduced Job reads/filters below the test's 32-row bound; a separate behavior
test covers age ordering, oldest, continuation and wrap. Current source passed
the default suite (650 passed, 296 skipped), guarded PostgreSQL suite (931
passed, 15 skipped), Ruff and diff check. A fresh guarded database is receiving
one uninterrupted 100,000-Job authenticated API prefill; its final provenance,
accepted-ID reconciliation, query plans and current-source Docker CPU vertical
are still required for handoff.

Final checkpoint (supersedes the open items above):

- The frozen-source prefill completed in one run: 100,000 accepted, 0 errors,
  provenance unchanged, with accepted IDs reconciled.
- Isolated and shared 720 s runs exposed ledger gaps above one second on the
  cold path: 1,051.1 ms and 2,069.9 ms. The cause was that ledger cadence
  followed decision-tick latency. An independent 250 ms
  `coordinator-accounting` heartbeat now commits the ledger. It locks only
  ledger rows, and policy-locked callers refund overlap. On failure it stops
  the coordinator, and the next leader catches up from the committed boundary.
- On current source:
  - Six 12 s cadence windows and two 720 s runs, one of them the
    shared-cluster 100-event cold rebuild, stayed at or below 635.7 ms.
  - Queue steady/dispatch plans, three weighted runtime runs (Jain minimum
    0.9784), reservation and race traces, and tick and heartbeat DB-fault
    tests were all rerun.
  - Frozen sync, Ruff, the default suite (652 passed), the guarded suite (940
    passed), the Docker CPU vertical (2 passed) and UI typecheck/build pass.
- The B05 Decimal-function `search_path` defect behind the
  `ANALYZE`/autoanalyze failure on `fairness_ledgers` is recorded as open
  finding B13-R12, outside B13 scope.
- The status and gate matrix are in the evidence document; B13 awaits
  independent Task Review.

R13 checkpoint (2026-09-26, after Task Review round 1 "Không duyệt"):

- Finding B13-R13: at the default 50% tenant quota, each allocation or release
  that crossed quota headroom appended an eligibility event and replayed the
  tenant's queue, which excluded the tenant from dispatch.
- Fix, test-first:
  - Migration `0017` adds a per-tenant CPU/memory headroom staircase
    (`quota_headroom_steps`) and a queued-size histogram
    (`queue_request_sizes`). `eligible_since` is base age only.
  - Dispatch and release write no Job rows or events for quota.
  - The R05 block/unblock boundaries are unchanged.
- Two sub-defects found at 100k, each fixed with a red test first:
  - JIT compile of the 100-tenant queue statement took ~1.1 s, so
    coordinator transactions now set `jit = off`.
  - A resumed oldest stream walked the tenant's whole queue, so it is now
    gated by an ordered oldest-age probe.
- Final source `8ebff3fc…`. Every P report was regenerated on it: the API
  prefill, the drained, default-quota and pending 100k queue runs, 8 cadence
  runs (≤498.1 ms), real-time fairness at the default quota (3 runs, Jain
  ≥0.9994, zero idle-fit, zero events) plus the 6,000m control, reservation
  and race traces, and DB fault.
- Verification: default suite 652 passed, guarded suite 947 passed, Docker
  CPU vertical 2 passed, Ruff, UI typecheck/build and diff check.
- Kept the unused `0009` `eligibility_signature` column for frozen-evidence
  schema provenance. R12 remains open for the user's decision.

## Existing Components And Gap Map

| Requirement | Existing implementation/test | Gap to close |
|---|---|---|
| Dominant resource-time, floor, ordering, reservation | `src/nexa/scheduler/{accounting,policy}.py`; `tests/scheduler/` | Production integration and measured restart/cadence proof |
| Durable ledger and boundaries | `src/nexa/coordinator/accounting.py`; B11 integration | Per-tick work across open segments, commit cadence, exact no-double-charge evidence |
| Hard admission and fenced dispatch | `application/job_service.py`, `policy_service.py`, `coordinator/service.py`, cleanup; B08/B11 tests | Concurrency and revalidation regressions under B13 changes |
| Queue retrieval | `coordinator/snapshot.py`, `dispatch.py`; indexes and unused `queue_heads` table | Whole-queue eligibility UPDATE, computed-priority sort, no maintained heads, unbounded reset and oldest lookup |
| Benchmark | B03/B04 simulator and report | B13 frozen 5-seed result, P 100,000-job query plans and 3 repeated real runs |

## Data And Transaction Boundaries

- `account_locked(session, now)` charges the open allocation interval once using the persisted boundary; `rebase_locked(session, now)` closes old segments and opens new ones after allocation, release, weight, or capacity mutation in the same transaction. Keep exact Decimal arithmetic and aggregate tenant vectors before dominant share.
- Queue derived state must be reproducible from Job, Spec, policy, counters, inventory, and DB time. Changes to submit/state/retry/priority, quota/concurrency, inventory/template, and reservation invalidate the affected head/candidate view. On restart/takeover, rebuild in bounded batches; steady-state ticks cannot rebuild the full queue.
- Candidate retrieval emits at most 16 normal candidates and one independent oldest/reserved candidate per tenant. Cursors advance past blocked windows. Heap ordering uses exact `Decimal` score, share/weight, sequence, tenant ID. It may propose; commit transaction reads authoritative rows again.
- Use the existing global-policy, tenant-policy, counter, worker, job, attempt/lease, allocation lock order. Keep the worker-callback lock exception in the concurrency contract. No Docker, filesystem, or network I/O inside these transactions.

### Task 1: Establish B13 Regressions

**Files:** `tests/integration/test_coordinator_b13.py`, `tests/coordinator/test_runtime.py`, existing B11 tests as needed.

**Interfaces:** Exercise `CoordinatorService.tick`, `account_locked`, `read_snapshot`, and `apply_decision` via a guarded PostgreSQL 17 fixture; observe persisted rows, not mocks.

- [ ] Add failing tests for a blocked seventeenth candidate, true queue head after mutation, contiguous eligible age across quota/concurrency/capability changes, exact ledger boundary and restart, and stale proposal rejection.
- [ ] Run each targeted test to confirm the expected failure before source edits.
- [ ] Keep each test's oracle independent: explicit expected Job/Allocation/Segment rows, timestamps, counts, and choice IDs.

### Task 2: Bounded Queue And Eligibility Maintenance

**Files:** `src/nexa/coordinator/snapshot.py`, `dispatch.py`, `service.py`; `src/nexa/infrastructure/persistence/schema_v7.py`, `schema.py`; one new Alembic revision after `20260923_0007`; direct mutation services only where needed.

**Interfaces:** `read_snapshot(session, now, policy, worker, inventory, cursors) -> SchedulingSnapshot`; keep `CandidateWindow` contract and exact B04 policy inputs. Maintain `(tenant_id, priority) -> earliest relevant job` and indexed retry/aging promotion; use bounded batch/cursor methods for backlog.

- [ ] Write failing DB tests that inspect head rows and verify 16+1, candidate 17 progress, retry/aging boundary, and reservation outside the normal window.
- [ ] Add only the schema/index/derived-state support required by those tests; migration includes upgrade, guarded downgrade, and schema metadata parity.
- [ ] Replace per-tick whole-queue eligibility updates and computed full-queue priority sort with indexed bounded retrieval and event-driven/batched invalidation. Verify query plans on a real queue before calling the path bounded.
- [ ] Revalidate candidate eligibility and all capacity/quota/authority under commit locks; reject stale proposal without allocating.
- [ ] Run targeted tests, then B04/B11 and admission/policy/cleanup regressions.

### Task 3: Accounting And Cadence

**Files:** `src/nexa/coordinator/accounting.py`, `runtime.py`, `service.py`, focused tests.

**Interfaces:** `account_locked(session, now)` and `rebase_locked(session, now)` remain transactional; no change to Decimal representation or segment provenance.

- [ ] Add a failing test showing repeated ticks do not traverse historical segments or double charge; add a test of aggregate `(4,1)+(1,4)` at `(8,8)` as `5/8`, quarantine, and weight/capacity boundary.
- [ ] Implement a bounded open-segment path and measure committed `accounted_through` intervals with the coordinator running; DB failure must stop new allocation and catch up on recovery.
- [ ] Verify rollback and new-process restart preserve score and floor exactly.

### Task 4: Quota, Reservation, And Race Regression

**Files:** focused integration tests and production modules only for reproduced bugs.

**Interfaces:** Existing REST admission and policy services, B11 coordinator, fenced cleanup.

- [ ] Test submit replay/rate/counter and policy reduction below held resources; race dispatch against policy update, release, duplicate callback, and leadership takeover.
- [ ] Test large-job reservation with small arrivals before/after protected dispatch, actual drain of a fitting small job, invalidation reason, and release proof.
- [ ] Fix each observed bug test-first and rerun directly affected suites.

### Task 5: Frozen Benchmark And Evidence

**Files:** `benchmarks/b13/`, `benchmarks/results/b13/`, `docs/evidence/B13-production-fairness.md`.

**Interfaces:** Reuse B04 simulator traces and production REST API. Benchmark fixture seeding through SQL is labeled a DB microbenchmark, never API acceptance.

- [ ] Freeze seeds (at least five), trace/cohort/window, capacity/reserve/quota/weights, revision/diff, and baseline cost before running D fairness. Compute Jain from dominant resource-time per weight, not score or completion count.
- [ ] On a dedicated PostgreSQL 17 test database, prefill at least 100,000 queued jobs through authenticated production API with versioned quota/rate and accepted-ID records. Warm up, run at least three measured repetitions, retain raw EXPLAIN (ANALYZE, BUFFERS) for snapshot, commit, no-fit, drain, promotion, head, reservation, and accounting paths.
- [ ] Record median/spread/errors, rows/buffers/sorts/query counts, accepted-ID/counter/ledger reconciliation, restart, environment, command, raw manifest/checksums, and plot generation. Mark any missing layer honestly.

### Task 6: Review, Docs, And Verification

**Files:** `docs/coordinator.md`, `docs/project-structure.md`, affected contract, `README.md`, `ROADMAP.md`, B13 evidence.

- [ ] Document exact invalidation/rebuild and transaction design, changed schema and operational commands; keep other milestones' status intact.
- [ ] Run `ruff check`, `ruff format --check`, default Pytest, guarded PostgreSQL tests, migration upgrade/downgrade/upgrade, relevant Docker CPU smoke, and `git diff --check` on current source (`PYTHONPATH=src` or reinstall after frozen sync).
- [ ] Review the whole tracked diff and every untracked file for correctness, secret leakage, and gate applicability. Record B13-Rxx causes, reproductions, changes, closure evidence, and remaining blockers.
- [ ] Hand over only after implementation and mandatory B13 evidence are complete; otherwise report precise unfinished gate and commands, without a completion claim.

## Gate Map

ACC-04/05/06/07/08/09/10/11/12/20/21/28/35/39 apply to the B13 affected behavior, with evidence layers separated. ACC-02/03 apply only if API/authorization changes. B14/B15 checkpoint/recovery control, B22 load/soak/chaos, B23 GPU, and release acceptance are outside this implementation and cannot be marked passed by B13.
