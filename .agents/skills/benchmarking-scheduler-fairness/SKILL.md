---
name: benchmarking-scheduler-fairness
description: Use when running or reviewing Nexa scheduler fairness, aging, reservation, queue-scalability, or load benchmark evidence; do not use for ordinary scheduler implementation, generic testing, or product release checks without benchmark data.
---

# Benchmark scheduler fairness

Use the approved policy and acceptance gates as the test oracle. Read [PLAN.md](../../../PLAN.md) §5–§6 and §14, then [invariants](../../../docs/invariants.md) INV-04–07 and [acceptance](../../../docs/acceptance.md) ACC-08–11, ACC-29, and ACC-35. Do not invent thresholds or replace the policy.

First classify the evidence layer and environment: simulator/property evidence (D), production PostgreSQL/API evidence (P), Linux/Docker execution evidence (L), browser evidence (W), or real GPU evidence (G). A simulator can support policy claims only; it cannot establish lease, API throughput, container, portability, or GPU gates. Record `gate_id → applicability → status → evidence`, keeping applicability separate from status. Use only the five statuses defined in acceptance:

- `specified`: criteria exist, but acceptance execution has not begun; specification-only work stays here.
- `not-run`: the gate applies to the implementation under review, but its check has not been executed.
- `blocked`: a concrete missing dependency or environment prevents verification; name what is missing.
- `pass`: evidence for the exact revision/configuration/environment satisfies every applicable criterion.
- `fail`: evidence demonstrates at least one unmet criterion.

An instruction-only skill can exist before product code or a benchmark harness. Skill validation or an approved specification does not establish runtime evidence. When a requested run lacks its harness or environment, report the missing prerequisite and the appropriate status; do not invent commands, results, or a `pass`.

For a comparison, freeze one revision, policy version, capacity, reserves, quotas, weights, candidate trace, and seed set. Run the approved policy and each comparison baseline on the same inputs. Use at least five simulator seeds; use the acceptance-required repeated real runs and warm-up when the gate calls for them. Preserve the exact command or script, configuration, environment/capability, timestamp, and raw output. Define baseline cost from the same matched run (for example resource-time, latency, or accepted/error counts) rather than from a different workload.

Check the behavior that is easy to mis-measure:

- charge held allocation dominant resource-time, including quarantine, and verify restart/no-double-charge and virtual-floor behavior;
- apply weighted tenant ordering and tie-breaks, eligibility-based 60-second aging (maximum 2), and one 120-second reservation with an invalidation reason;
- include continuous-demand, large-job-before-small-job, quota/capacity, and final-dispatch traces; do not infer starvation bounds from one favorable trace;
- compute weighted Jain only over a declared valid window and group, with exclusions and baseline cost recorded; never select a window after seeing the result;
- retain allocation/resource-time, wait, and reservation data for all evidence; collect accepted IDs, API errors, and query plans when the gate uses P/L runtime execution, and label them unavailable rather than implying simulator coverage.

Report `specified`, `not-run`, `blocked`, `pass`, or `fail` per applicable gate, with evidence or the reason verification has not occurred. Keep simulator and runtime results visibly separate and disclose missing evidence. Do not change policy, quota, trace, or threshold to turn a failure into a pass, and do not call FIFO/RR/WRR/DRR/DRF the product policy.
