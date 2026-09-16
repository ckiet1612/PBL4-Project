---
name: verifying-recovery-fencing
description: Use when testing or assembling evidence for Nexa lease expiry, fencing, stale callbacks, cancel/reaper races, quarantine, checkpoint recovery, or reconciliation; do not use for generic debugging, implementation planning, or a one-off documentation review.
---

# Verify recovery and fencing

Use [PLAN.md](../../../PLAN.md) §9 and §14, [invariants](../../../docs/invariants.md) INV-08–17, and [acceptance](../../../docs/acceptance.md) ACC-12–23 and ACC-31 as the oracle. Record `gate_id → applicability → status → evidence`, keeping applicability separate from status. Use only the five statuses defined in acceptance:

- `specified`: criteria exist, but acceptance execution has not begun; specification-only work stays here.
- `not-run`: the gate applies to the implementation under review, but its check has not been executed.
- `blocked`: a concrete missing dependency or environment prevents verification; name what is missing.
- `pass`: evidence for the exact revision/configuration/environment satisfies every applicable criterion.
- `fail`: evidence demonstrates at least one unmet criterion.

An instruction-only skill can exist before product code or a recovery harness. Skill validation or an approved specification does not establish runtime evidence. If a requested verification lacks its PostgreSQL/API/Docker or filesystem fixture, describe the evidence protocol within the task's authorized scope and report the missing prerequisite with the appropriate status. Do not invent commands, runtime evidence, or a `pass`.

Define one fault or race and its injection point before running it. Capture a timeline from the accepted operation through recovery and reconciliation. Track coordinator epoch, worker incarnation, job fence, attempt, lease, desired state, allocation identity, state/event sequence, counters, idempotency key, container identity, and final-result identity as separate fields. Record revision, environment/capability, configuration, command, timestamp, raw report, and gate IDs.

The minimum assertions are:

- lease expiry uses database time; a delayed renewal response cannot extend the runner deadline, which is monotonic from the renewal send time minus safety margin;
- expiry revokes and fences the attempt, then quarantines its allocation; expiry, heartbeat loss, or process exit alone never proves the container stopped or makes capacity available;
- a stale leader, worker, attempt, lease, or desired-state callback is rejected, while a healthy attempt survives coordinator turnover; publish checks every required epoch/fence/lease condition;
- cancel commit beats later completion, terminal state and final result are unique and immutable, and reaper/callback/control races change state, events, counters, and idempotency exactly once;
- cleanup verifies the expected container identity and stopped state before release; an unreachable host keeps the allocation held;
- recovery/resume preserves job and session identity, creates a new attempt, validates checkpoint provenance and compatibility, keeps at least two committed checkpoints, and falls back from a corrupt newest checkpoint before any restart-safe fallback; automatic recovery remains distinct from manual retry;
- response-loss, full restart/reboot, and dependency/readiness failures reconcile accepted IDs and durable state, fail closed when DB/schema/storage checks cannot commit, and do not claim RPO or recovery from logs alone.

Reconcile accepted IDs, allocation/quota/ledger, event sequences, counters, result catalog, quarantine, and filesystem metadata after every scenario and restart. Logs or “restarted successfully” messages are not sufficient. Do not call Docker inside a transaction, weaken a failure guarantee, or use a simulator/mock to claim a production lease, API, container, or GPU gate. Use `superpowers:systematic-debugging` for finding and fixing a root cause; this skill structures evidence and correctness checks.
