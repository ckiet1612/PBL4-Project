# ADR-0003: Worker authority, fencing and cleanup

- **Date:** 2026-09-17
- **Status:** accepted
- **Decision source:** PLAN §3, §5, §9; B01 concretizes message identity, acknowledgment and cleanup authority.

## Context

Lease expiry or loss of a worker response does not prove the old container stopped. Extending a runner deadline before the API acknowledges authority would let a partition prolong compute beyond confirmed lease. A restarted worker also needs an atomic way to replace its incarnation and, when safe, adopt an exact live managed container. Finally, startup can fail before Docker creates a container, so cleanup cannot require a fabricated container ID. Coordinator epoch, worker incarnation and job fence protect different races.

## Options considered

1. Full authority tuple for renew/publish plus a narrower cleanup proof path after revoke; quarantine until verified stop.
2. Release on lease/heartbeat expiry. Risks overlapping containers and oversubscription.
3. Reject every callback after revoke, including cleanup. Prevents safe convergence and can strand allocation permanently.

## Decision

Use option 1. Execution authority is `(worker_id, worker_incarnation_id, attempt_id, allocation_id, lease_id, job_fence)`. Dispatch records coordinator epoch and atomically generates the Attempt startup nonce, but healthy attempt authority does not depend on current leader. Claim reuses that stored nonce rather than creating it. DB time decides lease validity. For each start/adopt/renew callback the worker records the first local monotonic send instant, but it updates the runner only after a valid acknowledgment received before `first_send + lease_duration - safety_margin`. Failure, delayed response and same-callback replay do not extend or rebase unconfirmed authority.

Expiry/reaper revokes lease, increments fence and quarantines allocation. Renew/checkpoint/complete require full live authority and desired state. Cleanup accepts exact revoked identity plus a proof union: `ContainerStoppedProof` for a created exact identity, or `NoContainerProof` backed by startup nonce, monotonic executor sequence, a later tombstone, no create in flight and Docker inspection. It grants no renew/publish ability. Allocation release occurs only when API transaction verifies that proof. Workload restart policy is `no`.

Bootstrap returns worker identity/credential, not an incarnation chosen by the client. After taking the local singleton, each process calls idempotent `workerCreateIncarnation`; the server locks Worker, generates the ID, increments sequence, ends the prior incarnation and makes the new one current in STARTING. Old callbacks then fail stale. The worker drains the bounded reconciliation view. An exact still-live prior-incarnation container may transfer only through `workerAdoptAttempt`, which locks Job→Attempt/Lease→reservations and atomically appends the prior→current Authority grant lineage, binds Attempt/Lease plus every still-active same-Attempt checkpoint/result reservation to the current incarnation, preserves reservation IDs/callbacks and returns the complete transfer snapshot. A completed artifact-upload response from any lineage predecessor may be replayed with its stable descriptor-derived key only after current-Authority authentication and exact attempt/allocation/lease/fence/upload-metadata checks; no upload receipt is scanned or rewritten. The local durable runner bindings retain their message/control sequences and payload hashes and must match that response before continuation. Its runner deadline follows the same acknowledgment-gated rule. Failed/expired/mismatched/late adoption is failed or stopped and cleaned before READY; it never guesses or allocates a replacement reservation within the adopted Attempt.

Executor operations are serialized by a local per-attempt lock. Before create it records `CREATE_IN_FLIGHT`; a pre-create cancel/failure writes a higher-sequence tombstone before emitting `NoContainerProof`. Reconciliation always returns the dispatch-committed startup nonce and derived claim state. A revoked attempt that never claimed may therefore initialize a durable sequence-1 tombstone under the attempt lock without Docker create; API accepts its proof only while claim/start/container records are all absent. Delayed claim/start is rejected remotely, and the tombstone rejects delayed local create/start at an older/equal sequence. Start commit resets DB expiry to DB-now +45 seconds; same-callback replay returns the original acknowledgment without renewal.

## Consequences

- Capacity may remain unavailable during host silence; correctness is preferred to premature reuse.
- Worker protocol and callback receipts must be idempotent under lost responses.
- Trusted runner independently stops compute after deadline even when agent dies.
- A DB renewal commit whose response misses the local candidate deadline may cause conservative stop/recovery; it never grants hidden local runtime authority.
- Incarnation adoption adds a transaction and reconciliation path but removes ambiguity about callback ownership after worker restart.
- Executor must persist enough local startup/tombstone state to prove that no container exists without inventing identity.
- UI distinguishes requested stop/quarantine from verified stopped/released.

## Transition and rollback

B09–B15 implement the protocol. Weakening the authority tuple or releasing without proof changes failure guarantees and requires PLAN approval. Protocol additions must remain backward-incompatible until explicitly versioned.

## Acceptance

ACC-01, ACC-12–16, ACC-20–22, ACC-25 and ACC-31. Evidence requires failed/delayed/replayed renew, bootstrap→READY, restart/adoption/old-callback rejection, pre-create cancel/failure, agent/container kill, stale leader/fence, cancel/complete and duplicate reaper/cleanup timelines.
