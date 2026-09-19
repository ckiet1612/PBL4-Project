# B04 fairness, aging, and reservation evidence

- **Task:** B04 — pure weighted dominant resource-time policy, eligible aging, and one local reservation
- **Status:** `implemented — review findings remediated, awaiting independent Task Review re-review`
- **Base revision:** `c14fcc822e86ac6aaa3c83c2008a9fc777708b47`
- **Working tree:** B04 was implemented directly on `main` under the approved no-branch/no-commit exception. The base tree was clean and all B04 files were initially untracked, so `git diff` alone does not describe the work.
- **Initial tracked-tree manifest:** `dd64edd736af4a4b0050eb2bf4ee0afb9be20460113aec33899f88c17ad8181a`
- **Evidence time:** 2026-09-19 (`Asia/Ho_Chi_Minh`)
- **Evidence layer:** D — deterministic unit/property/model and simulator evidence only

## Scope and boundary

B04 adds immutable product scheduling values in `src/nexa/domain/`, deterministic accounting and policy code in `src/nexa/scheduler/`, and a separate B04 harness that maps B03 traces into the product snapshot contract. The policy is stateless and receives time explicitly. It performs no I/O, reads no global clock, uses no random tie-break, assigns no fence or attempt, and does not select a concrete GPU UUID.

The harness owns simulated ledger, virtual-floor, allocation and reservation state. It preserves B03 baseline semantics and imports the product policy rather than copying it. Duration and future release information remain engine-only. B04 does not add PostgreSQL, migrations, API/coordinator/worker processes, Docker execution, durable transactions, lease/fence/recovery, product CLI, Web UI, real GPU discovery, or runtime acceptance evidence.

## Source and artifacts

| Item | Path / value | SHA-256 |
|---|---|---|
| B04 product, harness and test manifest | ordered `sha256sum` manifest for `src/nexa/domain/{__init__,scheduling}.py`, `src/nexa/scheduler/*.py`, `benchmarks/b04/*.py`, `tests/scheduler/*.py`, `tests/benchmarks/b04/*.py` | `a6ea2808d621dd4fe9d82496a83613b87bfbaaea62734bc7260241fd527435bb` |
| B04 fixture manifest | ordered `sha256sum` manifest for `benchmarks/fixtures/b04-*.json` | `d010315d38ad1bdbe5fbd285fbe17bbcb7a88e094a15607f78b3d8ab3ec60bd3` |
| Suite configuration | `benchmarks/fixtures/b04-suite.json` | `6f6725d8c5de3e436fc851e5a0a7826129508a35335b0ccccf4a2488d8d26809` |
| Raw matched bundle | `benchmarks/results/b04-fairness.json` (92,953,374 bytes) | `1231f79713e68bcbe0c91f14c993dd5e15f3a1ed6d22246920d16af7854f62fb` |
| Comparison table | `benchmarks/plots/b04-fairness.csv` (30,149 bytes) | `4e644f193dbfefd9b32af1ae77bc14ff3cb98e8f5923d91965128df610c5eeb0` |
| Comparison/timeline report | `benchmarks/plots/b04-fairness.svg` (74,104 bytes) | `94f4e315f911dccbc18dc72faf60ad6a95a1c2b0bbc3ea8ce71071cd3a24da96` |
| Python manifest | `pyproject.toml` | `d375adb7638276202408c1e88a573dfc3b55c340572a2cec4f6d1a609d8b0e54` |
| Python lockfile | `uv.lock` | `7edf67b031d3d5ee4444a8809d8f431c76d5b994b9f444b4ea511af5072adf15` |

The selected B03 artifacts were not changed:

| Artifact | SHA-256 |
|---|---|
| `benchmarks/results/b03-baselines.json` | `3353608adc556e672c205fae606033b904aa0fc2204e766403eb466f4587d9a9` |
| `benchmarks/plots/b03-comparison.csv` | `fd186c107dd72d736f5a461e499bda496a2de42949d9b3dff8a0d684e8300711` |
| `benchmarks/plots/b03-comparison.svg` | `d5278185daf90217b0051a77ca1bc94416e5595ac44409108c1466441d6786b3` |

## Contract and arithmetic

The product interface returns immutable `Ok`/`Err` values over typed decisions (`Dispatch`, `CreateReservation`, `DrainForReservation`, `InvalidateReservation`, `NoDecision`) and the five required policy errors. Each tenant snapshot admits at most 16 normal candidates plus one `oldest_eligible`; duplicates across those positions are de-duplicated and excess input returns `CandidateBoundExceeded`. While a reservation is active, its queued candidate occupies that extra slot for the owning tenant so bounded pagination cannot manufacture a `no_longer_eligible` invalidation.

Accounting uses integer milliseconds and `Decimal` with a local precision of 50 and `ROUND_HALF_EVEN`. B04 selects no PostgreSQL precision/scale; B05 must choose a durable representation compatible with these semantics. Held and quarantined allocations are aggregated per tenant before dominant share is calculated and remain chargeable until release. The required hand calculation is covered directly: allocations `(4 CPU, 1 RAM)` and `(1 CPU, 4 RAM)` on capacity `(8, 8)` aggregate to `(5, 5)`, so dominant share is `5/8`, not `1/2 + 1/2`. Property tests compare production arithmetic with an independent `Fraction` oracle.

Repeated accounting at one instant adds zero charge; time regression is an error. The model reconstructs policy/accounting inputs without hidden state, preserves score/floor and does not double-charge. This is model evidence, not a database restart or crash-recovery result.

## Fixed suite

The suite was frozen before measurement with policy version `1`, seeds `7,11,19,23,29`, threshold `19/20`, and policies `nexa`, FIFO, RR, WRR, DRR and DRF. Every policy receives the same materialized workload, capacity, quota, concurrency and user mapping for a profile. Cost is candidate evaluations and policy decisions from that same virtual run; it is not wall-clock or production latency.

| Profile | Capacity / weights | Declared purpose |
|---|---|---|
| `uniform-equal` | 4000 CPU millis, 4 GiB RAM; `1:1:1` | Uniform 5-second continuous demand; valid Jain per seed |
| `mixed-equal` | 4000 CPU millis, 8 GiB RAM; `1:1:1` | Continuous 1/5/30/120-second mix; valid Jain and all feasible jobs dispatch |
| `weighted-124` | 7000 CPU millis, 7 GiB RAM; `1:2:4` | Quota/concurrency permit the target weighted cohort |
| `gpu-slots` | 4000 CPU millis, 4 GiB RAM, four simulated UUID slots; `1:2:1` | Whole-slot policy and duplicate-UUID invariant in layer D |
| `large-reservation` | 4000 CPU millis, 4 GiB RAM; `1:1` | Staggered releases expose fitting small jobs that reservation drain withholds; arrivals continue before and after protected dispatch |
| `constrained-diagnostic` | 7000 CPU millis, 7 GiB RAM; `1:2:4` | Target cohort is infeasible; exclusions and Jain `null` are required |

The deterministic payload contains 180 runs: six profiles x five seeds x six policies. Raw provenance includes trace and materialized checksums per run, capacities, quotas, weights, outcomes, allocations, resource-time, decisions, score/floor, reservation events, exclusions, cost and invariant violations.

## Fairness results

All 20 valid product-policy fairness runs meet the fixed `J >= 0.95` threshold. Fractions below are the exact raw values; decimals are display-only.

| Profile | Seed | Weighted Jain | Decimal | Status |
|---|---:|---:|---:|---|
| `uniform-equal` | 7 | `79202000000/79250976003` | 0.999382 | `pass` |
| `uniform-equal` | 11 | `316808000000/316898973003` | 0.999713 | `pass` |
| `uniform-equal` | 19 | `316808000000/316898973003` | 0.999713 | `pass` |
| `uniform-equal` | 23 | `316808000000/316988967003` | 0.999429 | `pass` |
| `uniform-equal` | 29 | `316808000000/316899003003` | 0.999713 | `pass` |
| `mixed-equal` | 7 | `2870408/2873289` | 0.998997 | `pass` |
| `mixed-equal` | 11 | `717602000000/717836197509` | 0.999674 | `pass` |
| `mixed-equal` | 19 | `2870408000000/2873691594027` | 0.998857 | `pass` |
| `mixed-equal` | 23 | `179400500000/179811131253` | 0.997716 | `pass` |
| `mixed-equal` | 29 | `2870408000000/2872131126003` | 0.999400 | `pass` |
| `weighted-124` | 7 | `251384/253173` | 0.992934 | `pass` |
| `weighted-124` | 11 | `375769/376819` | 0.997214 | `pass` |
| `weighted-124` | 19 | `1` | 1.000000 | `pass` |
| `weighted-124` | 23 | `4516587/4529621` | 0.997122 | `pass` |
| `weighted-124` | 29 | `1213491816003/1228710642017` | 0.987614 | `pass` |
| `gpu-slots` | 7 | `722402/723177` | 0.998928 | `pass` |
| `gpu-slots` | 11 | `732047580002/734910534021` | 0.996104 | `pass` |
| `gpu-slots` | 19 | `1456849/1463049` | 0.995762 | `pass` |
| `gpu-slots` | 23 | `5929225/5942679` | 0.997736 | `pass` |
| `gpu-slots` | 29 | `5919493866001/5933828262015` | 0.997584 | `pass` |

Every row above includes all three declared tenants, has zero invariant violations and zero undispatched feasible jobs. Baseline failures remain in the CSV/raw output; they were not discarded or used to tune the threshold.

For `constrained-diagnostic`, all five product runs preserve weighted Jain as `null`: every tenant is excluded with `quota_or_concurrency_does_not_permit_weighted_share`. This profile is diagnostic, not a fairness pass or fail, and is displayed as `N/A` in the report.

## Aging and reservation results

Contract checks embedded in the raw bundle record effective priority at eligible ages `59 -> 0`, `60 -> 1`, `119 -> 1`, `120 -> 2`, and `121 -> 2` for base priority zero. At 119 seconds the boundary case dispatches normally; at 120 seconds it creates a reservation. Invalidation preserves the explicit reasons `cancelled`, `no_longer_eligible`, `policy_incompatible`, and `capability_incompatible`.

All five `large-reservation` seeds produce the same deterministic lifecycle:

| Event | Virtual time | Observation |
|---|---:|---|
| Large job arrives behind four running small jobs | 1,000 ms | It is eligible/feasible against total capacity but does not fit free capacity |
| Reservation created and drain begins | 121,000 ms | Eligible age is exactly 120 seconds |
| First queued small job arrives | 125,000 ms | It is eligible and later fits the first released 1-CPU slot |
| Existing allocations release at staggered times | 130,000, 150,000, 170,000 and 190,000 ms | Drain decisions retain `small-late-*` as fitting alternatives but do not dispatch them |
| Additional small jobs arrive while draining | 140,000, 160,000 and 180,000 ms | The fitting backlog grows while reservation identity remains unchanged |
| Large job dispatches under reservation | 190,000 ms | Eligible age 189 seconds; all five baselines dispatch the same job at 270,000 ms instead |
| Small-job arrivals continue after protected dispatch | 195,000 and 205,000 ms | The arrival stream crosses the protected dispatch time |
| Run completes | 320,000 ms | 11/11 jobs complete, zero undispatched jobs and zero invariant violations |

This trace establishes the requested finite model behavior under healthy resources, positive weights, stable quotas, feasible requests and finite runtimes. It does not establish a universal runtime wait bound.

## Review remediation

The earlier read-only internal review was distinct from independent Task Review. Its findings and the later Task Review rejection findings were handled as follows:

| ID / severity | Finding | Closure evidence |
|---|---|---|
| `I-1` Important | Adapter could fall back to `now - arrival` and silently accept missing/duplicate eligible-age mappings. | `eligible_wait_overrides` is now mandatory and must match queued job IDs exactly; missing, duplicate, negative or non-integer values are rejected. Adapter regressions cover missing/duplicate mappings and remaining-quota eligibility. |
| `I-2` Important | Accounting used only the oldest queued job per tenant and could miss a newer eligible request. | Accounting now builds the full bounded adapter window and records `eligible_tenant_ids`. A mixed-request regression proves the tenant remains active, floor reaches `0.75`, and it retains ordering ahead of a newly active tenant. |
| `I-3` Important | Reservation profile status did not enforce the 120-second eligible-age threshold. | Report validation requires reason `eligible_wait_threshold` and integer eligible age at least 120, in addition to same-job create/drain/late-arrival/dispatch lifecycle. An early-reservation negative test fails the profile. |
| `M-1` Minor | `tenant_order` implied rows were serialized in policy fairness order. | Raw fields were renamed to `tenant_states` / `tenant_state_fields`; the values remain sufficient to reconstruct and audit the policy key without a misleading order claim. |
| `B04-R01` P1 | A valid reserved candidate could disappear when 16 older jobs became quota-eligible and occupied both the normal window and recalculated `oldest_eligible`. | The adapter retains the active reserved candidate in the extra slot without exceeding 16 + 1. A simulator regression creates reservation `reservation-000001` at 120,001 ms, releases quota/capacity at 130,000 ms, and proves the same reservation dispatches immediately without invalidation. |
| `B04-R02` P2 | The selected trace had simultaneous releases, and the report passed on any arrival between create/dispatch without proving drain withheld a fitting job. | The versioned trace now has releases at 130/150/170/190 seconds, fitting small candidates recorded on drain decisions, arrivals before and after large dispatch, and a validator that rejects each missing condition independently. Candidate IDs must match serialized eligible+fit candidate-state rows and real arrived jobs; staggered release only counts allocations already held when reservation was created. Nexa dispatches at 190 seconds; FIFO/RR/WRR/DRR/DRF dispatch at 270 seconds on the same materialized trace. |

The remediated 180-run suite retained the six-profile definition, five fixed seeds, all six policies and the `19/20` fairness threshold. The reservation fixture was deliberately replaced to close `B04-R02`; fairness fixtures and thresholds were not tuned. All 30 Nexa profile rows pass, and a second full replay reproduces JSON/CSV/SVG byte-for-byte.

The latest independent Task Review decision was `Không duyệt` before these changes. No post-remediation Task Review has been performed; this evidence records closure work for re-review and does not claim approval.

A focused read-only internal rereview after the final validator hardening found no remaining Critical, Important or Minor finding in the `B04-R01`/`B04-R02` remediation scope. It confirmed fabricated or mismatched candidate evidence, future arrivals and unrelated post-create releases fail profile validation. This is D-layer engineering review only, not Task Review approval.

## Reproduction

The selected bundle and report are generated only from source-controlled fixtures:

```sh
uv sync --frozen --all-groups --no-editable --reinstall-package nexa

uv run --no-sync python -m benchmarks.b04.cli compare \
  --suite benchmarks/fixtures/b04-suite.json \
  --output benchmarks/results/b04-fairness.json

uv run --no-sync python -m benchmarks.b04.cli report \
  --input benchmarks/results/b04-fairness.json \
  --csv benchmarks/plots/b04-fairness.csv \
  --svg benchmarks/plots/b04-fairness.svg
```

The actual run used isolated `uv 0.12.15` at `/tmp/nexa-b04-uv.KA55DI/uv-aarch64-apple-darwin/uv` because system `PATH` has no `uv`. Reproduction ran the unchanged 180-run compare a second time under `benchmarks/tmp/`, regenerated CSV/SVG from that replay, and passed three byte-for-byte `cmp` checks. The raw JSON passed `jq`, the CSV parsed as 180 rows with all 30 Nexa `profile_status` values equal to `pass`, and the SVG passed `xmllint`. A 1200x5322 PNG rendered from the selected SVG with the host Arial font was inspected at full height and at the reservation section; text, explicit `N/A` values and all five reservation lifecycles were readable without overlap or clipping.

## Environment and verification

| Component | Observed value |
|---|---|
| Host | macOS 27.0, Darwin arm64; simulator/development host only |
| Python | CPython 3.12.13 |
| `uv` | 0.12.15 isolated binary; system PATH unchanged |
| pytest | 9.1.1 |
| Hypothesis | 6.168.0 |
| Ruff | 0.16.8 |
| Git | 2.50.1 (Apple Git-155) |

Final command results come from the fresh verification run after `B04-R01` and `B04-R02` remediation:

| Command/check | Final result |
|---|---|
| `uv lock --check` | `pass`; 30 packages resolved from the existing lock |
| `uv sync --frozen --all-groups --no-editable --reinstall-package nexa` | `pass`; local package rebuilt and reinstalled |
| `uv run --no-sync ruff check .` | `pass` |
| `uv run --no-sync ruff format --check .` | `pass`; 82 files already formatted |
| `uv run --no-sync pytest -q tests/scheduler tests/benchmarks/b04` | `pass`; 91 tests |
| `uv run --no-sync pytest -q tests/benchmarks` | `pass`; 102 tests including B03 regressions |
| `uv run --no-sync pytest -q` | `pass`; 190 tests |
| Selected compare/report, parsing, render and three `cmp` checks | `pass`; 180 runs, all 30 Nexa profile rows pass, hashes above reproduced byte-for-byte |
| `git diff --check` and Git visibility/ignore audit | `pass`; all B04 untracked files visible and `.env` remains ignored by the dedicated rule |
| Focused internal remediation rereview | `pass` for `B04-R01`/`B04-R02`; no remaining Critical/Important/Minor finding, distinct from Task Review |
| Independent Task Review | latest decision was `Không duyệt`; `B04-R01`/`B04-R02` are remediated locally and await re-review |

## Acceptance mapping

Applicability and status are separate. A `pass` below applies only to the stated B04 D-layer subset; the project gate remains `specified` where its full P/L/G criteria are not yet implemented or executed.

| Gate | B04 applicability | B04 D status | Project gate status | Evidence / missing scope |
|---|---|---|---|---|
| ACC-04 | Capacity/quota/concurrency and simulated UUID model subset | `pass` | `specified` | Property/integration timelines have no oversubscription or duplicate simulated UUID. Host inventory/reserve, concurrent DB allocation and Docker/GPU enforcement remain B09/B11/B13/G work. |
| ACC-08 | Accounting/floor model subset | `pass` | `specified` | Held/quarantined resource-time, <=1-second tick, no double-charge, rational oracle and snapshot reconstruction pass in model. Atomic DB ledger/restart/reconciliation remain B08/B11/B13. |
| ACC-09 | Weighted policy simulator subset | `pass` | `specified` | 20/20 valid five-seed runs meet `J >= 0.95` with declared windows/cohorts and matched baselines. PostgreSQL/runtime and any L/G claims remain future work. |
| ACC-10 | Aging/reservation deterministic subset | `pass` | `specified` | Boundary/property tests and five reservation traces cover create/drain/withheld fitting alternatives/staggered release/dispatch/invalidation and post-dispatch arrivals. Durable production scheduling remains B13. |
| ACC-11 | Candidate-bound type/behavior only | `specified` | `specified` | B04 enforces 16 plus oldest candidate input, but DB indexes, query plans, heap rebuild, keyset behavior and 100,000-job evidence belong to B13/B22. |
| ACC-35 | B04 simulator benchmark subset | `pass` | `specified` | Fixed suite, five seeds, matched trace/capacity/quota, costs, raw/tables/plot and byte replay exist. Real P/L/G benchmark repetitions remain B22/B24 and conditional B23. |
| ACC-39 | B04 Python unit/property/lint subset | `pass` | `specified` | Ruff and 190 Python tests pass. PostgreSQL, Playwright, image/scan, branch/release and runtime gates are outside B04. |

## Limitations and handoff

- Simulator results do not prove PostgreSQL durability, transactional allocation, API throughput, 100,000-job queue scalability, Linux/cgroup/Docker enforcement, restart recovery or real GPU behavior.
- Simulated GPU UUID exclusivity is policy-model evidence only; it is not discovery, device isolation or CUDA evidence.
- Candidate construction may scan harness state. B04 does not claim production query complexity.
- The 92,953,374-byte raw artifact is below GitHub's 100 MB hard limit but above its recommended 50 MiB range; it is retained to preserve full accounting/decision audit evidence and will add repository-history weight.
- B05 must choose durable precision/scale and schema constraints compatible with monotone Decimal scores, accounting instants, virtual floor and immutable proposal versions.
- B11 must recheck job/policy/quota/capacity/GPU/fence under lock and atomically commit attempts/allocations; a B04 `Dispatch` is only a proposal.
- B13 must persist/rebuild ledgers, floor and reservation state, provide bounded indexed candidates/oldest eligible jobs, and supply ACC-08/09/10/11 P-layer evidence.
- B04 closes only one dependency of B11. B08 and B10 remain required.

This report prepares the B04 handoff. It does not claim that independent Task Review has approved the work.
