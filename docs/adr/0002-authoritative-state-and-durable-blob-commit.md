# ADR-0002: Authoritative state, transactions and durable blobs

- **Date:** 2026-09-17
- **Status:** accepted
- **Decision source:** PLAN §2–§3, §5, §9–§10; B01 concretizes lock/idempotency/blob ordering without changing the approved durability guarantee.

## Context

Accepted jobs, allocation/quota, leases, fairness accounting, idempotency and artifact references must survive process/reboot failures without treating metrics/cache/filesystem names as a second control-plane authority. Large immutable blobs should not be stored in PostgreSQL.

## Options considered

1. PostgreSQL authoritative metadata plus durable filesystem immutable blobs, with explicit two-phase application protocol.
2. Filesystem manifests as state authority. Weak transactional coupling makes counters/fences/references race-prone.
3. Store all bytes in PostgreSQL. Strong transaction boundary but poor fit for large checkpoint/result streams and not the approved architecture.

## Decision

Use option 1. PostgreSQL owns all mutable/correctness state and blob metadata/references. Blob commit is bounded staging → checksum/size → file fsync → atomic same-filesystem rename → directory fsync → metadata transaction. Metadata never precedes durable bytes. Crash before metadata produces an orphan; GC deletes only after TTL plus transactionally proven absence of committed reference, active upload or restore. For attempt output, the successful/idempotently replayed Artifact response is the only committed ID/checksum/size/media binding; worker persists and returns it to the trusted runner before runner creates a final manifest.

State, event sequence, relevant counters/ledger and idempotency/callback receipt commit atomically. Unique constraints, row locks and CAS produce one winner. Auth precedes replay; completed replay precedes If-Match. External Docker/filesystem mutation does not occur in DB transactions.

Deployment maintenance state is a versioned global policy transition: `NORMAL→ADMISSION_OFF→WRITE_FROZEN`, then reverse after paired restore verification and readiness/reconciliation. The freeze marker and audit are authoritative DB facts; entering or leaving a state uses If-Match and guard rechecks rather than an untracked operator convention. `WRITE_FROZEN` blocks workload/configuration authority changes but retains narrowly scoped existing-SYSTEM_ADMIN session/rate metadata, append-only audit for allowed reads, domain-free idempotent replay and the guarded recovery transition. This preserves recovery authentication and auditability without permitting new principals, grants, blobs, job authority or capacity release.

One-time CLI and worker secrets are a deliberate replay exception: only credential metadata/hash and a secret-free locator are committed, never the raw value or an idempotency response containing it. After first delivery, same-key replay returns `409 one_time_secret_unavailable` plus the locator. A new CLI key is used only after revoking the unknown token; a new worker-bootstrap key atomically rotates the unknown current credential inside the explicit rotation window.

## Consequences

- Backup/restore must pair PostgreSQL and artifact snapshots with a manifest; DB-only restore is insufficient.
- Orphan detection and missing-reference readiness checks are mandatory.
- Cache/heap/metrics rebuild from DB and never decide correctness.
- A successful result commit may precede container cleanup; allocation remains charged until release proof.

## Transition and rollback

B05 implements constraints/transactions, B07 artifact protocol, B13 ledger, B19 GC and B21 backup. Schema rollback is allowed only in maintenance before new writes; an old snapshot cannot promise retention of later data.

## Acceptance

ACC-01, ACC-07–08, ACC-11, ACC-17, ACC-20, ACC-23, ACC-28, ACC-31 and ACC-33. Required evidence includes response-loss races, fsync/rename faults, referenced-blob GC races, accepted-ID/counter reconciliation and paired backup/restore.
