# B15 control and recovery evidence (cancel, pause/resume, retry, reaper, admin worker)

Trạng thái: **đã triển khai; Task Review vòng 1 không duyệt, mọi finding vòng 1 đã sửa, chờ duyệt vòng 2**. Baseline `b73da3b` (B01–B14 đã duyệt).
Không dùng Superpowers theo prompt B15. Mục 13 của prompt bị cắt, nên báo cáo cuối dùng
định dạng hợp lý và ghi rõ việc bị cắt.

## Kế hoạch

### Context map

| Requirement (PLAN B15 / prompt) | Contract | Code before B15 | Tests before B15 | Gap closed by B15 |
|---|---|---|---|---|
| Cancel QUEUED/RETRY_WAIT/PAUSED → CANCELLED; active → CANCELLING → CANCELLED | state-machines.md:37–40, 56, 70; concurrency-recovery.md:45, 101–103 | `report_cleanup` handles CANCELLING→CANCELLED; no route/service | test_cleanup_b11 CANCELLING fixtures | `cancelJob` route + `JobControlService.cancel` |
| Pause RUNNING → PAUSING → PAUSED with checkpoint | state-machines.md:21–24, 71, 78, 125 | runner rejects `REQUEST_CHECKPOINT PAUSE`; checkpoint service requires desired RUNNING | none | `pauseJob`, checkpoint in PAUSING, publish → STOPPING, worker pause flow, PAUSING cleanup → PAUSED, PAUSE_ABORTED |
| Resume PAUSED → QUEUED | state-machines.md:26, 54 | none | none | `resumeJob` |
| Manual retry FAILED → new job | state-machines.md:41, 55, 121, 127; concurrency-recovery.md:77 | submit admission only | submit tests | `retryFailedJob`, CheckpointReference MANUAL_RETRY, restore from reference |
| Lease reaper | state-machines.md:25, 27, 77, 88; concurrency-recovery.md:46, 72–73, 92 | none (`ix_attempt_leases_active_expiry` exists) | none | `CoordinatorService.reap_leases` |
| Pause-crash 3 outcomes, CFP | state-machines.md:15–18, 29–35; concurrency-recovery.md:109–113 | queue predicate hardcodes desired RUNNING ∧ intent NULL; dispatch hardcodes RUN; worker rejects CFP | none | 0019 predicate, CFP dispatch/claim/start, cleanup branches |
| Admin worker drain/disable/enable | state-machines.md:96–104; openapi 957–1010 | none | none | admin routes + service |
| Read APIs | openapi 450–472, 957–1045 | none | none | `listJobAttempts`, admin workers/allocations/recovery events |
| terminal_at + retention | invariants; openapi Job `terminal_at` | column exists, never set | none | set on every terminal path, idempotency expiry extension, leader sweep |
| CLI control | docs/cli.md | no control commands | route contract test | `nexa job cancel|pause|resume|retry|attempts`, `nexa admin worker|allocations|recovery-events` |
| B14-R03/R05/R10/R11 | B14 evidence findings | see §Findings | — | fixed with regression tests |

### Design decisions (5.A–M)

- **5.A Common control pipeline.** `JobControlService` (new module `application/job_control.py`) with one
  `_control()` skeleton: validate (Pydantic `ControlRequest`/`RetryRequest`, 400) → principal + authz
  (`JobService._authorize` then job visibility: MEMBER only own `submitter_user_id`, TENANT_ADMIN
  tenant-wide, else 404; SA only via active membership, per concurrency-recovery.md:136) →
  `begin_idempotency` (context tenant, principal user, operation id, key; replay before If-Match) →
  `resolve_expected_version` (428) → global policy `FOR SHARE`/`FOR UPDATE` (WRITE_FROZEN → 409) →
  locks in contract order (policy → counters global→tenant→user → job → session → attempt → lease →
  allocation → reservation) → version (412) → state guard (409/422) → mutation + event + audit +
  counters + `complete_idempotency` in one `run_transaction` (deadlock retry ≤3, then 503 via the
  existing handler). Reason is stored verbatim only in `audit_records.reason` (widened to 256 by
  0019); `events.reason` uses stable codes (`USER_CANCEL`, `USER_PAUSE`, `USER_RESUME`,
  `MANUAL_RETRY`, `LEASE_EXPIRED`, `WORKER_DISABLED`, `PAUSE_ABORTED`). Reason text is never logged.
  Why: one audited path makes replay-before-If-Match and lock order identical for all four controls.
- **5.B Cancel.** Immediate branch closes RetrySchedule, drops the reservation row, clears intent,
  decrements outstanding, sets `terminal_at`. Active branch with live authority: lease revoked, grant
  ended, fence +1, attempt `STOPPING` with `failure_class=USER_CANCEL/USER_CANCEL`, allocation
  `QUARANTINED`, active checkpoint/result reservations `ABANDONED` (same as `fail_attempt`). From
  RECOVERING only state/desired change. Cleanup (existing branch) ends the attempt `CANCELLED`.
- **5.C Pause.** RUNNING ∧ desired RUNNING only; non-checkpointable template → 422. Lease is kept.
  Deterministic in-flight rule (ambiguity, safe option recorded once): an INTERVAL cycle that is
  already open when the pause is observed is **finished**, and because desired is PAUSED its publish
  moves the attempt to `STOPPING` (state-machines.md:71), so it is the pause checkpoint; otherwise the
  worker opens exactly one cycle with `REQUEST_CHECKPOINT reason=PAUSE`. Either way the pause makes
  at most one reservation. Then `REQUEST_STOP PAUSE` → `STOPPED` → cleanup with
  `ContainerStoppedProof`. Server: reserve/publish accept PAUSING ∧ desired PAUSED (intent RUN or
  CFP); `reserve_result`/`complete` with desired PAUSED → 409; a deterministic 422 publish in PAUSING
  (intent RUN) aborts the pause in the same transaction (reservation REJECTED, PAUSING→RUNNING,
  desired RUNNING, event `PAUSE_ABORTED`, state-machines.md:24). Pause deadline: 40 s from
  observation (lease 45 − margin 5); if not stopped the worker fails `INFRASTRUCTURE/RUNNER_UNAVAILABLE`.
- **5.D Resume.** PAUSED only (409). Needs a committed non-corrupt compatible checkpoint of the job,
  a MANUAL_RETRY reference, or `restart_safe` (422). New `ready_sequence` from the global counter
  version (bumped), eligible_since set by trigger 0012; retry_count/outstanding unchanged. Allowed in
  ADMISSION_OFF.
- **5.E Reaper (as built, corrected in round 2).** `CoordinatorService.reap_leases(epoch)` called
  from `tick` behind a 1 s probe (like `promote_retries`). Unlocked probe uses
  `ix_attempt_leases_active_expiry` with DB time, batch 16; per lease one transaction
  (`reaper.reap_lease_locked`): `_leader` → policy_versions `FOR UPDATE` (WRITE_FROZEN → skip) →
  job → logical_sessions → attempt → lease → allocation → open grants `FOR UPDATE`, then DB time is
  re-read and the **CAS** is `revoked_at IS NULL ∧ expires_at <= now` (`reaper.py:86-89`). No
  counters row is locked (the reaper changes no counter), and fence equality is **not** part of the
  CAS: it selects the branch. Active job (DISPATCHING/RUNNING/PAUSING; CANCELLING and RECOVERING leases
  are already revoked) whose attempt has the job's fence → `fence_attempt` (attempt `LOST` + `INFRASTRUCTURE/LEASE_EXPIRED`, revoke, end
  grant, QUARANTINED, reservations abandoned) and event `ATTEMPT_LOST` with state RECOVERING and
  fence +1, desired unchanged. Otherwise (terminal job, or an attempt already fenced) →
  `revoke_leftover_authority` (revoke, end grant, QUARANTINED, the leftover result/checkpoint
  reservation `ABANDONED`) and `LEASE_REVOKED` with `keep_version=True` (no state change, so the
  ETag is kept; L1/L2). Renew-first and cancel-first win because they hold the lease/job row, move
  `expires_at` or set `revoked_at`, and the CAS rereads the row after the lock (B15-R23 tests with
  both CAS conjuncts mutated). One failing lease is logged `coordinator_reap_lease_failed` and does
  not stop the batch or the tick (B15-R26).
- **5.F Cleanup branches.** `report_cleanup` gains: PAUSING (desired PAUSED + committed compatible
  checkpoint) → PAUSED; RECOVERING + desired PAUSED + committed checkpoint + retryable class →
  PAUSED; RECOVERING + desired PAUSED + no checkpoint + restart_safe + retry < max + retryable
  class → RETRY_WAIT with `recovery_intent=CHECKPOINT_FOR_PAUSE`; non-retryable class → FAILED
  even with desired PAUSED and a checkpoint (finding B15-R02). Promotion predicate becomes
  `(desired RUNNING ∧ intent NULL) ∨ (desired PAUSED ∧ intent CFP)`. **D1:** 0019 widens the B13
  queue predicate to `state='QUEUED'` and adds CHECK `ck_jobs_queued_dispatchable` so no other
  QUEUED combination can exist; the 4 partial indexes and 7 trigger/helper functions are recreated,
  downgrade restores them semantically identical (whitespace-normalized source match, tested). Python filters drop the conjuncts. B13 plan + microbench
  rerun (ACC-11). CFP: dispatch sets `execution_intent = recovery_intent or RUN`; claim/start accept
  desired PAUSED ∧ CFP (start → PAUSING); runner makes one PAUSE checkpoint at the first step
  boundary and never a result.
- **5.G Worker/runner (as built).** Renew ack `desired_state=PAUSED` drives the pause flow
  (`REQUEST_CHECKPOINT PAUSE` or the open interval cycle → publish → `REQUEST_STOP PAUSE` →
  `STOPPED{PAUSE}` → cleanup with proof). Cancel is **not** a runner control: cancel revokes the
  lease in its transaction, the next renew gets 409, reconciliation reports the allocation
  `REVOKED`, the worker stops the container (`docker stop`, exact identity) and sends cleanup with
  `ContainerStoppedProof`; this is the existing B10/B11 revoke path, so cancel works even when the
  runner is unresponsive. After a verified cleanup the worker discards pending renew operations
  of that attempt (B15-R09). Runner self-stops are labelled from `STOPPED.reason` (B15-R10).
  Journal records `control_desired`, the pause cycle and the stop sent. B14-R05 and B14-R10 fixed
  here.
- **5.H Manual retry.** FAILED only; source unchanged; If-Match = source ETag; idempotency
  `resource_id` = new job. Admission reuses the submit helper (mode, counters, rate, quota, template,
  artifacts, ready_sequence from global counter). New job/session/spec (same canonical spec, input
  and model references), `retry_of_job_id`, QUEUED/RUNNING, retry 0, fence 0. Optional
  `checkpoint_id`: same tenant (404), COMMITTED, not CORRUPT, exact compatibility with the new job's
  immutable template (422); blob size/checksum verified outside the transaction; inserts
  `checkpoint_references(reason=MANUAL_RETRY)`. Restore prefers the job's own checkpoints, then the
  referenced one.
- **5.I Admin workers.** SA only, `_ensure_mutable`, `AdminReasonRequest`, Idempotency-Key, If-Match
  on worker version, audit. drain ENABLED→DRAINING 202; disable any→DISABLED 202 and fences every
  live attempt of the worker (attempt STOPPING `INFRASTRUCTURE/WORKER_DISABLED`, job RECOVERING
  or CANCELLING kept, QUARANTINED), jobs locked in job_id order; enable → ENABLED 200 only when the
  current incarnation is READY and reconciled, else 409. Never releases.
- **5.J Read APIs.** `listJobAttempts` (jobs:read, newest first, keyset ≤100),
  `adminListWorkers`/`adminGetWorker` (ETag)/`adminListAllocations`/`adminListRecoveryEvents`
  (SA, admin:read, signed cursor, ≤100, read audit). Timestamps: 3 digit ms + `Z`.
- **5.K terminal_at/retention.** Every terminal path sets `terminal_at = clock_timestamp()` and in the
  same transaction `UPDATE idempotency_records SET expires_at = greatest(expires_at, terminal_at +
  retention) WHERE resource_id = job_id`. Leader sweep (1 s probe) deletes ≤100 COMPLETED records of
  submit/cancel/pause/resume/retry with `expires_at < now` whose job is terminal; skipped in
  WRITE_FROZEN. Index `ix_idempotency_records_resource` added in 0019.
- **5.L CLI.** New commands with required `--reason`, `--if-match`, stable Idempotency-Key, exit codes
  from `cli/errors.py`; docs/cli.md updated.
- **5.M Images/migration.** CPU image `nexa/cpu-iterative:b15`, worker `nexa/b15-worker:local`, built
  locally, not pushed. Template versions via test fixtures only. 0019 + `schema_v16` + `schema.py`;
  SCHEMA_GENERATION unchanged (additive/widening, an old app never writes a row violating the new
  CHECK).

### Transition table (test oracle)

`d` = desired_state, `i` = recovery_intent. Every row also: version +1, one event sequence,
same-transaction audit for user/admin actions.

| (state, d, event) | Result | Effects | Contract |
|---|---|---|---|
| (QUEUED/RETRY_WAIT/PAUSED, *, cancel) | CANCELLED, d CANCELLED | drop reservation, close schedule, i NULL, outstanding −1, terminal_at, retention | SM:37 |
| (DISPATCHING/RUNNING/PAUSING, *, cancel) | CANCELLING, d CANCELLED | revoke lease, end grant, fence +1, attempt STOPPING USER_CANCEL, alloc QUARANTINED, reservations ABANDONED | SM:38, CR:101 |
| (RECOVERING, *, cancel) | CANCELLING, d CANCELLED | none else | SM:38 |
| (CANCELLING/terminal, *, cancel) | 409 state_conflict | none | SM:42, 56 |
| (CANCELLING, CANCELLED, cleanup proof) | CANCELLED | attempt CANCELLED, release, active −1, outstanding −1, ledger close, terminal_at | SM:39 |
| (RUNNING, RUNNING, pause) checkpointable | PAUSING, d PAUSED | lease kept | SM:21 |
| (RUNNING, RUNNING, pause) not checkpointable | 422 infeasible_request | none | SM:53 |
| (other, *, pause) | 409 | none | SM:53 |
| (PAUSING, PAUSED, checkpoint publish) | PAUSING | checkpoint COMMITTED, attempt STOPPING | SM:22, 71 |
| (PAUSING, PAUSED, 422 publish, i NULL) | RUNNING, d RUNNING | reservation REJECTED, PAUSE_ABORTED | SM:24 |
| (PAUSING, PAUSED, cleanup + checkpoint) | PAUSED | attempt CANCELLED(PAUSE), release, active −1 only, ledger close, i NULL | SM:23, 78, 125 |
| (PAUSING, PAUSED, cleanup w/o checkpoint) | 409 | no release | SM:23 |
| (PAUSED, PAUSED, resume) usable | QUEUED, d RUNNING | new ready_sequence, eligible_since | SM:26 |
| (PAUSED, PAUSED, resume) unusable | 422 | none | SM:26 |
| (other, *, resume) | 409 | none | SM:54 |
| (DISPATCHING/RUNNING/PAUSING, *, lease expired) | RECOVERING, d unchanged | attempt LOST INFRASTRUCTURE/LEASE_EXPIRED, revoke, fence +1, QUARANTINED | SM:25, 27, 77 |
| (SUCCEEDED, *, lease expired unrevoked) | SUCCEEDED | revoke, QUARANTINED, LEASE_REVOKED event | SM:44, 88 |
| (RECOVERING, PAUSED, cleanup + checkpoint, retryable) | PAUSED | release, active −1, i NULL, no retry | SM:30, CR:111 |
| (RECOVERING, PAUSED, cleanup, no ckpt, restart_safe, retry<max, retryable) | RETRY_WAIT, d PAUSED, i CFP | release, active −1, retry +1, schedule | SM:31, CR:112 |
| (RECOVERING, RUNNING, cleanup, INFRA, retry<max, ckpt∨restart_safe) | RETRY_WAIT | as B11 | SM:29 |
| (RECOVERING, RUNNING/PAUSED, cleanup, otherwise) | FAILED | release, active −1, outstanding −1, terminal_at | SM:32–33, CR:113 |
| (RETRY_WAIT, RUNNING ∧ i NULL ∨ PAUSED ∧ i CFP, due) | QUEUED | schedule closed | SM:35 |
| (QUEUED, PAUSED, i CFP, dispatch) | DISPATCHING | attempt intent CFP | SM:15 |
| (DISPATCHING, PAUSED, i CFP, start) | PAUSING | attempt RUNNING | SM:18 |
| (PAUSING, PAUSED, reserve/complete result) | 409 stale_authority | none | SM:18, CR:112 |
| (RETRY_WAIT, *, resume) | 409 | none | SM:54 |
| (FAILED, *, retry) | source FAILED; new QUEUED | new job/session/spec, counters, optional reference | SM:41 |
| (not FAILED, *, retry) | 409 | none | SM:55 |
| (worker ENABLED, drain) | DRAINING | audit | SM:98 |
| (worker any, disable) | DISABLED | fence live attempts → RECOVERING/CANCELLING, QUARANTINED | SM:98, 104 |
| (worker DRAINING/DISABLED, enable) READY+reconciled | ENABLED | no release | SM:98 |

SM = docs/contracts/state-machines.md, CR = docs/contracts/concurrency-recovery.md.

### Ordered tasks

| # | Task | Files | Tests first | Verify |
|---|---|---|---|---|
| 1 | Migration 0019 + schema_v16 | migrations/versions/20260926_0019_b15_control_recovery.py, persistence/schema_v16.py, schema.py | tests/integration/test_migration_b15.py (upgrade/downgrade, CHECK, indexes, parity), offline SQL | ruff, `--run-postgres` migration tests |
| 2 | Queue filters + CFP dispatch/promotion | coordinator/{eligibility,snapshot,dispatch,retry}.py | test_control_b15 CFP promotion/dispatch | PG coordinator tests, B13 plan/microbench |
| 3 | Control pipeline + cancel | application/job_control.py, api/routes_jobs.py, api/schemas.py | test_control_b15 cancel matrix, authz, replay, 412/428, WRITE_FROZEN | PG tests |
| 4 | Reaper | coordinator/service.py, coordinator/reaper.py | lease expiry, duplicate reaper, renew race | PG tests |
| 5 | Cleanup branches + terminal_at | execution_cleanup.py, execution_service.py | pause-crash 3 outcomes, cancel-complete both orders | PG tests |
| 6 | Pause + CFP server | job_control.py, checkpoint_service.py, execution_service.py, worker_service.py | pause/abort/CFP start | PG tests |
| 7 | Resume, manual retry | job_control.py, job_service.py, checkpoint_restore.py | resume/retry matrix | PG tests |
| 8 | Admin worker + read APIs | admin_service.py or admin_workers.py, routes_admin.py, routes_jobs.py | admin worker matrix, pagination, 3-digit timestamps | PG + route contract |
| 9 | Retention sweep | coordinator/service.py | sweep bounds, WRITE_FROZEN skip | PG tests |
| 10 | Worker/runner (R05, R10) | worker/{agent,execution,checkpoint_flow,dispatch}.py, workloads/trusted_runner.py | unit tests without Docker | pytest -q |
| 11 | Images | scripts/b09_build_image.sh, deploy/b10/Dockerfile | — | docker inspect digests |
| 12 | CLI | cli/commands/{job,admin}.py, cli/client.py | tests/cli | pytest tests/cli |
| 13 | Docker C0–C10 | tests/docker/test_b15_control_recovery.py | the scenario test itself | NEXA_RUN_DOCKER=1 |
| 14 | Docs + R03/R11 | docs/*.md | test for 3-digit event timestamps | review |

## Revision and environment

- Baseline: `b73da3b` (B14) on `main`. B15 is the uncommitted diff on top.
  Tracked changes: `git diff --shortstat` = 55 files changed, 2640
  insertions(+), 675 deletions(-) (README, ROADMAP, 9 docs, 30 source modules,
  14 existing test files). There are 31 new untracked files, 41,812
  lines in total: this evidence file, `raw/B15-images.json`,
  `raw/B15-docker.json` (run 14), `raw/B15-docker-run16.json`,
  `raw/B15-docker-run17.json`, `raw/B15-docker-run18.json`,
  `raw/B15-docker-run19.json`, the B13 rerun reports and leader-probe plans
  under `benchmarks/results/b15/` (7 files), migration
  `20260926_0019_b15_control_recovery.py`, the source files
  `application/admin_workers.py`, `application/job_control.py`,
  `application/job_recovery.py`, `coordinator/reaper.py`,
  `coordinator/retention.py` and `persistence/schema_v16.py`, and 10 test
  files: `cli/test_control_commands_b15`, `docker/test_b15_control_recovery`,
  `integration/test_admin_workers_b15`, `integration/test_callback_lock_order_b15`,
  `integration/test_control_b15`, `integration/test_migration_b15`,
  `integration/test_retention_b15`, `integration/test_worker_pause_b15`,
  `worker/test_control_flow_b15` and `workloads/test_runner_checkpoint_b15`.
  Nothing is committed, pushed or branched.
- Host: macOS 27.0 (26A428), Apple silicon. Docker Desktop (context
  `desktop-linux`): engine 29.8.0, VM `linux/arm64`, kernel
  `7.0.12-linuxkit`, 8 CPU, 4,106,604,544 B RAM. This is not bare Linux.
- PostgreSQL 17.11 in the existing container `nexa_b13_pg` (loopback port
  15439). Guarded databases: `nexa_b05_test_b15` (suite),
  `nexa_b05_test_b15docker` (Docker scenarios; the not-restart-safe test
  recreates its schema) and `nexa_b05_test_b15b13_*` (B13 bench copies made
  with `CREATE DATABASE … TEMPLATE` from `…b13r13_base` and
  `…b13r13_cadence_long`, which are only read). No evidence DB of a previous
  task was modified. No `nexa_b10_*` container or volume was stopped or
  changed.
- Python 3.12.13 in `.venv`; `uv` 0.9.27 at `/tmp/nexa-b12-uv-bootstrap/bin/uv`.
- CPU image (round-2 build 2026-09-27 08:39:53Z, after every round-2 fix):
  `nexa/cpu-iterative:b15` =
  `nexa/cpu-iterative@sha256:c30fa52a0e0dd7ecdc25bf4ceae4ffe2ef711c1c4630029d60dd8b7535342024`
  (image id is the same digest; linux/arm64, user 1000:1000, entrypoint
  `python -m nexa.workloads.trusted_runner`, label
  `io.nexa.runner.checkpoint=cpu-state-v1`, base
  `python:3.12-slim@sha256:2f17fc04…06a9`, 207,595,475 B).
- Worker image (08:40:01Z): `nexa/b15-worker:local`, image id
  `sha256:bcefa221f16a392b97ec8defe2fa16c066a5e1ce37dd6c4f1a25fe05257aeae3`
  (command `nexa-worker`, 428,551,519 B). Local tag only; nothing was pushed.
- Both images carry the same 127 `.py` files under `nexa/` as `src/nexa`
  (same paths and SHA-256, listing hash `78579699…05a0`), checked at the build
  and again after run 19 on the final source. Runs 18 and 19 and the final
  Docker regression used these images. The round-1 images of runs 14–17
  (`823af64e…ec89`, worker `acf90130…c4bf`) and six earlier builds are
  superseded; [raw/B15-images.json](raw/B15-images.json) lists them with the
  finding that caused each rebuild and the round-1 rechecks after
  B15-R18/R19. Old B14 images: `nexa/cpu-iterative@sha256:e0e6222e…9b6f` and
  worker `sha256:c55c25da…8b42`.
- `nexa_b10_smoke3-caddy-1` (a B10 container) is `Up 21 hours (unhealthy)`
  and holds 14,590 PIDs (`docker stats`, 2026-09-27 09:15Z; 10,372 at 02:38Z,
  9,078 at 00:45Z). It was not stopped (prompt rule). No run in this session
  hit the PID limit, so it blocked nothing. It is recorded here and
  in the final report for the user.

## Reproduction commands

`<guarded URL>` stands for `postgresql+psycopg://postgres:<password>@127.0.0.1:15439`.
The password comes from `PGPW=$(docker exec nexa_b13_pg printenv POSTGRES_PASSWORD)`
and is never printed. `NEXA_DATABASE_URL` is never set.

```sh
U=/tmp/nexa-b12-uv-bootstrap/bin/uv
$U run --no-sync ruff check .
$U run --no-sync ruff format --check .
git diff --check
PYTHONPATH=src:. $U run --no-sync pytest -q
NEXA_TEST_DATABASE_URL=<guarded URL>/nexa_b05_test_b15 PYTHONPATH=src:. \
  $U run --no-sync pytest --run-postgres -q -p no:randomly

# Images (final build)
BASE_IMAGE_REF=python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 \
  IMAGE_REF=nexa/cpu-iterative:b15 IMAGE_ARCH=linux/arm64 scripts/b09_build_image.sh
docker build --pull=false -f deploy/b10/Dockerfile -t nexa/b15-worker:local .

# B15 Docker scenarios C0–C10 and C5b/C7b (run 19; runs 14, 16, 17 used 823af64e…ec89)
NEXA_TEST_DATABASE_URL=<guarded URL>/nexa_b05_test_b15docker \
NEXA_RUN_DOCKER=1 NEXA_B11_RUNTIME_EVIDENCE=1 \
NEXA_B09_IMAGE_REF=nexa/cpu-iterative@sha256:c30fa52a0e0dd7ecdc25bf4ceae4ffe2ef711c1c4630029d60dd8b7535342024 \
NEXA_B11_WORKER_IMAGE=nexa/b15-worker:local \
NEXA_B15_EVIDENCE_OUT=<run json> NEXA_B15_WORKER_LOG_OUT=<worker log> \
PYTHONPATH=src:. $U run --no-sync pytest --run-postgres -q -s -p no:randomly \
  tests/docker/test_b15_control_recovery.py

# B09/B10/B11/B14 Docker regression on the same images
NEXA_TEST_DATABASE_URL=<guarded URL>/nexa_b05_test_b15docker \
NEXA_RUN_DOCKER=1 NEXA_B11_RUNTIME_EVIDENCE=1 NEXA_B10_RUNTIME_EVIDENCE=1 \
NEXA_B09_IMAGE_REF=nexa/cpu-iterative@sha256:c30fa52a…2024 NEXA_B11_WORKER_IMAGE=nexa/b15-worker:local \
NEXA_B14_EVIDENCE_OUT=<b14 json> \
PYTHONPATH=src:. $U run --no-sync pytest --run-postgres -q -p no:randomly -rs tests/docker \
  --deselect tests/docker/test_b15_control_recovery.py

# B13 queue microbench rerun (each on its own TEMPLATE copy, 20 s apart; see B13 evidence)
NEXA_TEST_DATABASE_URL=<guarded URL>/<copy> PYTHONPATH=src:. \
  $U run --no-sync python -m benchmarks.b13.queue_microbench \
  --tenants 100 --jobs-per-tenant 1000 \
  --api-prefill-result benchmarks/results/b13/b13-api-prefill-100k-r13.json --output <report> \
  { --repetitions 3                               # steady    (copy of …b13r13_base)
  | --repetitions 3 --prepare-dispatch            # dispatch  (copy of …b13r13_base)
  | --repetitions 10 --prepare-default-quota      # default-quota (copy of …b13r13_base)
  | --repetitions 3                               # drained-steady (copy of …b13r13_cadence_long)
  | --repetitions 3 --prepare-dispatch }          # drained-dispatch (copy of …b13r13_cadence_long)
```

Each copy was migrated from 0018 to head (0019) through the Alembic Python
API with the explicit guarded URL before the run. The reaper and retention
probes were then run with `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` inside a
rolled-back transaction with `jit=off` on the drained-steady copy. The
statements are the ones `reaper.expired_leases` and
`retention.expired_records` build (compiled with literal binds).

## Verification results (final source)

The final source is the tree after every round-2 fix (Task Review round 1).
Every count below comes from a command run in this session. Rows marked
"round 1" or "earlier source" are kept for history and say what changed after
them. The targeted red → green checks and mutations of each round-2 fix are in
the Findings table (B15-R04, R07, R08, R20–R30, R35–R38).

| Check | Command | Result |
|---|---|---|
| Ruff lint | `ruff check .` | All checks passed |
| Ruff format | `ruff format --check .` | 395 files already formatted |
| Whitespace | `git diff --check` | no output, exit 0 (untracked files checked separately for trailing whitespace) |
| Default suite | `pytest -q` | **978 passed, 443 skipped** in 34.96 s |
| PostgreSQL suite | `pytest --run-postgres -q -p no:randomly` on `nexa_b05_test_b15` | **1403 passed, 18 skipped** in 445.39 s (the 18 skips are the Docker tests without `NEXA_RUN_DOCKER`; the 3 warnings are the two Starlette/anyio deprecations and a pydantic `UnsupportedFieldAttributeWarning` for the B08 `created_after` query parameter, also seen in B14 logs) |
| Docker regression | B09/B10/B11/B14 command above, round-2 images | **15 passed, 1 skipped, 2 deselected** in 709.37 s; no workload container left |
| B15 Docker (final source) | run 19, command above, round-2 images | **2 passed** in 946.73 s |
| B15 Docker (round 2, failed) | run 18, before the derived C8 limit | **1 failed, 1 passed** in 446.77 s (C7a, see run history) |
| B15 Docker (round 1) | run 17, after B15-R19, round-1 images | **2 passed** in 903.47 s |
| B15 Docker (earlier source) | run 16, before B15-R19 | **2 passed** in 891.35 s |
| B15 Docker (timeline run, earlier source) | run 14, before B15-R18, B15-R19 and the C6 oracle fix | **2 passed** in 912.28 s |
| Round-1 suites and regression | same commands on the round-1 source | default 968 passed, 415 skipped (32.72 s); PostgreSQL 1365 passed, 18 skipped (549.10 s); Docker regression 15 passed, 1 skipped, 2 deselected (726.20 s) |
| B15-R19 red → green (round 1) | `pytest --run-postgres -k adopt_transfers tests/integration/test_control_b15.py` | before the route fix **1 failed** (`transferred_checkpoint_reservation` ≠ reserve answer); after **1 passed** |
| Docker regression (earlier source) | same command after B15-R18, before B15-R19 | **1 failed, 14 passed, 1 skipped, 2 deselected** in 754.50 s: B14 S6 "worker READY with a new incarnation" timed out; root cause B15-R19 |
| B13 queue rerun | microbench, 5 modes (round 1, not rerun: round 2 left `eligibility.py`, `snapshot.py` and the queue indexes unchanged; its 0019 changes are the B15-R25 backfill of terminal jobs, the B15-R07 `ready_checked_at` column and the B15-R38 downgrade) | see [B13 queue plan and microbench](#b13-queue-plan-and-microbench-acc-11) |

The Docker regression per file: `test_real_runner.py` (B09) 9 passed,
`test_real_executor.py` 2 passed, `test_b11_vertical.py` 2 passed,
`test_b11_worker_ipc.py` 1 passed, `test_b14_checkpoint_restore.py` (S1–S6) 1
passed, and `test_b10_worker_restart.py` 1 skipped ("worker and runner must
share a Linux monotonic clock domain", the same skip as B14 on Docker
Desktop). The 2 deselected items are the B15 Docker tests, which ran on their
own.

### Docker run history (not final-source evidence)

Run 19 is the final-source run (round 2, round-2 images). Runs 14, 16 and 17
used the round-1 images: run 17 ran after B15-R19, run 16 before it, and run
14 before B15-R18 and the C6 oracle fix; run 14 gives the detailed timelines
below. Runs 1–13, 15 and 18 each stopped at the first failing scenario of
the main test, and each failure was traced to a root cause before the next
run. Product defects became findings with a red test first. Harness defects
were fixed in the test only, and the images were kept.

| Run | Failed at | Passed before the failure | Cause | Fix |
|---|---|---|---|---|
| 1, 2 | C5a | C0, C1, C2, C4 | The kill landed after the start ACK but before the runner took its deadline; the worker reported `TIMEOUT/STARTUP_TIMEOUT` (B15-R11) | Harness waits for the workload process before the kill; R11 recorded, not fixed |
| 3 | C5b | C0, C1, C2, C4, C5a | Harness: a non-restart-safe submit used another `template_id`, which the contract pins to `cpu-iterative` | C5b/C7b moved to a second test on a fresh DB with a non-restart-safe template version |
| 4 | C8 | C0, C1, C2, C4, C5a | `/fail` observation said `runtime_limit_reached=false` and was rejected (B15-R10 amendment) | Worker fix, images rebuilt |
| 5 | C5a | C0, C1, C2, C4 | Reaped unclaimed offer reported `CLAIMED` (B15-R12) | Server fix |
| 6 | C8 | C0, C1, C2, C4, C5a | Harness: the C8 runtime limit was long enough for the job to finish `SUCCEEDED` | Harness limit tightened; the run-6 worker log pointed at the C5a poll `409` of B15-R13 |
| 7 | C5a | C0, C1, C2, C4 | Worker `STARTING` while a `CHECKPOINT_FOR_PAUSE` offer was live (B15-R13) | Server fix, images rebuilt |
| 8 | C7a | C0, C1, C2, C4, C5a, C8 | Harness oracle: the retried C8 job carries the runtime-limit spec, so the result's `spec_checksum` differs from R0 at byte 170 | Oracle compares the computation with R0 under the job's own `spec_checksum` |
| 9 | C8 | C0, C1, C2, C4, C5a | `INTERNAL/CHECKPOINT_PROTOCOL_ERROR` instead of `TIMEOUT` (B15-R14) | Worker + runner fix, images rebuilt |
| 10, 11 | C3 | C0, C1, C2, C4, C5a, C7a, C8 | Runner stuck `STOPPING` after a `TERM` to a supervisor that had already exited (B15-R15) | Runner fix, images rebuilt |
| 12 | C8 | C0, C1, C2, C4, C5a | `STOPPED` sent only to a renewal connection; R14 linger broke a B09 bound (B15-R16) | Runner linger ≤3 s + worker keeps terminal frames, images rebuilt |
| 13 | C9 | C0, C1, C2, C3, C4, C5a, C7a, C8 | Stale journaled `/fail` blocked reconciliation after a verified cleanup (B15-R17) | Worker fix, images rebuilt |
| 14 | — | all | — | timeline evidence below |
| 15 | C6 | C0, C1, C2, C4, C5a, C8, C7a, C3, C9 (C5b/C7b test passed) | Harness oracle race: after drain, the running attempt was sampled during a periodic checkpoint cycle (`CHECKPOINTING`, a live state), and the oracle required `RUNNING`. First run with the B15-R18 sweep | Oracle accepts `RUNNING` or `CHECKPOINTING`; `settle()` now requires a terminal attempt state (`SUCCEEDED`/`FAILED`/`LOST`/`CANCELLED`) instead of excluding three live states |
| 16 | — | all | — | source before B15-R19 |
| 17 | — | all | — | last round-1 run; C4 took the "pause stop won" path |
| 18 | C7a | C0, C1, C2, C4 (barrier crash path), C5a, C8 (C5b/C7b test passed) | Harness: the C7a retry inherits the C8 spec, including its fixed 28 s runtime limit. The retry restored a C8 checkpoint, committed one more, then its worker sent `/fail` and the job ended `FAILED` (test log; run 19 recreated the database, so the attempt's failure class is not in the raw file). A fixed limit does not guarantee that the compute left after the restored checkpoint fits in the limit (2L ≥ T + G + 1, see Self-review); run 18's C0 took 55.2 s | C8 limit derived from C0 of the same run: `L = ceil((3T + G + 1) / 4)` (T = C0 compute time, G = longest C0 checkpoint gap), asserted `G + 1 < L < T` |
| 19 | — | all | — | **final source**, round-2 images; 2 passed in 946.73 s |

Scenario order in the main test is C0, C1, C2, C4, C5a, C8, C7a, C3, C9, C6,
C10. The not-restart-safe test (C5b, C7b) passed from run 4 on.

## Docker scenarios, timelines and checksums

Run 14 (round-1 source and images) gives the detailed timelines below;
the final-source run 19 is compared scenario by scenario in
[Round-1 runs (16, 17) and final-source run (19)](#round-1-runs-16-17-and-final-source-run-19),
and its full timelines are in
[raw/B15-docker-run19.json](raw/B15-docker-run19.json). Run 14: database
`nexa_b05_test_b15docker`, CPU image `823af64e…ec89`, worker image
`acf90130…c4bf`, `iterations` 250,000,000, checkpoint interval 5 s,
linux/arm64. The worker is the real `nexa-worker` container with the Docker
socket; the API and the coordinator run in the test process against
PostgreSQL. Lease 45 s, safety margin 5 s.

After each scenario, `settle()` asserted these checks:
- the accepted job ID has exactly one submit idempotency record;
- every allocation is `RELEASED` only after a verified cleanup
  (`ALLOCATION_RELEASED` count = allocations, every container stopped and
  verified, every lease revoked, every grant ended);
- no `RESERVED` reservation is left, and `checkpoint_sequence` equals the
  highest reservation;
- the event sequence is gapless from 1;
- one final result, from the last attempt, when `SUCCEEDED`, and none
  otherwise;
- the global/tenant/user `admission_counters` (all three equal, one tenant
  and one submitter) equal the recomputed (non-terminal jobs, unreleased
  allocations);
- no ledger segment is open, and the charge is summed in `Decimal`;
- no workload container is left for any attempt.

At the end, the test checked that every submitted ID has a job and a session,
every job is terminal, the counters are (0, 0), and no workload container is
left. The tenant fairness score was recorded after each scenario and is
non-decreasing across all 11 samples, including the C10 restart.

### Summary per scenario

| Scenario | What happened (run 14) | Key oracle values |
|---|---|---|
| C0 baseline R0 | Uninterrupted run, 4 checkpoints | R0 `sha256:8065fa80…61bf`, 52.5 s |
| C1 pause/resume | Pause while RUNNING → pause checkpoint seq 2 → cleanup → `PAUSED` (attempt 1 `CANCELLED`/`PAUSE`, allocation released, retry 0). Resume → same job/session, attempt 2 fence 2 restores seq 2 → `SUCCEEDED` | retry_count 0 after resume; result = R0 |
| C2 cancel RUNNING | Cancel → `CANCELLING`, lease revoked `USER_CANCEL`, fence 1→2, allocation `QUARANTINED` → worker stops the exact container → verified cleanup → `CANCELLED`. The same request replayed with the same key returned the identical body and ETag; a new cancel key then got `409` | `replayed` true; no result; counters (0, 0) |
| C3 worker SIGKILL past the lease | Worker killed; the runner's own deadline ended the container at 23:43:23.627, 1.877 s before DB lease expiry (23:43:25.504). The reaper fenced at 23:43:25.895 (`LOST`/`LEASE_EXPIRED`, fence 2, `QUARANTINED`). The quarantined allocation kept being charged (15.083 → 15.924 in 3 s). A restarted worker (new incarnation `…8189`) proved the stop; the release came only then (23:43:30.457); retry attempt 2, fence 3, restored seq 1 | worker dead 98.7 s until reaped; result = R0 |
| C4 pause-crash after checkpoint | Pause checkpoint seq 2 committed, then the workload was killed before the stop: attempt `FAILED` `INFRASTRUCTURE/RUNNER_UNAVAILABLE`, job `PAUSED` without a retry (retry_count 0). Resume → attempt 2 restores seq 2. **Round-1 source:** the `/fail` after the committed pause checkpoint is B15-R21; since round 2 that `/fail` is `stale_authority` and the attempt ends `CANCELLED/PAUSE` (runs 18 and 19) | result = R0 |
| C5a pause-crash before first checkpoint, restart-safe | Pause at 23:41:10.938, kill at 23:41:11.263, no checkpoint → one `CHECKPOINT_FOR_PAUSE` retry (retry_count 1), fallback to input, one pause checkpoint, `PAUSED`; then cancelled from `PAUSED` | exactly one CFP attempt; no result |
| C5b same, not restart-safe | Separate DB/template version: → `FAILED` with desired `PAUSED`, retry_count 0, no second attempt | no CFP attempt |
| C6 drain/disable/enable | Drain → `DRAINING`: the running attempt continued, and a job submitted afterwards stayed `QUEUED` with no attempt for 7 s. Disable → `DISABLED`/`STARTING`: the running attempt was fenced `STOPPING` `INFRASTRUCTURE/WORKER_DISABLED`, lease revoked `WORKER_DISABLED`, fence 1→2, allocation `QUARANTINED`. The disabled worker still reconciled: verified cleanup released the allocation (23:46:58.526) and nothing was dispatched. Enable succeeded on the first request (after reconciliation) → the queued job ran, then retry attempt 2 (fence 3) restored seq 1 | enable attempts 1; both results = R0 |
| C7a manual retry with checkpoint | Retry of the C8 `FAILED` job with its checkpoint `01a0e018…375a` seq 2 → new job/session, `retry_of_job_id` set, `JOB_ACCEPTED (MANUAL_RETRY)`, restore selected; source job unchanged | computation = R0 under the job's `spec_checksum` (`99197db5…5450`) |
| C7b manual retry without checkpoint | Retry of the C5b `FAILED` job → new job from input | = C5b R0 `e3bf14c5…9744` |
| C8 timeout | 28 s runtime-limit spec → `TIMEOUT/RUNTIME_LIMIT_REACHED`, `FAILED` without retry | no result; OOM and log-flood **not-run** (the fixture has no memory or log-volume mode) |
| C9 network loss | After the first checkpoint the worker container was disconnected from the Docker `bridge` network (its only route to the API) until the reaper fenced and the runner stopped. Old container finished 23:45:18.031, 4.859 s before DB expiry 23:45:22.890; reaper fence 2; same worker incarnation reconnects, proves the stop, cleanup; new runner started 23:45:38.263 | compute gap 20.232 s (no overlap); result = R0 |
| C10 full stack restart | Two jobs (RUNNING, QUEUED) at stop. API, coordinator and worker stopped together, PostgreSQL and storage kept. Coordinator epoch 1 → 2. The new worker incarnation `…b504` adopted the running attempt: grants under both incarnations, fence stays 1, no retry. The queued job then ran | retry_counts [0, 0]; both = R0 |

### Result checksums

| Result | Job | Checksum | Equal to |
|---|---|---|---|
| R0 (C0) | `01a0e014…8122` | `sha256:8065fa806cd202777ce6eba406948602e53fe45e10c6bde40947ea3929a461bf` | — |
| R1 (C1 after resume) | `01a0e015…4d01` | same | R0 |
| R3 (C3 after reap + retry) | `01a0e019…bf9e` | same | R0 |
| R4 (C4 after pause-crash + resume) | `01a0e017…113d` | same | R0 |
| R6a, R6b (C6 fenced + queued) | `01a0e01c…4c12`, `01a0e01d…2dcd` | same | R0 |
| R7a (C7a manual retry) | `01a0e018…4567` | `sha256:99197db5048013710f2e78d94be0f88740922d7394ac63a20e15d66dd2e85450` | R0 bytes with the C8 spec's `spec_checksum` |
| R9 (C9 after network loss) | `01a0e01b…fa01` | same as R0 | R0 |
| R10a, R10b (C10) | `01a0e01f…765c`, `01a0e01f…aecb` | same as R0 | R0 |
| R0' (C5b baseline, not-restart-safe DB) | `01a0e021…0aba` | `sha256:e3bf14c5896a9f217ba79dcd949a8d5ea3ff8d32a981f08554e1401342799744` | — |
| R7b (C7b) | `01a0e021…cbe9` | same as R0' | R0' |

### Counters and ledger

| Point | (outstanding, active) | Ledger segments (count, charged, Decimal) |
|---|---|---|
| C3 reaped, allocation `QUARANTINED` | — | charge of the job 15.08325082322637836843326692 → 15.92399524149019132303716697 3 s later (quarantine is charged) |
| After every `settle()` | (0, 0), except C10 job 1: (1, 1) while job 2 still ran | all segments closed; per job below |
| End of main test | (0, 0) | 13 accepted jobs: 2 `CANCELLED`, 1 `FAILED`, 10 `SUCCEEDED`; coordinator epochs [1, 2] |
| End of not-restart-safe test | (0, 0) | 3 jobs; epoch [1] |

Per-job ledger totals: C0 14.14303110384465012617577634 (11 segments), C1
16.25804590832969197220996819 (14), C2 5.587333097777341046746713888 (5), C3
27.52232208588315725871365521 (13), C4 14.63109036359949069728058274 (12),
C5a 5.285618754717217818904831894 (7), C6 fenced job
23.92833152345489555740004725 (19), C6 queued job
14.90649718900859500607358128 (11), C7a 7.770308989527515100323854643 (6),
C8 8.397304826876799337655576718 (7), C9 29.65925982174404820395524993
(15), C10 14.80345941154210423979878779 (11) and
17.81402596273058178845538688 (14), C5b baseline
13.26255137246211067925845859 (10), C5b 2.313691370078674307601280365 (3),
C7b 13.67881303196104545045902058 (11). Fairness score samples (main test):
14.14 → 30.40 → 35.99 → 50.62 → 55.91 → 64.30 → 72.07 → 99.60 → 129.25 →
168.09 → 200.71 (full precision in `raw/B15-docker.json`).

### Timelines

Columns are kept apart as the recovery-fencing oracle requires. The
coordinator epoch is in each heading. `incarnation` is the attempt's worker
incarnation (C10 also shows the lease holder and every grant). `fence` is the
attempt's job fence and `intent` its execution intent. The lease column gives
DB issue → expiry / revocation times (UTC) and the revoke reason. The
allocation column gives its state, `Q` if it was quarantined, and the release
reason. `retry_count`, the event sequence and the checkpoint sequence are on
the job line. Event reasons are stable codes only; no free-text user reason
is stored in events or in raw evidence.


#### C0_baseline / timeline — job `01a0e014…8122`, epoch 1

Job: `SUCCEEDED`/desired `RUNNING`, intent `None`, retry_count 0, fence 1, event_sequence 9, checkpoint_sequence 4; counters (outstanding, active) [0, 0]; ledger {'charged': '14.14303110384465012617577634', 'count': 11}; results 1.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e014…b3e9` | `01a0e014…a855` | 1 | RUN | SUCCEEDED | —/— | 23:37:47.379 → 23:39:13.488 / 23:38:38.989 (—) | RELEASED VERIFIED_CLEANUP | `01a0e015…6ab9` 1, `01a0e015…1ea4` 2, `01a0e015…a2f7` 3, `01a0e015…d090` 4 |

Events: 1 23:37:47.055 JOB_ACCEPTED (Job accepted); 2 23:37:47.379 JOB_DISPATCHING; 3 23:37:49.172 ATTEMPT_STARTED (attempt_started); 4 23:38:00.525 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:38:10.204 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 6 23:38:18.083 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 7 23:38:27.933 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 8 23:38:33.182 RESULT_RECOGNIZED (result_recognized); 9 23:38:38.989 ALLOCATION_RELEASED (VERIFIED_CLEANUP).

#### C1_pause_resume / paused — job `01a0e015…4d01`, epoch 1

Job: `PAUSED`/desired `PAUSED`, intent `None`, retry_count 0, fence 1, event_sequence 7, checkpoint_sequence 2; counters (outstanding, active) None; ledger None; results 0.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e015…cfc6` | `01a0e014…a855` | 1 | RUN | CANCELLED | —/PAUSE | 23:38:39.598 → 23:39:51.554 / 23:39:06.967 (—) | RELEASED VERIFIED_CLEANUP | `01a0e016…fc08` 1, `01a0e016…958f` 2 |

Events: 1 23:38:39.394 JOB_ACCEPTED (Job accepted); 2 23:38:39.598 JOB_DISPATCHING; 3 23:38:48.553 ATTEMPT_STARTED (attempt_started); 4 23:38:58.496 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:38:58.546 PAUSE_REQUESTED (USER_PAUSE); 6 23:39:05.897 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 7 23:39:06.967 ALLOCATION_RELEASED (VERIFIED_CLEANUP).

#### C1_pause_resume / timeline — job `01a0e015…4d01`, epoch 1

Job: `SUCCEEDED`/desired `RUNNING`, intent `None`, retry_count 0, fence 2, event_sequence 15, checkpoint_sequence 4; counters (outstanding, active) [0, 0]; ledger {'charged': '16.25804590832969197220996819', 'count': 14}; results 1.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e015…cfc6` | `01a0e014…a855` | 1 | RUN | CANCELLED | —/PAUSE | 23:38:39.598 → 23:39:51.554 / 23:39:06.967 (—) | RELEASED VERIFIED_CLEANUP | `01a0e016…fc08` 1, `01a0e016…958f` 2 |
| 2 | `01a0e016…9d30` | `01a0e014…a855` | 2 | RUN | SUCCEEDED | —/— | 23:39:07.371 → 23:40:16.515 / 23:39:39.330 (—) | RELEASED VERIFIED_CLEANUP | `01a0e016…97a9` 3, `01a0e016…e073` 4 |

Events: 1 23:38:39.394 JOB_ACCEPTED (Job accepted); 2 23:38:39.598 JOB_DISPATCHING; 3 23:38:48.553 ATTEMPT_STARTED (attempt_started); 4 23:38:58.496 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:38:58.546 PAUSE_REQUESTED (USER_PAUSE); 6 23:39:05.897 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 7 23:39:06.967 ALLOCATION_RELEASED (VERIFIED_CLEANUP); 8 23:39:07.249 JOB_RESUMED (USER_RESUME); 9 23:39:07.371 JOB_DISPATCHING; 10 23:39:07.478 CHECKPOINT_RESTORE_SELECTED (CHECKPOINT_RESTORED); 11 23:39:07.944 ATTEMPT_STARTED (attempt_started); 12 23:39:17.920 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 13 23:39:27.720 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 14 23:39:33.488 RESULT_RECOGNIZED (result_recognized); 15 23:39:39.330 ALLOCATION_RELEASED (VERIFIED_CLEANUP).

#### C2_cancel_running / timeline — job `01a0e016…08ff`, epoch 1

Job: `CANCELLED`/desired `CANCELLED`, intent `None`, retry_count 0, fence 2, event_sequence 5, checkpoint_sequence 0; counters (outstanding, active) [0, 0]; ledger {'charged': '5.587333097777341046746713888', 'count': 5}; results 0.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e016…2f3b` | `01a0e014…a855` | 1 | RUN | CANCELLED | USER_CANCEL/USER_CANCEL | 23:39:40.182 → 23:40:34.809 / 23:39:50.069 (USER_CANCEL) | RELEASED Q VERIFIED_CLEANUP | — |

Events: 1 23:39:39.800 JOB_ACCEPTED (Job accepted); 2 23:39:40.182 JOB_DISPATCHING; 3 23:39:49.778 ATTEMPT_STARTED (attempt_started); 4 23:39:50.069 CANCEL_REQUESTED (USER_CANCEL); 5 23:40:00.571 ALLOCATION_RELEASED (VERIFIED_CLEANUP).

#### C3_worker_dead_past_lease / reaped — job `01a0e019…bf9e`, epoch 1

Job: `RECOVERING`/desired `RUNNING`, intent `None`, retry_count 0, fence 2, event_sequence 5, checkpoint_sequence 1; counters (outstanding, active) None; ledger None; results 0.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e019…ed03` | `01a0e014…a855` | 1 | RUN | LOST | INFRASTRUCTURE/LEASE_EXPIRED | 23:42:30.834 → 23:43:25.504 / 23:43:25.895 (LEASE_EXPIRED) | QUARANTINED Q  | `01a0e019…fe87` 1 |

Events: 1 23:42:25.159 JOB_ACCEPTED (Job accepted); 2 23:42:30.834 JOB_DISPATCHING; 3 23:42:32.074 ATTEMPT_STARTED (attempt_started); 4 23:42:42.410 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:43:25.895 ATTEMPT_LOST (LEASE_EXPIRED).

#### C3_worker_dead_past_lease / timeline — job `01a0e019…bf9e`, epoch 1

Job: `SUCCEEDED`/desired `RUNNING`, intent `None`, retry_count 1, fence 3, event_sequence 15, checkpoint_sequence 4; counters (outstanding, active) [0, 0]; ledger {'charged': '27.52232208588315725871365521', 'count': 13}; results 1.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e019…ed03` | `01a0e014…a855` | 1 | RUN | LOST | INFRASTRUCTURE/LEASE_EXPIRED | 23:42:30.834 → 23:43:25.504 / 23:43:25.895 (LEASE_EXPIRED) | RELEASED Q VERIFIED_CLEANUP | `01a0e019…fe87` 1 |
| 2 | `01a0e01a…199e` | `01a0e01a…8189` | 3 | RUN | SUCCEEDED | —/— | 23:43:40.528 → 23:44:57.257 / 23:44:21.338 (—) | RELEASED VERIFIED_CLEANUP | `01a0e01a…8946` 2, `01a0e01a…8cf8` 3, `01a0e01a…270b` 4 |

Events: 1 23:42:25.159 JOB_ACCEPTED (Job accepted); 2 23:42:30.834 JOB_DISPATCHING; 3 23:42:32.074 ATTEMPT_STARTED (attempt_started); 4 23:42:42.410 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:43:25.895 ATTEMPT_LOST (LEASE_EXPIRED); 6 23:43:30.457 ALLOCATION_RELEASED (VERIFIED_CLEANUP); 7 23:43:33.041 RETRY_READY (BACKOFF_ELAPSED); 8 23:43:40.528 JOB_DISPATCHING; 9 23:43:41.439 CHECKPOINT_RESTORE_SELECTED (CHECKPOINT_RESTORED); 10 23:43:42.060 ATTEMPT_STARTED (attempt_started); 11 23:43:52.780 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 12 23:44:02.375 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 13 23:44:09.685 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 14 23:44:15.615 RESULT_RECOGNIZED (result_recognized); 15 23:44:21.338 ALLOCATION_RELEASED (VERIFIED_CLEANUP).

#### C4_pause_crash_after_checkpoint / paused — job `01a0e017…113d`, epoch 1

Job: `PAUSED`/desired `PAUSED`, intent `None`, retry_count 0, fence 2, event_sequence 8, checkpoint_sequence 2; counters (outstanding, active) None; ledger None; results 0.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e017…8248` | `01a0e014…a855` | 1 | RUN | FAILED | INFRASTRUCTURE/RUNNER_UNAVAILABLE | 23:40:08.778 → 23:41:05.975 / 23:40:24.243 (—) | RELEASED Q VERIFIED_CLEANUP | `01a0e017…a8f7` 1, `01a0e017…4502` 2 |

Events: 1 23:40:00.881 JOB_ACCEPTED (Job accepted); 2 23:40:08.778 JOB_DISPATCHING; 3 23:40:10.105 ATTEMPT_STARTED (attempt_started); 4 23:40:20.754 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:40:20.830 PAUSE_REQUESTED (USER_PAUSE); 6 23:40:23.952 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 7 23:40:24.243 ATTEMPT_FAILED (RUNNER_UNAVAILABLE); 8 23:40:24.407 ALLOCATION_RELEASED (VERIFIED_CLEANUP).

#### C4_pause_crash_after_checkpoint / timeline — job `01a0e017…113d`, epoch 1

Job: `SUCCEEDED`/desired `RUNNING`, intent `None`, retry_count 0, fence 3, event_sequence 17, checkpoint_sequence 5; counters (outstanding, active) [0, 0]; ledger {'charged': '14.63109036359949069728058274', 'count': 12}; results 1.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e017…8248` | `01a0e014…a855` | 1 | RUN | FAILED | INFRASTRUCTURE/RUNNER_UNAVAILABLE | 23:40:08.778 → 23:41:05.975 / 23:40:24.243 (—) | RELEASED Q VERIFIED_CLEANUP | `01a0e017…a8f7` 1, `01a0e017…4502` 2 |
| 2 | `01a0e017…60fa` | `01a0e014…a855` | 3 | RUN | SUCCEEDED | —/— | 23:40:24.752 → 23:41:41.174 / 23:41:02.514 (—) | RELEASED VERIFIED_CLEANUP | `01a0e017…28db` 3, `01a0e017…026a` 4, `01a0e017…7612` 5 |

Events: 1 23:40:00.881 JOB_ACCEPTED (Job accepted); 2 23:40:08.778 JOB_DISPATCHING; 3 23:40:10.105 ATTEMPT_STARTED (attempt_started); 4 23:40:20.754 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:40:20.830 PAUSE_REQUESTED (USER_PAUSE); 6 23:40:23.952 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 7 23:40:24.243 ATTEMPT_FAILED (RUNNER_UNAVAILABLE); 8 23:40:24.407 ALLOCATION_RELEASED (VERIFIED_CLEANUP); 9 23:40:24.552 JOB_RESUMED (USER_RESUME); 10 23:40:24.752 JOB_DISPATCHING; 11 23:40:24.949 CHECKPOINT_RESTORE_SELECTED (CHECKPOINT_RESTORED); 12 23:40:25.494 ATTEMPT_STARTED (attempt_started); 13 23:40:37.988 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 14 23:40:45.535 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 15 23:40:54.995 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 16 23:40:56.751 RESULT_RECOGNIZED (result_recognized); 17 23:41:02.514 ALLOCATION_RELEASED (VERIFIED_CLEANUP).

#### C5a_pause_crash_before_checkpoint_restart_safe / paused — job `01a0e017…d805`, epoch 1

Job: `PAUSED`/desired `PAUSED`, intent `None`, retry_count 1, fence 3, event_sequence 12, checkpoint_sequence 1; counters (outstanding, active) None; ledger None; results 0.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e017…4ee7` | `01a0e014…a855` | 1 | RUN | FAILED | INFRASTRUCTURE/RUNNER_UNAVAILABLE | 23:41:02.992 → 23:41:55.302 / 23:41:11.263 (—) | RELEASED Q VERIFIED_CLEANUP | — |
| 2 | `01a0e018…7c62` | `01a0e014…a855` | 3 | CHECKPOINT_FOR_PAUSE | CANCELLED | —/PAUSE | 23:41:14.069 → 23:42:07.620 / 23:41:24.890 (—) | RELEASED VERIFIED_CLEANUP | `01a0e018…060a` 1 |

Events: 1 23:41:02.883 JOB_ACCEPTED (Job accepted); 2 23:41:02.992 JOB_DISPATCHING; 3 23:41:10.291 ATTEMPT_STARTED (attempt_started); 4 23:41:10.938 PAUSE_REQUESTED (USER_PAUSE); 5 23:41:11.263 ATTEMPT_FAILED (RUNNER_UNAVAILABLE); 6 23:41:11.459 ALLOCATION_RELEASED (VERIFIED_CLEANUP); 7 23:41:13.936 RETRY_READY (BACKOFF_ELAPSED); 8 23:41:14.069 JOB_DISPATCHING; 9 23:41:20.043 CHECKPOINT_FALLBACK_TO_INPUT (CHECKPOINT_FALLBACK_TO_INPUT); 10 23:41:20.628 ATTEMPT_STARTED (attempt_started); 11 23:41:23.874 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 12 23:41:24.890 ALLOCATION_RELEASED (VERIFIED_CLEANUP).

#### C5a_pause_crash_before_checkpoint_restart_safe / timeline — job `01a0e017…d805`, epoch 1

Job: `CANCELLED`/desired `CANCELLED`, intent `None`, retry_count 1, fence 3, event_sequence 13, checkpoint_sequence 1; counters (outstanding, active) [0, 0]; ledger {'charged': '5.285618754717217818904831894', 'count': 7}; results 0.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e017…4ee7` | `01a0e014…a855` | 1 | RUN | FAILED | INFRASTRUCTURE/RUNNER_UNAVAILABLE | 23:41:02.992 → 23:41:55.302 / 23:41:11.263 (—) | RELEASED Q VERIFIED_CLEANUP | — |
| 2 | `01a0e018…7c62` | `01a0e014…a855` | 3 | CHECKPOINT_FOR_PAUSE | CANCELLED | —/PAUSE | 23:41:14.069 → 23:42:07.620 / 23:41:24.890 (—) | RELEASED VERIFIED_CLEANUP | `01a0e018…060a` 1 |

Events: 1 23:41:02.883 JOB_ACCEPTED (Job accepted); 2 23:41:02.992 JOB_DISPATCHING; 3 23:41:10.291 ATTEMPT_STARTED (attempt_started); 4 23:41:10.938 PAUSE_REQUESTED (USER_PAUSE); 5 23:41:11.263 ATTEMPT_FAILED (RUNNER_UNAVAILABLE); 6 23:41:11.459 ALLOCATION_RELEASED (VERIFIED_CLEANUP); 7 23:41:13.936 RETRY_READY (BACKOFF_ELAPSED); 8 23:41:14.069 JOB_DISPATCHING; 9 23:41:20.043 CHECKPOINT_FALLBACK_TO_INPUT (CHECKPOINT_FALLBACK_TO_INPUT); 10 23:41:20.628 ATTEMPT_STARTED (attempt_started); 11 23:41:23.874 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 12 23:41:24.890 ALLOCATION_RELEASED (VERIFIED_CLEANUP); 13 23:41:25.007 JOB_CANCELLED (USER_CANCEL).

#### C6_drain_disable_enable / fenced — job `01a0e01c…4c12`, epoch 1

Job: `RECOVERING`/desired `RUNNING`, intent `None`, retry_count 0, fence 2, event_sequence 5, checkpoint_sequence 2; counters (outstanding, active) None; ledger None; results 0.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e01c…7fc3` | `01a0e01a…8189` | 1 | RUN | STOPPING | INFRASTRUCTURE/WORKER_DISABLED | 23:46:20.807 → 23:47:32.448 / 23:46:47.458 (WORKER_DISABLED) | QUARANTINED Q  | `01a0e01d…0ae3` 1 |

Events: 1 23:46:20.512 JOB_ACCEPTED (Job accepted); 2 23:46:20.807 JOB_DISPATCHING; 3 23:46:29.831 ATTEMPT_STARTED (attempt_started); 4 23:46:40.272 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:46:47.458 ATTEMPT_FENCED (WORKER_DISABLED).

#### C6_drain_disable_enable / queued_timeline — job `01a0e01d…2dcd`, epoch 1

Job: `SUCCEEDED`/desired `RUNNING`, intent `None`, retry_count 0, fence 1, event_sequence 9, checkpoint_sequence 4; counters (outstanding, active) [0, 0]; ledger {'charged': '14.90649718900859500607358128', 'count': 11}; results 1.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e01d…a38d` | `01a0e01a…8189` | 1 | RUN | SUCCEEDED | —/— | 23:47:04.645 → 23:48:35.871 / 23:47:59.041 (—) | RELEASED VERIFIED_CLEANUP | `01a0e01d…26f8` 1, `01a0e01d…8ea6` 2, `01a0e01d…e613` 3, `01a0e01e…1886` 4 |

Events: 1 23:46:40.340 JOB_ACCEPTED (Job accepted); 2 23:47:04.645 JOB_DISPATCHING; 3 23:47:05.263 ATTEMPT_STARTED (attempt_started); 4 23:47:15.852 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:47:27.924 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 6 23:47:38.081 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 7 23:47:48.166 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 8 23:47:52.968 RESULT_RECOGNIZED (result_recognized); 9 23:47:59.041 ALLOCATION_RELEASED (VERIFIED_CLEANUP).

#### C6_drain_disable_enable / timeline — job `01a0e01c…4c12`, epoch 1

Job: `SUCCEEDED`/desired `RUNNING`, intent `None`, retry_count 1, fence 3, event_sequence 16, checkpoint_sequence 6; counters (outstanding, active) [0, 0]; ledger {'charged': '23.92833152345489555740004725', 'count': 19}; results 1.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e01c…7fc3` | `01a0e01a…8189` | 1 | RUN | FAILED | INFRASTRUCTURE/WORKER_DISABLED | 23:46:20.807 → 23:47:32.448 / 23:46:47.458 (WORKER_DISABLED) | RELEASED Q VERIFIED_CLEANUP | `01a0e01d…0ae3` 1 |
| 2 | `01a0e01e…ea0a` | `01a0e01a…8189` | 3 | RUN | SUCCEEDED | —/— | 23:47:59.194 → 23:49:23.682 / 23:48:48.793 (—) | RELEASED VERIFIED_CLEANUP | `01a0e01e…516b` 3, `01a0e01e…0b53` 4, `01a0e01e…2976` 5, `01a0e01e…9e41` 6 |

Events: 1 23:46:20.512 JOB_ACCEPTED (Job accepted); 2 23:46:20.807 JOB_DISPATCHING; 3 23:46:29.831 ATTEMPT_STARTED (attempt_started); 4 23:46:40.272 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:46:47.458 ATTEMPT_FENCED (WORKER_DISABLED); 6 23:46:58.526 ALLOCATION_RELEASED (VERIFIED_CLEANUP); 7 23:47:00.477 RETRY_READY (BACKOFF_ELAPSED); 8 23:47:59.194 JOB_DISPATCHING; 9 23:48:06.355 CHECKPOINT_RESTORE_SELECTED (CHECKPOINT_RESTORED); 10 23:48:07.056 ATTEMPT_STARTED (attempt_started); 11 23:48:17.870 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 12 23:48:26.076 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 13 23:48:33.786 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 14 23:48:41.016 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 15 23:48:42.929 RESULT_RECOGNIZED (result_recognized); 16 23:48:48.793 ALLOCATION_RELEASED (VERIFIED_CLEANUP).

#### C7a_manual_retry_with_checkpoint / timeline — job `01a0e018…4567`, epoch 1

Job: `SUCCEEDED`/desired `RUNNING`, intent `None`, retry_count 0, fence 1, event_sequence 8, checkpoint_sequence 2, retry_of `01a0e018…1848`; counters (outstanding, active) [0, 0]; ledger {'charged': '7.770308989527515100323854643', 'count': 6}; results 1.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e018…4b76` | `01a0e014…a855` | 1 | RUN | SUCCEEDED | —/— | 23:41:56.400 → 23:43:02.811 / 23:42:24.755 (—) | RELEASED VERIFIED_CLEANUP | `01a0e018…d68e` 1, `01a0e019…d41c` 2 |

Events: 1 23:41:56.248 JOB_ACCEPTED (MANUAL_RETRY); 2 23:41:56.400 JOB_DISPATCHING; 3 23:41:56.881 CHECKPOINT_RESTORE_SELECTED (CHECKPOINT_RESTORED); 4 23:41:57.400 ATTEMPT_STARTED (attempt_started); 5 23:42:07.613 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 6 23:42:16.930 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 7 23:42:18.935 RESULT_RECOGNIZED (result_recognized); 8 23:42:24.755 ALLOCATION_RELEASED (VERIFIED_CLEANUP).

#### C8_timeout / timeline — job `01a0e018…1848`, epoch 1

Job: `FAILED`/desired `RUNNING`, intent `None`, retry_count 0, fence 2, event_sequence 7, checkpoint_sequence 3; counters (outstanding, active) [0, 0]; ledger {'charged': '8.397304826876799337655576718', 'count': 7}; results 0.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e018…31e7` | `01a0e014…a855` | 1 | RUN | FAILED | TIMEOUT/RUNTIME_LIMIT_REACHED | 23:41:25.292 → 23:42:38.805 / 23:41:55.735 (—) | RELEASED Q VERIFIED_CLEANUP | `01a0e018…5468` 1, `01a0e018…375a` 2 |

Events: 1 23:41:25.206 JOB_ACCEPTED (Job accepted); 2 23:41:25.292 JOB_DISPATCHING; 3 23:41:26.756 ATTEMPT_STARTED (attempt_started); 4 23:41:39.603 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:41:48.978 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 6 23:41:55.735 ATTEMPT_FAILED (RUNTIME_LIMIT_REACHED); 7 23:41:55.935 ALLOCATION_RELEASED (VERIFIED_CLEANUP).

#### C9_network_loss / timeline — job `01a0e01b…fa01`, epoch 1

Job: `SUCCEEDED`/desired `RUNNING`, intent `None`, retry_count 1, fence 3, event_sequence 15, checkpoint_sequence 4; counters (outstanding, active) [0, 0]; ledger {'charged': '29.65925982174404820395524993', 'count': 15}; results 1.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e01b…5096` | `01a0e01a…8189` | 1 | RUN | LOST | INFRASTRUCTURE/LEASE_EXPIRED | 23:44:21.743 → 23:45:22.890 / 23:45:23.423 (LEASE_EXPIRED) | RELEASED Q VERIFIED_CLEANUP | `01a0e01b…3afc` 1 |
| 2 | `01a0e01c…e263` | `01a0e01a…8189` | 3 | RUN | SUCCEEDED | —/— | 23:45:36.916 → 23:46:56.614 / 23:46:20.013 (—) | RELEASED VERIFIED_CLEANUP | `01a0e01c…eb81` 2, `01a0e01c…b184` 3, `01a0e01c…ed03` 4 |

Events: 1 23:44:21.546 JOB_ACCEPTED (Job accepted); 2 23:44:21.743 JOB_DISPATCHING; 3 23:44:32.458 ATTEMPT_STARTED (attempt_started); 4 23:44:42.552 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:45:23.423 ATTEMPT_LOST (LEASE_EXPIRED); 6 23:45:26.877 ALLOCATION_RELEASED (VERIFIED_CLEANUP); 7 23:45:29.469 RETRY_READY (BACKOFF_ELAPSED); 8 23:45:36.916 JOB_DISPATCHING; 9 23:45:37.579 CHECKPOINT_RESTORE_SELECTED (CHECKPOINT_RESTORED); 10 23:45:38.826 ATTEMPT_STARTED (attempt_started); 11 23:45:53.476 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 12 23:46:01.419 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 13 23:46:10.968 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 14 23:46:14.130 RESULT_RECOGNIZED (result_recognized); 15 23:46:20.013 ALLOCATION_RELEASED (VERIFIED_CLEANUP).


#### C10_full_restart / timeline 1 — job `01a0e01f…765c`, epoch 2

Job: `SUCCEEDED`/desired `RUNNING`, intent `None`, retry_count 0, fence 1, event_sequence 8, checkpoint_sequence 3; counters (outstanding, active) [1, 1]; ledger {'charged': '14.80345941154210423979878779', 'count': 11}; results 1.

| # | attempt | incarnation (attempt → lease holder; grants) | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e01f…f162` | `01a0e01f…b504` → `01a0e01f…b504`; `01a0e01a…8189`, `01a0e01f…b504` | 1 | RUN | SUCCEEDED | —/— | 23:48:49.470 → 23:50:21.588 / 23:49:43.490 (—) | RELEASED VERIFIED_CLEANUP | `01a0e01f…c618` 1, `01a0e01f…9227` 2, `01a0e01f…1099` 3 |

Events: 1 23:48:49.317 JOB_ACCEPTED (Job accepted); 2 23:48:49.470 JOB_DISPATCHING; 3 23:48:57.249 ATTEMPT_STARTED (attempt_started); 4 23:49:07.702 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:49:25.622 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 6 23:49:33.720 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 7 23:49:37.550 RESULT_RECOGNIZED (result_recognized); 8 23:49:43.490 ALLOCATION_RELEASED (VERIFIED_CLEANUP).

#### C10_full_restart / timeline 2 — job `01a0e01f…aecb`, epoch 2

Job: `SUCCEEDED`/desired `RUNNING`, intent `None`, retry_count 0, fence 1, event_sequence 10, checkpoint_sequence 5; counters (outstanding, active) [0, 0]; ledger {'charged': '17.81402596273058178845538688', 'count': 14}; results 1.

| # | attempt | incarnation (attempt → lease holder; grants) | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e01f…89e8` | `01a0e01f…b504` → `01a0e01f…b504`; `01a0e01f…b504` | 1 | RUN | SUCCEEDED | —/— | 23:49:43.596 → 23:51:22.531 / 23:50:48.602 (—) | RELEASED VERIFIED_CLEANUP | `01a0e020…7af4` 1, `01a0e020…d4cf` 2, `01a0e020…f995` 3, `01a0e020…5d55` 4, `01a0e020…0d3a` 5 |

Events: 1 23:48:49.355 JOB_ACCEPTED (Job accepted); 2 23:49:43.596 JOB_DISPATCHING; 3 23:49:52.382 ATTEMPT_STARTED (attempt_started); 4 23:50:02.961 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:50:11.077 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 6 23:50:19.893 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 7 23:50:30.287 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 8 23:50:40.754 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 9 23:50:42.788 RESULT_RECOGNIZED (result_recognized); 10 23:50:48.602 ALLOCATION_RELEASED (VERIFIED_CLEANUP).


Not-restart-safe database (same run 14 session):

#### C5b_baseline / timeline — job `01a0e021…0aba`, epoch 1

Job: `SUCCEEDED`/desired `RUNNING`, intent `None`, retry_count 0, fence 1, event_sequence 9, checkpoint_sequence 4; counters (outstanding, active) [0, 0]; ledger {'charged': '13.26255137246211067925845859', 'count': 10}; results 1.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e021…d473` | `01a0e020…6bdc` | 1 | RUN | SUCCEEDED | —/— | 23:50:59.246 → 23:52:23.222 / 23:51:47.643 (—) | RELEASED VERIFIED_CLEANUP | `01a0e021…9c34` 1, `01a0e021…4185` 2, `01a0e021…e6d8` 3, `01a0e021…0132` 4 |

Events: 1 23:50:58.838 JOB_ACCEPTED (Job accepted); 2 23:50:59.246 JOB_DISPATCHING; 3 23:51:00.003 ATTEMPT_STARTED (attempt_started); 4 23:51:10.740 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:51:20.375 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 6 23:51:28.133 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 7 23:51:38.000 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 8 23:51:41.872 RESULT_RECOGNIZED (result_recognized); 9 23:51:47.643 ALLOCATION_RELEASED (VERIFIED_CLEANUP).

#### C5b_pause_crash_before_checkpoint_not_restart_safe / timeline — job `01a0e021…53d2`, epoch 1

Job: `FAILED`/desired `PAUSED`, intent `None`, retry_count 0, fence 2, event_sequence 6, checkpoint_sequence 0; counters (outstanding, active) [0, 0]; ledger {'charged': '2.313691370078674307601280365', 'count': 3}; results 0.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e021…91cb` | `01a0e020…6bdc` | 1 | RUN | FAILED | INFRASTRUCTURE/RUNNER_UNAVAILABLE | 23:51:48.153 → 23:52:40.209 / 23:51:56.382 (—) | RELEASED Q VERIFIED_CLEANUP | — |

Events: 1 23:51:47.931 JOB_ACCEPTED (Job accepted); 2 23:51:48.153 JOB_DISPATCHING; 3 23:51:55.199 ATTEMPT_STARTED (attempt_started); 4 23:51:55.974 PAUSE_REQUESTED (USER_PAUSE); 5 23:51:56.382 ATTEMPT_FAILED (RUNNER_UNAVAILABLE); 6 23:51:56.596 ALLOCATION_RELEASED (VERIFIED_CLEANUP).

#### C7b_manual_retry_without_checkpoint / timeline — job `01a0e021…cbe9`, epoch 1

Job: `SUCCEEDED`/desired `RUNNING`, intent `None`, retry_count 0, fence 1, event_sequence 9, checkpoint_sequence 4, retry_of `01a0e021…53d2`; counters (outstanding, active) [0, 0]; ledger {'charged': '13.67881303196104545045902058', 'count': 11}; results 1.

| # | attempt | incarnation | fence | intent | state | failure | lease issued → expires / revoked (reason) | allocation | checkpoints (id, seq) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01a0e021…dd43` | `01a0e020…6bdc` | 1 | RUN | SUCCEEDED | —/— | 23:51:57.007 → 23:53:24.169 / 23:52:46.923 (—) | RELEASED VERIFIED_CLEANUP | `01a0e022…32c0` 1, `01a0e022…a027` 2, `01a0e022…605d` 3, `01a0e022…32f0` 4 |

Events: 1 23:51:56.804 JOB_ACCEPTED (MANUAL_RETRY); 2 23:51:57.007 JOB_DISPATCHING; 3 23:51:58.523 ATTEMPT_STARTED (attempt_started); 4 23:52:08.451 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 5 23:52:16.625 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 6 23:52:27.039 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 7 23:52:34.936 CHECKPOINT_COMMITTED (CHECKPOINT_COMMITTED); 8 23:52:41.172 RESULT_RECOGNIZED (result_recognized); 9 23:52:46.923 ALLOCATION_RELEASED (VERIFIED_CLEANUP).


### Round-1 runs (16, 17) and final-source run (19)

Runs 16 and 17 used the round-1 images of run 14 (`823af64e…ec89`,
`acf90130…c4bf`). Run 16 had the B15-R18 sweep and the C6 oracle fix; run 17
added B15-R19 and is the last round-1 run. Run 19 is the **final source**:
every round-2 fix, the round-2 images (`c30fa52a…2024`, `bcefa221…aae3`, both
equal to `src/nexa`), the C4 barrier and the derived C8 limit. All used
database `nexa_b05_test_b15docker`. Compact raw:
[raw/B15-docker-run16.json](raw/B15-docker-run16.json),
[raw/B15-docker-run17.json](raw/B15-docker-run17.json),
[raw/B15-docker-run19.json](raw/B15-docker-run19.json) (each with the
not-restart-safe test under `not_restart_safe_database`). Every `settle()`
check passed. Each run has its own input and spec, so checksums differ between
runs; within a run, every compared result equals that run's R0 (C7a/C7b use
their own spec, as in run 14). Per attempt, state, failure, fence, intent,
release reason and quarantine are the same in runs 16, 17 and 19 except C4
(and the C6 enable count in run 19, explained below the table).

| Scenario | Run 14 | Run 16 | Run 17 | Run 19 (final source) |
|---|---|---|---|---|
| C0 | SUCCEEDED, 52.5 s, R0 `8065fa80…61bf` | SUCCEEDED, 51.9 s, R0 `545b904b…f7e4` | SUCCEEDED, 55.4 s, R0 `8d443776…bf60` | SUCCEEDED, 52.2 s, R0 `9d8bc2f9…308b` |
| C1 | PAUSED (attempt 1 `CANCELLED/PAUSE`) → SUCCEEDED, retry_count 0, = R0 | same, = R0 | same, = R0 | same, = R0 (attempt 2 restores seq 2) |
| C2 | CANCELLED (`USER_CANCEL`), replay identical | same | same | same |
| C3 | runner stop 1.877 s before DB expiry; reaped `LOST/LEASE_EXPIRED`; quarantined charge 15.083 → 15.924 in 3 s; retry 1 → SUCCEEDED = R0 | 1.843 s before expiry; same states; charge 15.054 → 15.822 in 3 s; retry 1 → SUCCEEDED = R0 | 1.753 s before expiry; same states; charge 16.307 → 17.154 in 3 s; retry 1 → SUCCEEDED = R0 | 1.818 s before expiry (worker dead 94.0 s until reaped); same states; charge 16.949 → 17.786 in 3 s; retry 1 (fence 3) restores seq 1 → SUCCEEDED = R0 |
| C4 | workload killed after a committed checkpoint → PAUSED (`FAILED/RUNNER_UNAVAILABLE`, retry_count 0) → SUCCEEDED = R0 | same | pause stop won the race (no kill): attempt 1 `CANCELLED/PAUSE`, PAUSED with retry_count 0 → resume restores sequence 2 → SUCCEEDED = R0 | barrier: workload SIGKILLed (exit 137) after the pause checkpoint commit while the worker was frozen → attempt 1 `CANCELLED/PAUSE`, PAUSED, retry_count 0, allocation released after verified cleanup → resume restores seq 2 → SUCCEEDED = R0 |
| C5a | PAUSED with retry_count 1 (`INFRASTRUCTURE`), CFP attempt `CANCELLED/PAUSE` → CANCELLED | same | same | same (CFP attempt fence 3, one pause checkpoint) |
| C5b | FAILED (`RUNNER_UNAVAILABLE`), not restart-safe | same | same | same |
| C6 | drain keeps work; disable → `STOPPING/WORKER_DISABLED`, QUARANTINED then released after proof; enable on the first try; both jobs SUCCEEDED = R0 | same (enable 1 try) | same (enable 1 try) | same states; enable succeeded on the 6th request (5 × `409`, 1 s apart) |
| C7a / C7b | new Job with `retry_of_job_id`, SUCCEEDED, own-spec checksum | same | same | same (C7a restores seq 3 of the C8 job; C7b = R0' `4b566163…333c`) |
| C8 | FAILED `TIMEOUT/RUNTIME_LIMIT_REACHED`, no retry (limit 28 s) | same (28 s) | same (28 s) | same; limit 36 s derived from C0 (compute 43.666 s, checkpoint gap 10.989 s), 3 checkpoints before the timeout |
| C9 | runner stop 4.859 s before expiry; 20.232 s gap before the new runner; same incarnation; reaped fence 2; retry 1 → SUCCEEDED = R0 | 4.931 s; 14.605 s gap; same incarnation; fence 2; = R0 | 4.528 s; 21.204 s gap; same incarnation; fence 2; = R0 | 4.864 s; 20.044 s gap; same incarnation; fence 2; = R0 |
| C10 | epoch 1 → 2; RUNNING job adopted (1 attempt, fence 1), QUEUED job dispatched; both SUCCEEDED = R0, retry_count 0 | same | same | same |
| Totals | 13 accepted Jobs (2 CANCELLED, 1 FAILED, 10 SUCCEEDED); epochs [1, 2]; fairness samples 14.14 → 200.71, non-decreasing | 13 accepted Jobs (same states); epochs [1, 2]; 13.97 → 190.98, non-decreasing | 13 accepted Jobs (same states); epochs [1, 2]; 14.98 → 194.30, non-decreasing (11 samples) | 13 accepted Jobs (same states); epochs [1, 2]; 14.14 → 204.59, non-decreasing (11 samples); not-restart-safe test 3 jobs, epoch [1] |

In runs 14–17 C4 killed the workload as soon as the harness saw the pause
checkpoint commit (polled every 10 ms), so the kill raced the pause stop:
runs 14 and 16 recorded "workload killed", run 17 "pause stop won". The
`FAILED/RUNNER_UNAVAILABLE` attempt of runs 14 and 16 is the B15-R21 defect.
Round 2 replaced the race with a barrier and a strict oracle (see
Self-review). In run 19 the worker was frozen while the publish committed, the
workload was killed (exit 137) before the worker could send its stop, and
after unfreezing the worker ended the attempt `CANCELLED/PAUSE`, not `/fail`:
the B15-R21 fix on real containers.

C6 enable count: runs 14–17 predate the round-2 B15-R07 rule. Enable of a
DISABLED worker now needs the latest heartbeat to have passed every READY
check (`ready_checked_at = last_heartbeat_at`). The heartbeat that arrived
while the fenced container was still being cleaned up could not pass, so the
harness got `409` until the next heartbeat (interval about 5 s in the worker
log) and then `200`. No dispatch happened while the worker was DISABLED.

### B13 queue plan and microbench (ACC-11)

B15 widens the Job lock set (dispatch now locks the chosen Job row by `pk_jobs`)
and adds the leader probes (retry promotion, lease reaper, idempotency sweep). Per
`.agents/skills/benchmarking-scheduler-fairness`, the B13 R13 microbench was rerun
with the same inputs: API-prefilled 100k fixture
(`b13-api-prefill-100k-r13.json`, sha256 `037d2d8a…c3c3`), 100 tenants × 1,000
Jobs, `jit = off`, EXPLAIN (ANALYZE, BUFFERS) after the measured ticks. Every
database is a fresh template copy of the B13 evidence DB, migrated to `0019`:
`…b13r13_base` for steady, dispatch and default quota, and
`…b13r13_cadence_long` for the two drained modes. The B13 evidence DBs were not
touched. The B13 `r13-pending` mode cannot be rerun on a copy, because the base
DB already holds the B13 worker ("Refusing to replace an existing worker"). The
steady and dispatch base-copy modes replace it. Each report records its head,
tracked-diff hash and untracked file hashes under `provenance`.

| Mode | B13 reps | B13 median (range) ms | B15 reps | B15 median (range) ms | Decisions (B13 = B15) | Rows written per tick |
|---|---|---|---|---|---|---|
| steady | 3 | 172.2 (138.4–174.6) | 3 | 122.2 (114.4–197.5) | NoDecision | 64 jobs, 1 event (replay page) |
| dispatch | 3 | 134.7 (123.6–186.4) | 3 | 121.4 (113.6–187.7) | NoDecision | 64 jobs, 1 event (replay page) |
| default-quota | 10 | 478.6 (444.7–527.0) | 10 | 450.2 (364.4–528.1) | CreateReservation, Dispatch | 1 job, 0 events |
| drained-steady | 3 | 223.5 (181.7–233.7) | 3 | 166.4 (165.9–240.2) | DrainForReservation | 0 |
| drained-dispatch | 3 | 448.8 (427.2–450.3) | 3 | 440.2 (386.3–506.7) | CreateReservation, Dispatch | 1 job, 0 events |

All five medians are at or below the B13 medians, with no errors. This is one
Docker Desktop host, so the numbers compare this change against B13 on the same
machine and are not a latency claim (B22).

Plans, matched by SQL text against the B13 r13 reports:

- No `Seq Scan` on `jobs` in any mode. The batched queue-selection statement has
  the same node shape as B13 (`ix_jobs_b13_head`, `ix_jobs_b13_priority_age`,
  `ix_jobs_oldest_eligible`, `ix_jobs_b13_submitter`), with at most 16 `jobs`
  rows per loop. The only `jobs` node above 16 rows is the 64-Job replay page
  (`uq_jobs_tenant_job`, one loop), which is identical in B13.
- New in B15: the retry promotion probe `SELECT EXISTS … jobs ⨝ retry_schedules`
  (`ix_jobs_retry_ready`, `ix_retry_schedules_ready`, 0 rows). In the drained
  modes it also adds the dispatched Job's `FOR UPDATE` and state update, one row
  each by `pk_jobs`.
- Changed: in steady and dispatch, the planner reads `queue_eligibility_events`
  (100 rows, 26 pages, no statistics after the template copy) by `Seq Scan`
  instead of a bitmap scan of `ix_queue_eligibility_pending`. The row count is
  the same and the table is not `jobs`.

Leader probes, EXPLAIN (ANALYZE, BUFFERS) in a rolled-back transaction on the
drained-steady copy (100,000 jobs, 100,419 idempotency records, 6 lease rows),
`benchmarks/results/b15/b15-reaper-sweep-explain*.json`:

| Probe | Before B15-R18 | After B15-R18 |
|---|---|---|
| `reaper.expired_leases` | Index Only Scan `ix_attempt_leases_active_expiry`, 0.153 ms | same plan, 0.064 ms |
| `retention.expired_records` | Seq Scan `jobs` → Index Scan `ix_idempotency_records_resource`, 24.071 ms | Index Scan `ix_idempotency_records_b15_sweep` → `pk_jobs` per row, 0.217 ms |

The before plan is finding B15-R18. The coordinator runs the sweep on every
1-second probe. The queue microbench statements do not include these probes
(no captured statement touches `idempotency_records` or `attempt_leases`), so
the table above is not affected by the R18 change, which was made after it.


## Self-review

The full tracked diff and every untracked file were reread against PLAN B15,
the contracts and AGENTS.md invariants. Targeted greps over the changed and
new source found:
- no log call carrying a reason, body or payload;
- no Docker/subprocess call in the application or coordinator modules (every
  Docker action stays in the worker, outside a DB transaction);
- no hardcoded host path or secret;
- no `NEXA_DATABASE_URL` use; the one hit is the migration test's comment
  saying it stays unset.

Edits to approved (prior-task) tests, each with its reason:

| File | Change | Why |
|---|---|---|
| `tests/api/test_operation_matrix.py` | `EXPECTED_B15_OPERATIONS` plus admin worker, allocation and recovery-event cases | New B15 routes enter the per-operation scope matrix (ACC-02) |
| `tests/cli/test_api_route_contract.py` | B15 routes moved from unsupported to supported | The CLI now calls them |
| `tests/cli/test_pagination_and_headers.py` | `admin worker list` removed from the unregistered list | Command now exists |
| `tests/integration/test_checkpoint_b14.py` | Fixture gains `template_values`/`input_media_type` | Needed by B15 resume/retry fixtures; B14 assertions unchanged |
| `tests/workloads/test_runner_checkpoint_b14.py` | Removed the "`REQUEST_CHECKPOINT` reason `PAUSE` fails closed" case; round 2 also removed the `missing_state` case | B15 makes `PAUSE` a valid reason (5.C). B15-R20: a missing state now waits for the first write; its fail-closed end (deadline, invalid state) is in `test_runner_checkpoint_b15.py`. The other fail-closed cases stay |
| `tests/worker/test_checkpoint_flow_b14.py` | `frames_first` in the fake runner; round 2: `test_runner_invalid_control_answer_fails_closed` writes an invalid `state.json` instead of deleting it, and expects the runner's `STOPPED{FAILURE}` frame to fail the attempt | B15-R14/R16: terminal frames can arrive on any connection. B15-R20: a deleted state now waits instead of failing, so the test uses the fail-closed input that still fails |
| `tests/worker/test_runner_control_b10.py` | New case for a kept terminal frame before the ACK | B15-R16 |
| `tests/workloads/test_runner.py` | R15/R16 runner tests | B15-R15/R16 |
| `tests/integration/test_worker_api_b10.py`, `tests/integration/test_worker_authority_b10.py` (round 2) | Lock-order helper and two tests renamed and changed from policy → receipt → worker to policy → worker (`FOR UPDATE`) → receipt: `test_heartbeat_policy_lock_precedes_worker_and_receipt`, `test_policy_lock_precedes_worker_and_receipt_for_adopt_and_renew`; the adopt-after-expiry case expects `stale_authority` | B15-R27: the old order was the deadlock; B15-R30: SM:59 names `stale_authority` |
| `tests/integration/test_coordinator_b13.py:1383` (round 2) | The offer is marked `CLAIMED` before the test fails it | The `/fail` guard added with B15-R21 follows SM:75: an unclaimed `CREATED` offer cannot fail. The B13 reservation assertions are unchanged |
| `tests/integration/test_jobs_b08.py:747` (round 2) | Added an assertion that the job list writes three millisecond digits | B15-R29; no existing assertion changed |
| `tests/integration/test_idempotency_b06.py` (round 2) | New test `test_record_swept_between_conflict_and_lock_is_inserted_again` | B15-R37 (L3); no existing test changed |
| `tests/docker/test_real_runner.py` | B09 `test_supervisor_registration_after_startup_deadline_never_launches_cpu` ACKs `STOPPED` like the worker and waits for the container stop | B15-R16 linger (≤3 s without an ACK); the no-launch assertions are unchanged |

Other review notes:
- The B09 bound `test_watchdog_stops_cpu_after_controller_disconnect_at_runtime_limit`
  broke with the R14 runner change (1 failed, 1 passed on the R15 images).
  R16 fixed it; on the final images B09 has 9 passed.
- The C5b/C7b scenarios use a separate database with a non-restart-safe
  template version, because the contract pins `template_id` to
  `cpu-iterative`.
- The C7a oracle compares the computation, not the raw bytes. The source job
  is the C8 job, whose spec carries a runtime limit, so its `spec_checksum`
  differs.
- Round 2 changed the C8 runtime limit from a fixed 28 s to a value derived
  from the C0 baseline of the same run (see run 18 in the run history). The
  C7a retry inherits the C8 spec, so it needs 2L ≥ T + G + 1 (T = C0 compute
  time, G = the longest gap between C0's start and checkpoint marks), while C8
  needs L < T. The test takes the midpoint L = ⌈(3T + G + 1)/4⌉ and asserts
  both bounds. Runs 14–17 used 28 s.
- Round 2 made C4 deterministic, in the test only (no product hook). The
  harness method `crash_after_pause_commit` holds the attempt's `RESERVED`
  checkpoint row `FOR UPDATE`, so the pause publish blocks. Once
  `pg_blocking_pids` shows the publish waiting, it freezes the worker
  container (`docker pause`), commits so the publish commits, waits for the
  committed checkpoint, SIGKILLs the workload and reads its exit state, and
  only then unfreezes the worker. No `REQUEST_STOP{PAUSE}` can reach the
  workload in between. The oracle requires exit code 137, then `PAUSED`
  with attempt 1 `CANCELLED/PAUSE` (B15-R21: the forced stop proves the
  end, and `/fail` is no longer accepted in `STOPPING`), retry_count 0, no
  retry schedule, then resume restores that checkpoint and the result equals
  R0.
- C3/C9 use the container's Docker `FinishedAt` as the runner stop time.
- `tests/docker/test_b15_control_recovery.py` was edited after run 14 (run 15
  found the race). C6 now accepts `RUNNING` or `CHECKPOINTING` for the drained
  attempt, and `settle()` requires one of the four terminal attempt states
  instead of excluding three live ones. The first change widens the oracle to
  a live state the contract already defines; the second makes `settle()`
  stricter. Runs 16 and 17 passed with both.
- Two changes were not test-first. The ACC-03 case (retry referencing another
  tenant's committed checkpoint → 404, no Job, no reference) was added after
  `job_control.py` already filtered by tenant, so it was never red on the
  product. Replacing both tenant filters (`job_control.py` lines 477 and 544)
  with `True` made it fail (503 instead of 404), and the file was restored.
  B15-R18 has no red test either: on the small test databases the planner
  already picks the index. Its evidence is the 100k plan probe before and
  after the fix. The three retention tests pass on the final query.
- The worker log is saved with `NEXA_B15_WORKER_LOG_OUT` for diagnosis only;
  it is not in the repository.
- A `JournalCorruption` warning from `_result_once` around some completions
  appears in worker logs. It is a caught, logged path. Every affected job
  still finished with exactly one recognised result and matched R0. It was
  not diagnosed further (see Limits).
- B15-R19 was test-first. `test_adopt_transfers_the_reservation_exactly_as_reserved`
  failed on the product (the adopt answer carried `reserved_at` with six
  fractional digits, the reserve answer three) and passed after
  `adopt_attempt` returned the stored wire body as a `JSONResponse`. The B14
  test `test_adoption_reconcile_explains_every_transferred_reservation` and the
  B14 Docker S6 test were not edited. The images were not rebuilt: the changed
  route runs only in the API, which the harness serves in-process from `src`.
  In round 1 the other nine routes that re-serialise through `response_model`
  were left as they were (B15-R19 open part); round 2 fixed them (B15-R29). A grep of `src/nexa/worker` finds whole
  reservation bodies compared only in adoption reconciliation
  (`checkpoint_flow.py` `held == server` and `_cycle_holds`); every other
  check compares id and sequence fields, which carry no timestamp.

## Acceptance matrix

Layers: U = unit / fake Docker; P = PostgreSQL 17 + in-process API; C =
Docker containers on Docker Desktop (linux/arm64 VM). Status covers the B15
slice only. `docs/acceptance.md` is not edited: every gate there stays
`specified` until Task Review. "PG" means the full PostgreSQL run in
Verification; "U" means the default run.

| Gate | B15 criterion | Layer | Tests | Evidence | Applicability | Status | Limits |
|---|---|---|---|---|---|---|---|
| ACC-15 | Terminal immutable; cancel commit blocks a later completion; cancel/complete race has one winner | P | `test_completion_committed_before_cancel_wins`, `test_concurrent_cancel_and_complete_have_exactly_one_winner`, `test_cancel_running_fences_authority_and_cleanup_cancels`, `test_cancel_queued_job_is_terminal_once_and_replays_before_if_match`, `test_cancel_waiting_or_paused_job_closes_schedule_and_clears_intent`, `test_cancel_recovering_job_waits_for_cleanup_without_new_fence`; round 2: `test_three_way_race_queued_on_the_job_lock_has_one_winner` (cancel with a valid If-Match × complete × reaper, both orders), `test_cancel_of_a_dispatching_offer_fences_it_before_any_claim`, `test_cancel_of_a_reaped_job_keeps_the_lost_attempt_terminal` (B15-R34), `test_another_worker_cannot_learn_that_a_job_was_cancelled` (B15-R35) | PG | B15 | pass | The race is P only; C2 cancels a RUNNING job without a racing completion |
| ACC-15 | Cancel RUNNING end to end | C | C2 | runs 14, 16, 17 and 19 (final source), raw/B15-docker.json, raw/B15-docker-run16.json, raw/B15-docker-run17.json, raw/B15-docker-run19.json | B15 | pass | Docker Desktop |
| ACC-15 | `PAUSED` only after checkpoint + cleanup; in-flight interval cycle becomes the pause checkpoint; rejected checkpoint aborts the pause | U | `test_renewed_pause_checkpoints_then_stops_and_cleans_up_without_failure`, `test_an_open_interval_cycle_becomes_the_pause_checkpoint`, `test_checkpoint_committed_before_the_pause_was_observed_ends_the_pause`, `test_rejected_pause_checkpoint_keeps_running_after_the_pause_is_aborted`, `test_runner_takes_a_pause_checkpoint_then_stops_for_pause`, `test_pause_stop_frame_without_a_pause_request_is_a_failure`; round 2: the six B15-R20 runner tests in `tests/workloads/test_runner_checkpoint_b15.py`, `test_checkpoint_for_pause_before_the_first_state_write_pauses`, the three B15-R21 worker tests (`test_container_exit_after_the_pause_checkpoint_committed_pauses`, `test_runner_stop_after_the_pause_checkpoint_committed_pauses`, `test_unconfirmed_pause_stop_after_the_checkpoint_committed_is_forced`), `test_rejected_checkpoint_of_a_checkpoint_for_pause_attempt_fails_it_at_once` (B15-R28) | U | B15 | pass | — |
| ACC-15 | same | P | `test_pause_checkpoints_then_cleanup_pauses_and_keeps_admission`, `test_pause_finishes_an_open_interval_cycle`, `test_rejected_pause_checkpoint_aborts_the_pause`, `test_pause_requires_checkpointable_running_job`, `test_pause_of_a_queued_job_is_a_state_conflict`; round 2: `test_failure_after_the_pause_checkpoint_committed_is_stale_and_cleanup_pauses` (B15-R21, TIMEOUT and OOM), `test_rejected_checkpoint_for_pause_checkpoint_fails_without_a_retry` (B15-R28), and the worker + PostgreSQL test `test_checkpoint_for_pause_before_the_first_state_write_ends_paused` (B15-R20: real agent and runner, real API client, state write delayed) | PG | B15 | pass | — |
| ACC-15 | Pause-crash: after checkpoint → `PAUSED` without retry; before checkpoint → exactly one `CHECKPOINT_FOR_PAUSE` retry when restart-safe, `FAILED` when not or out of budget | P | `test_pause_crash_after_checkpoint_pauses_without_retry`, `test_pause_crash_before_checkpoint` (parametrised), `test_pausing_failure_with_committed_checkpoint`, `test_checkpoint_for_pause_retry_is_promoted_and_dispatched_under_its_intent`, `test_checkpoint_for_pause_attempt_starts_into_pausing_and_never_reserves_a_result`, `test_reaper_keeps_desired_paused_for_expired_pausing_lease` | PG | B15 | pass | B15-R02 (non-retryable class wins over desired PAUSED) is an interpretation |
| ACC-15 | same with real containers | C | C4, C5a, C5b | runs 14, 16, 17 and 19 | B15 | pass | Out-of-budget branch is P only. C4 in runs 14–17 raced the pause stop (runs 14 and 16: kill landed, attempt `FAILED/RUNNER_UNAVAILABLE`, the B15-R21 defect; run 17: pause stop won). Run 19 (final source) used the round-2 barrier: kill after the commit, attempt `CANCELLED/PAUSE` |
| ACC-15 | Resume creates a new attempt of the same job/session and does not consume the retry budget | P | `test_resume_requeues_the_same_job_without_consuming_retry`, `test_resume_needs_an_uncorrupted_checkpoint_or_restart_safe_input` | PG | B15 | pass | — |
| ACC-15 | same | C | C1, C4 (retry_count 0 after resume) | runs 14, 16, 17 and 19 | B15 | pass | — |
| ACC-15 | Manual retry = new job/session with `retry_of_job_id`; source unchanged; optional checkpoint reference verified and restored | P | `test_manual_retry_creates_a_new_job_and_leaves_the_source_unchanged`, `test_manual_retry_references_a_verified_checkpoint`, `test_manual_retry_first_attempt_restores_the_referenced_checkpoint`, `test_manual_retry_rejects_an_unrestorable_checkpoint` | PG | B15 | pass | B15-R05, R06 interpretations |
| ACC-15 | same | C | C7a, C7b | runs 14, 16, 17 and 19 | B15 | pass | — |
| ACC-16 | Lease expiry revokes/fences but keeps the allocation (`QUARANTINED`); release only after exact-identity proof | P | `test_reaper_moves_expired_running_lease_to_recovering_once`, `test_reaper_revokes_live_lease_of_succeeded_job_without_fence`, `test_reaped_unclaimed_offer_stays_unclaimed_for_tombstone_cleanup` (B15-R12), `test_disable_fences_the_running_attempt_and_cleanup_releases_it` | PG | B15 | pass | — |
| ACC-16 | same with real containers; old callbacks blocked; the worker reconciles to READY | U + C | `test_failure_rejected_after_the_reaper_revoked_the_lease_ends_with_verified_cleanup`, `test_cleanup_replayed_before_a_stale_failure_discards_it_during_reconciliation`, `test_cancel_revocation_stops_the_container_and_discards_the_rejected_renewal`; C3, C9 | U, runs 14, 16, 17 and 19 | B15 | pass | Host reboot not run |
| ACC-16 | Drain/disable/enable semantics; enable only after a fresh reconciled heartbeat | P | `test_drain_keeps_running_attempts_and_enable_restores_dispatch`, `test_enable_requires_a_fresh_reconciled_heartbeat`, `test_disable_revokes_a_terminal_jobs_leftover_lease_without_a_fence`, `test_disable_and_reaper_race_fence_the_attempt_once`, `test_worker_mutation_preconditions_authorization_and_frozen_mode`, `test_worker_with_a_dispatched_checkpoint_for_pause_offer_stays_ready_to_claim_it`, `test_worker_running_a_pausing_attempt_stays_ready` (B15-R13); round 2: `test_drain_keeps_a_committed_offer_pollable_until_it_succeeds` (B15-R22), `test_enable_of_a_disabled_worker_requires_a_heartbeat_that_passed_readiness` (B15-R07), `test_steady_heartbeats_keep_the_worker_etag` (B15-R08), `test_disable_fences_several_offers_in_job_order` (mutation: reversed lock order → `DID NOT RAISE`), `test_reaper_abandons_the_leftover_result_reservation_of_a_terminal_job` (B15-R36) | PG | B15 | pass | B15-R07 enable rule is an interpretation (tightened in round 2) |
| ACC-16 | same | C | C6 | runs 14, 16, 17 and 19 | B15 | pass | Single worker |
| ACC-22 | Crash recovery within the 2-retry budget with backoff; failure callback typed and fenced/quarantined before release | P + C | `test_reaper_moves_expired_running_lease_to_recovering_once`, `test_pause_crash_before_checkpoint` (retry_count 2 → `FAILED`, budget exhausted), B14 `test_retry_b14` (backoff, budget; regression in PG); C3, C9 (retry 1, `RETRY_READY` after backoff) | PG, runs 14, 16, 17 and 19 | B15 | pass | Budget exhaustion with real containers not run (P only) |
| ACC-22 | Runtime limit → `TIMEOUT`, no blind retry | U + C | `test_runner_runtime_limit_stop_is_a_timeout`; C8 | U, runs 14, 16, 17 and 19 | B15 | pass | Startup-limit and runtime-limit stops are both `RUNTIME_LIMIT_REACHED` (B15-R10 limit) |
| ACC-22 | OOM → typed `OOM/CONTAINER_OOM` failure → `FAILED` without a retry (a restart-safe job with a committed checkpoint included) | U + P | U: `tests/worker/test_container_exit_b14.py` (exit 137 with `OOMKilled` → `OOM/CONTAINER_OOM`); P: `test_container_oom_fails_the_job_without_a_retry` (round 2, written after the code; mutation: treating OOM as retryable in `execution_cleanup.py` turns it red with `RETRY_WAIT`), `test_failure_after_the_pause_checkpoint_committed_is_stale_and_cleanup_pauses` (OOM after the pause checkpoint → `stale_authority`, PAUSED) | U, PG | B15 | pass | — |
| ACC-22 | OOM and log bound with real containers | C | Cited, not rerun as B15 scenarios: B09 `test_production_container_enforces_cpu_pid_scratch_and_memory_bounds` (one real cgroup OOM kill) and `test_watchdog_stops_workload_when_runner_log_bound_is_exceeded` (log bound), both in the B09 file of the Docker regression on the round-2 images; B14 S2 and S5b (real workload `docker kill` → typed `INFRASTRUCTURE` failure, retry within budget) and S6 (worker kill, adoption); B11 `test_two_tenant_api_to_docker_result_and_release[True]` (real worker killed after the completion commits and before cleanup; the new incarnation cleans the exact container under the stored grant, see [B11 evidence](B11-coordinator-dispatch-result.md)) | Docker regression (this session), B09/B11/B14 evidence | B15 (cited) | pass (cited) | The B15 fixture has no memory or log-volume mode, so no B15 C scenario runs OOM or log flood; the U + P row above covers the B15 no-retry rule |
| ACC-07 | Control replay before `If-Match`; 412/428; same key = same body; admin mutations idempotent | P | `test_cancel_queued_job_is_terminal_once_and_replays_before_if_match`, `test_control_preconditions_authorization_and_frozen_mode`, `test_worker_mutation_preconditions_authorization_and_frozen_mode` | PG | B15 | pass | ≥30-day retention covered by the sweep tests below |
| ACC-07 | Idempotency record kept ≥ retention after terminal; bounded sweep; no writes while frozen | P | `test_sweep_deletes_only_expired_completed_job_records_of_terminal_jobs`, `test_sweep_is_bounded_per_transaction`, `test_sweep_writes_nothing_while_frozen_or_without_leadership`; round 2: `test_record_swept_between_conflict_and_lock_is_inserted_again` (B15-R37), `test_b15_upgrade_backfills_retention_of_jobs_terminal_before_it` (B15-R25) | PG | B15 | pass | — |
| ACC-07 | CLI sends a stable contract Idempotency-Key | U | `test_job_control_generates_a_contract_idempotency_key`, `test_job_control_sends_reason_etag_key_and_tenant`, `test_admin_worker_actions_forward_reason_etag_and_key` | U | B15 | pass | — |
| ACC-07 | Replay in real flow | C | C2 replay | runs 14, 16, 17 and 19 | B15 | pass | — |
| ACC-13 | Runner stops compute at its monotonic deadline independent of the worker; no compute overlap after partition/agent kill | U | `test_failing_renewals_let_the_runner_stop_itself_at_its_authority_deadline`, `test_deadline_after_the_supervisor_reported_exit_still_emits_the_stop_frame` (B15-R15) | U | B15 | pass | — |
| ACC-13 | same | C | C3 (runner ended 1.877 s before DB expiry; run 16: 1.843 s; run 17: 1.753 s; run 19: 1.818 s), C9 (4.859 s before, 20.232 s gap before the new runner; run 16: 4.931 s, 14.605 s; run 17: 4.528 s, 21.204 s; run 19: 4.864 s, 20.044 s) | runs 14, 16, 17 and 19 | B15 | pass | In C3/C9 the workload may have finished its compute before the deadline; the cut of a still-computing workload at the deadline is the B09 Docker test (regression 9 passed) |
| ACC-13 | Renew committed before the reaper keeps the attempt | P | `test_renew_committed_before_reaper_keeps_the_attempt`; round 2 (B15-R23): `test_renew_committed_after_the_probe_keeps_the_attempt`, `test_cancel_committed_after_the_probe_is_not_reaped` (probe, then the competing commit, then the locked reap; mutations of each CAS conjunct at `reaper.py:86-89` turn one of them red) | PG | B15 | pass | — |
| ACC-14 | ≤1 authorized attempt; fence monotonic; unique final result; one fence per race (reaper×reaper, reaper×cancel, disable×reaper) | P | `test_duplicate_reapers_and_cancel_fence_the_attempt_exactly_once`, `test_reaper_and_cancel_race_has_one_fence`, `test_disable_and_reaper_race_fence_the_attempt_once`, `test_concurrent_cancel_and_complete_have_exactly_one_winner`, `test_checkpoint_for_pause_attempt_starts_into_pausing_and_never_reserves_a_result`; round 2: `test_three_way_race_queued_on_the_job_lock_has_one_winner` (removing the reaper CAS recheck fails both orders), `test_reaper_queued_before_a_completion_fences_and_the_completion_is_stale`, `test_completion_committed_after_the_probe_is_not_fenced`, `test_stale_fence_incarnation_and_lease_answer_stale_authority` (B15-R30), `test_two_callbacks_of_one_worker_serialise_without_a_deadlock` (B15-R27) | PG | B15 | pass | Single node |
| ACC-14 | same | C | every settle: one result from the last attempt, fences 1→2→3 | runs 14, 16, 17 and 19 | B15 | pass | — |
| ACC-20 | 0 accepted jobs lose their trace across API/coordinator/worker restart | C | C10 (epoch 1→2, adoption without retry), final accepted-ID reconciliation (13 + 3 jobs) | runs 14, 16, 17 and 19 | B15 | pass | Full-stack process restart only |
| ACC-20 | Adopting an attempt with a journaled open checkpoint reservation: the transferred reservation equals the reserve answer, so the new incarnation reconciles to READY (B15-R19) | P + C | `test_adopt_transfers_the_reservation_exactly_as_reserved`, B14 `test_adoption_reconcile_explains_every_transferred_reservation` (U); B14 Docker S6 | PG, U, Docker regression | B15 | pass | Which reconcile branch S6 took is not recorded (the journal records are tombstoned when it ends); the held = server branch is proven by the P and U tests |
| ACC-20 | Host reboot | L | — | — | B22 | not-run | Not claimed; Docker Desktop only |
| ACC-21 | Dependency failure fails closed: frozen mode writes nothing (control, admin, reaper, promotion, sweep); renew failure stops the runner; network loss fences | P | `test_control_preconditions_authorization_and_frozen_mode`, `test_reaper_writes_nothing_while_frozen_or_without_leadership`, `test_sweep_writes_nothing_while_frozen_or_without_leadership`, `test_worker_mutation_preconditions_authorization_and_frozen_mode`; round 2: `test_one_failing_lease_does_not_stop_the_reaper_or_the_tick` and `test_coordinator_tick_runs_the_reaper` (B15-R26) | PG | B15 | pass | DB outage itself is B11/B13 evidence (unchanged) |
| ACC-21 | same | C | C9 | runs 14, 16, 17 and 19 | B15 | pass | Network partition of the worker only |
| ACC-27 | API + CLI control flows: cancel/pause/resume/retry/attempts, admin worker/allocations/recovery events; bounded pages | P | `test_list_job_attempts_is_newest_first_with_a_signed_keyset_cursor`, `test_admin_allocations_and_recovery_events_are_bounded_signed_pages`, `test_get_worker_has_etag_inventory_and_millisecond_timestamps`, `test_job_events_have_exactly_three_millisecond_digits`, `test_renew_and_read_routes_write_three_digit_timestamps` (B15-R29) | PG | B15 (API, CLI) | pass | — |
| ACC-27 | CLI contract, exit codes, no reason echo | U | all 13 tests in `tests/cli/test_control_commands_b15.py`; `test_api_route_contract.py`; `test_operation_matrix.py` | U | B15 (CLI) | pass | — |
| ACC-27 | UI flows (Playwright) | W | — | — | B17/B18 | specified | Not B15 |
| ACC-28 | Migration 0019 upgrade/downgrade, CHECK, indexes, offline SQL, metadata parity | P | all 8 tests in `tests/integration/test_migration_b15.py` (round 2 added the backfill, audit-reason precheck and offline downgrade SQL tests and made the parity test compare index predicates; B15-R25, B15-R38) | PG | B15 | pass | Maintenance upgrade/restore is B21/B25 |
| ACC-39 | Ruff, Pytest (default + PostgreSQL), Docker tests, image build for the task | U + P + C | Verification table | this session | B15 | pass | No CI run (nothing pushed). Round-2 images were built after every round-2 fix; both carry the same 127 `.py` files as the final `src/nexa` (listing hash `78579699…05a0`, rechecked after run 19) |
| ACC-02 (regression) | New operations have exact scopes; MEMBER sees only own jobs; SA only via membership for job control, admin-only for workers | U + P | `test_operation_matrix.py`, `test_control_preconditions_authorization_and_frozen_mode`, `test_worker_mutation_preconditions_authorization_and_frozen_mode` | U, PG | B15 slice | pass | — |
| ACC-03 (regression) | Manual retry checkpoint and control stay within the tenant (404 across tenants) | P | `test_manual_retry_references_a_verified_checkpoint` (another tenant's committed checkpoint → 404, no Job or reference written; added after the implementation, mutation check: dropping the tenant filter turns it red), `test_control_preconditions_authorization_and_frozen_mode` | PG | B15 slice | pass | — |
| ACC-04 (regression) | `QUARANTINED` allocations stay inside capacity until proof | P + C | reaper/disable tests above; C3/C6/C9 allocation columns | PG, runs 14, 16, 17 and 19 | B15 slice | pass | No GPU |
| ACC-08 (regression) | Counters and ledger atomic; reaper/cancel/disable count once; quarantine charged; no reset across restart | P + C | race tests above; settle counters; C3 quarantined charge; non-decreasing fairness samples across C10 | PG, runs 14, 16, 17 and 19 | B15 slice | pass | — |
| ACC-11 (regression) | Widened queue predicate keeps the B13 plan and latency | P | B13 microbench rerun (5 modes), leader-probe EXPLAIN | benchmarks/results/b15, section above | B15 slice | pass | Docker Desktop, one host; r13-pending not rerunnable on a copy; latency is B22 |
| ACC-17/18 (regression) | B14 checkpoint/restore on the new images | C | `test_b14_checkpoint_restore.py` S1–S6 | Docker regression | B15 slice | pass | S6 failed twice before the B15-R19 fix and passed in the final regression |

## Limits

- Docker Desktop linux/arm64 VM on macOS, four passing runs per scenario
  (runs 14, 16 and 17 on the round-1 source and images, run 19 on the final
  source and round-2 images; only run 19 is final-source evidence, and each
  scenario passed once there). Earlier runs failed as listed in the run
  history. It
  is not bare Linux, not GPU, not two hosts, and not a reboot. It does not
  replace B20 (hardening/security) or B22 (reliability/benchmark release
  evidence).
- `test_b10_worker_restart.py` is skipped on Docker Desktop (clock domain),
  as in B14.
- The pause deadline (40 s) is an in-memory timer in the worker. A worker
  restart while `PAUSING` restarts from the journal, and the server reaper
  still bounds it by the lease.
- The worker cannot tell a startup-limit stop from a runtime-limit stop; both
  are reported `RUNTIME_LIMIT_REACHED` (B15-R10).
- OOM and log-flood are not run by the B15 Docker scenarios (the fixture has
  no memory or log-volume mode); the matrix row ACC-22 cites the earlier
  real-container evidence (B09, B11, B14).
- The cancel/complete race is proven in PostgreSQL only.
- In C3 and C9 the workload may have finished computing before the runner
  deadline, so these runs prove "no overlap" but not "cut at the deadline".
  The B09 Docker test covers the cut.
- R14/R15/R16 residuals:
  - a runner stop while no worker connection opens within 3 s still ends as
    `RUNNER_PROTOCOL_ERROR`;
  - a watchdog stop that fails for another reason still leaves the runner
    `STOPPING` until reconciliation removes the container.
- A caught `JournalCorruption` warning around some completions was seen in
  worker logs. It changed no outcome but was not diagnosed.
- B15-R11 (container death in the start window → `STARTUP_TIMEOUT`) is open.
  The C5 harness avoids that window.
- B15-R32: retry promotion to `QUEUED` still takes `KEY SHARE` on the
  submitter's `users` row through the B13 `queue_submitters` FK, so it can
  still meet a request that holds that row `FOR UPDATE`; the promotion is then
  retried on the next probe.
- B15-R33: stale callback rejections and worker reconcile outcomes are not
  persisted as events. They are visible only in the API answer and the worker
  log; the recovery-event API returns the `RECOVERY_EVENT_TYPES` set.
- B13-R12 and B14-R04 stay open and were not touched.
- `nexa_b10_smoke3-caddy-1` PID growth: see Revision and environment. It was
  not stopped.


## Raw evidence

- [raw/B15-docker.json](raw/B15-docker.json): compact run 14 timelines, checksums,
  counters, ledger and fairness samples (C0–C10, C5b/C7b).
- [raw/B15-docker-run16.json](raw/B15-docker-run16.json) and
  [raw/B15-docker-run17.json](raw/B15-docker-run17.json): the same fields for
  run 16 (before B15-R19) and run 17 (last round-1 run).
- [raw/B15-docker-run18.json](raw/B15-docker-run18.json): round-2 run 18,
  failed at C7a (see run history); kept as the record of that failure.
- [raw/B15-docker-run19.json](raw/B15-docker-run19.json): the same fields for
  run 19 (final source, round-2 images), including the C4 crash path and exit
  code, the derived C8 limit and its C0 inputs, and the C6 enable count.
- [raw/B15-images.json](raw/B15-images.json): final (round-2) and superseded
  image digests, package comparison against `src/nexa` and the round-1
  B15-R18/R19 rechecks.
- `benchmarks/results/b15/b15-queue-api-100k-*.json`: the five B13 microbench
  reruns (with `provenance`).
- `benchmarks/results/b15/b15-reaper-sweep-explain-before-r18.json` and
  `b15-reaper-sweep-explain.json`: leader-probe plans before and after B15-R18.

Raw files hold IDs, states, stable codes, timestamps, checksums and numbers
only: no credential, input, checkpoint content or free-text reason. This
evidence covers B15 on Docker Desktop and does not replace B20 or B22.

## Findings

| ID | Kind | Summary | Proposal | Status |
|---|---|---|---|---|
| B15-R02 | contract gap | SM:30–33 do not say whether a non-retryable failure class wins over desired PAUSED with a committed checkpoint | Non-retryable class → FAILED (SM:33 guard "desired not CANCELLED" is satisfied); record for contract clarification | implemented as proposed, open for review |
| B15-R03 | contract drift (B10/B11) | A fenced callback with a stale `job_fence` returns `409 state_conflict` ("Worker authority is stale") from `_authority_rows`; SM:59/CR:70 name `stale_authority` | B15 changes only the committed-cancel case to `stale_authority`; aligning the generic fence mismatch touches approved B10/B11 contract tests and needs a decision. **Round 2:** closed by B15-R30 | closed by B15-R30 |
| B15-R04 | latent lock cycle (B11/B14) | `coordinator/dispatch.apply_decision` and `coordinator/retry.promote_due_locked` update the same `jobs` row twice in one transaction (state, then `_event`). PostgreSQL re-runs FK checks for a row version created by the current transaction, taking `KEY SHARE` on the submitter's `users` row; request transactions hold that row `FOR UPDATE` (`_revalidate_principal`) before policy/job locks → a cycle that ends by deadlock detection or the 1 s coordinator `lock_timeout`. Observed first in the B15 reaper (`LockNotAvailable ... relation "users"`), fixed there by a single job UPDATE; Round 1 also claimed that no B15 path updates a job twice; review R31 found that too broad, and it is withdrawn | Fold the state change into `_event(**changes)` in dispatch/retry. **Round 2:** done; dispatch and retry promotion/block each issue one job UPDATE (red `LockNotAvailable` on `users` in `test_dispatch_updates_the_job_once_and_ignores_a_locked_submitter` and `test_retry_promotion_and_block_update_the_job_once`, green after). Residual B15-R32 | fixed in round 2 (residual B15-R32) |
| B15-R05 | contract gap | `ExecutionCheckpoint`/`ExecutionContext` are `additionalProperties:false` and carry only the retried Job's `logical_session_id`; a manual retry restores a source-Job checkpoint whose provenance names the source session, so the worker cannot re-verify inherited `session_id` | Server verifies inherited provenance at claim (owner job + owner session, `restore_candidates`); worker checks provenance `job_id` = record `job_id` and every other provenance field, skips only `session_id` when `record.job_id != context.job_id`. Proposal: add an optional source-session field to `ExecutionCheckpoint` in a future contract change | implemented as proposed, open for review |
| B15-R06 | interpretation | SM:41 "no second lineage" does not say whether a second manual retry of the same FAILED source with a new `Idempotency-Key` is allowed | Read as dedup of the same request only: same key replays the first new Job; a new key admits another Job (source stays FAILED, each new Job has `retry_of_job_id`), bounded by admission/quota like submit | implemented as proposed, open for review |
| B15-R07 | contract gap | SM:98 enables a DRAINING/DISABLED worker only when "READY + reconciled", but a heartbeat never reports `READY` while `admin_state=DISABLED` (B10 `_worker_ready` rule), so a disabled worker could never be enabled | `enable` requires: heartbeat ≤30 s; health READY when DRAINING; current incarnation not ended, reconciliation drained and its snapshot equal to the live allocation snapshot; no QUARANTINED allocation on the worker; inventory passes the READY inventory check. Health stays `STARTING` after enabling a DISABLED worker and the next heartbeat sets READY before any dispatch or poll. **Round 2 (review R07):** enable of a DISABLED worker skipped the storage/inventory/container checks of `_can_be_ready`. The heartbeat now stores `worker_incarnations.ready_checked_at` = its time when every READY check but the admin state passed, and `NULL` otherwise; enable requires it to equal `last_heartbeat_at`. Red first (enable returned 200 after a heartbeat that failed storage readiness): `test_enable_of_a_disabled_worker_requires_a_heartbeat_that_passed_readiness` | implemented as proposed + round-2 fix, open for review |
| B15-R08 | usability (B10) | Every heartbeat (≈5 s) increments `workers.version`, so the worker ETag used by admin `If-Match` goes stale quickly; drain/disable/enable must follow a fresh GET or return 412 | Keep B10 behaviour; propose a separate admin-facing version for `Worker` in a future contract change. **Round 2 (review R08):** fixed without a contract change: heartbeat and health sweep bump `workers.version` only when `health`, `current_inventory_version` or `ready_at` changes (admin actions bump it as before). Red first: `test_steady_heartbeats_keep_the_worker_etag`; documented in docs/cli.md | fixed in round 2 |
| B15-R09 | defect (B10), fixed | A renew rejected with 409 after cancel/disable/reaper revocation stayed pending in the worker journal forever, so reconciliation of that attempt never drained and the worker could not become reconciled (blocks `enable`, B15-R07) | After a verified cleanup ACK the worker discards the attempt's pending renew operations (`WorkerState.discard_released_renewal`, `agent._discard_released_renewals`); tests in `tests/worker/test_control_flow_b15.py` | fixed in B15, open for review |
| B15-R10 | defect (B11), fixed | A runner self-stop (`STOPPED{RUNTIME_LIMIT}` or `STOPPED{LEASE_DEADLINE}`) was reported as `INTERNAL/WORKLOAD_EXIT_NONZERO`, so a runtime limit was not `TIMEOUT` and a lost-authority stop was not retryable (C8/C9 would be mislabelled) | `_STOP_FAILURES`: RUNTIME_LIMIT → `TIMEOUT/RUNTIME_LIMIT_REACHED`, LEASE_DEADLINE → `INFRASTRUCTURE/RUNNER_UNAVAILABLE`, FAILURE after a rejected checkpoint control → `INTERNAL/CHECKPOINT_PROTOCOL_ERROR`, other → `INTERNAL/WORKLOAD_EXIT_NONZERO`. Limit: the worker cannot tell a startup-limit stop from a runtime-limit stop, both are `RUNTIME_LIMIT_REACHED`. **Amendment (Docker run 4, C8):** the first fix still sent the CONTAINER observation with `runtime_limit_reached=false`; the server (`fail_attempt`) rejects a failure class its observation contradicts, so every `/fail` got 409, renewals stopped and the reaper fenced the attempt `LOST/LEASE_EXPIRED` → retry (job `01a0df6b-4907-…`, reaped 20:33:58.517). The worker unit fake did not apply that server check. Fix: `_execution_failed` sets `runtime_limit_reached` exactly when the failure is `TIMEOUT/RUNTIME_LIMIT_REACHED`; the B15 worker test helper `failures()` now asserts the server's observation consistency rule (red before the fix: 2 tests), and both images were rebuilt | fixed in B15, open for review |
| B15-R11 | defect (B10/B11), not fixed | A container that dies after the start callback ACK but **before** the runner accepts `SET_AUTHORITY_DEADLINE` is not observed: the start path keeps retrying the deadline (`RunnerControlError`) for the 30 s startup window and reports `TIMEOUT/STARTUP_TIMEOUT` (non-retryable) instead of the observed exit `INFRASTRUCTURE/RUNNER_UNAVAILABLE` (retryable). Seen in Docker runs 1–2 when the C5 kill landed in that window (job `01a0df5c-7535-…`: ATTEMPT_STARTED 20:16:22.543, ATTEMPT_FAILED STARTUP_TIMEOUT 20:16:53.049) | On `RunnerControlError` in the start path call `_observe_container_exit` and map with `container_exit_failure` (as `_result_once` does for adopted attempts). Changing the fenced B10/B11 start path is outside B15; the C5 harness now waits for the workload process itself before the kill | open |
| B15-R12 | defect (B15 × B10), fixed | The server reported `claim_state=CLAIMED` for every attempt not in `CREATED`. B15 is the first code that fences an offer the worker never claimed (reaper `LOST`, cancel/disable `STOPPING`), so the reconciliation item became `REVOKED`+`CLAIMED` with no container and no local record; the worker's `REVOKED`+`UNCLAIMED` tombstone branch never matched, reconciliation stayed incomplete, the worker stayed `STARTING` forever and the allocation stayed `QUARANTINED`. Seen in Docker run 5 C5a (job `01a0df79-9a7e-…`: CFP attempt 2 dispatched 20:48:18.111, never claimed, reaped 20:49:04.055) | `_reconciliation_item` derives `UNCLAIMED` from `attempts.claimed_at IS NULL` (set only by the claim transaction). Test `test_reaped_unclaimed_offer_stays_unclaimed_for_tombstone_cleanup` (red: `CLAIMED`) then NO_CONTAINER cleanup → `RELEASED` | fixed in B15, open for review |
| B15-R13 | defect (B15 × B10), fixed | `WorkerService._can_be_ready` (B10 heartbeat READY rule) returned False for every unreleased allocation whose Job had `desired_state != RUNNING`. B15 adds live authority under desired `PAUSED` (a PAUSING attempt and every `CHECKPOINT_FOR_PAUSE` offer), so after a CFP dispatch the next heartbeat set the worker `STARTING`, every poll got `409 state_conflict` ("not eligible to poll"), the offer was never claimed and the reaper fenced it `LOST/LEASE_EXPIRED` → a second retry. It was intermittent because a worker poll (1 s) that ran before the next heartbeat (5 s) still claimed the offer. Docker run 5 C5a (job `01a0df79-9a7e-…`, dispatch 20:48:18.111, reaped 20:49:04.055) and run 7 C5a (`retry_count` 2; full worker log: polls 409 every 5 s from 21:19:58 to 21:20:50, READY again right after the NO_CONTAINER tombstone of the reaped offer at 21:20:46). Contract internal-interfaces.md:298–300 makes READY depend on resolved pages/identities, and keeps a current-incarnation `UNCLAIMED` allocation eligible for the first poll; it has no desired-RUNNING condition | `_can_be_ready` accepts desired `RUNNING` or `PAUSED`; every lease/grant/incarnation/container check stays. A cancel always revokes the lease, so desired `CANCELLED` still blocks READY. Tests `test_worker_with_a_dispatched_checkpoint_for_pause_offer_stays_ready_to_claim_it` (heartbeat → READY, poll returns the CFP offer) and `test_worker_running_a_pausing_attempt_stays_ready` (red: `STARTING`). The Docker harness now saves the full worker log (`NEXA_B15_WORKER_LOG_OUT`), which located this after run 5's log tail only showed the B15-R12 period | fixed in B15, open for review |
| B15-R14 | defect (B14 × B09/B10), fixed | Docker run 9 C8 ended `INTERNAL/CHECKPOINT_PROTOCOL_ERROR` instead of `TIMEOUT/RUNTIME_LIMIT_REACHED` (attempt `01a0dfae-9dec-…`: checkpoint cycles 1–2 committed, cycle 3 reserved 21:46:26.13, runtime limit ≈21:46:27.6, `/fail` 21:46:28.70). Two combined defects. **(1) Worker:** after the runner rejected a frame-driven checkpoint control because it was stopping, the checkpoint frame stayed `pending_execution_message` and was reprocessed each loop, so `_ipc_once` never reopened the channel and the `STOPPED{RUNTIME_LIMIT}` frame stayed unread. **(2) Runner:** `serve_control` returned as soon as its state was `STOPPED`, even when no connection had received `STOPPED`, so the container exited 0 with the cause unsent. A probe with the real runner in the B14 worker harness stopped the runner after k alternating `_result_once`/`_ipc_once` steps: k=0 and k=5 → TIMEOUT; k=1–2 → no failure ever (stall), or `INTERNAL/RUNNER_PROTOCOL_ERROR` once the container exits; k=3–4 → TIMEOUT, or RUNNER_PROTOCOL_ERROR once it exits. The exact CPE path in run 9 (the checkpoint read by `docker exec tar` from a container that is shutting down) was not reproduced deterministically | Worker: a checkpoint frame received after `checkpoint_rejected` is committed without processing (`_result_attempt`), so the channel reopens and `STOPPED` names the cause. Runner: after `STOPPED` it keeps serving until one connection has been sent every pending frame, bounded by `timeout_seconds` (30 s); a connected peer still sees the container stop right after `STOPPED` (B09 runner-stop bounds unchanged). Red tests: `test_checkpoint_frame_rejected_by_a_stopping_runner_lets_the_stop_frame_through` (red: no failure), `test_stopped_runner_serves_until_a_worker_receives_its_stop_frame` and `test_stopped_runner_without_a_worker_stops_serving_after_its_bound` (red: serving ended at once). Images rebuilt. Residual limit: a `STOPPED` frame that arrives in the same batch as another unhandled frame is acked `INVALID` by the B10 recorder; the runner may then exit before a resend, and the worker reports `RUNNER_PROTOCOL_ERROR`  **Superseded by B15-R16:** the runner part of this fix (serve up to 30 s until every frame was *sent*) was wrong; see R16 | worker part fixed in B15; runner part replaced by R16 |
| B15-R15 | defect (B09), fixed | Docker runs 10–11 C3 (worker SIGKILLed past its lease): the reaper fenced the attempt, yet the runner never emitted `STOPPED` and never exited. A diagnostic run read the runner state inside the container: `state=STOPPING`, `stop_reason=LEASE_DEADLINE`, pending `CHECKPOINT_READY, PROGRESS, RESULT_PREPARE` with no `STOPPED`, and only the runner process was left. The C3 workload runs ≈40 s, so it finished before the lease deadline while the worker was dead, and the supervisor reported `EXIT 0` and closed its socket. At the deadline `stop_workload` sent `TERM` to that closed socket; the resulting `BrokenPipeError` was caught by the watchdog, which set `STOPPING` and returned, so the runner served forever with no stop cause. Compute had already ended (no overlap), but the stop frame and the container exit were lost. An isolated repro with the workload still running (B09 harness, `docker exec` client SIGKILLed) stopped correctly, which rules out the channel and the probe | `stop_workload` skips `TERM` once the supervisor has reported its exit, and ignores a failed `TERM` delivery; only the supervisor's exit report still confirms the stop (unchanged fail-closed wait). Red test `test_deadline_after_the_supervisor_reported_exit_still_emits_the_stop_frame` (red: `BrokenPipeError`). Images rebuilt; a C0+C3 diagnostic run then saw `STOPPED{LEASE_DEADLINE}` and C3 passed. Not changed: a watchdog stop that fails for any other reason still leaves the runner `STOPPING` until the worker's reconcile removes the container (B09 behavior) | fixed in B15, open for review |
| B15-R16 | defect (B15-R14 runner design × B10 control connections), fixed | Two failures traced to the R14 runner change. **(1) Docker run 12 C8** ended `INTERNAL/RUNNER_PROTOCOL_ERROR` instead of `TIMEOUT/RUNTIME_LIMIT_REACHED` (attempt `01a0dfeb-32e5-…`). Worker log: checkpoint cycle 3 reserved 22:52:34.26, renewal 22:52:37.01, checkpoint artifact upload 37.45, `/fail` 39.00. The runner stopped for its runtime limit during the cycle. The renewal's control connection (`RunnerControl._receive_ack`) was sent every pending frame, `STOPPED` included, and dropped all but its own ACK. R14 treated *sent* as delivered, so the runner exited 0 and the worker mapped the exit to `RUNNER_PROTOCOL_ERROR`. **(2) B09 gate regression:** the 30 s linger broke `test_watchdog_stops_cpu_after_controller_disconnect_at_runtime_limit` (the container must stop ≤7 s after a controller disconnect). A rerun on the R15 images gave 1 failed, 1 passed | Runner: after `STOPPED` it serves at most `STOP_FRAME_LINGER_SECONDS` = 3 s, and exits earlier once an ACK other than `INVALID` names the `STOPPED` sequence. Worker: every connection (IPC, renewal/adopt/start deadline, result/checkpoint control) keeps a runner `STOPPED`/`FAILED` frame through `_record_runner_message` and ACKs it. A terminal frame behind an unfinished one (sequence gap) is journaled apart as `pending_terminal_message` (`OUT_OF_ORDER`); a different second one is `INVALID`. Once the container exit is proven, `_result_attempt` uses the kept terminal frame instead of the exit code. Red tests: `test_stopped_runner_serves_until_a_worker_acknowledges_its_stop_frame` and `test_stopped_runner_without_an_acknowledgment_stops_serving_after_its_linger` (red: the runner exited on the first send). Also `test_stop_frame_seen_only_by_a_renewal_names_the_cause_after_the_runner_exits` (red: `KeyError` on the kept frame; with only the exit-path change reverted it reproduces run 12's `INTERNAL/RUNNER_PROTOCOL_ERROR`) and `test_terminal_frame_behind_a_pending_frame_is_kept_once` (red: second frame not `INVALID`). Written after the implementation: `test_control_connection_keeps_only_a_terminal_frame_before_its_ack`. B09 `test_supervisor_registration_after_startup_deadline_never_launches_cpu` now ACKs `STOPPED` like the worker and waits for the container stop; its no-launch assertions are unchanged. Without the ACK the runner lingers 3 s and the late `docker exec` succeeds against the stopped runner. Images rebuilt; B09 `test_real_runner.py` 9 passed. Residual: a stop while no worker connection opens within 3 s still ends as `RUNNER_PROTOCOL_ERROR` (the IPC loop polls every 0.25 s, renewal every 5 s) | fixed in B15, open for review |
| B15-R17 | defect (B15-R09 scope × B10 failure replay), fixed | Docker run 13 C9 (worker network disconnected during renewals): the runner stopped itself at its deadline and the reaper fenced the attempt (`LEASE_EXPIRED`), as required. While disconnected the worker had journaled `/fail INFRASTRUCTURE/RUNNER_UNAVAILABLE`. After the reconnect, reconciliation proved the exact stopped container and the cleanup was verified (23:27:34.02), but the pending failure was then sent on every reconciliation pass and rejected `409 state_conflict` ("Failure authority is stale", lease revoked) from 23:27:29 until the test timed out. Each rejection aborted `reconcile_once`, the worker never became reconciled and attempt 2 was never claimed within 120 s. B15-R09 discarded only pending renewals after a verified cleanup. | After a verified cleanup ACK the worker also discards that attempt's pending failure (`WorkerState.discard_released_authority`, renamed from `discard_released_renewal`; `agent._discard_released_authority`): the server accepts cleanup only after the lease is revoked and accepts a failure only while it is live, so the failure can never be accepted. The reconciliation replay loop skips a callback that an earlier verified cleanup in the same pass discarded. The server still rejects a stale failure (no contract change). Red tests first: `test_failure_rejected_after_the_reaper_revoked_the_lease_ends_with_verified_cleanup` (reproduced the endless 409) and `test_cleanup_replayed_before_a_stale_failure_discards_it_during_reconciliation` (KeyError). Images rebuilt (CPU `823af64e…`, worker `acf90130…`). | fixed in B15, open for review; Docker run 14 passed |
| B15-R18 | efficiency (B15), fixed | Found by the leader-probe EXPLAIN at 100k (not by a test). `retention.expired_records` joined `jobs` to `idempotency_records`. On a backlog with no terminal Jobs the planner estimated one terminal Job and read `jobs` by `Seq Scan` (24.071 ms at 100,000 Jobs), and the leader runs the sweep every second. A first fix (correlated scalar subquery for the Job state) removed the scan but took 1,097 ms. `expires_at < clock_timestamp()` is volatile, so it is not an index bound, and the scan filtered every entry of the partial sweep index. | The Job state is read by a correlated scalar subquery (not flattened into a join), and the expiry bound is the stable `now()` (transaction start). An earlier instant can only keep more records, never delete one early. Plan: Index Scan `ix_idempotency_records_b15_sweep`, then `pk_jobs` per candidate, 0.217 ms (`b15-reaper-sweep-explain-before-r18.json`, `b15-reaper-sweep-explain.json`). No red test. On the small test tables the planner prefers sequential scans whatever the query, so the evidence is the 100k probe. `tests/integration/test_retention_b15.py` (3) stays green. Residual: expired records of Jobs that are still live are rescanned each second until those Jobs end. The lease reaper keeps `clock_timestamp()`, because lease expiry needs the current DB time and `attempt_leases` holds only live leases (Index Only Scan, 0.064 ms) | fixed in B15, open for review |
| B15-R19 | contract defect (B10/B14 adopt route), fixed for adopt; rest open | Found by the final Docker regression (B14 S6 failed twice in a row; it passed in the regression before B15-R18). The worker was killed after it journaled an open checkpoint reservation. The next incarnation's adopt returned 200, then every `reconcile_once` raised `RuntimeError` ("adoption reservation snapshot mismatch"), so the worker never became READY. The kept journal showed the same reservation with `reserved_at` `…38.694Z` (reserve answer, a stored `json_wire_value` body returned as `JSONResponse`) and `…38.694000Z` (adopt answer, a dict re-serialised by Pydantic through `response_model=AdoptResponse`). `reconcile_adoption` compares the dicts exactly and fails closed. The B14 test compared only three keys, and S6 usually killed the worker before it journaled the reservation (the `held is None` branch matches by callback ID), so the defect was latent. The 6-digit form breaks `docs/contracts.md` (timestamps have exactly three millisecond digits). The nine other routes that return a dict through a `response_model` with timestamps (worker heartbeat, poll, renew, reconciliation and fail; job list, job result and logical session; artifact list) re-serialise the same way (cleanup already returns `JSONResponse`); no worker check compares their timestamps with a journaled value. | Adopt returns the stored wire body as `JSONResponse` (`routes_worker.py`); red test `test_adopt_transfers_the_reservation_exactly_as_reserved` (transferred reservation equals the reserve answer; three-digit timestamps). After the fix, the Docker regression passed (15 passed, 1 skipped, S6 included) and B15 run 17 passed. For the other routes, propose one shared serialisation fix in a later task (they span B05–B14 approved routes and their tests). | adopt fixed in B15; the other nine routes fixed in round 2 by B15-R29 |
| B15-R20 | defect (B15 pause × runner), fixed in round 2 | Review R20: a pause (or a CFP start) whose `REQUEST_CHECKPOINT PAUSE` reached the runner before the workload's first `state.json` write failed the attempt (`ProtocolError` → FAILED) instead of pausing | The runner keeps such a request as `checkpoint_waiting` (journaled, replay-safe) and stages it once the state exists; it fails closed only at the request's own checkpoint deadline, or if the state that appears is invalid. Another request while one waits is rejected, and a staged result waits behind it. The watchdog polls it without taking the lock when nothing waits. Runner U (red first): `test_pause_request_before_the_first_state_write_waits_for_it`, `test_waiting_request_replays_and_survives_reload`, `test_another_request_while_one_waits_is_rejected`, `test_state_that_never_appears_fails_at_the_checkpoint_deadline`, `test_invalid_state_that_appears_later_still_fails_closed`, `test_result_staged_while_a_request_waits_is_deferred`. Worker U: `test_checkpoint_for_pause_before_the_first_state_write_pauses`. Worker + PG: `test_checkpoint_for_pause_before_the_first_state_write_ends_paused` (real agent, real runner and `WorkerApiClient` against the in-process API on PostgreSQL; only Docker is faked; the state write is delayed 5 s after the pause request). Mutation: with the wait replaced by the old immediate staging, that test fails (`RunnerState.STOPPED is not RUNNING`); restored, it passes | fixed in round 2, open for review |
| B15-R21 | defect (B15 pause × fail path), fixed in round 2 | Review R21: after the pause checkpoint committed (attempt `STOPPING`), `/fail` (e.g. TIMEOUT or OOM) was accepted and moved the job to RECOVERING instead of ending it PAUSED | Server: `fail_attempt` answers `409 stale_authority` "Attempt is already stopping" for a `STOPPING` attempt (SM:75); its cleanup ends the pause (SM:23). Worker: once the pause checkpoint is committed, any end of the workload (container exit, runner `STOPPED`/`FAILED`, or the pause deadline) goes to forced stop + cleanup, never `/fail`. PG (red: RECOVERING): `test_failure_after_the_pause_checkpoint_committed_is_stale_and_cleanup_pauses` (TIMEOUT and OOM → `stale_authority`, job PAUSED, attempt `CANCELLED/PAUSE`, retry 0). Worker U (red: `/fail` sent): `test_container_exit_after_the_pause_checkpoint_committed_pauses`, `test_runner_stop_after_the_pause_checkpoint_committed_pauses`, `test_unconfirmed_pause_stop_after_the_checkpoint_committed_is_forced`. B13 test `test_coordinator_b13.py:1383` updated (it failed a STOPPING attempt). C4 now uses a barrier (below) | fixed in round 2, open for review |
| B15-R22 | defect (B15 drain), fixed in round 2 | Review R22: poll refused a `DRAINING` worker, so an offer committed before the drain was never claimed and was later reaped (`ATTEMPT_LOST`, retry consumed) | Both poll implementations (`execution_service.py`, `worker_service.py`) refuse only `DISABLED`; dispatch alone enforces "no new allocation" while draining (SM:103). PG (red first): `test_drain_keeps_a_committed_offer_pollable_until_it_succeeds` (dispatch → drain → poll → claim/start → SUCCEEDED, no `ATTEMPT_LOST`, retry 0) | fixed in round 2, open for review |
| B15-R23 | test gap (reaper CAS), closed in round 2 | Review R23: no test showed that a renew or cancel committed between the reaper's probe and its lock keeps the attempt | `test_renew_committed_after_the_probe_keeps_the_attempt` and `test_cancel_committed_after_the_probe_is_not_reaped` (probe, then the competing commit, then `reap_lease_locked`). Mutations of `reaper.py:86-89`: dropping the `expires_at` conjunct fails the renew test; dropping the `revoked_at` conjunct fails the cancel test; the file was restored byte-identical | closed in round 2 |
| B15-R24 | evidence (ACC-22) | Review R24: ACC-22 OOM/log flood was `not-run` without citing existing evidence | Matrix now cites B09 real-container OOM and log bound, B14 S2/S5b/S6, B11 real kills, and the OOM → no-retry chain in U and P; B15 itself adds no OOM/log fixture | updated in round 2 |
| B15-R25 | defect (B15 migration), fixed in round 2 | Review R25: jobs terminal before 0019 have `terminal_at` NULL, so the sweep never extended nor deleted their records correctly | 0019 sets `terminal_at = updated_at` for terminal jobs without one and shifts each swept record's `expires_at` by `terminal_at − created_at`. PG: `test_b15_upgrade_backfills_retention_of_jobs_terminal_before_it`; docs/database.md | fixed in round 2, open for review |
| B15-R26 | defect (B15 coordinator), fixed in round 2 | Review R26: one lease that failed (e.g. its job locked past `lock_timeout`) aborted the whole reaper batch and the tick | Per-lease failure is logged `coordinator_reap_lease_failed` and skipped (it stays due for the next probe); `LeadershipLost` still propagates. Each maintenance step (reap, promote, sweep) is isolated in the tick, so dispatch still runs. PG (red: 55P03 aborted the reaper): `test_one_failing_lease_does_not_stop_the_reaper_or_the_tick`; `test_coordinator_tick_runs_the_reaper` | fixed in round 2, open for review |
| B15-R27 | defect (B10 callback lock order), fixed in round 2 | Review R27: `_callback` inserted the receipt (FK `KEY SHARE` on `workers`) before `_worker_auth` took `workers FOR UPDATE`; two callbacks of one worker deadlocked on the lock upgrade | `_callback` locks the worker row `FOR UPDATE` right after the policy row and before the receipt insert. Red first (`DeadlockDetected`): `test_two_callbacks_of_one_worker_serialise_without_a_deadlock`. B10 tests that pinned the old order were renamed and updated to policy → worker → receipt: `test_heartbeat_policy_lock_precedes_worker_and_receipt`, `test_policy_lock_precedes_worker_and_receipt_for_adopt_and_renew` | fixed in round 2, open for review |
| B15-R28 | defect (B15 CFP), fixed in round 2 | Review R28: a rejected manifest of a CHECKPOINT_FOR_PAUSE attempt was treated like a normal pause abort; a CFP attempt cannot run on, so it stayed PAUSING until the pause deadline | Server keeps the job PAUSING on a rejected CFP publish; the worker records `pause_rejected` and fails the attempt at once `INTERNAL/CHECKPOINT_PROTOCOL_ERROR` (non-retryable → FAILED, retry not consumed). Worker U: `test_rejected_checkpoint_of_a_checkpoint_for_pause_attempt_fails_it_at_once`; PG: `test_rejected_checkpoint_for_pause_checkpoint_fails_without_a_retry` | fixed in round 2, open for review |
| B15-R29 | contract defect (B05–B14 routes), fixed in round 2 | Review R29 and the open part of B15-R19: nine routes re-serialised through `response_model` and wrote six fractional digits | One helper `api/http.py` `wire_response` validates like `response_model` and writes timestamps with `json_wire_value` (exactly three digits); used by the nine routes (worker heartbeat, poll, renew, reconciliation, fail; job list, job result, logical session; artifact list) and adopt. Red first on the four checked routes: `test_renew_and_read_routes_write_three_digit_timestamps`. B08 test `test_jobs_b08.py:747` updated (it asserted the six-digit value) | fixed in round 2, open for review |
| B15-R30 | contract drift (B10/B11), fixed in round 2 | Review R30 (was B15-R03): stale fence, stale incarnation, revoked/expired lease or changed desired state answered `409 state_conflict` | These answer `409 stale_authority` (SM:59, CR:70) in `_authority_rows`, `_live_authority`, `fail_attempt` and the incarnation guard. PG: `test_stale_fence_incarnation_and_lease_answer_stale_authority`; admin and B10 authority test assertions updated from `state_conflict` | fixed in round 2, open for review |
| B15-R31 | docs | Review R31: C4 row contradiction, README:251/:318, database.md fence wording, CLI help "Retry a terminal job", R04 claim, 5.E reaper locks | All corrected: README status and next step; database.md says leftover-authority paths keep the fence; CLI help "Retry a FAILED job as a new job and session."; R04 claim withdrawn (row above); 5.E rewritten from `reaper.py`; C4 rows rewritten for the barrier | fixed in round 2 |
| B15-R32 | residual of B15-R04 | Retry promotion to `QUEUED` still takes `KEY SHARE` on the submitter's `users` row: the B13 `queue_submitters` trigger inserts the (tenant, submitter) row, whose FK points at `users`. So a promotion can still wait on a request transaction that holds that `users` row `FOR UPDATE`; if that request then waits for the job, the cycle ends by deadlock detection or the 1 s coordinator `lock_timeout`. The failed promotion is logged and retried on the next probe (B15-R26), and the job stays `RETRY_WAIT`. Dispatch and the retry block path no longer wait (tests above) | Not changed: it needs a change to the approved B13 trigger/FK. Proposal for a later task: take the `queue_submitters` row lock from the coordinator before the job, or drop the FK to `users` | open |
| B15-R33 | observability (review L5) | Stale callback rejections (`409 stale_authority`) and worker reconcile outcomes are not persisted as events; `RECOVERY_EVENT_TYPES` (`schema_v16.py:12-27`) lists what the recovery-event read API returns | Documented here and in Limits: those outcomes appear in the worker log and the API response only. Persisting them needs new event types and a contract change | documented, open |
| B15-R34 | interpretation (review F9) | A cancel of a job whose attempt the reaper already fenced `LOST` finishes with cleanup; the `LOST` attempt stays `LOST` (terminal attempt states are immutable, SM:77/78), and the job ends `CANCELLED` | Implemented as described and pinned by `test_cancel_of_a_reaped_job_keeps_the_lost_attempt_terminal` | interpretation, open for review |
| B15-R35 | information leak (review F8), fixed in round 2 | Review F8: `_authority_rows` answered "Job cancellation is committed" before the worker identity check, so another worker could learn that a job was cancelled | The cancel answer requires the attempt's own worker; any other worker gets the generic stale-authority answer. PG: `test_another_worker_cannot_learn_that_a_job_was_cancelled` | fixed in round 2, open for review |
| B15-R36 | defect (review L1/L2), fixed in round 2 | L1: the reaper's leftover branch and admin disable appended `LEASE_REVOKED` and bumped the version of a SUCCEEDED job. L2: `revoke_leftover_authority` (`job_recovery.py`) left an ACTIVE result reservation, unlike `fence_attempt` | `LEASE_REVOKED` for leftover authority keeps the job version (`keep_version=True`, ETag unchanged); `revoke_leftover_authority` abandons the leftover result and checkpoint reservations. PG: `test_reaper_abandons_the_leftover_result_reservation_of_a_terminal_job` and the version assertion in the existing leftover tests | fixed in round 2, open for review |
| B15-R37 | defect (review L3, B06 idempotency × B15 sweep), fixed in round 2 | L3: if the sweep deleted a record between `INSERT … ON CONFLICT DO NOTHING` and `SELECT … FOR UPDATE`, `begin_idempotency` raised `NoResultFound` (500) | The insert is retried up to three times when the conflicting row is gone (the key is free again). Red first (`NoResultFound`): `test_record_swept_between_conflict_and_lock_is_inserted_again` in `test_idempotency_b06.py` | fixed in round 2, open for review |
| B15-R38 | migration gaps (review L4), fixed in round 2 | L4: downgrade narrowed the audit reason to 128 without a precheck; the CFP precheck ran without a table lock; offline downgrade SQL was untested; metadata parity compared index names only; the downgrade test did not assert the dropped index and restored length; the upgrade holds ACCESS EXCLUSIVE on `jobs` | Downgrade takes `LOCK TABLE jobs, audit_records IN ACCESS EXCLUSIVE MODE`, then refuses when a CFP job is queued or a reason exceeds 128. Tests: `test_b15_downgrade_refuses_to_narrow_a_long_audit_reason`, `test_b15_offline_downgrade_sql_locks_prechecks_and_restores`, `test_b15_metadata_matches_migrated_indexes` (compares predicates); the downgrade test asserts `ix_events_b15_recovery` is dropped and the length is 128 again. Mutations: dropping `CHECKPOINT_RESTORE_UNAVAILABLE` from the migration's `ix_events_b15_recovery` predicate fails the parity test (`AssertionError: ix_events_b15_recovery`), and removing the audit-reason precheck fails the downgrade test; the file was restored. The upgrade lock is inherent to rebuilding the queue indexes and CHECK; docs/database.md says 0019 runs offline | fixed in round 2 (upgrade lock documented), open for review |
