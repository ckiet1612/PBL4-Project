# B09 Docker Executor and Trusted Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the worker-side resource discovery, bounded Docker executor, trusted-runner protocol/watchdog, and deterministic CPU workload image required by B09, with durable local identity bookkeeping and evidence-bounded tests.

**Architecture:** Keep all Docker access under `src/nexa/worker/`. A typed `ResourceProvider` probes the runtime actually hosting the workload and exposes a closed capability declaration. A `DockerExecutor` validates the immutable `ExecutionContext`, records intent and identity in a per-attempt journal before/after Docker side effects, and returns only exact cleanup proofs. The image entrypoint is a trusted runner that owns the bounded length-prefixed IPC channel, waits for an acknowledged authority deadline before launching CPU work, supervises a separate workload UID, and enforces startup/runtime/stop bounds independently of the worker process.

**Tech Stack:** Python 3.12, stdlib dataclasses/JSON/Unix sockets/subprocess/flock, Docker CLI/Engine API through an injected command adapter, Pytest/Hypothesis, Dockerfile with a pinned base digest, and the existing `uv`/Ruff toolchain when available.

**Spec:** `PLAN.md` sections 3, 4, 7, 9, 10, 13 and 14; `docs/contracts/internal-interfaces.md`; `docs/contracts/openapi.yaml`; `docs/contracts/domain-model.md`; `docs/contracts/concurrency-recovery.md`; `docs/contracts/workloads-checkpoints.md`; `docs/contracts/schemas/workload-manifests.schema.json`; `docs/adr/0001-process-boundaries-and-trust.md`; `docs/adr/0003-worker-authority-fencing-and-cleanup.md`; B09 request supplied by the user.

## Task Review Remediation Matrix

Task Review of base snapshot `e73d7f6` returned `Không duyệt`. Each row must
have a RED reproduction, a root-cause note, a GREEN regression test, and fresh
evidence before it can be marked closed.

| Finding | Reproduction/root cause | Required regression and implementation |
|---|---|---|
| B09-R01 | Runner entrypoint only ACKs; control socket/rootfs and UID model prevent production launch | Production executor/entrypoint launches verified CPU spec through a private writable control channel with distinct runner/workload UIDs |
| B09-R02 | Blocking `recv()` owns the loop; no independent startup/runtime/authority watchdog | Independent watchdog stops the full workload process group under controller loss, IPC/log pressure and CPU-bound work |
| B09-R03 | Stop observes only the direct parent | Descendant-liveness test; terminate then kill the full process group within five seconds before `STOPPED` |
| B09-R04 | Any prior ACK authorizes any later deadline; ACK at candidate is accepted | Bind deadline value to its callback candidate, require `ack_at < candidate`, reject failed/unacknowledged/expired updates |
| B09-R05 | Result message names exist without a stateful handshake | Fake peer covers progress and `RESULT_PREPARE` through `RESULT_READY`, descriptor/binding checks, bounds and replay |
| B09-R06 | Sequence is recorded before schema/effect validation; non-finite/null values pass | Strict closed payload validation before sequence commit; canonical type+payload hash; conflict fails/stops attempt |
| B09-R07 | One connection and in-memory sequence/result state | Durable runner protocol state with reconnect/lost-ACK replay across independent peers/processes |
| B09-R08 | Runtime digest hashes mutable inspect fields | Canonical digest of immutable container ID/image/labels/security/resource config; real create/start/stop/cleanup stability test |
| B09-R09 | `NO_CONTAINER` hashes a literal and lacks runtime observation/initial UNCLAIMED tombstone | Inspect exact identity under lock; fail on unavailable/uncertain/existing; allow sequence-1 server-confirmed UNCLAIMED tombstone |
| B09-R10 | Identity bind is treated as start success | Durable create-bound/start-in-flight/started phases; replay reconciles Docker state and never reports unconfirmed start |
| B09-R11 | Remove precedes durable cleanup evidence | Durable cleanup intent and stopped observation before remove; confirmed-absence replay after lost response/write crash |
| B09-R12 | Journal omits immutable architecture/adapter/framework/mount/limit fields | Persist canonical immutable request binding; parameterized field-change rejection and exact replay |
| B09-R13 | Mount validation is prefix-only | Open/no-follow trusted staging verification, checksum/size/type validation, reverify immediately before create |
| B09-R14 | Create/inspect/start receive independent budgets | Single injected monotonic startup deadline and remaining-budget propagation |
| B09-R15 | Probe advertises CPU adapter/framework for arbitrary images | Verified image labels/entrypoint/architecture metadata is the sole source of advertised image/adapter/framework capability |
| B09-R16 | Docker tests bypass production runner and omit direct abuse/deadline scenarios | Production executor/entrypoint harness for watchdog/controller loss, throttle, OOM, PID, scratch/log/IPC and UID/control isolation |
| B09-R17 | Materialization buffers the entire source before enforcing its declared size | Fixed-buffer streaming copy/hash stops after at most declared size plus one byte |

Implementation order is executor/journal/input/discovery (R08-R15), runner
protocol/watchdog/result flow (R01-R07), then the production Docker harness and
evidence (R16). README/ROADMAP remain in remediation status until every directly
applicable row has fresh evidence.

## Second Review Remediation Addendum (2026-09-21)

The second review closed R04, R05, R08, R12 and R15. The remaining work is
split into six reviewable tasks. The user approved the single-container,
non-root runner plus exact-ID `docker exec` supervisor design. This addendum
does not authorize a PLAN change, commit, push, branch/worktree creation or a
new Task Review request.

### Task A: Non-root runner and one-shot supervisor registration (R01)

**Files:** `src/nexa/worker/docker_config.py`, `src/nexa/worker/docker_client.py`,
`src/nexa/worker/executor.py`, `src/nexa/workloads/trusted_runner.py`,
`src/nexa/workloads/workload_supervisor.py`, `deploy/cpu-iterative/Dockerfile`,
`tests/worker/test_docker_config.py`, `tests/worker/test_docker_client.py`,
`tests/worker/test_executor.py`, `tests/workloads/test_runner.py`,
`tests/workloads/test_workload_supervisor.py`, `tests/docker/test_real_runner.py`.

**Interface:** `DockerCli.exec_detached(container_id, argv, *, user,
timeout_seconds)` operates only on the exact full container ID. Runner PID 1
creates a one-shot Unix registration socket, accepts only peer UID 1001 using
`SO_PEERCRED`, unlinks the socket after registration, and controls the
supervisor over the accepted stream. The workload child never inherits that
stream.

- [x] Add tests proving create config uses `1000:1000`, has no added
  capabilities, Dockerfile defaults to UID 1000, wrong registration peer is
  rejected, and executor starts the UID-1001 supervisor by exact container ID.
- [x] Run those tests and observe failures caused by the existing privileged UID model
  and missing exec/registration interfaces.
- [x] Implement the minimal exec backend, runner registration and supervisor
  client needed to pass them; registration timeout or channel loss fails closed.
- [x] Run the focused worker/workload tests and the production Docker UID/control
  isolation scenario.

### Task B: Terminal stop, descendant cleanup and serialized messages (R02, R03, R06, R07)

**Files:** `src/nexa/workloads/trusted_runner.py`,
`src/nexa/workloads/workload_supervisor.py`, `tests/workloads/test_runner.py`,
`tests/workloads/test_workload_supervisor.py`.

**Interface:** `REQUEST_STOP` compares its absolute grace deadline with the
runner clock and invokes cleanup immediately when expired. Authority updates
cannot renew an already-expired accepted deadline. Invalid binding/finalize
effects transition to a durable terminal failure that blocks all result effects.
Sequence allocation, pending mutation, ACK removal and persistence share the
runner lock.

- [x] Add table tests for stop before/equal/after grace expiry and renewal
  between watchdog ticks after authority expiry.
- [x] Add a real descendant test where the direct child exits before its child,
  plus a regression asserting the whole process group is gone before completion.
- [x] Add invalid-binding-then-corrected-finalize and concurrent monitor/watchdog
  barrier tests; assert terminal rejection and unique replayable sequences.
- [x] Run each new test and observe the specified wrong behavior, then implement
  one behavior at a time and rerun the focused file after each GREEN step.

### Task C: Exact reconciliation and monotonic clock domain (R09, R10, R11, R14)

**Files:** `src/nexa/worker/docker_client.py`, `src/nexa/worker/executor.py`,
`src/nexa/worker/journal.py`, `src/nexa/worker/models.py`,
`tests/worker/test_docker_client.py`, `tests/worker/test_executor.py`,
`tests/worker/test_executor_races.py`, `tests/worker/test_journal.py`.

**Interface:** tombstone compares authority, allocation, startup nonce and
immutable execution binding before returning proof. Timed-out
`START_IN_FLIGHT` may inspect and stop/remove the exact bound container but may
never restart it. Only Docker object-not-found is mapped to absence. Journal
startup timestamps are tagged with a persisted clock-domain identifier and are
reused only when the current identifier matches.

- [x] Add RED tests for tombstone nonce/authority mismatch, start-ACK loss plus
  cleanup after timeout, missing Docker socket classification, and same/different
  clock-domain reload.
- [x] Implement exact comparisons, inspection-only cleanup reconciliation,
  structured object-not-found classification and clock-domain persistence.
- [x] Run the journal, Docker client, executor and executor-race suites.

### Task D: Race-safe bounded input materialization (R13, R17)

**Files:** `src/nexa/worker/executor.py`, `tests/worker/test_executor.py`,
`tests/worker/test_executor_races.py`.

**Interface:** staging input is opened component-by-component relative to a
trusted directory FD using `openat` semantics and `O_NOFOLLOW`. The primary CPU
mount checksum must equal `ExecutionContext.input_checksum`. Copy and SHA-256
calculation stream through a fixed-size buffer and stop after reading at most
the declared size plus one byte.

- [x] Add RED tests for an ancestor symlink escape, context/mount checksum
  mismatch and a declared-one-byte source whose read counter never exceeds two
  bytes before rejection.
- [x] Implement descriptor-relative traversal, checksum binding and bounded
  streaming without loading the input into RAM.
- [x] Run executor and executor-race suites, including source-swap cases.

### Task E: Trigger-to-stop Docker evidence and truthful status (R16)

**Files:** `tests/docker/test_real_runner.py`,
`docs/evidence/B09-docker-executor-trusted-runner.md`,
`docs/evidence/raw/B09-docker-scenarios.jsonl`, `README.md`, `ROADMAP.md`,
`docs/project-structure.md`.

**Interface:** watchdog scenarios record the authority deadline or external
trigger before the stop request, independently observe workload death, and
assert the configured bound from that origin. Evidence never marks a finding
closed unless its regression and applicable Docker scenario pass.

- [x] Change timing tests first so the current implementation fails or exposes
  the unsupported measurement.
- [x] Update the harness to capture deadline/trigger, workload-death and runner
  message timestamps separately.
- [x] Rebuild the image and run all opt-in Docker scenarios on the available
  Docker Desktop Linux VM, preserving the platform limitation in evidence.
- [x] Rewrite status/evidence from fresh results; do not claim bare-Linux,
  portability, GPU, coordinator or release acceptance.

### Task F: Final regression and diff review

- [x] Run Ruff check and format check with `.venv/bin/ruff` and `PYTHONPATH=src`.
- [x] Run the focused B09 worker/workload suite, then the complete Pytest suite.
- [x] Run `git diff --check`, inspect tracked and untracked files separately,
  and verify no closed finding R04/R05/R08/R12/R15 regressed.
- [x] Report exact counts, skipped tests, Docker platform and any unverified gate;
  do not commit, push or request Task Review.

## Third Review Remediation Addendum (2026-09-21)

The third review keeps R03/R04/R05/R06/R08/R09/R10/R11/R12/R13/R15/R17
closed and reopens only R01, R02, R07, R14 and R16. The finite regression
points are authority-before-registration, expired authority and short grace,
serialized durable snapshots plus terminal replay, cross-domain `CREATED`, and
same-clock Docker timelines.

- [x] Queue accepted authority until the exact UID-1001 supervisor registers;
  launch once, reject expired authority and never enter the in-process UID
  fallback for a production launch spec.
- [x] Carry the requested bounded grace to the supervisor and prove real
  SIGTERM-ignoring children are killed for grace zero and sub-five-second grace.
- [x] Hold the runner lock across snapshot construction and atomic persistence;
  classify exact terminal retries as `DUPLICATE` before the terminal guard.
- [x] Reject start/exec for both `CREATED` and `START_IN_FLIGHT` after a clock
  domain change while leaving inspect/stop/cleanup available.
- [x] Measure deadline/runtime stop using guest monotonic timestamps, add the
  real registration barrier scenario, rebuild the image and refresh evidence.

## Fourth Review R01 Remediation Addendum (2026-09-21)

The fourth review keeps every other finding closed and reopens only R01 because
accepted authority moved the runner out of the watchdog startup branch while
the workload was still waiting for supervisor registration.

- [x] Add persisted-clock regressions for registration before, exactly at and
  after the 30-second startup deadline; only the pre-deadline case starts once.
- [x] Keep the startup deadline active until `runtime_started_at` exists and
  recheck the same deadline immediately before deferred `START`.
- [x] Send `START` before persisting the start transition, roll back a failed
  send, and keep a live registration stream available for bounded cleanup when
  `STARTED` is missing.
- [x] Fail closed without false `STOPPED` evidence if the supervisor transport
  disconnects after `START` and termination cannot be confirmed; propagate the
  fatal state through the control loop so PID 1 exits.
- [x] Add a real-Docker delayed-registration barrier proving no `STARTED` frame
  or CPU process, bounded `RUNTIME_LIMIT` stop, cleanup and refreshed evidence.

## Global Constraints

- PostgreSQL remains authoritative for jobs, attempts, allocations, leases, reservations, events and quota; the executor journal is local bookkeeping only and is never queried by API/coordinator/domain code.
- Only worker-side infrastructure may invoke Docker. The runner/workload image receives no Docker socket, worker credential, DB credential, arbitrary client command, client path, or unrestricted mount.
- `StartExecution` accepts only an authorized immutable context, exact image digest, internal artifact descriptors, allocation, startup nonce, runner channel and bounded resource settings.
- `prepare`/`start` are idempotent only for exact `(attempt_id, allocation_id, startup_nonce)` plus immutable content; changed context is rejected.
- Every create intent is durable before the side effect, every created identity is durable before the operation is acknowledged, and create timeout never triggers blind duplicate create.
- Container identity uses full ID plus a stable runtime identity digest and exact labels for attempt/allocation/startup nonce; names, image tags and PIDs are not proof.
- Cleanup returns only schema-valid `NO_CONTAINER` or exact `CONTAINER_STOPPED` proof after no create is in flight and identity/state checks succeed; it never changes allocation/job/quota state.
- Containers must be non-root for the workload, read-only root/input, network-disabled, capability-dropped, `no-new-privileges`, seccomp-enabled, no host namespaces/socket, CPU/RAM/PID/tmpfs/log/runtime bounded, and restart policy `no`.
- Trusted runner frames are length-prefixed UTF-8 JSON, maximum 64 KiB, closed-schema, bounded pending state, and sequence/ACK semantics exactly as the frozen contract.
- Runner deadline candidate is `first_send_monotonic + lease_duration - safety_margin`; failed/late/replayed callbacks never extend authority or reset runtime budget.
- B09 exposes checkpoint/result protocol boundaries and rejects unsupported lifecycle operations safely; it does not implement B10/B11 HTTP renewal, result publication, checkpoint restore, recovery, or release.
- Acceptance status is evidence-based: unit/mock checks are not Linux/cgroups/runtime acceptance. macOS Docker Desktop evidence is labelled development Docker/VM evidence, not Linux-host pass.

## Requirement Traceability

| Requirement | Source | Module/interface | Test/evidence | Owner |
|---|---|---|---|---|
| Discover host/runtime and compute reserve-safe allocatable capacity | internal interfaces §ResourceProvider; OpenAPI `WorkerInventory`; PLAN §4/§10 | `nexa.worker.capabilities.ResourceProvider` | `tests/worker/test_capabilities.py`; `scripts/b09_probe.py`; B09 evidence ACC-04 | B09 |
| Closed compatibility reasons and no CUDA→CPU fallback | internal interfaces; invariants | `CompatibilityResult` | `tests/worker/test_capabilities.py` | B09 |
| Durable per-attempt state and operation sequence | domain model `ExecutorAttemptRecord`; concurrency contract | `nexa.worker.journal.ExecutionJournal` | `tests/worker/test_journal.py` | B09 |
| Exact identity, idempotent prepare/start, tombstone and uncertain create | Executor contract; ADR-0003 | `nexa.worker.executor.DockerExecutor` | `tests/worker/test_executor.py` | B09 |
| Real Docker security/resource configuration | Executor postconditions; PLAN §9 | `DockerCli` + `ContainerConfig` | `tests/worker/test_docker_config.py`; opt-in `tests/docker/test_real_executor.py` | B09 |
| Trusted runner IPC, sequence, ACK, progress and result boundary | internal interfaces trusted runner channel | `nexa.worker.protocol`, `nexa.workloads.trusted_runner` | `tests/worker/test_protocol.py`, `tests/workloads/test_runner.py` | B09 |
| Authority deadline/watchdog and non-cooperative stop | internal interfaces; concurrency contract | `DeadlineController`, `RunnerSupervisor` | `tests/workloads/test_deadline.py`; opt-in Docker watchdog harness | B09 |
| Deterministic CPU iterative input/result | workloads/checkpoints §cpu-iterative | `nexa.workloads.cpu_iterative` | `tests/workloads/test_cpu_iterative.py`; image smoke | B09 |
| Reproducible image and verified digest/architecture reporting | PLAN §4/§14 | `deploy/cpu-iterative/Dockerfile`, `scripts/b09_build_image.sh` | `scripts/b09_smoke.sh`; evidence record | B09 |
| Handoff primitives for B10/B11/B14/B15/B23 | user B09 request; internal interfaces | public worker/workload modules and docs | evidence/handoff sections | B09 |

## Implementation Tasks

### Task 1: Add worker/workload package boundaries and typed contracts

**Files:**
- Create: `src/nexa/worker/__init__.py`
- Create: `src/nexa/workloads/__init__.py`
- Create: `src/nexa/worker/errors.py`
- Create: `src/nexa/worker/models.py`
- Create: `src/nexa/workloads/models.py`
- Test: `tests/worker/test_models.py`

**Interfaces:**
- Consumes: frozen `Authority`, `ExecutionContext`, `Allocation`, `ContainerIdentity`, `CleanupProof`, `WorkerInventory` shapes from the OpenAPI/internal contracts.
- Produces: immutable dataclasses `Authority`, `ResourceVector`, `ReservePolicy`, `WorkloadRequirement`, `ExecutionContext`, `ContainerIdentity`, `CleanupProof`, `StartExecution`, `ContainerObservation`, and typed `ExecutorError`/`DiscoveryError`/`CompatibilityReason` values used by every later task.

- [ ] **Step 1: Write failing model tests.** Assert strict checksum/UUID/architecture/image-digest/nonce validation, non-negative resource bounds, exact authority/context identity matching, and closed failure/proof enums.
- [ ] **Step 2: Run the focused tests to verify RED.** Run `pytest -q tests/worker/test_models.py`; expected failure is missing modules/classes or validation methods, not a collection error.
- [ ] **Step 3: Implement the minimal dataclasses and validators.** Use frozen dataclasses and explicit constructors; reject unknown fields at JSON boundaries; do not import Docker, SQLAlchemy, FastAPI or PyTorch.
- [ ] **Step 4: Run the focused tests to verify GREEN.** Run `pytest -q tests/worker/test_models.py` and then `ruff check` on the new modules.
- [ ] **Step 5: Refactor only naming/duplication while keeping tests green.** Keep domain-facing types transport-free and document which values are durable timestamps versus local monotonic values.

### Task 2: Implement host/runtime discovery and capability compatibility

**Files:**
- Create: `src/nexa/worker/capabilities.py`
- Create: `src/nexa/worker/probes.py`
- Test: `tests/worker/test_capabilities.py`
- Create: `scripts/b09_probe.py`

**Interfaces:**
- Consumes: `ProbeBackend` commands/filesystem hooks, configured `ReservePolicy`, allowlisted CPU image/adapters, and the typed models from Task 1.
- Produces: `ResourceProvider.discover() -> HostInventory`, `.allocatable(inventory, reserve) -> CapabilitySnapshot`, `.compatible(requirement, capability) -> CompatibilityResult`, plus a JSON report CLI that never mutates worker readiness.

- [ ] **Step 1: Write failing tests for reserve floors and rounding.** Cover CPU reserve `max(1000, ceil(20%))`, RAM reserve `max(2 GiB, ceil(20%))`, configured larger reserve, exact host-fit subtraction, tiny-host fail-closed, and no negative/over-host allocatable values.
- [ ] **Step 2: Write failing tests for closed compatibility.** Cover architecture/image/adapter/framework/CUDA/driver/GPU-count/resource reasons, missing runtime/cgroups/seccomp, unverified image digest, and no CUDA-to-CPU fallback.
- [ ] **Step 3: Run `pytest -q tests/worker/test_capabilities.py` and confirm expected failures.** Use injected probe fixtures so tests do not depend on this macOS host.
- [ ] **Step 4: Implement probes and capability models.** Probe Linux architecture, logical CPU, RAM, Docker/OCI version, cgroups version, kernel, seccomp, adapter/image/framework declarations, and optional GPU inventory through a narrow backend; fail closed on unsupported OS, unavailable Docker, malformed/unstable data, or missing required fields.
- [ ] **Step 5: Implement allocatable/compatible logic.** Round reserves upward before subtraction, preserve raw host values, keep capability arrays closed, and return a structured reason code plus safe detail without hostname/path/secret leakage.
- [ ] **Step 6: Run focused tests and the report smoke.** Run `pytest -q tests/worker/test_capabilities.py` and `python scripts/b09_probe.py --json`; on macOS the latter must return a typed unsupported/blocked report rather than inventing Linux capacity.

### Task 3: Implement durable local executor journal and per-attempt locking

**Files:**
- Create: `src/nexa/worker/journal.py`
- Test: `tests/worker/test_journal.py`

**Interfaces:**
- Consumes: Task 1 `ExecutorAttemptRecord` fields and a configurable journal root owned by the worker process.
- Produces: `ExecutionJournal.load(attempt_id)`, `.prepare(record)`, `.begin_create(...)`, `.bind_created(identity)`, `.tombstone(...)`, `.append_runner_state(...)`, `.lock(attempt_id)`, and atomic `JournalCorruption`/`JournalWriteError` failures.

- [ ] **Step 1: Write failing tests for initial state and operation sequence.** Verify exact JSON schema, `PREPARED → CREATE_IN_FLIGHT → CREATED/TOMBSTONED`, monotonic sequence, startup nonce uniqueness, and reload after process boundary.
- [ ] **Step 2: Write failing tests for crash points and corruption.** Inject failures before intent write, after intent write, after Docker identity bind, and during atomic replace; assert no success/proof is returned and corrupted/truncated files fail closed.
- [ ] **Step 3: Write a multiprocessing locking test.** Use a barrier and two processes on one attempt; assert only one operation owns the lock and the loser observes the committed sequence instead of creating a second record.
- [ ] **Step 4: Run `pytest -q tests/worker/test_journal.py` and confirm RED.** Confirm failures are caused by missing journal behavior.
- [ ] **Step 5: Implement atomic journal writes.** Write bounded JSON to a same-directory temporary file, `fsync` the file, atomically replace, `fsync` the directory, and use `fcntl.flock` on a stable per-attempt lock file held across the Docker side effect.
- [ ] **Step 6: Implement tombstone rules.** A tombstone advances sequence, records observed inspection checksum and reason, rejects delayed older/equal prepare/start, and is the only basis for a `NO_CONTAINER` proof after create-in-flight is absent.
- [ ] **Step 7: Run focused tests and `ruff check`.** Keep all journal writes local and free of DB/Docker imports.

### Task 4: Implement Docker command adapter and hardened container configuration

**Files:**
- Create: `src/nexa/worker/docker_client.py`
- Create: `src/nexa/worker/docker_config.py`
- Test: `tests/worker/test_docker_config.py`
- Test: `tests/worker/test_docker_client.py`

**Interfaces:**
- Consumes: exact `StartExecution`, `ContainerConfig`, `DockerCommandBackend` and Task 1 identity types.
- Produces: `DockerCli.create(config)`, `.start(container_id)`, `.inspect(container_id)`, `.stop(container_id, timeout)`, `.kill(container_id)`, `.remove(container_id)`, and a normalized `DockerInspection` with full ID, labels, runtime identity digest, state, exit code, OOM flag and config checksum.

- [ ] **Step 1: Write failing configuration tests.** Assert argv/config contains exact digest (never tag), read-only rootfs/input, `--network none`, dropped capabilities, `no-new-privileges`, seccomp, CPU quota, hard memory, PID cap, bounded tmpfs scratch, bounded Docker log driver/options, restart `no`, no privileged/host namespace/socket, and labels for attempt/allocation/startup nonce.
- [ ] **Step 2: Write failing command-adapter tests.** Assert commands use argument arrays, bounded stdout/stderr capture, timeout classification, full 64-character container ID validation, and no interpolation of client paths/commands/env.
- [ ] **Step 3: Run focused tests and confirm RED.** Run `pytest -q tests/worker/test_docker_config.py tests/worker/test_docker_client.py`.
- [ ] **Step 4: Implement `ContainerConfig` and command adapter.** Build only from allowlisted immutable execution context and internal staged artifact mounts; use `docker create --cidfile` or JSON output so the full ID is captured, then normalize inspect results and compute a canonical runtime identity digest from exact immutable labels/config/image ID/architecture.
- [ ] **Step 5: Implement timeout/uncertainty classification.** A create timeout returns `CREATE_OUTCOME_UNKNOWN` with the durable intent intact; it never retries create or emits a proof. An inspect not-found during create-in-flight remains uncertain.
- [ ] **Step 6: Run focused tests and static checks.** Run both test files and Ruff; do not claim real resource enforcement yet.

### Task 5: Implement `DockerExecutor` identity, idempotency, stop and cleanup

**Files:**
- Create: `src/nexa/worker/executor.py`
- Test: `tests/worker/test_executor.py`
- Test: `tests/worker/test_executor_races.py`

**Interfaces:**
- Consumes: Tasks 1–4 models/journal/Docker adapter and an injected monotonic/UTC clock.
- Produces: `DockerExecutor.prepare(request)`, `.start(prepared)`, `.signal_checkpoint(identity, reason, deadline)`, `.stop(identity, grace_seconds)`, `.inspect(identity)`, `.cleanup(identity)` implementing the frozen `Executor` contract.

- [ ] **Step 1: Write failing identity/idempotency tests.** Cover exact same request replay, changed immutable context rejection, authority/allocation/attempt mismatch, wrong image digest/architecture/input checksum, and stable labels/runtime digest.
- [ ] **Step 2: Write failing race/crash tests.** Cover concurrent same-identity start, start vs pre-create stop, tombstone vs delayed start, crash before/after create, crash before/after identity persistence, create timeout after Docker created, wrong-ID cleanup, inspect unavailable, and repeated cleanup.
- [ ] **Step 3: Run `pytest -q tests/worker/test_executor.py tests/worker/test_executor_races.py` and confirm RED.** Use a fake command backend with barriers/events; do not use sleeps to choose a winner.
- [ ] **Step 4: Implement `prepare`.** Under the attempt lock, validate the immutable context against the prepared journal record, verify exact image/capability/input descriptors and bounds, materialize only trusted internal mounts, persist `PREPARED`, and return a replayable prepared handle.
- [ ] **Step 5: Implement `start`.** Under the same lock, persist `CREATE_IN_FLIGHT` before Docker create; on success inspect exact labels/config, persist full identity before returning, and start only the bound container. On unknown create outcome return a typed error while retaining intent; never create a second container.
- [ ] **Step 6: Implement `signal_checkpoint` as a safe B09 boundary.** Return a typed `UNSUPPORTED`/`INVALID_STATE` result until B14 checkpoint lifecycle is present; do not claim a checkpoint was created.
- [ ] **Step 7: Implement stop/inspect/cleanup.** Stop/kill only exact full-ID plus runtime-digest matches; delayed/tombstoned operations are rejected; cleanup proves no container only after no create in flight and corroborated journal/inspection state, otherwise returns an uncertainty error.
- [ ] **Step 8: Run focused tests and refactor only after green.** Verify no executor path imports application/API/SQLAlchemy.

### Task 6: Implement strict trusted-runner IPC protocol and deadline state machine

**Files:**
- Create: `src/nexa/worker/protocol.py`
- Create: `src/nexa/workloads/trusted_runner.py`
- Create: `src/nexa/workloads/deadline.py`
- Test: `tests/worker/test_protocol.py`
- Test: `tests/workloads/test_deadline.py`
- Test: `tests/workloads/test_runner.py`

**Interfaces:**
- Consumes: internal trusted-runner channel schemas and Task 1 clocks/models.
- Produces: `encode_frame`, `FrameDecoder.feed`, strict `RunnerEnvelope`/`ControlEnvelope`, `SequenceState`, `AckState`, `DeadlineController`, and `RunnerSupervisor` with bounded buffers and typed protocol failures.

- [ ] **Step 1: Write failing protocol tests.** Cover fragmented frames, invalid UTF-8/JSON, duplicate/unknown fields/type/schema, oversized/truncated/flooded frames, duplicate same payload, same sequence changed payload, lower unseen sequence, ACK matching, and bounded pending messages.
- [ ] **Step 2: Write failing deadline tests.** Cover no compute before timely start ACK, candidate from first send, failed/late/replayed response, lost ACK replay with same sequence/payload, disconnect without extension, monotonic nondecreasing deadline, runtime budget not reset by renewal, and stop/cleanup not blocked by result/checkpoint handshake.
- [ ] **Step 3: Write failing watchdog tests.** Use an injected clock and non-cooperative child stub; assert startup ≤30 s, runtime ≤300 s or spec-shorter, graceful stop ≤5 s then kill, and controller-process loss does not stop the runner watchdog.
- [ ] **Step 4: Run focused tests and confirm RED.** Run `pytest -q tests/worker/test_protocol.py tests/workloads/test_deadline.py tests/workloads/test_runner.py`.
- [ ] **Step 5: Implement frame codec and sequence state.** Prefix each UTF-8 JSON frame with a 4-byte network-order length, reject lengths >64 KiB before allocation, enforce closed payload schemas, hash payload bytes, and return `DUPLICATE`, `INVALID`, `OUT_OF_ORDER`, or `STALE_AUTHORITY` exactly as specified.
- [ ] **Step 6: Implement deadline controller.** Record first-send monotonic time per callback before sending, apply `SET_AUTHORITY_DEADLINE` only after a valid acknowledgment before candidate, never rebase on retry/late response, and stop when the last accepted deadline/runtime bound expires.
- [ ] **Step 7: Implement runner supervisor.** Keep control IPC private and not inherited by workload, start the workload only after valid authority deadline, bound output/log frames and buffers, use a separate workload UID mechanism in the image, and kill the full process group on deadline/stop.
- [ ] **Step 8: Implement result/checkpoint boundary state.** Persist/replay `RESULT_PREPARE`, `PREPARE_RESULT`, bounded file batches, binding batches and finalization only as strict protocol messages; reject unsupported server reservation/publish operations rather than generating IDs or final manifests.
- [ ] **Step 9: Run focused protocol/deadline/runner tests and Ruff.** Capture timing assertions using injected clocks for pure state and a bounded real-process test only where actual stop is required.

### Task 7: Implement deterministic CPU workload adapter and result oracle

**Files:**
- Create: `src/nexa/workloads/cpu_iterative.py`
- Create: `src/nexa/workloads/cpu_entrypoint.py`
- Test: `tests/workloads/test_cpu_iterative.py`
- Test: `tests/workloads/test_cpu_entrypoint.py`

**Interfaces:**
- Consumes: strict input JSON and immutable `iterations/seed/modulus/spec_checksum` values from the CPU template contract.
- Produces: `CpuIterativeAdapter.validate_input`, `.run`, `.result_bytes`, `.progress`, `.failure_class`; exact result JSON with only `iterations`, `final_accumulator`, `input_checksum`, `spec_checksum`.

- [ ] **Step 1: Write failing oracle tests.** Cover known small vectors, arbitrary-precision semantics, input/parameter bounds, canonical JSON bytes/checksum, progress from step 1, deterministic repeat, and invalid-input classification.
- [ ] **Step 2: Run `pytest -q tests/workloads/test_cpu_iterative.py` and confirm RED.**
- [ ] **Step 3: Implement strict validation and computation.** Parse only an object with `initial_value`, use Python integer arithmetic before modulus, calculate `a0` and each step exactly, and keep result field order/canonical serialization stable.
- [ ] **Step 4: Implement entrypoint integration.** Read only a trusted staged input path supplied by runner, write result to a runner-owned staging directory, emit bounded progress snapshots, and never read/write control credentials or fabricate artifact IDs.
- [ ] **Step 5: Run focused tests and an in-process smoke.** Verify exact bytes and failure classes before adding the image.

### Task 8: Build reproducible CPU image and opt-in real-Docker harness

**Files:**
- Create: `deploy/cpu-iterative/Dockerfile`
- Create: `deploy/cpu-iterative/runner-config.json`
- Create: `scripts/b09_build_image.sh`
- Create: `scripts/b09_smoke.sh`
- Create: `scripts/b09_docker_tests.sh`
- Test: `tests/docker/test_real_executor.py`
- Test: `tests/docker/test_real_runner.py`

**Interfaces:**
- Consumes: Task 4 hardened config, Task 6 runner, Task 7 CPU entrypoint, a user-provided or locally built immutable image digest.
- Produces: build/run commands, preflight checks, raw JSON reports containing image ID/digest/architecture/config and bounded scenario timelines; no registry push.

- [ ] **Step 1: Write failing harness tests/preflight tests.** Assert missing daemon, non-Linux guest, unavailable cgroups/seccomp, wrong architecture, or missing image digest produce explicit `blocked/not-run` output and nonzero status when the suite is explicitly requested.
- [ ] **Step 2: Add the image recipe.** Pin the base image by digest, install only required runtime packages, create distinct runner/workload UIDs, set read-only-safe directories, provide no network dependency, and keep the entrypoint under the trusted runner.
- [ ] **Step 3: Add build/smoke scripts.** Build with a deterministic tag only for local reference, resolve and print the resulting content digest/ID/architecture, run exact CPU input through the runner, and clean only containers/volumes created by the script via explicit IDs.
- [ ] **Step 4: Add real-Docker scenarios.** Cover inspect config, identity labels/digest, rootfs/input write denial, network denial, no socket/credential visibility, CPU/RAM/PID/scratch/log bounds, restart `no`, startup/runtime/stop deadlines, controller kill, and exact cleanup proof. Use bounded abuse inputs; never use Docker prune or broad deletion.
- [ ] **Step 5: Run the suite on the current host.** Use `scripts/b09_docker_tests.sh --require` so unsupported Linux-only checks are reported as blocked/not-run rather than skipped green; record Docker Desktop/VM boundary and exact image digest.

### Task 9: Documentation, evidence and downstream handoff

**Files:**
- Create: `docs/evidence/B09-docker-executor-trusted-runner.md`
- Create: `docs/worker-executor.md`
- Create: `docs/trusted-runner.md`
- Modify: `docs/project-structure.md`
- Modify: `docs/environment-inventory.md`
- Modify: `README.md`
- Modify: `ROADMAP.md`
- Modify: `.gitignore` only if required for bounded B09 raw-output directories

**Interfaces:**
- Consumes: verified commands/raw reports from Tasks 1–8 and current revision/environment.
- Produces: reproducible B09 evidence with gate status, limits, exact image/config identity, and handoff contracts for B10/B11/B14/B15/B23.

- [ ] **Step 1: Write the evidence skeleton before running final verification.** Include revision, environment, Docker context, image digest/ID/architecture, config, command, raw report path, requirement/gate mapping, and `pass/not-run/blocked/fail` status.
- [ ] **Step 2: Document process/UID/mount/control boundaries.** State how runner/workload are launched, which FD/path is private, how stop reaches descendants, and any platform limitation without claiming an unverified Linux property.
- [ ] **Step 3: Document journal/crash/uncertain-create/cleanup behavior and typed failure mapping.** Explicitly separate local proof from later API release/quota/state transitions.
- [ ] **Step 4: Document deadline timeline and result/checkpoint boundaries.** Include first-send candidate equation, ACK/retry rules, runtime budget, and unsupported B14/B11 operations.
- [ ] **Step 5: Update project structure/README/ROADMAP.** Mark B09 as implemented only if direct implementation checks are complete; say “chờ Task Review” and list blocked Linux/runtime evidence when applicable. Do not alter B01–B08 status or PLAN.
- [ ] **Step 6: Run documentation link/format checks and inspect all untracked files.** Ensure no secrets, host paths, fake registry digests or raw workload data are included.

### Task 10: Full verification and self-review

**Files:**
- Modify only files with findings from Tasks 1–9.
- Test: all B09 focused and repository regression suites.

- [ ] **Step 1: Run focused B09 unit/protocol/workload tests.** Use `.venv/bin/pytest -q tests/worker tests/workloads` if `uv` is unavailable; otherwise use `uv run --no-sync pytest -q tests/worker tests/workloads`.
- [ ] **Step 2: Run image/real-Docker harness.** Execute `scripts/b09_docker_tests.sh --require` and preserve raw output; classify each scenario by environment instead of treating a preflight block as pass.
- [ ] **Step 3: Run Python regression and quality checks.** Execute `uv lock --check`, frozen sync if available, Ruff check/format, full `pytest -q`, and `git diff --check`; use the existing `.venv` only as a local fallback and record the command actually run.
- [ ] **Step 4: Review security and scope.** Search for Docker imports outside `src/nexa/worker`, raw client path/command/env handling, secrets in logs/docs, broad cleanup commands, unsafe JSON/pickle, image tags/latest, hidden CUDA fallback, DB/API imports in runner/workload, and any claim that local output equals a succeeded Job.
- [ ] **Step 5: Apply at most two focused fix-review rounds.** For every finding record source, location, impact, correction and verification; do not reopen closed findings without new code/evidence.
- [ ] **Step 6: Re-run all affected verification commands and finalize evidence.** Do not claim B09 complete until the fresh outputs and diff review support the exact claim; report remaining Linux/GPU/host limitations explicitly.

## Acceptance and Evidence Boundary

- `ACC-04`: discovery/reserve/compatibility and executor limit portions only; worker READY and coordinator allocation remain B10/B11.
- `ACC-13`: runner deadline/watchdog portion only; DB lease renewal HTTP belongs to B10/B11.
- `ACC-22`: startup/runtime/OOM/failure classification scenarios that actually ran.
- `ACC-25`: container isolation/resource configuration and behavior; Linux/cgroups evidence requires a Linux host.
- `ACC-23`/`ACC-31`: only direct cleanup/identity or response-loss scenarios implemented here; no full recovery/reconcile claim.
- Unit tests, fake Docker, and macOS Docker Desktop do not prove Linux-host acceptance, two-host portability, GPU support, production result publication, quota release, or full recovery.

## Handoff

- **B10:** consumes `ResourceProvider`, `DockerExecutor`, `ExecutionJournal`, exact identity/cleanup proofs, trusted-runner IPC and deadline primitives; adds worker bootstrap/incarnation/heartbeat/poll/reconcile and real API renewal.
- **B11:** consumes immutable `ExecutionContext` validation, start observations, result reservation/file-binding protocol seam and exact proof/identity fields; owns coordinator dispatch, fenced publish/release and PostgreSQL state transitions.
- **B14:** consumes CPU adapter oracle, progress/result protocol boundary and explicit unsupported checkpoint signal; owns checkpoint persistence, restore, manifest/provenance and fallback.
- **B15:** consumes typed failure classes, stop/deadline/tombstone/cleanup observations; owns cancel/pause/resume/retry/reaper/quarantine/recovery state machine.
- **B23:** consumes capability extension points and image declaration shape; GPU provider/isolation/acceptance remains unimplemented and unverified.

## Final Status Rule

The final report must say either “B09 đã triển khai và kiểm chứng trong phạm vi nêu trên, sẵn sàng chuyển Task Review” with exact evidence, or “B09 chưa hoàn tất vì …” with the concrete blocker. No commit, push, branch, release, external deployment, or Task Review request is part of this plan.
