# B10 Worker Heartbeat And Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The direct user request forbids commits, branches, worktrees, PRs, and external review dispatch, so those normally suggested actions are intentionally absent.

**Goal:** Implement the local Nexa worker process, worker-authenticated PostgreSQL-backed B10 API operations, heartbeat health, startup reconciliation/adoption, lease renewal, durable pending operations, and a reproducible minimal Compose/bootstrap contract.

**Architecture:** PostgreSQL remains authoritative through a new application-layer `WorkerService`; FastAPI routes only validate wire data and call that service. The local agent owns the OS singleton, one-time bootstrap credential storage, Docker discovery/executor integration, durable callback/deadline state, runner IPC, and independent bounded loops for heartbeat, reconciliation, renewal, and poll. B11/B15 endpoints that are not B10-owned remain explicit client boundaries or test peers and never become production no-ops.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy 2/psycopg, PostgreSQL 17, `httpx`, asyncio, `fcntl`, Docker CLI, existing B09 executor/journal/runner protocol, Pytest/Hypothesis, Ruff, Docker Compose.

**Spec:** Direct B10 execution brief supplied on 2026-09-21, `PLAN.md` sections 9-14, `docs/contracts/openapi.yaml`, `docs/contracts/internal-interfaces.md`, `docs/contracts/state-machines.md`, and `docs/contracts/concurrency-recovery.md`.

## Global Constraints

- The newest user request, then approved PLAN, then approved contracts are authoritative; do not edit PLAN or frozen contracts to fit implementation.
- Worker communicates with the control plane only through REST and never receives a PostgreSQL credential.
- Only worker-side executor code accesses the Docker socket; no Docker/filesystem/network I/O occurs inside a database transaction.
- Worker credential, bootstrap secret, input, and checkpoint material never enter logs, evidence, runner, or workload.
- DB time decides heartbeat freshness and lease validity; local monotonic time from first send decides the candidate runner deadline.
- Every unreleased allocation, including quarantine, remains capacity/quota held until a later verified release transaction.
- B10 does not create Attempt/Allocation/Lease, recognize results, or implement B11/B15 failure/cleanup/reaper state machines.
- Exact callback/idempotency replay returns the stored acknowledgment without repeating mutations.
- No commit, push, branch, worktree, PR, release, task dispatch, or external deployment is authorized.

## Requirement Traceability

| B10 requirement | Source | Module | Test | Evidence target | Initial status |
|---|---|---|---|---|---|
| OS singleton before bootstrap/Docker | brief 4.1; INV-12 | `nexa.worker.singleton` | two-process contention and FD inheritance | B10 identity timeline | planned |
| Durable worker credential/bootstrap | brief 4.2; B06 auth | `nexa.worker.credentials`, `nexa.worker.client` | one-time response loss, file mode/fsync, no blind rotation | B10 bootstrap matrix | planned |
| Server-generated incarnation | OpenAPI `workerCreateIncarnation`; concurrency table | `WorkerService.create_incarnation` | concurrent request, replay, old replay, rotate/revoke race | PostgreSQL/API timeline | planned |
| Heartbeat/inventory/readiness/health aging | PLAN 9; OpenAPI heartbeat; INV-02/12 | `WorkerService.heartbeat`, health monitor | 5/15/30, duplicate callback, false READY, capacity regression | ACC-16/21 scoped evidence | planned |
| Bounded signed reconciliation pages | OpenAPI reconciliation; brief 6.2 | `WorkerService.get_reconciliation` | >100 rows, bound cursor, conflict, exact item identity | API/PostgreSQL evidence | planned |
| Poll without dispatch creation | OpenAPI poll; state-machine modes | `WorkerService.poll` | READY/admin/mode matrix, null offer, no new rows | B10/B11 handoff | planned |
| Exact live adoption | OpenAPI adopt; concurrency table | `WorkerService.adopt_attempt`, journal rebind | replay, expiry/revoke/mismatch, reservation transfer | ACC-13/16 scoped evidence | planned |
| DB-time renewal/progress replay | OpenAPI renew; INV-12/13 | `WorkerService.renew_attempt` | 0/null, same/conflicting progress, stale authority, mode race | lease timeline | planned |
| First-send runner deadline | concurrency recovery 78-90 | durable agent state, runner control client | failed/delayed/replayed ack, restart/boot mismatch | API/worker/runner timeline | planned |
| Startup reconcile and pending operations | brief 6-8 | `WorkerAgent`, `PendingOperationStore` | empty READY, managed orphan, outside-deployment untouched, pending survives restart | worker/test-peer evidence | planned |
| Real entrypoint/config/Compose contract | brief 8 | `nexa-worker`, `compose.yaml`, worker settings | config validation, graceful shutdown, compose config | reproducible commands | planned |
| Documentation and scoped acceptance | brief 10 | worker doc, README/ROADMAP/evidence | command/output review and diff review | handoff to B11/B15 | planned |

---

### Task 1: Define B10 wire schemas and in-transaction worker authentication

**Files:**
- Modify: `src/nexa/api/schemas.py`
- Modify: `src/nexa/application/identity_service.py`
- Test: `tests/application/test_worker_schemas_b10.py`
- Test: `tests/integration/test_worker_auth_b10.py`

**Interfaces:**
- Consumes: B06 opaque worker credential and frozen OpenAPI B10 schemas.
- Produces: strict Pydantic request/response models and `IdentityService.revalidate_worker_credential(session, credential, expected_worker_id)` that locks the exact credential row and checks hash/scope/revoke/expiry using DB time inside the caller transaction.

- [ ] Write schema tests for closed inventory, heartbeat, authority, reconciliation, poll, adopt, and renew unions, including request bounds and exact constants.
- [ ] Run the focused schema test and confirm RED because B10 models do not exist.
- [ ] Implement the minimal strict schema models and canonical response models.
- [ ] Write PostgreSQL race tests where rotation/revocation waits on the worker/credential lock and the B10 transaction must reject the changed credential before mutation.
- [ ] Run the auth race tests and confirm RED because only standalone `resolve_worker_credential` exists.
- [ ] Add the transaction-scoped credential verifier without changing existing B06 route behavior.
- [ ] Run focused schema/auth tests and existing B06 identity regressions.

### Task 2: Implement incarnation, callback replay, reconciliation, heartbeat, poll, and health aging

**Files:**
- Create: `src/nexa/application/worker_service.py`
- Create: `src/nexa/api/routes_worker.py`
- Modify: `src/nexa/api/dependencies.py`
- Modify: `src/nexa/api/app.py`
- Test: `tests/application/test_worker_service_b10.py`
- Test: `tests/integration/test_worker_api_b10.py`
- Test: `tests/api/test_operation_matrix.py`

**Interfaces:**
- Consumes: worker credential verifier, B05 Worker/Incarnation/Inventory/Attempt/Allocation/Lease/Authority/callback tables, signed `CursorCodec`, operational mode.
- Produces: `create_incarnation`, `get_reconciliation`, `heartbeat`, `poll`, `sweep_health`, and routes for the four corresponding OpenAPI operations.

- [ ] Write failing PostgreSQL tests for concurrent incarnation creation, same-key response replay, newer-incarnation old replay, exact path/credential binding, and WRITE_FROZEN behavior.
- [ ] Implement create-incarnation with idempotency scope first, Worker then credential locking, DB-time mutation, prior-incarnation ending, server UUIDv7, and STARTING reset.
- [ ] Write failing reconciliation tests for empty state, HELD and QUARANTINED rows, `UNCLAIMED/CLAIMED/STARTED`, exact expected container, 101 rows, cursor binding/expiry/snapshot conflict, and old/current incarnation rejection.
- [ ] Implement bounded keyset reconciliation with a signed worker/incarnation/snapshot cursor and at most 100 items per page.
- [ ] Write failing heartbeat tests for inventory persistence/dedup, callback replay without refresh, old incarnation, false reconcile flag, unresolved allocation, observed-container mismatch, capacity regression, admin states, and mode matrix.
- [ ] Implement heartbeat inventory checksum/versioning and server-side READY guards; never infer release from heartbeat loss or missing containers.
- [ ] Write failing health-aging tests proving DB-time READY/STARTING to SUSPECT at 15 seconds and UNAVAILABLE at 30 seconds without a new worker request.
- [ ] Implement a bounded application lifespan health monitor calling `sweep_health`; make monitor shutdown deterministic in tests.
- [ ] Write and implement poll tests showing at most one committed offer, B10 returns only null offers, does not create authority rows, and does not hold a transaction during optional wait.
- [ ] Register routes/security scheme and update the exact operation-ID regression.
- [ ] Run focused application/API/PostgreSQL tests.

### Task 3: Implement atomic adoption and lease renewal

**Files:**
- Modify: `src/nexa/application/worker_service.py`
- Modify: `src/nexa/api/routes_worker.py`
- Test: `tests/integration/test_worker_adoption_b10.py`
- Test: `tests/integration/test_worker_renewal_b10.py`

**Interfaces:**
- Consumes: exact prior/current `Authority`, current STARTING incarnation, container identity, Job/Attempt/Lease/Allocation rows, active checkpoint/result reservations, callback receipts.
- Produces: replayable `adopt_attempt` acknowledgment with transferred reservation snapshots and replayable `renew_attempt` DB-time lease acknowledgment.

- [ ] Write failing adoption tests for exact live predecessor, current STARTING requirement, immediate-prior incarnation, container mismatch, expired/revoked lease, desired-state/intent mismatch, callback replay, and reservation transfer preserving IDs/callbacks.
- [ ] Implement adoption in global lock order Worker -> Job -> Attempt/Lease -> Allocation -> reservation metadata, ending the prior grant, appending immutable lineage, rebinding attempt/lease/reservations, and extending lease from locked DB time.
- [ ] Write failing renewal tests for STARTING post-adoption renewal, exact full authority, coordinator epoch independence, `0/null`, higher progress, byte-equivalent replay, conflicting same sequence, expired/revoked lease, and WRITE_FROZEN.
- [ ] Implement renewal with callback receipt linearization, DB-time expiry, desired-state/intent checks, and monotonic progress semantics.
- [ ] Add concurrency tests for adoption versus revoke/renew and ensure losing operations leave no partial grant/reservation/progress mutation.
- [ ] Run focused PostgreSQL tests serially.

### Task 4: Implement worker configuration, singleton, credential storage, API client, and durable pending state

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/nexa/config.py`
- Create: `src/nexa/worker/singleton.py`
- Create: `src/nexa/worker/credentials.py`
- Create: `src/nexa/worker/client.py`
- Create: `src/nexa/worker/state.py`
- Test: `tests/worker/test_singleton_b10.py`
- Test: `tests/worker/test_credentials_b10.py`
- Test: `tests/worker/test_client_b10.py`
- Test: `tests/worker/test_state_b10.py`

**Interfaces:**
- Consumes: B06 worker bootstrap endpoint and B10 REST operations.
- Produces: validated `WorkerSettings`, lifetime `LocalWorkerLock`, atomic mode-0600 credential file, bounded `WorkerApiClient`, and fsync-safe agent state storing callback ID, exact payload, first-send monotonic nanoseconds, boot/clock domain, runner control sequence, acknowledgment, and pending status.

- [ ] Write a multiprocessing RED test proving the second process fails before invoking bootstrap/incarnation/Docker callbacks and the stable lock inode is never removed.
- [ ] Implement `fcntl.flock` singleton with non-inheritable close-on-exec FD and explicit exit reason.
- [ ] Write RED tests for credential file mode, atomic replace/directory fsync, identity/expiry validation, absent-file bootstrap, one-time response loss, and refusal to generate a new bootstrap idempotency key after uncertain delivery.
- [ ] Implement credential storage/bootstrap recovery behavior without logging raw values.
- [ ] Move `httpx` into runtime dependencies, refresh the lockfile, and write MockTransport tests for exact headers/routes/status/error parsing and bounded retry rules.
- [ ] Write RED tests for pending-operation reload, same-boot monotonic reuse, reboot/clock-domain mismatch fail-closed, and payload/callback immutability.
- [ ] Implement atomic bounded agent-state persistence.
- [ ] Run focused worker tests, `uv lock --check`, and secret-log assertions.

### Task 5: Integrate B09 journal, exact container discovery, runner controls, reconciliation, and worker loops

**Files:**
- Modify: `src/nexa/worker/models.py`
- Modify: `src/nexa/worker/docker_config.py`
- Modify: `src/nexa/worker/executor.py`
- Modify: `src/nexa/worker/journal.py`
- Create: `src/nexa/worker/runner_control.py`
- Create: `src/nexa/worker/agent.py`
- Create: `src/nexa/worker/main.py`
- Modify: `src/nexa/worker/__init__.py`
- Test: `tests/worker/test_journal_adoption_b10.py`
- Test: `tests/worker/test_runner_control_b10.py`
- Test: `tests/worker/test_reconciliation_b10.py`
- Test: `tests/worker/test_agent_b10.py`

**Interfaces:**
- Consumes: B09 `ResourceProvider`, `DockerExecutor`, `ExecutionJournal`, immutable Docker labels/digest, `DockerControlChannel`, runner frame protocol, B10 REST client and durable state.
- Produces: deployment-bound discovery, atomic journal authority rebind, runner deadline ACK handling, startup reconciliation, independent heartbeat/renew/poll tasks, graceful shutdown, and console entrypoint.

- [ ] Write RED tests requiring immutable installation/deployment labels and proving containers outside the deployment are never inspected/stopped.
- [ ] Extend B09 container configuration/runtime digest and Docker discovery with the configured installation label while preserving B09 exact-identity regressions.
- [ ] Write RED journal tests for exact prior-authority rebind, transferred reservation snapshot checks, preserved execution binding/sequences, and conflict fail-closed.
- [ ] Implement a narrow atomic `rebind_authority` operation; do not call `prepare` again.
- [ ] Write RED runner-control tests for control sequence/hash replay, progress-message ACK, timely candidate application, delayed API acknowledgment, delayed runner ACK, disconnect, and boot-domain mismatch.
- [ ] Implement runner control over the existing replayable private channel; only persist/apply a deadline after both valid API acknowledgment timing and runner ACK.
- [ ] Write RED reconciliation tests for empty READY, exact live adoption, expired/revoked stop path, `UNCLAIMED` sequence-1 tombstone, unknown create/start outcome, managed orphan, pagination restart, and pending cleanup/failure surviving agent restart.
- [ ] Implement reconciliation without Docker I/O in server transactions. Unacknowledged B11/B15 peer operations remain durable pending and block READY.
- [ ] Write RED lifecycle tests for singleton -> credential -> incarnation -> capability/journal checks -> reconcile -> heartbeat READY, plus SIGTERM, slow Docker call, poll/heartbeat independence, adopted renewal while STARTING, and bounded backoff.
- [ ] Implement `WorkerAgent.run()` with separate bounded asyncio tasks and `nexa-worker` console entrypoint.
- [ ] Run focused worker/workload tests and the existing B09 regression suite.

### Task 6: Add the minimal B10 Compose/bootstrap contract and operator documentation

**Files:**
- Create: `compose.yaml`
- Modify: `.env.example`
- Create: `docs/worker-agent.md`
- Modify: `docs/project-structure.md`
- Modify: `docs/environment-inventory.md`
- Modify: `docs/authentication.md`
- Modify: `README.md`
- Modify: `ROADMAP.md`
- Create: `docs/evidence/B10-worker-heartbeat-reconcile.md`

**Interfaces:**
- Consumes: actual worker entrypoint/settings and current B06 bootstrap/B09 executor paths.
- Produces: a minimal non-production B10 Compose contract and exact run/bootstrap/recovery documentation; B21 clean-host/portability remains open.

- [x] Add a Compose contract where only worker mounts Docker socket, worker has no DB credential, API/worker use separate secret mounts, journal/credential/state volumes are persistent, and workload restart policy remains owned by B09 as `no`.
- [x] Validate configuration with `docker compose config` using safe placeholder image/secret/path values; do not start or deploy the stack externally.
- [x] Document bootstrap, credential recovery, paths, STARTING -> READY, 5/15/30 health, singleton/incarnation inspection, adoption/renewal, pending cleanup, shutdown, and current milestone limits.
- [x] Update README/ROADMAP to state B10 implementation status only after verification, preserving B01-B09 approval and leaving B11+ incomplete.
- [x] Build the evidence document with environment/revision, requirement/gate/command/result/raw-evidence links, fault timelines, real API/PostgreSQL versus Docker/test-peer layers, and explicit B11/B15 handoff.

### Task 7: Run review, verification, and bounded remediation

**Files:**
- Review: all B10 diffs and every untracked B10 file.
- Update: only files needed to fix findings or make evidence accurate.

**Interfaces:**
- Consumes: completed implementation and recorded test outputs.
- Produces: scoped B10 verification report with no unsupported Linux/GPU/release or independent-review claim.

- [x] Run focused RED/GREEN records during implementation, then `uv lock --check` and `uv sync --frozen --all-groups --no-editable`.
- [x] Run `uv run --no-sync ruff check .` and `uv run --no-sync ruff format --check .`.
- [x] Run the frozen-environment pytest suite with the available `.venv` equivalent;
  the macOS host has no `uv` executable, so the exact `uv run --no-sync pytest -q`
  spelling is recorded as unavailable rather than inferred.
- [x] If guarded PostgreSQL 17 is available, run B10 PostgreSQL tests and then the repository PostgreSQL suite with `--run-postgres`; otherwise record `blocked` with the exact missing prerequisite/command.
- [x] If the verified B09 image/runtime is available, run `scripts/b09_docker_tests.sh` plus B10 runtime scenarios; otherwise keep Docker/runtime gates `not-run` or `blocked` and do not infer them from unit tests.
- [x] Run `docker compose config`, `git diff --check`, `git status --short --untracked-files=all`, `git diff`, and inspect every untracked file separately.
- [x] Perform one consolidated code review for correctness, security, lock order, stale authority, secret handling, false READY, and milestone leakage.
- [x] Fix findings in one batch, rerun only affected regressions plus final required checks, with at most two extra review rounds.
- [x] Finalize evidence statuses using only `specified`, `not-run`, `blocked`, `pass`, or `fail`, and report: `B10 đã triển khai và kiểm chứng trong phạm vi đã ghi nhận; chờ Task Review độc lập.` only when every directly applicable executed gate supports that statement.

## Current Execution Status (2026-09-21)

This plan remains **scoped-verified, pending independent review**, not
approved. Server wire schemas, guarded PostgreSQL incarnation/heartbeat/
pagination/adoption/renewal and null poll, local singleton/private
credential/pending state, nonempty reconciliation, runner control ACK/deadline,
ARM64 identity handling, uncertain-heartbeat replay and a live Docker
worker-kill/restart scenario have evidence in the B10 evidence document. The
initial schema, false READY, restarted inventory-version, heartbeat replay and
ARM64 identity tests were observed RED before their fixes.

The runtime evidence is Docker Desktop's Linux VM. Bare-Linux portability,
GPU, clean-host Compose acceptance, release gates and independent Task Review
remain open. B11/B15 failure, cleanup, reaper, result and release transitions
remain explicit boundaries, not production no-ops.

## Mandatory Review Remediation (B10-R01-R14)

The independent review of snapshot `81395e6` rejected the partial B10 work.
The following remediation batches replace any earlier task wording that would
allow an empty-only worker or specification-only evidence. No batch may claim
closure until its focused test was observed RED and then GREEN.

### Batch A: Transaction, fencing, wire and readiness authority

**Files:**
- Modify: `src/nexa/application/worker_service.py`
- Modify: `src/nexa/api/schemas.py`
- Modify: `src/nexa/api/app.py`
- Modify: `src/nexa/infrastructure/artifacts/store.py`
- Test: `tests/integration/test_worker_api_b10.py`
- Test: `tests/integration/test_worker_authority_b10.py`
- Test: `tests/application/test_worker_schemas_b10.py`

- [x] R03: reject adoption replay after a newer incarnation while preserving a
  valid current-incarnation replay without a second lease extension.
- [x] R04: lock and compare `Job.job_fence` for adoption and renewal before any
  lineage, reservation, lease or progress mutation.
- [x] R05: acquire all decision locks before reading `clock_timestamp()`; cover
  lease-expiry and health-threshold waits with deterministic lock blockers.
- [x] R06: accept the approved `PAUSED/CHECKPOINT_FOR_PAUSE` continuation while
  retaining the normal `RUNNING/RUN` authority path.
- [x] R07: make reconciliation snapshot generations restartable after
  allocation changes and keep traversal/query work bounded per page.
- [x] R08: reject false READY for invalid host reserve, missing seccomp/closed
  capability declarations, or failed server artifact-storage write/fsync/
  watermark probe; filesystem I/O remains outside the DB transaction.
- [x] R09: make health sweep a no-op in `WRITE_FROZEN` under the policy lock.
- [x] R10: reject poll unless the current worker is `READY` and `ENABLED` and
  mode permits polling.
- [x] R11: acquire/create callback receipt scope before policy and Worker locks
  for heartbeat, adoption and renewal.
- [x] R13: require the complete progress wire snapshot and reject booleans for
  `progress_sequence` including the zero/no-progress branch.

### Batch B: Durable local bootstrap and pending operations

**Files:**
- Modify: `src/nexa/worker/state.py`
- Modify: `src/nexa/worker/client.py`
- Modify: `src/nexa/worker/main.py`
- Test: `tests/worker/test_local_state_b10.py`
- Test: `tests/worker/test_client_b10.py`
- Test: `tests/worker/test_agent_b10.py`

- [x] R02: validate every persisted operation, bound the encoded document
  before atomic write, replay exact unacknowledged operations, resolve stored
  acknowledgments, and fail closed after boot/clock-domain change.
- [x] R12: validate/read the bootstrap secret before recording send intent;
  convert sanitized transport failures to unavailable exit status without
  logging credentials; retain recovery intent only for an uncertain send.

### Batch C: Nonempty worker reconciliation and independent loops

**Files:**
- Create: `src/nexa/worker/agent.py`
- Create: `src/nexa/worker/runner_control.py`
- Modify: `src/nexa/worker/main.py`
- Modify: `src/nexa/worker/journal.py`
- Modify: `src/nexa/worker/client.py`
- Test: `tests/worker/test_reconciliation_b10.py`
- Test: `tests/worker/test_runner_control_b10.py`
- Test: `tests/worker/test_agent_b10.py`

- [x] R01: drain all reconciliation pages; discover only exact installation
  labels; adopt/rebind exact live identities; create safe tombstone/cleanup
  pending operations for invalid or absent identities; renew adopted attempts
  while STARTING; persist first-send deadlines and require runner control ACK.
- [x] Run heartbeat, renewal, polling, reconciliation and IPC as independently
  bounded loops with deterministic SIGTERM shutdown and durable restart replay.
- [x] Prove more than 100 rows, orphan/mismatch, delayed callback, and worker
  kill/restart against the real B09 runner where the environment permits; keep
  B11/B15 failure/cleanup peers explicitly test-only.

### Batch D: Reproducible TLS/restart stack and final evidence

**Files:**
- Modify: `compose.yaml`
- Modify: `deploy/b10/Dockerfile`
- Modify: `docs/worker-agent.md`
- Modify: `docs/evidence/B10-worker-heartbeat-reconcile.md`
- Modify: `README.md`
- Modify: `ROADMAP.md`

- [x] R14: use the B06 TLS boundary for worker-to-API transport, add the worker
  restart policy without changing workload restart `no`, and run a stack smoke
  covering bootstrap plus process restart with retained credential/journal.
- [x] Re-run focused suites, the serial guarded PostgreSQL suite, B09 Docker
  regression, Ruff/format/lock/diff checks, and review all tracked/untracked
  B10 files. Record unavailable bare-Linux/GPU gates as `blocked` or `not-run`,
  never as pass.
