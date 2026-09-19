# B03 simulator and baseline evidence

- **Task:** B03 — deterministic simulator, trace, metrics and FIFO/RR/WRR/DRR/DRF baselines
- **Status:** `complete — focused independent Task Review: Duyệt`
- **Base revision:** `4545c508b9191fb9bd891715ba9f4c73ecc6bd0a`
- **Working tree:** B03 was implemented directly on `main` under the approved no-branch/no-commit exception. The base tree was clean; the dirty state contains the B03 files and documentation listed below, including deletion of the replaced `benchmarks/.gitkeep` marker. Because there is no B03 commit, artifact and source-manifest hashes freeze the reviewed inputs.
- **Evidence time:** 2026-09-19 04:32:50 `+07` (`Asia/Ho_Chi_Minh`)
- **Evidence layer:** D — deterministic simulator/property evidence only

## Scope and boundary

B03 provides a pure virtual-time engine, immutable integer resource model, canonical versioned traces, exact rational comparisons, a shared `SchedulerPolicy` snapshot contract, five baseline policies, raw metrics and deterministic CSV/SVG reporting. The simulator owns all mutation and validates capacity, hard quota, tenant concurrency and simulated GPU UUID exclusivity before every allocation.

This is not the production scheduler. It has no PostgreSQL, API, coordinator, worker, Docker, Linux cgroup, lease, fence, checkpoint, recovery, production fairness ledger, aging or reservation implementation. FIFO, RR, WRR, DRR and DRF remain comparison baselines; none is the B04 product policy.

## Source and artifacts

| Item | Path / value | SHA-256 |
|---|---|---|
| Simulator and test source manifest | `benchmarks/__init__.py`, `benchmarks/simulator/*.py`, `tests/benchmarks/*.py`; hash of the ordered `shasum` manifest | `ed0a4e55e3a21840761ed1bffdeb662e6ee1f53f6e0903070c3052b0973601ca` |
| Small trace bytes | `benchmarks/fixtures/small-trace.json` | `c0ebccc19aa22d655980c00469a0a7d5c90ab13e33ee2594b1542f8c41871d8e` |
| Standard trace bytes | `benchmarks/fixtures/standard-trace.json` | `cdaf96a888261f6590267dc210a9bebdc0964321310850aa2c646b98873aefaa` |
| Standard trace canonical content | `b03-standard-v1`, version `1` | `979e64fbc03ecba5a17819fa2e099a4c674eace4bac6372fcdfed297a1c15fae` |
| Raw comparison bundle | `benchmarks/results/b03-baselines.json` | `3353608adc556e672c205fae606033b904aa0fc2204e766403eb466f4587d9a9` |
| Comparison table | `benchmarks/plots/b03-comparison.csv` | `fd186c107dd72d736f5a461e499bda496a2de42949d9b3dff8a0d684e8300711` |
| Comparison plot | `benchmarks/plots/b03-comparison.svg` | `d5278185daf90217b0051a77ca1bc94416e5595ac44409108c1466441d6786b3` |

The source-manifest digest was computed with:

```sh
shasum -a 256 benchmarks/__init__.py benchmarks/simulator/*.py tests/benchmarks/*.py | shasum -a 256
```

## Environment and commands

| Component | Observed value |
|---|---|
| Host | macOS 27.0, Darwin arm64; development/simulator host, not Linux acceptance evidence |
| Python | CPython 3.12.13 |
| `uv` | 0.12.15, isolated checksum-verified binary; system PATH remained unchanged |
| pytest | 9.1.1 |
| Hypothesis | locked by the existing B02 dependency set; no B03 dependency change |
| Ruff | 0.16.8 |
| Git | 2.50.1 (Apple Git-155) |

The selected comparison used the following exact commands. The absolute `uv` path is temporary environment provenance, not a repository path or product default.

```sh
UV=/tmp/nexa-b03-uv.kmM6tZ/uv-aarch64-apple-darwin/uv

/usr/bin/time -p "$UV" run --no-sync python -m benchmarks.simulator.cli compare \
  --trace benchmarks/fixtures/standard-trace.json \
  --seeds 7,11,19,23,29 \
  --output benchmarks/results/b03-baselines.json

"$UV" run --no-sync python -m benchmarks.simulator.cli report \
  --input benchmarks/results/b03-baselines.json \
  --csv benchmarks/plots/b03-comparison.csv \
  --svg benchmarks/plots/b03-comparison.svg
```

The measured comparison command wall time was `1.08 s` real (`0.95 s` user, `0.04 s` sys). This external measurement is not stored in deterministic JSON and is not API, PostgreSQL, Docker or production throughput evidence.

Deterministic reproduction used the same commands with outputs under `benchmarks/tmp/`, followed by three `cmp` checks. JSON, CSV and SVG all matched byte-for-byte; only those exact temporary files and the empty temporary directory were then removed.

## Selected workload

| Setting | Value |
|---|---|
| Simulator version | `1.0.0-b03` |
| Baseline versions | `1` for `fifo`, `rr`, `wrr`, `drr`, `drf` |
| Seeds | `7, 11, 19, 23, 29` |
| Capacity | 4000 CPU millis, 8 GiB RAM, two simulated whole GPU UUIDs |
| Tenants | `tenant-a`, `tenant-b`, `tenant-c`; weights `1:2:4` |
| Tenant quota | each tenant: 4000 CPU millis, 8 GiB RAM, 2 simulated GPUs, 8 running jobs |
| Trace | 3 continuous generator groups x 36 jobs, plus one large job and one intentionally infeasible job |
| Duration choices | 1, 5, 30 and 120 seconds |
| Fairness window | predeclared `[1000, 120000]` ms |
| Fairness group | all three tenants included; no exclusions |

Materialized workload checksums were stable across every baseline for each seed:

| Seed | Materialized checksum |
|---:|---|
| 7 | `d5651f7ff543c093cd6873be5a00db9b19e722169d99752459dd876f5a4ac52c` |
| 11 | `c2184746da67cfe9cfd36346fecefd8e39a9f866ac24e7024c2cec88a4389251` |
| 19 | `792cbb865de4b446482f7d96c0f090c99099b3c407558294cadac9e77417ee59` |
| 23 | `a7b5b57cb27fd10a4f9d0dcae9c7d8b0c4057a3e083a8120a210d24455a85e8f` |
| 29 | `3695c362ca632cb148a64106cd6f8fdce03fd0d2be9f271f8f26d6a6816c43b8` |

## Results

There are 25 matched runs: five baselines x five seeds. Every run recorded 110 arrivals, 109 accepted and completed jobs, one rejected/infeasible job (`request_exceeds_capacity`), zero undispatched feasible jobs and zero invariant violations.

The table reports the median across the five matched seeds. Throughput is simulated completions per virtual second. Baseline cost is candidate evaluations from the same run; it is not wall-clock cost.

| Baseline | p95 wait ms | Max wait ms | Virtual runtime ms | Throughput | Weighted Jain | Candidate cost |
|---|---:|---:|---:|---:|---:|---:|
| DRF | 681720 | 830178 | 959000 | 0.1137 | 0.6341 | 3489 |
| DRR | 665233 | 786178 | 915000 | 0.1191 | 0.7782 | 3638 |
| FIFO | 763094 | 820055 | 890000 | 0.1225 | 0.3333 | 3348 |
| RR | 712709 | 788178 | 917000 | 0.1189 | 0.5866 | 3379 |
| WRR | 679720 | 797178 | 926000 | 0.1177 | 0.8183 | 3514 |

Weighted Jain uses exact fractions in raw output with `x_i = dominant_resource_time_i / weight_i`. The decimal values above are display-only summaries. The window and tenant group were declared in the trace before execution. These baseline results do not establish the PLAN target `J >= 0.95`, do not evaluate the B04 production policy and do not pass ACC-09.

## Verification

| Command/check | Final verification result |
|---|---|
| `uv run --no-sync pytest -q tests/benchmarks -vv` | pass, 69 tests |
| `uv sync --frozen --all-groups --no-editable` | pass with the existing lockfile; no dependency or lockfile change |
| `uv run --no-sync pytest -q` | pass, 99 tests |
| `uv run --no-sync ruff check .` | pass |
| `uv run --no-sync ruff format --check .` | pass, 56 files already formatted |
| Selected compare/report plus three `cmp` checks | pass; JSON/CSV/SVG reproduced byte-for-byte |
| JSON/CSV/XML parsing | pass with `jq`, Python CSV reader and `xmllint`/ElementTree |
| SVG visual render | pass via macOS Quick Look thumbnail; standard report is readable, and `small-trace` shows five explicit Jain `N/A` labels rather than zero bars |
| `git diff --check` | pass |
| Scope scan | no database, Docker, PyTorch, system clock, sleep or `src/nexa` import in B03 source/tests |
| Git status/diff/untracked/ignore audit | reviewed; `.env` is ignored by `.gitignore:42`, selected B03 evidence is visible to Git |
| Focused independent Task Review | `Duyệt`; no remaining Critical/Important finding and no new Minor finding |

The first independent Task Review returned `Không duyệt` with five Important findings: dominant resource-time summed per allocation rather than aggregating each tenant's held resources first; DRR advanced tenants before spending remaining credit; Jain eligibility did not validate weighted-share quota/concurrency feasibility; SVG/CSV lacked useful per-tenant data and a real allocation timeline; and an unnecessary `pyproject.toml` change exceeded scope. The review also noted two Minor gaps in property-test quota/concurrency coverage and invalid-report partial-output handling.

Remediation round 1 added focused regression tests before the fixes. Dominant resource-time now integrates the dominant share of each tenant's aggregate held vector over exact interval boundaries. DRR retains the active tenant while credit can pay the next job. Fairness assessment limits quota rejection checks to the predeclared window and excludes tenants whose quota/concurrency cannot make the weighted share feasible. CSV/SVG now include per-tenant wait/resource-time values and allocation intervals from a real selected seed. Report content is fully rendered before either output is written. Property generation now varies tenant weights, quotas and concurrency and asserts those limits at every allocation boundary.

Focused rereview found one additional R3 boundary case: a quota-rejected job before the declared fairness window could still exclude a tenant from a later valid window. Remediation round 2 added a failing regression for an old rejection at `t=0` with a valid `[5000,6000]` window, then constrained quota-rejection checks to `window.start_ms <= arrival_ms < window.end_ms`. The focused, full and byte-reproduction checks above were rerun after this change.

`pyproject.toml` and `uv.lock` are unchanged. The repository-root import setup is local to `tests/benchmarks/conftest.py`, so no dependency or project configuration change was required.

The next user review retained R3 and added R6. R3 reproduced with CPU capacity `3000`, equal weights, quotas `500/1000/3000` and window `[0,1000]`: the previous code excluded A but reported Jain `25/26` for B/C even though B could reach only share `1/3`, below the reduced target `1/2`. The new regression verifies quota/concurrency feasibility again after each exclusion while at least two tenants remain. The result retains only C, records explicit exclusion reasons for A/B and leaves Jain undefined.

R6 reproduced on `small-trace.json`, seed `7`: all five raw Jain values were `null`, while SVG rendered five zero-height bars. Report aggregation now preserves `None` if any matched seed is undefined and renders an explicit `N/A` per baseline without a numeric bar. The regression also covers mixed defined/undefined seeds so an undefined run cannot be silently discarded. Two independently rendered small-trace CSV/SVG pairs matched byte-for-byte; the selected standard 25-run JSON/CSV/SVG remained byte-identical with the hashes above.

Focused independent rereview returned `Duyệt` on 19/09/2026 with no remaining Critical/Important finding and no new Minor finding. The reviewer independently reproduced R3 and R6, reran 69 B03 tests and 99 full-suite tests, checked Ruff formatting/lint and diff hygiene, matched the 25-run resource-time/Jain oracle, replayed JSON/CSV/SVG byte-for-byte and confirmed the source-manifest digest. `ROADMAP.md` now marks only B03 complete; B04 remains separate future work.

## Acceptance mapping

Applicability and status are kept separate. No runtime gate is promoted by this simulator evidence.

| Gate | B03 applicability | Status | Evidence / missing scope |
|---|---|---|---|
| ACC-08 | D model-property precursor only | `specified` | Held allocation resource-time is modeled, but no durable DB ledger, tick, restart or double-charge evidence exists. |
| ACC-09 | D baseline-comparison precursor | `specified` | Five matched seeds and baseline timelines exist; the B04 weighted dominant resource-time policy, aging/reservation and production evidence do not. |
| ACC-10 | Standard trace precursor only | `specified` | Large-before-small and max-wait data exist; B03 deliberately has no product aging/reservation implementation or reservation timeline. |
| ACC-11 | Not applicable to B03 simulator | `specified` | Requires PostgreSQL indexes/query plans and a queue of at least 100,000; no DB exists in B03. |
| ACC-29 | Not applicable to B03 simulator | `specified` | Requires production API/PostgreSQL on Linux, 100 tenants/clients and at least 100,000 outstanding jobs. |
| ACC-35 | D subset only | `specified` | Raw baseline bundle, seeds, trace/config, hashes and reproducible plots exist; policy and later P/L/G evidence remain future work. |

## Limitations

- Virtual time cannot measure real scheduling latency, API throughput, database contention, query plans, container startup or host utilization.
- Simulated GPU UUID exclusivity cannot establish NVIDIA discovery, CUDA compatibility, device isolation, GPU utilization or real GPU recovery.
- The engine does not model lease expiry, fencing, stale callbacks, quarantine, checkpoint recovery, coordinator restart or worker reconciliation.
- The selected trace has 110 jobs and three tenants. It is not the 100-tenant/100-client/100,000-job load profile.
- Candidate-evaluation counts describe these pure baseline implementations only; they do not prove the production queue complexity bound.
- B03 contains no product scheduler. B04 must implement and separately test weighted dominant resource-time, eligible aging and one local reservation.
