# B04 Fairness Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The user explicitly selected inline execution in the current checkout and forbids branch, worktree, commit, push, merge, or publish operations.

**Goal:** Implement the pure weighted dominant resource-time policy with hard quota, eligible aging, and one local reservation, integrate it with the B03 simulator without changing B03 baseline semantics, and produce reproducible five-seed B04 evidence.

**Architecture:** Immutable product scheduling types live under `src/nexa/domain`, while deterministic accounting and decision logic live under `src/nexa/scheduler` and depend only on those types plus the Python standard library. A separate `benchmarks/b04` harness reuses B03 trace/model/clock/baselines, builds bounded product snapshots, applies product decisions on an explicit event timeline, and emits B04-only raw/report artifacts. The harness owns simulated mutable state; neither policy nor accounting keeps global or singleton state.

**Tech Stack:** Python 3.12, frozen dataclasses, `Decimal` arithmetic with an explicit local context, Pytest, Hypothesis, B03 virtual clock/trace/baselines, deterministic JSON/CSV/SVG generation.

**Spec:** Approved [PLAN.md](../../../PLAN.md) sections 5, 6, 10, 13, and 14; [internal scheduler interface](../../contracts/internal-interfaces.md); [domain fairness model](../../contracts/domain-model.md); INV-02 through INV-07; ACC-04, ACC-08 through ACC-11, ACC-35, and ACC-39; plus the user-provided B04 execution brief dated 2026-09-19.

## Global Constraints

- Implement only the pure B04 policy and simulator evidence; do not add PostgreSQL, API, coordinator, worker, Docker, recovery, product CLI, or UI behavior.
- Product code must not import `benchmarks`, perform I/O, read a global clock, use randomness, assign fences, or select concrete GPU UUIDs.
- Use deterministic non-binary arithmetic for policy ordering/accounting; compare accounting against an independent `Fraction` oracle in tests.
- Treat HELD and QUARANTINED allocations as capacity/quota held and chargeable until explicit release.
- Limit each tenant window to at most 16 normal candidates plus one `oldest_eligible`; reject excess instead of truncating.
- Keep B03 source semantics, selected raw artifact, CSV, SVG, and checksums unchanged.
- Do not commit. Record Git status, HEAD `c14fcc822e86ac6aaa3c83c2008a9fc777708b47`, initial tracked-tree manifest SHA-256 `dd64edd736af4a4b0050eb2bf4ee0afb9be20460113aec33899f88c17ad8181a`, and B03 input artifact hashes in B04 evidence.

## Requirement Map

| Requirement | Product/harness location | Primary tests | Evidence/dependency |
|---|---|---|---|
| Immutable contract, typed decisions/errors, bounded candidate windows | `src/nexa/domain/scheduling.py`, `src/nexa/scheduler/policy.py` | `tests/scheduler/test_contract.py`, `test_policy.py` | B11 consumes proposal/version types; B13 supplies DB snapshots |
| Dominant resource-time, quarantine charge, virtual floor | `src/nexa/scheduler/accounting.py` | `tests/scheduler/test_accounting.py`, `test_properties.py` | ACC-08 D-model evidence; B05 chooses storage precision |
| Eligibility, deterministic ordering, 60-second aging | `src/nexa/scheduler/policy.py` | `tests/scheduler/test_policy.py` | ACC-09/10 D evidence |
| One local reservation, drain/dispatch/invalidate timeline | `src/nexa/scheduler/policy.py`, `benchmarks/b04/engine.py` | `tests/scheduler/test_reservation.py`, `tests/benchmarks/b04/test_engine.py` | B11/B13 apply reservation transactionally |
| Capacity/quota/concurrency/GPU model invariants | policy validation plus B04 harness allocation ledger | unit/property/integration tests | ACC-04 D subset only |
| B03 adapter and matched baselines | `benchmarks/b04/adapter.py`, `engine.py`, `suite.py` | B03 regression suite plus B04 integration | preserves B03 evidence; no runtime claim |
| Five-seed fairness/starvation report | `benchmarks/b04/cli.py`, `report.py`, fixtures/results/plots | report/replay tests and selected benchmark run | `docs/evidence/B04-fairness.md`, ACC-09/10 D subset |

---

### Task 1: Define immutable scheduling contract values

**Files:**
- Create: `src/nexa/domain/scheduling.py`
- Create: `src/nexa/scheduler/__init__.py`
- Create: `tests/scheduler/test_contract.py`

**Interfaces:**
- Produces: `ResourceCapacity`, `ResourceRequest`, `HeldAllocation`, `AllocationState`, `TenantLedger`, `TenantPolicySnapshot`, `Candidate`, `CandidateWindow`, `ReservationSnapshot`, `SchedulingSnapshot`.
- Produces: `Dispatch`, `CreateReservation`, `DrainForReservation`, `InvalidateReservation`, `NoDecision`, `Ok[T]`, `Err[E]`, and the five required error classes.
- Time unit: integer milliseconds for accounting instants; eligible age remains integer seconds from authoritative accumulated eligibility.

- [x] Write contract tests for immutability, decision payload versions, all five error variants, and value equality without relying on source text.
- [x] Run `./.venv/bin/pytest -q tests/scheduler/test_contract.py` and confirm RED because product types do not exist.
- [x] Implement the minimal frozen value types and result/decision/error unions.
- [x] Run the focused test and confirm GREEN; run Ruff on the new files.

### Task 2: Implement deterministic accounting and virtual floor

**Files:**
- Create: `src/nexa/scheduler/accounting.py`
- Create: `tests/scheduler/test_accounting.py`

**Interfaces:**
- Produces: `advance_accounting(snapshot: SchedulingSnapshot, now_ms: int) -> Ok[AccountingState] | Err[PolicyError]`.
- `AccountingState` returns immutable updated ledgers, post-transition virtual floor, aggregate held vectors, dominant shares, and eligible-tenant IDs. The caller owns persistence/application; repeated calls at the same instant add zero charge.
- Decimal rule: `localcontext(prec=50, rounding=ROUND_HALF_EVEN)` with exact integer-to-Decimal conversion; no fixed database scale is selected by B04.

- [x] Write RED tests for the `(4,1)+(1,4)` hand calculation, quarantine charging, multiple resources, repeated tick, time regression, allocation/release/weight/capacity boundaries, inactive-to-active floor, tenant return, simultaneous activation, and empty backlog.
- [x] Include a `Fraction` oracle property test whose expected value does not call production helpers.
- [x] Implement aggregate-vector dominant share and immutable ledger advancement.
- [x] Run focused accounting tests GREEN and format/lint the touched files.

### Task 3: Implement eligibility, aging, ordering, and bounded dispatch

**Files:**
- Create: `src/nexa/scheduler/policy.py`
- Create: `tests/scheduler/test_policy.py`

**Interfaces:**
- Produces: `WeightedDominantResourceTimePolicy.decide(snapshot, now_ms) -> Ok[SchedulingDecision] | Err[PolicyError]`.
- Candidate eligibility checks queued state, retry time, template/capability compatibility, total capacity, tenant hard resource quota, and tenant/user concurrency. Current-free fit is checked only when selecting a dispatch.
- Tenant key: accounted score, dominant share divided by weight, oldest ready sequence, tenant ID. Job key: negative effective priority, ready sequence, job ID.

- [x] Write RED tests for every tenant/job tie-break, retry/capability/concurrency/quota filters, non-fitting-but-eligible age, 59/60/61 and 119/120/121 boundaries, priority cap, FIFO ties, input-order invariance, candidate de-duplication, continuation cursors, and all invalid snapshots.
- [x] Implement validation, eligibility, deterministic sort keys, fitting-candidate selection, and `NoDecision` reasons.
- [x] Run focused policy tests GREEN and the contract/accounting suite.

### Task 4: Implement reservation lifecycle

**Files:**
- Modify: `src/nexa/scheduler/policy.py`
- Create: `tests/scheduler/test_reservation.py`

**Interfaces:**
- Active reservation validation uses `ReservationSnapshot.invalid_reason` when the coordinator/harness reports cancel, policy, capability, or eligibility invalidation.
- A valid active reservation returns `DrainForReservation` while it cannot fit current free resources and `Dispatch(..., reservation_id=...)` when it can fit.

- [x] Write RED tests for one reservation only, fairness-selected tenant rather than global FIFO, oldest candidate outside the normal 16, create/drain/dispatch, explicit invalidation reasons, and infeasible jobs never creating a reservation.
- [x] Implement minimal reservation precedence and lifecycle decisions.
- [x] Run focused reservation tests GREEN and all scheduler tests.

### Task 5: Extend B03 with a B04 product-policy simulator

**Files:**
- Create: `benchmarks/b04/__init__.py`
- Create: `benchmarks/b04/adapter.py`
- Create: `benchmarks/b04/engine.py`
- Create: `tests/benchmarks/b04/test_adapter.py`
- Create: `tests/benchmarks/b04/test_engine.py`

**Interfaces:**
- `ProductSnapshotAdapter.build(...)` maps B03 immutable jobs/tenants/allocations plus explicit harness state into the product snapshot, with at most 16 normal candidates and one oldest eligible per tenant.
- `ProductPolicySimulator.run(trace, policy)` owns ledger/floor/reservation state, schedules arrival/release plus accounting/aging/reservation wakeups, applies logical decisions, and records deterministic allocation, score, candidate, and reservation events.
- Duration and future releases remain engine-only; policy sees neither future duration nor future trace.

- [x] Write RED integration tests proving non-fitting large jobs remain visible, 1-second accounting wakeups do not double-charge, 60/120-second wakeups occur without arrival/release, drain blocks small dispatches, the loop terminates, and every dispatch stays within capacity/quota/concurrency with unique simulated GPU UUIDs.
- [x] Implement adapter and engine minimally; reuse B03 clock/model/trace and allocation ledger rather than copying baseline algorithms.
- [x] Run B04 integration tests GREEN, then run `tests/benchmarks` to prove B03 behavior remains unchanged.

### Task 6: Add model/property coverage and snapshot reconstruction

**Files:**
- Create: `tests/scheduler/test_properties.py`
- Create: `tests/benchmarks/b04/test_properties.py`

**Interfaces:**
- Properties generate bounded tenants, candidates, held allocations, time advances, releases, and eligibility changes.
- Reconstruction serializes only immutable harness state and creates a new policy instance; decisions/accounting must match without hidden state.

- [x] Write RED property/model tests for no oversubscription, no duplicate GPU, no quota/concurrency breach, monotone score/floor, result references only snapshot candidates, deterministic input permutation, rational accounting agreement, reconstruction equivalence, and explicit stalled-loop reporting.
- [x] Add only the production/harness behavior needed to satisfy the failing properties.
- [x] Run scheduler and B04 property suites GREEN with the default deterministic Hypothesis profile.

### Task 7: Build the fixed B04 benchmark suite and reproducible reports

**Files:**
- Create: `benchmarks/fixtures/b04-suite.json`
- Create: versioned B04 trace fixtures under `benchmarks/fixtures/b04-*.json`
- Create: `benchmarks/b04/suite.py`
- Create: `benchmarks/b04/cli.py`
- Create: `benchmarks/b04/report.py`
- Create: `tests/benchmarks/b04/test_cli.py`
- Create: `tests/benchmarks/b04/test_report.py`
- Create after verification: `benchmarks/results/b04-fairness.json`
- Create after verification: `benchmarks/plots/b04-fairness.csv`
- Create after verification: `benchmarks/plots/b04-fairness.svg`

**Fixed benchmark table (declared before measurement):**

| Profile | Seeds | Capacity/resources | Weights | Purpose/pass rule |
|---|---|---|---|---|
| `uniform-equal` | 7,11,19,23,29 | CPU/RAM, uniform 5 s jobs | 1:1:1 | Valid continuous-demand Jain for B04 is at least 0.95 per seed |
| `mixed-equal` | 7,11,19,23,29 | CPU/RAM, 1/5/30/120 s mix | 1:1:1 | Valid Jain at least 0.95 per seed; all feasible jobs dispatch |
| `weighted-124` | 7,11,19,23,29 | CPU/RAM, concurrency/quota configured to permit target | 1:2:4 | Valid weighted Jain at least 0.95 per seed |
| `gpu-slots` | 7,11,19,23,29 | simulated whole GPU UUID slots plus CPU/RAM | 1:2:1 | No duplicate UUID; valid weighted Jain at least 0.95 per seed |
| `large-reservation` | 7,11,19,23,29 | large job blocked by ongoing small arrivals | 1:1 | Large job is not dispatched initially, reservation drains, large job dispatches while small arrivals continue |
| `constrained-diagnostic` | 7,11,19,23,29 | quota/concurrency makes target ratio infeasible | 1:2:4 | Explicit exclusions or Jain null/N/A; never presented as a fairness pass |

All six policies (`nexa`, `fifo`, `rr`, `wrr`, `drr`, `drf`) use the same materialized B03 trace, capacity, tenant quota/concurrency, and explicit B04 user mapping within each profile. Cost is candidate evaluations and policy decisions from the same virtual run; no wall-clock duration is used as algorithmic evidence.

- [x] Write RED CLI/report tests for matched materialized checksums, five seeds, fixed profile config, deterministic raw JSON, per-seed pass/fail visibility, null Jain preservation, tenant/allocation/reservation timelines, and atomic report output.
- [x] Implement suite loading, matched baseline/product execution, raw schema, CSV, and readable SVG generation.
- [x] Read and follow `.agents/skills/benchmarking-scheduler-fairness/SKILL.md` before running selected measurements.
- [x] Run the fixed suite once without tuning after results; if a required profile fails, preserve the result, diagnose with a regression test, fix correctness only, and rerun the unchanged suite.
- [x] Reproduce JSON/CSV/SVG into `benchmarks/tmp/` and compare byte-for-byte; parse JSON/CSV/XML and render-inspect the SVG.

### Task 8: Document evidence, update current-state docs, and verify

**Files:**
- Create: `docs/evidence/B04-fairness.md`
- Modify: `README.md`
- Modify: `ROADMAP.md`
- Modify: `docs/project-structure.md`
- Modify only if its current-state sentence is obsolete: `AGENTS.md`
- Update checkboxes/status in this plan.

**Interfaces:**
- Evidence separates direct D-layer results from future P/L/G requirements and records the B04 handoff contracts for B05/B11/B13.
- ROADMAP status is `đã implement, chờ Review` until an independent Task Review approves the final diff.

- [x] Write the evidence from generated raw data, including HEAD/dirty manifest, exact tool versions/commands, fixture/config hashes, per-profile/per-seed results, reservation timeline, limitations, and ACC applicability.
- [x] Update README commands and project structure using only scripts/modules that now exist; leave B03 artifact references intact.
- [x] Run focused scheduler/B04 tests, B03 regressions, full pytest, lock check, frozen non-editable sync/reinstall, Ruff check/format, artifact replay, `git diff --check`, status/diff/untracked/ignore audits, and source/artifact hashes.
- [x] Use `superpowers:requesting-code-review` for an internal final diff review and resolve correctness findings with RED regressions.
- [x] Use `superpowers:verification-before-completion` before reporting; state `sẵn sàng review`, not `Task Review đã duyệt`.

### Task 9: Preserve an active reservation in the bounded adapter snapshot

**Files:**
- Modify: `tests/benchmarks/b04/test_engine.py`
- Modify: `benchmarks/b04/adapter.py`

**Interfaces:**
- When `active_reservation` names a queued job in its owning tenant, `ProductSnapshotAdapter.build(...)` must retain that exact candidate in the tenant window without exceeding 16 normal candidates plus one `oldest_eligible` candidate.
- The policy remains responsible for validating the retained candidate and dispatching, draining, or invalidating the reservation. The adapter must not infer a new invalidation reason from pagination.

- [x] Add a simulator regression with 16 older priority-2 jobs initially blocked by tenant quota, a protected job reserved at `120001 ms`, and release at `130000 ms`; assert the original reservation identity dispatches immediately and no invalidation occurs.
- [x] Run the focused regression and confirm RED because the protected job disappears from both `normal` and `oldest_eligible` after release.
- [x] Make the active reserved candidate occupy the extra `oldest_eligible` slot for its tenant while preserving the 16-candidate normal bound and continuation cursor.
- [x] Run the focused regression GREEN, then run adapter, engine, scheduler reservation, and property tests.

### Task 10: Require observable starvation prevention in reservation evidence

**Files:**
- Modify: `tests/benchmarks/b04/test_report.py`
- Modify: `tests/benchmarks/b04/test_engine.py`
- Modify: `benchmarks/b04/cli.py`
- Modify: `benchmarks/b04/report.py`
- Modify: `benchmarks/fixtures/b04-large-reservation.json`
- Regenerate: `benchmarks/results/b04-fairness.json`
- Regenerate: `benchmarks/plots/b04-fairness.csv`
- Regenerate: `benchmarks/plots/b04-fairness.svg`
- Modify: `README.md`
- Modify: `docs/evidence/B04-fairness.md`

**Interfaces:**
- Each serialized decision records the IDs of other eligible candidates that fit current free resources. Reservation profile validation requires a drain decision that withholds at least one such candidate, at least two distinct allocation release times after reservation creation and no later than protected dispatch, an arrival strictly between create and dispatch, and another arrival strictly after protected dispatch.
- The fixed reservation trace uses staggered releases and continuous small arrivals. All six policies still run the same materialized trace for each of the five fixed seeds; only Nexa is eligible for the reservation profile pass status.

- [x] Add RED report tests that independently reject missing fitting-job withholding, non-staggered releases, and arrivals ending before protected dispatch.
- [x] Serialize fitting alternative candidate IDs and implement the stricter reservation evidence predicate; run the focused report tests GREEN.
- [x] Replace the reservation fixture with staggered initial releases and small arrivals before and after protected dispatch; add an engine assertion that Nexa drains instead of dispatching a fitting small job and dispatches the large job before comparison baselines.
- [x] Run the focused engine test GREEN and confirm all jobs still finish without capacity/quota/concurrency violations.
- [x] Run the unchanged six-profile, five-seed, six-policy suite; regenerate JSON/CSV/SVG, replay byte-for-byte, and validate 180 rows, 30 Nexa profile passes, and 20 valid fairness passes.
- [x] Update hashes, reservation timeline, review remediation, evidence limitations, README wording, and this plan; run the full verification and hygiene commands without committing or changing the ROADMAP review status.

## Completion and Handoff

B04 is ready for independent review only when the pure policy and simulator tests pass, every mandatory valid fairness seed reaches the fixed `J >= 0.95` target, the large-job trace proves create/drain/dispatch while small work continues, the B03 suite/artifact remains reproducible, reports replay byte-for-byte, and no Critical/Important correctness finding remains. B05 receives immutable value/arithmetic compatibility requirements but still chooses database precision; B11 receives typed proposal/version/reservation semantics and must recheck/commit atomically; B13 receives the bounded candidate/floor/accounting contract and must supply durable ledger/index/heap/query evidence.
