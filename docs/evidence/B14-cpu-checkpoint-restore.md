# B14 CPU checkpoint/restore evidence (manifest, provenance, fallback)

Status: **B14 đã triển khai, chờ Task Review.** This file is not an approval.
Round 2 (after review round 1) fixed B14-R01, R06, R07, R08 and R09 with
closure tests. After Docker Desktop was restarted, the 59 previously `blocked`
PostgreSQL items ran again (58 passed, 1 skipped: opt-in B10 Docker test), both
images were rebuilt from the final source, the 43 PostgreSQL B14 items passed,
and Docker scenarios S1–S6 passed with the new CPU digest
`sha256:e0e6222e…b6f`. All six recovery results are byte-identical to R0 (see
[Docker scenarios](#docker-scenarios-timelines-and-checksums)). The full
PostgreSQL suite (1238 passed, 16 skipped) and the B09/B10/B11 Docker
regression on the new images also passed.

Evidence chạy trên Docker Desktop Linux VM, không phải bare Linux/GPU; không
thay ACC-22 đầy đủ (B15) hay load/soak (B22).

## Kế hoạch

Written before the code and updated to what was actually built. Deviations
from the first plan are marked **(changed)**.

### Context map

| Requirement | Contract | Code before B14 (dd1f566) | Gap closed by B14 |
|---|---|---|---|
| Reserve checkpoint id/sequence (A) | `workerReserveCheckpoint`, database.md `checkpoint_reservations` | table, partial unique `RESERVED` per attempt, `UNIQUE(job_id, sequence)`, `jobs.checkpoint_sequence`; no route | service, route, receipt replay |
| Publish checkpoint (B) | `workerPublishCheckpoint`, `CheckpointManifest`, artifacts.md | `checkpoints` table, deferred source trigger, owner `CHECKPOINT` references; no route | closed canonical validator, fenced publish, 422 → `REJECTED` |
| Retention ≥2 (C) | PLAN §7, artifacts.md | no GC/prune path; B07 cleanup touches staging only | proof tests (GC is B19) |
| Restore selection (D) | `ExecutionContext.restore_checkpoint` | claim always wrote `restore_checkpoint = null` | verification outside the transaction, CORRUPT marking, fallback |
| Worker cycle (E) | internal-interfaces.md checkpoint messages | protocol rejected checkpoint controls; `ResultFlow` overwrote the checkpoint reservation | journaled `CheckpointFlow`, adoption fix, restore download/mount |
| Runner/adapter (F) | trusted-runner.md, cpu-iterative v1 | step 0..N only, result-only runner | resumable state, runner checkpoint protocol |
| CORRUPT (G) | `CheckpointRecord.state` `COMMITTED`/`CORRUPT`; `checkpoints` immutable with `CHECK state='COMMITTED'` | no representation | insert-only `checkpoint_corruptions` (migration 0018, `schema_v15`) |
| Cleanup retry (H) | database.md retry rules | `RETRY_WAIT` only when `restart_safe` | committed checkpoint OR `restart_safe` |
| Promotion (I) | coordinator.md | nothing promoted `RETRY_WAIT → QUEUED` | leader-only bounded promotion |
| List checkpoints (J) | `GET /v1/jobs/{job_id}/checkpoints` | not routed | route + keyset pagination |
| CLI (K) | cli.md | B12 `job` group | `nexa job checkpoints` |

### Design decisions (as built)

- **A — reserve** (`checkpoint_service.py`, `routes_worker.py`). One
  transaction through the B11 callback helper: worker bearer, CallbackId
  receipt, `FOR UPDATE` Job → Attempt → lease → allocation → grant. It checks
  live authority against DB time, attempt `RUNNING`, Job and desired state
  `RUNNING`, no `recovery_intent`, template `checkpointable`, no open
  checkpoint reservation and no active result reservation (409, never
  auto-abandon). `sequence = jobs.checkpoint_sequence + 1` is written back in
  the same transaction; attempt `RUNNING → CHECKPOINTING`. The receipt keeps
  the 201 body, so a replay returns the same id and sequence. Reason: reuse the
  proven B11 receipt and lock order; the Job counter never reuses a sequence,
  even after `REJECTED`/`ABANDONED`.
- **B — publish.** A short read transaction resolves the reservation and
  artifacts. Blob bytes are read and validated outside any transaction: strict
  JSON (duplicate keys and non-finite numbers rejected), RFC 8785 canonical
  bytes, closed schema, `manifest_checksum` over the manifest without its own
  field, provenance and compatibility equal to the database values, and the
  CPU `state.json` equal to the manifest cursor. The commit transaction
  re-locks in the same order, re-checks authority, and inserts `checkpoints`,
  the `CHECKPOINT` references, the reservation `COMMITTED`, attempt
  `CHECKPOINTING → RUNNING`, event `CHECKPOINT_COMMITTED` and the receipt.
  Deterministic defects run a separate authority-checked transaction that
  sets `REJECTED`, returns the attempt to `RUNNING`, emits `CHECKPOINT_REJECTED`
  and answers 422. Wrong authority is 409 with no side effect; storage failure
  is 503 with no side effect. **(changed)** A worker whose new incarnation has
  not yet reported inventory (`current_inventory_version` NULL) gets 503
  `Retry-After: 1` instead of a false `CHECKPOINT_COMPATIBILITY_MISMATCH`.
- **C — retention.** No delete or prune path was added. Tests prove every
  published checkpoint stays referenced and that B07 staging cleanup leaves it
  alone. GC is B19.
- **D — restore selection** (`checkpoint_restore.py`, `execution_service.py`).
  Candidates are `COMMITTED` checkpoints of the Job without a corruption row,
  newest sequence first. Each is verified outside the claim transaction:
  blob size and checksum, canonical closed manifest, identity against the row,
  provenance against the Job, compatibility against the claiming worker. A
  corrupt candidate is marked in its own short transaction (insert
  `checkpoint_corruptions` + `CHECKPOINT_CORRUPT/<reason>`), and the next older
  one is tried. An incompatible candidate emits `CHECKPOINT_INCOMPATIBLE`. The
  claim transaction re-checks that the non-corrupt candidate list is unchanged
  and read-locks the chosen artifacts (else 503, retry). It writes
  `restore_checkpoint = {record, manifest, files}`, which the B11 trigger keeps
  immutable; replay returns the stored context. Events:
  `CHECKPOINT_RESTORE_SELECTED/CHECKPOINT_RESTORED`; no valid candidate on a
  later attempt of a `restart_safe` Job gives
  `CHECKPOINT_FALLBACK_TO_INPUT`. **(changed in self-review, S1)** Every
  restore event appends to the Job event sequence with an audit row, but none
  changes `jobs.version`. Claim acknowledgment makes no Job version change
  (`state-machines.md`), so an `If-Match` taken before the claim stays valid. **(changed)** For a non-restart-safe Job
  with no valid candidate, the claim emits
  `CHECKPOINT_RESTORE_UNAVAILABLE` and `startAttempt` refuses with 409, so
  input is never replayed from step 0. The planned "fail inside claim"
  (Option Y) was dropped because the claim response must stay a normal
  `ClaimResponse`. **(changed in round 2, B14-R01)** The poll offer now fills
  its existing `checkpoint` field with the newest non-corrupt
  `CheckpointRecord` of the Job (before: always `null`). The worker journals
  whether a checkpoint was offered. If the claim then froze no restore and the
  template snapshot is not `restart_safe`, the worker fails the attempt
  `INCOMPATIBLE/CHECKPOINT_RESTORE_UNAVAILABLE` before any container is
  created, through the B11 NoContainerProof path. It never waits for a
  startup timeout. A worker that cannot verify or download a frozen restore
  fails the same way.
- **E — worker cycle** (`worker/checkpoint_flow.py`, `agent.py`,
  `result_flow.py`). The cycle state lives in the journal
  (`runner_state["checkpoint_flow"]`) and is written before every side effect:
  reserve callback → reservation → `REQUEST_CHECKPOINT` → file descriptors →
  upload keys → artifact ids → binding → `FINALIZE_CHECKPOINT_MANIFEST` →
  manifest artifact → publish callback. A monotonic timer opens a cycle every
  `checkpoint_interval_seconds` (5–60), only when no runner message is pending,
  no result flow is active and the attempt is not finishing. A restarted
  worker waits one full interval. Errors: reserve 409 → drop the cycle and
  wait a full interval; other reserve errors → retry the same callback;
  publish 409/503 → keep the reservation and retry the same callback; publish
  422 → disable checkpoints for this attempt while the workload keeps running;
  runner `INVALID` or protocol defect → fail the attempt
  `INTERNAL/CHECKPOINT_PROTOCOL_ERROR` (the B11 fail path abandons the open
  reservation). Adoption: `reconcile_adoption` accepts only an explained
  transferred reservation, and `ResultFlow` no longer overwrites it with `None`.
  **(changed in round 2, B14-R09)** A result reservation that the server
  committed before the worker journaled the answer is explained, and adopted,
  when the journaled `reservation_callback_id` equals the transferred
  reservation's `callback_id` and no different answer was journaled. Any other
  difference still refuses adoption. **(changed in round 2, B14-R08)**
  `WorkerApiClient.upload_artifact` checks the committed answer (a JSON object
  whose `kind`, `media_type`, `size_bytes` and `checksum` equal the descriptor,
  with a UUID `artifact_id`) and raises only `ValueError`. The checkpoint cycle
  maps it to `CheckpointProtocolError`, so only that attempt fails
  `INTERNAL/CHECKPOINT_PROTOCOL_ERROR` and is cleaned up; the result path maps
  it to `INTERNAL/INVALID_RESULT`.
  **(changed in self-review)** A leftover `restore-state.json` whose size or
  SHA-256 does not match the frozen descriptor is removed and fetched again
  (S5). Launch selection (image label, digest, restore download) runs inside
  the startup budget and failure handling, so a digest mismatch still fails
  with a `NoContainerProof` (S6). `ResultFlow` journals the completion request
  with its callback id and replays it without reading the stopped container
  again (S4).
- **E2 — container exit (added, not in the first plan)** (`execution.py`,
  `agent.py`, `state.py`). The crash → retry branch needs the worker to notice
  a workload container that died under a live worker. When the control relay
  fails and `docker inspect` shows the exact container stopped, the worker
  journals `container_exit` and then reports once with a `CONTAINER`
  observation: OOM → `OOM/CONTAINER_OOM`; exit 0 without a terminal frame →
  `INTERNAL/RUNNER_PROTOCOL_ERROR`; any other exit →
  `INFRASTRUCTURE/RUNNER_UNAVAILABLE`. It then cleans up the exact identity. A
  running container with a failing channel is not an exit. Pending renew
  operations of that attempt are discarded only after the failure is
  acknowledged and cleanup is verified.
- **F — runner/adapter** (`cpu_state.py`, `cpu_iterative.py`,
  `cpu_entrypoint.py`, `trusted_runner.py`). The adapter resumes from step k
  with `a_(k+1) = (a_k·1664525 + 1013904223 + k) mod m`. **(changed)** The live
  snapshot is `/output/state.json` (not `state.snapshot.json`). It is canonical
  RFC 8785 JSON, ≤4 KiB, bound to the input and spec checksums, written
  atomically (temp file, fsync, replace, mode 0640) at start, at most once per
  second on the 65,536-step stride, and at the final step. On
  `REQUEST_CHECKPOINT` the runner validates the snapshot and copies it to
  `checkpoint-<seq>-state.json` (0440, logical `state.json`, `CHECKPOINT_FILE`);
  after the binding and `FINALIZE_CHECKPOINT_MANIFEST` it writes
  `checkpoint-<seq>-manifest.json` and emits `CHECKPOINT_READY`. **(changed)**
  Restore is mounted read-only at the fixed path `/input/restore-state.json`
  (not `/input/restore/state.json`); the runner re-verifies it before the
  workload starts and never lets the cursor regress. **(changed in round 2,
  B14-R06)** The first progress frame of a resumed attempt reports the
  restored step (`step / iterations`), not step 0. **(changed in round 2,
  B14-R07)** Checkpoint copies get their mode through `fchmod` on the open
  descriptor, so a symlink in `/output` is never followed. `RESULT_PREPARE` waits
  while a checkpoint is open. The result stays the B09 v1 format, so a resumed
  result is byte-equal to R0. Launch spec v1 and B09 hardening are unchanged.
- **G — images and CORRUPT.** `checkpoint_corruptions(tenant_id,
  checkpoint_id PK, reason_code, detected_at)` **(changed: no `job_id` column;
  the Job comes through `checkpoints`)**. It has an FK to
  `checkpoints(tenant_id, checkpoint_id)` and an immutable trigger, in
  additive migration `20260926_0018` + `schema_v15`. The API derives
  `state = CORRUPT`. Reason: `checkpoints` is immutable with
  `CHECK state = 'COMMITTED'`, and changing that would edit a released
  migration. The CPU image adds only the label
  `io.nexa.runner.checkpoint=cpu-state-v1`; the executor enables the
  checkpoint launch only for an image with that label. The checkpointable
  template is a test fixture version, not a seed change.
- **H — cleanup** (`execution_cleanup.py`). Retry happens when the failure
  class is `INFRASTRUCTURE`, `retry_count < max_retries`, desired state is
  `RUNNING`, and the Job is `restart_safe` or has a `COMMITTED` non-corrupt
  checkpoint. Backoff is `min(30, 2^(retry-1))` s plus `randbelow(1001)` ms.
  One `retry_schedules` row is written, with `waiting_reason =
  waiting_for_retry`. Open checkpoint reservations become `ABANDONED`.
- **I — promotion** (`coordinator/retry.py`, `coordinator/service.py`). An
  unlocked, index-backed probe runs at most once per second. Then, under live
  leadership: policy → GLOBAL counter → Jobs (bounded batch of 16) →
  schedules. This is the same prefix as the B13 tick/cleanup order. Conditions
  are DB time ≥ `ready_at`, `RETRY_WAIT`, desired `RUNNING`, no
  `recovery_intent`, schedule open and `retry_number = retry_count`. Effects:
  close the schedule, set Job `QUEUED` with a new ready sequence, emit
  `RETRY_READY/BACKOFF_ELAPSED`, CAS on state/version. Outstanding counters do
  not change. A capability- or quota-infeasible Job stays `RETRY_WAIT` with a
  waiting reason and one `RETRY_BLOCKED` event. The tick promotes due retries
  before it decides.
- **J — listJobCheckpoints** (`routes_jobs.py`, `job_service.py`). Same
  tenant/membership/role/scope rule as the Job read route. Returns `COMMITTED`
  and `CORRUPT` records, because the enum has `CORRUPT` and users need to see
  why a restore fell back. No reason field is exposed, since the contract
  record has none. Keyset pagination is `(sequence desc, checkpoint_id desc)`,
  page ≤100. No reservation, staging or blob content is returned. Timestamps
  are serialized with exactly 3 ms digits.
- **K — CLI** (`cli/commands/job.py`). `nexa job checkpoints JOB_ID [--cursor]
  [--page-size] [--tenant]` follows the B12 list pattern. Done.

### Ordered tasks (status)

| # | Task | Main files | Tests written first | Status |
|---|---|---|---|---|
| 1 | Migration + `schema_v15` | `migrations/versions/20260926_0018_b14_checkpoint_corruption.py`, `schema_v15.py`, `schema.py` | `test_migrations.py`, `test_schema_metadata.py`, `test_checkpoint_corruption_b14.py` | done |
| 2 | Server reserve/publish | `checkpoint_validation.py`, `checkpoint_service.py`, `execution_artifacts.py`, `routes_worker.py`, `schemas.py` | `test_checkpoint_validation_b14.py`, `test_checkpoint_b14.py`, `test_openapi_b14.py` | done |
| 3 | Restore selection | `checkpoint_restore.py`, `execution_service.py` | `test_checkpoint_restore_b14.py` | done |
| 4 | Cleanup + promotion | `execution_cleanup.py`, `coordinator/retry.py`, `coordinator/service.py` | `test_retry_b14.py` | done |
| 5 | Adapter/runner | `cpu_state.py`, `cpu_iterative.py`, `cpu_entrypoint.py`, `trusted_runner.py`, `worker/protocol.py` | `test_cpu_state_b14.py`, `test_runner_checkpoint_b14.py` | done |
| 6 | Worker cycle + container exit | `checkpoint_flow.py`, `agent.py`, `execution.py`, `result_flow.py`, `dispatch.py`, `executor.py`, `docker_client.py`, `client.py`, `models.py`, `state.py` | `test_checkpoint_flow_b14.py`, `test_container_exit_b14.py` | done |
| 7 | Images | `deploy/cpu-iterative/Dockerfile` | label check in the Docker test | done: both images rebuilt from the final source in round 2 (see Revision) |
| 8 | List/CLI | `routes_jobs.py`, `job_service.py`, `cli/commands/job.py` | `test_checkpoint_b14.py` list test, `test_job_commands.py`, `test_api_route_contract.py`, `test_operation_matrix.py` | done |
| 9 | Docker test | `tests/docker/test_b14_checkpoint_restore.py` | — (the test is the scenario) | done: S1–S6 pass (round 2) |
| 10 | Docs | database.md, project-structure.md, coordinator.md, worker-agent.md, trusted-runner.md, worker-executor.md, artifacts.md, cli.md, README, ROADMAP | — | done |

TDD disclosure: most server, runner and worker changes were driven by a test
that first failed for the intended reason. Exceptions, written before or
together with their tests: parts of the worker cycle wiring in `agent.py`,
`listJobCheckpoints` in `job_service.py`, the CLI command, and two
acceptance-guard tests added at the end
(`test_pickled_state_is_rejected_and_no_source_module_deserializes_pickle`,
`test_checkpoint_cycle_logs_no_state_content`). The guards passed on first
run; they pin existing behavior and are not red-first. Self-review fixes S1
and S3–S7 were red-first. The observed red failure for each is in the
Self-review table. S2 and S8 correct the Docker test and docs, which could
not run in round 1, so they have no red run. Round-2 closure tests are checked
by mutation (Self-review, round 2).

## Revision and environment

- Baseline: `dd1f566` (B13) on `main`. B14 is the uncommitted diff on top.
  Tracked changes: `git diff --stat` = 39 files changed, 1976 insertions(+),
  240 deletions(-) (README, ROADMAP, the CPU Dockerfile, 8 docs, 23 source
  modules, 5 existing test files). There are 23 new untracked files, 11,875
  lines in total. They are this evidence file, `raw/B14-images.json`,
  `raw/B14-verification.json` and `raw/B14-docker-timeline.json`,
  migration `20260926_0018_b14_checkpoint_corruption.py`, the source files
  `checkpoint_restore.py`, `checkpoint_service.py`, `checkpoint_validation.py`,
  `coordinator/retry.py`, `schema_v15.py`, `worker/checkpoint_flow.py` and
  `workloads/cpu_state.py`, and 11 test files: `test_openapi_b14`,
  `test_checkpoint_validation_b14`, `docker/test_b14_checkpoint_restore`,
  `integration/test_checkpoint_b14`, `test_checkpoint_corruption_b14`,
  `test_checkpoint_restore_b14`, `test_retry_b14`, `worker/test_checkpoint_flow_b14`,
  `test_container_exit_b14`, `workloads/test_cpu_state_b14` and
  `test_runner_checkpoint_b14`. Nothing is committed, pushed or branched.
- Host: macOS 27.0 (26A428), Apple silicon. Docker Desktop (context
  `desktop-linux`), round 2 after the user restarted it: engine 29.8.0, VM
  `linux/arm64`, kernel `7.0.12-linuxkit`, 8 CPU, 4,106,604,544 B RAM.
  Round 1 ran on engine 29.5.3, kernel `6.12.76-linuxkit`.
- PostgreSQL 17.11 (`PG_VERSION=17.11-1.pgdg13+2`, image `postgres:17`,
  `sha256:f4c66b82…1232`) in the existing container `nexa_b13_pg` (loopback
  port 15439), guarded databases `nexa_b05_test_b14` (suite) and
  `nexa_b05_test_b14docker` (Docker scenarios). No evidence DB of a previous
  task was used or modified. No `nexa_b10_*` container was stopped or changed;
  `nexa_b10_smoke3-caddy-1` still runs and grows (1,000 PIDs at 20:11, 1,183
  at 20:26 +07), far below the 30,865 that blocked round 1.
- Python 3.12.13 in `.venv`; `uv` 0.9.27 at `/tmp/nexa-b12-uv-bootstrap/bin/uv`.
- CPU image, rebuilt in round 2 (2026-09-26 20:09:27–20:09:29 +07) from the
  final source: `nexa/cpu-iterative:b14` =
  `nexa/cpu-iterative@sha256:e0e6222eb899498c447eeee9f44ac053a62af271da2d1b95316b09117a999b6f`
  (image id is the same digest; linux/arm64, user 1000:1000, entrypoint
  `python -m nexa.workloads.trusted_runner`, label
  `io.nexa.runner.checkpoint=cpu-state-v1`, base
  `python:3.12-slim@sha256:2f17fc04…06a9`, 207,207,858 B). The image
  matches the final source: the 121 `.py` files under `nexa/` in the image
  (`/opt/nexa/nexa`) have the same paths and SHA-256 as `src/nexa` in the
  working tree, with none missing either way (checked at 20:42 +07, after the
  R06 mutation check restored `trusted_runner.py` byte for byte). No file
  under `deploy/` or `migrations/` changed after the build. It replaces the stale round-1 digest
  `sha256:e1ae46e237f1ff2c8d4db1d8f2e92f40bffa3c062c14ff2b78c611b4b9d94c3d`,
  which is not evidence. Old pre-B14 image:
  `nexa/cpu-iterative@sha256:341d943487940cb67f9e5ef61c2334593d8eab99619fc0f977e4f28a6d0e8e9a`
  (no checkpoint label). Details are in [raw/B14-images.json](raw/B14-images.json).
- Worker image, built in round 2 (20:09:37–20:09:45 +07):
  `nexa/b14-worker:local`, image id
  `sha256:c55c25da63cd2debffebff97008522364c292d348ee4c267305f97318a308b42`
  (linux/arm64, command `nexa-worker`, 427,690,280 B). Its installed
  package (`/opt/nexa/.venv/lib/python3.12/site-packages/nexa`) passes the same
  121-file SHA-256 comparison with `src/nexa`. It is a local tag only; nothing
  was pushed.

## Reproduction commands

`<guarded URL>` stands for `postgresql+psycopg://postgres:<password>@127.0.0.1:15439`.
The password comes from `PGPW=$(docker exec nexa_b13_pg printenv POSTGRES_PASSWORD)`,
URL-encoded, never printed. `NEXA_DATABASE_URL` is never set.

```sh
U=/tmp/nexa-b12-uv-bootstrap/bin/uv
$U sync --frozen --all-groups --no-editable
$U run --no-sync ruff check .
$U run --no-sync ruff format --check .
PYTHONPATH=src:. $U run --no-sync pytest -q
NEXA_TEST_DATABASE_URL=<guarded URL>/nexa_b05_test_b14 PYTHONPATH=src:. \
  $U run --no-sync pytest --run-postgres -q
# connection-flake reruns (same DB, only last-failed, no reordering):
NEXA_TEST_DATABASE_URL=<guarded URL>/nexa_b05_test_b14 PYTHONPATH=src:. \
  $U run --no-sync pytest --run-postgres -q --lf -p no:randomly

# Images (round 2)
BASE_IMAGE_REF=python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 \
  IMAGE_REF=nexa/cpu-iterative:b14 IMAGE_ARCH=linux/arm64 scripts/b09_build_image.sh
docker build --pull=false -f deploy/b10/Dockerfile -t nexa/b14-worker:local .

# Docker scenarios + B09/B10/B11 regression
NEXA_RUN_DOCKER=1 NEXA_B11_RUNTIME_EVIDENCE=1 \
  NEXA_B09_IMAGE_REF=nexa/cpu-iterative@sha256:e0e6222eb899498c447eeee9f44ac053a62af271da2d1b95316b09117a999b6f \
  NEXA_B11_WORKER_IMAGE=nexa/b14-worker:local \
  NEXA_B14_EVIDENCE_OUT=docs/evidence/raw/B14-docker-timeline.json \
  NEXA_TEST_DATABASE_URL=<guarded URL>/nexa_b05_test_b14docker PYTHONPATH=src:. \
  $U run --no-sync pytest --run-postgres tests/docker -q
```

`NEXA_B14_ITERATIONS` (default 250,000,000) sets the workload length. The B14
test runs the coordinator in-process; `test_b11_vertical.py` (the existing B11
harness, unchanged) passes the guarded test URL to its coordinator child process as
`NEXA_DATABASE_URL`.

Round 2 ran the same commands, split so that each part has its own result.
All used `-p no:randomly`, and `-p no:cacheprovider` except the `--lf` rerun:

```sh
# PostgreSQL B14 items (43)
NEXA_TEST_DATABASE_URL=<guarded URL>/nexa_b05_test_b14 PYTHONPATH=src:. \
  $U run --no-sync pytest -q --run-postgres -p no:randomly -p no:cacheprovider \
  tests/integration/test_checkpoint_b14.py tests/integration/test_checkpoint_restore_b14.py \
  tests/integration/test_checkpoint_corruption_b14.py tests/integration/test_retry_b14.py \
  tests/integration/test_migrations.py::test_b14_checkpoint_corruption_upgrade_is_additive_and_downgrades \
  tests/integration/test_migrations.py::test_b14_offline_sql_creates_insert_only_corruption_table
# Docker S1–S6 alone (writes the timeline), then B09/B10/B11 regression
# with the same variables minus NEXA_B14_EVIDENCE_OUT and with
# --deselect tests/docker/test_b14_checkpoint_restore.py::test_b14_cpu_checkpoint_crash_resume_corruption_and_adoption
... $U run --no-sync pytest --run-postgres -p no:randomly -p no:cacheprovider -rA -s \
  tests/docker/test_b14_checkpoint_restore.py
```

## Verification results (final source)

"Final source" is the tree after the last round-2 source edit, before the
`--lf` rerun at 20:07 +07. After that only the Docker test file (20:21, the
H1–H3 harness fixes), docs and evidence changed. `trusted_runner.py` was
reverted for the R06 mutation check at 20:41 and restored byte for byte; the
121-file SHA-256 comparison with both images confirms the content (see
Revision). The mutation window (20:41:38–20:41:41) fell inside the full
PostgreSQL run below. That run imported `trusted_runner` at collection (20:38),
and no test starts the runner from source in a subprocess, so it did not see
the mutated file. Times are 2026-09-26 +07.

| Command (see Reproduction) | When | Result | Status |
|---|---|---|---|
| `uv sync --frozen --all-groups --no-editable` | 20:46:47 | "Audited 41 packages in 16ms" | pass |
| `ruff check .` | 20:37 | "All checks passed!" | pass |
| `ruff format --check .` | 20:37 | "377 files already formatted" | pass |
| `pytest -q -p no:cacheprovider` (default, no PostgreSQL) | 20:37:21–20:37:55 | 900 passed, 354 skipped, 0 failed, 32.12 s | pass |
| `pytest -q --run-postgres --lf -p no:randomly` (the 59 round-1 `blocked` items; `.pytest_cache` kept) | 20:07:23–20:08:48 | 58 passed, 1 skipped (the opt-in B14 Docker test), 0 failed, 81.90 s | pass |
| CPU image build | 20:09:27–20:09:29 | `sha256:e0e6222e…b6f` | pass |
| worker image build | 20:09:37–20:09:45 | `sha256:c55c25da…8b42` | pass |
| PostgreSQL B14 items (43) | 20:12:55–20:13:44 | 43 passed, 0 failed, 47.16 s | pass |
| Docker S1–S6 | 20:21:30–20:31:07 | 1 passed, 568.50 s | pass |
| Docker B09/B10/B11 regression on the new images | 20:32:26–20:36:16 | 14 passed, 1 skipped (B10 opt-in, Linux host only), 1 deselected (S1–S6), 228.56 s | pass |
| full `pytest -q --run-postgres -p no:randomly -p no:cacheprovider` | 20:38:09–20:45:49 | 1238 passed, 16 skipped, 0 failed, 457.82 s | pass |
| `git diff --check` | 20:37, again 20:54 | no output | pass |
| rerun after the last doc/evidence edits: `ruff check`, `ruff format --check`, default suite | 20:54:16–20:54:50 | "All checks passed!", "377 files already formatted", 900 passed, 354 skipped, 0 failed, 32.51 s | pass |

The 16 skips of the full PostgreSQL run are the opt-in Docker tests (B09
real executor/runner 11, B10 1, B11 3, B14 1); they ran separately above.
The 59 `--lf` items are listed in [raw/B14-verification.json](raw/B14-verification.json)
(`round1.unconverged`); 12 of them are B14 PostgreSQL items. During round 2 PostgreSQL accepted every
connection, and `nexa_b10_smoke3-caddy-1` stayed between 1,000 and 1,307 PIDs.
`docker exec nexa_b13_pg postgres --version` answers
`PostgreSQL 17.11 (Debian 17.11-1.pgdg13+2)`. The Hypothesis property tests
(`test_cpu_state_b14.py`) run inside the default suite.

### Round 1 history (not final-source evidence)

Round 1 ran on the round-1 tree (last source edit 16:48:29 +07), with ruff
clean, the default suite at 885 passed / 353 skipped, and the PostgreSQL suite
below:

| Command | Result | Status then |
|---|---|---|
| `pytest --run-postgres -q`, round 0 (full suite, random order) | 1090 passed, 15 skipped, 50 failed, 83 errors, 292.36 s | connection failures |
| round 1 (`--lf -p no:randomly`) | 74 passed, 22 failed, 37 errors, 107.59 s | connection failures |
| rounds 2–6 (`--lf -p no:randomly`) | 0 passed, 59 errors each, about 11 s each | `blocked` (B14-R02) |

Round-1 PostgreSQL totals: 1164 passed (1090 + 74), 15 skipped, 59
`blocked`, 0 failures from code. After round 6 a probe opened one
connection per minute, 30 times from 17:02:22 to 17:31:45 +07. Every probe
failed the same way, so the 59 items never ran again. At 17:34 the server
still logged 120 `could not fork` lines in 2 minutes, and
`nexa_b10_smoke3-caddy-1` was still at 30,865 PIDs. The list of the 59 items
is in [raw/B14-verification.json](raw/B14-verification.json). 12 of them are
B14 items (below). The other 47 are B05–B13 items and the fixture of the
opt-in B14 Docker test, which, like the B11 vertical test, connects before it
skips.

Every one of the 133 round-0 failures and errors is
`psycopg.OperationalError: … server sent an error response during SSL exchange`.
The PostgreSQL server log shows the cause:
`could not fork new process for connection: Resource temporarily unavailable`,
355 lines in 5 minutes. Round 1 had two assertion messages, and both come from
a failed connection:
- `test_concurrent_same_key_commits_one_job_and_replays_one_snapshot` got
  `[202, 503]`: the API answers `503 dependency_unavailable` when its DB
  connection fails.
- `test_two_migration_runners_serialize_on_the_advisory_lock` reported that
  the runners "did not wait on the advisory lock", because one migration
  subprocess could not connect.

No failure had any other cause. After round 1 even `docker exec` failed
(`fork/exec /usr/bin/runc: resource temporarily unavailable`), with
`nexa_b10_smoke3-caddy-1` at 30,865 PIDs and all containers at 30,954.

The run before the self-review fixes (same suite, earlier tree) converged:
1045 passed / 14 skipped / 76 failed / 97 errors in round 0, then 5 connection
reruns, 1216 passed and 16 skipped in total, 0 failed. It is listed only to
show that these tests pass when connections work. It is not final-source
evidence. In that run one pre-existing test,
`test_constraints_artifacts.py`, left a backend `idle in transaction` holding
a lock. That backend was terminated in `nexa_b05_test_b14` only; no other
database was touched.

Hypothesis property tests (`test_cpu_state_b14.py`,
`test_resume_from_any_step_is_byte_equal_to_an_uninterrupted_run`) run inside
the default suite above. PostgreSQL version: `nexa_b13_pg` runs image
`postgres:17` = `sha256:f4c66b820c6f974249089d3d16d86a3698eae11e8746eb6644b2271031e91232`,
`PG_VERSION=17.11-1.pgdg13+2` (from `docker inspect` metadata; `docker exec`
could not fork).

## Docker scenarios, timelines and checksums

Status for every scenario: **pass** (round 2). One run of
`tests/docker/test_b14_checkpoint_restore.py` from 2026-09-26 20:21:30 to
20:31:07 +07: 1 passed in 568.50 s (call 565.52 s), guarded database
`nexa_b05_test_b14docker`. Inputs: CPU image
`nexa/cpu-iterative@sha256:e0e6222eb899498c447eeee9f44ac053a62af271da2d1b95316b09117a999b6f`
(linux/arm64), worker image `nexa/b14-worker:local`
(`sha256:c55c25da…8b42`), 250,000,000 iterations, checkpoint interval 5 s.
The coordinator runs in-process; the worker runs as a container. Every value
below is copied by script from
[raw/B14-docker-timeline.json](raw/B14-docker-timeline.json). Times are UTC
(local time is +07). The test collects and skips without `NEXA_RUN_DOCKER=1`.

Each scenario writes a timeline (IDs, states, reason codes and checksums
only; no credential, input or checkpoint content) with these separate fields:
coordinator epoch; worker incarnation (per attempt, per grant and per lease);
job fence (Job, attempt, lease, grant); attempt id/number/state/failure
class/reason; lease id, revoked, revoke reason; desired state; allocation
id/state/quarantined/release reason; Job state, `event_sequence`,
`checkpoint_sequence`, `retry_count`/`max_retries`; ordered events (sequence,
type, reason, ms time); submit idempotency key and record count; container
identity (short id, stopped, verified); checkpoint id + sequence + attempt +
corruption reason; reservations; retry schedules; restore selection per
attempt; ledger segments; result id and checksum. After each scenario the
test waits for allocation release (it commits after cleanup proof, which
follows `SUCCEEDED`) and then asserts: Job terminal, one final result, every
allocation `RELEASED`, no open reservation or grant, no running container for
the Job, and fairness scores non-decreasing.

| Scenario | Fault and injection point | Observed outcome | Status |
|---|---|---|---|
| S1 baseline R0 | none | 4 checkpoints (seq 1–4), no restore, `retry_count` 0, result R0 | pass |
| S2 crash-resume | `docker kill` of the workload after 2 committed checkpoints | attempt 1 `INFRASTRUCTURE/RUNNER_UNAVAILABLE`, `retry_count` 1; `RETRY_READY` (#8) before `CHECKPOINT_RESTORE_SELECTED` (#10); attempt 2 (fence 3) restores seq 2, the newest; result = R0 | pass |
| S3 corrupt newest | coordinator paused, first byte of the newest `state.json` blob flipped, then kill | seq 2 marked `CHECKPOINT_CORRUPT/CHECKPOINT_CHECKSUM_MISMATCH` once; seq 1 restored; result = R0 | pass |
| S4 corrupt all | every state blob flipped, then kill | seq 1 and 2 marked; `CHECKPOINT_FALLBACK_TO_INPUT`; restore none; result = R0 | pass |
| S5a kill before first checkpoint, then between | two kills | attempt 2 restore none (fallback to input); attempt 3 (fence 5) restores seq 1, the newest, written by attempt 2; `retry_count` 2 = `max_retries`; result = R0 | pass |
| S5b kill right after publish | poll 10 ms for a new `COMMITTED`, then kill | seq 1 committed 13:29:08.617, attempt failed 13:29:09.194; attempt 2 restores seq 1; result = R0 | pass |
| S6 worker SIGKILL during an open reservation | `docker kill` of the worker while a reservation is `RESERVED` | same container `37c9fe9619ac`, `retry_count` 0; grants for incarnations `…b3bc214e095a` and `…88dbad23a40d` (fence 1); lease and attempt now on `…88dbad23a40d`; the open reservation ended `COMMITTED`; result = R0 | pass |

A worker kill and restart is not a Job event, so S6 shows it through the two
grants and the changed lease/attempt incarnation, not in the event table.
Commits were 8–15 s apart with a 5 s interval: the next cycle is scheduled one
interval after the previous cycle ends (`checkpoint_flow.py`), and a cycle
(reserve, two uploads, finalize, publish) took about 3–10 s on this VM.

#### S1 baseline R0

Job `01a0dde1-0b55-7be6-a2f8-46e35d693914` ended `SUCCEEDED` (desired `RUNNING`): job fence 1, `event_sequence` 9, `checkpoint_sequence` 4, `retry_count` 0 of 2, coordinator epoch 1. Submit key `b14-docker-baseline` has 1 idempotency record. Ledger: 12 segments, charged 15.40414…. Checkpoints (sequence, attempt): 1 (attempt 1), 2 (attempt 1), 3 (attempt 1), 4 (attempt 1). Reservations: 4, all `COMMITTED`. Result `01a0dde1-c6eb-749c-adb5-44d9accaa370` comes from attempt 1. Wall time 57.0 s.

| Attempt | Fence | State | Failure | Incarnation: attempt / lease / grant(s) | Container (stopped, verified) | Allocation | Restore |
|---|---|---|---|---|---|---|---|
| 1 | 1 | `SUCCEEDED` | — | `…b3bc214e095a` / `…b3bc214e095a` (fence 1, revoked true) / `…b3bc214e095a` (fence 1, ended true) | `5c6ab8f52d0f` (true, true) | `RELEASED/VERIFIED_CLEANUP` | none |

| # | UTC | Event | Reason |
|---|---|---|---|
| 1 | 13:21:48.370 | `JOB_ACCEPTED` | `Job accepted` |
| 2 | 13:21:48.543 | `JOB_DISPATCHING` | — |
| 3 | 13:21:49.723 | `ATTEMPT_STARTED` | `attempt_started` |
| 4 | 13:22:01.922 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 5 | 13:22:10.700 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 6 | 13:22:21.298 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 7 | 13:22:31.962 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 8 | 13:22:38.522 | `RESULT_RECOGNIZED` | `result_recognized` |
| 9 | 13:22:44.755 | `ALLOCATION_RELEASED` | `VERIFIED_CLEANUP` |

#### S2 crash-resume

Job `01a0dde1-ea01-7d78-845f-f29cac92b5c8` ended `SUCCEEDED` (desired `RUNNING`): job fence 3, `event_sequence` 15, `checkpoint_sequence` 4, `retry_count` 1 of 2, coordinator epoch 1. Submit key `b14-docker-crash-resume` has 1 idempotency record. Ledger: 16 segments, charged 19.57005…. Checkpoints (sequence, attempt): 1 (attempt 1), 2 (attempt 1), 3 (attempt 2), 4 (attempt 2). Reservations: 4, all `COMMITTED`. Retry schedules: #1 `INFRASTRUCTURE`, closed. Result `01a0dde2-ea99-7f85-b71b-8ba61c70e84f` comes from attempt 2.

| Attempt | Fence | State | Failure | Incarnation: attempt / lease / grant(s) | Container (stopped, verified) | Allocation | Restore |
|---|---|---|---|---|---|---|---|
| 1 | 1 | `FAILED` | `INFRASTRUCTURE/RUNNER_UNAVAILABLE` | `…b3bc214e095a` / `…b3bc214e095a` (fence 1, revoked true) / `…b3bc214e095a` (fence 1, ended true) | `001fee6852d1` (true, true) | `RELEASED/VERIFIED_CLEANUP`, was quarantined | none |
| 2 | 3 | `SUCCEEDED` | — | `…b3bc214e095a` / `…b3bc214e095a` (fence 3, revoked true) / `…b3bc214e095a` (fence 3, ended true) | `c6b33855624c` (true, true) | `RELEASED/VERIFIED_CLEANUP` | seq 2 `…9d84cee4ee8b` |

| # | UTC | Event | Reason |
|---|---|---|---|
| 1 | 13:22:45.378 | `JOB_ACCEPTED` | `Job accepted` |
| 2 | 13:22:45.566 | `JOB_DISPATCHING` | — |
| 3 | 13:22:56.328 | `ATTEMPT_STARTED` | `attempt_started` |
| 4 | 13:23:08.232 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 5 | 13:23:16.251 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 6 | 13:23:16.849 | `ATTEMPT_FAILED` | `RUNNER_UNAVAILABLE` |
| 7 | 13:23:17.149 | `ALLOCATION_RELEASED` | `VERIFIED_CLEANUP` |
| 8 | 13:23:19.444 | `RETRY_READY` | `BACKOFF_ELAPSED` |
| 9 | 13:23:19.551 | `JOB_DISPATCHING` | — |
| 10 | 13:23:19.706 | `CHECKPOINT_RESTORE_SELECTED` | `CHECKPOINT_RESTORED` |
| 11 | 13:23:20.320 | `ATTEMPT_STARTED` | `attempt_started` |
| 12 | 13:23:35.804 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 13 | 13:23:47.142 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 14 | 13:23:53.340 | `RESULT_RECOGNIZED` | `result_recognized` |
| 15 | 13:23:59.382 | `ALLOCATION_RELEASED` | `VERIFIED_CLEANUP` |

#### S3 corrupt newest

Job `01a0dde3-0cbf-7219-9419-562c3fd96300` ended `SUCCEEDED` (desired `RUNNING`): job fence 3, `event_sequence` 17, `checkpoint_sequence` 5, `retry_count` 1 of 2, coordinator epoch 1. Submit key `b14-docker-corrupt-newest` has 1 idempotency record. Ledger: 18 segments, charged 21.41212…. Checkpoints (sequence, attempt): 1 (attempt 1), 2 (attempt 1, `CORRUPT/CHECKPOINT_CHECKSUM_MISMATCH`), 3 (attempt 2), 4 (attempt 2), 5 (attempt 2). Reservations: 5, all `COMMITTED`. Retry schedules: #1 `INFRASTRUCTURE`, closed. Result `01a0dde4-2644-7961-a733-8b63f4601720` comes from attempt 2.

| Attempt | Fence | State | Failure | Incarnation: attempt / lease / grant(s) | Container (stopped, verified) | Allocation | Restore |
|---|---|---|---|---|---|---|---|
| 1 | 1 | `FAILED` | `INFRASTRUCTURE/RUNNER_UNAVAILABLE` | `…b3bc214e095a` / `…b3bc214e095a` (fence 1, revoked true) / `…b3bc214e095a` (fence 1, ended true) | `6e1e32de9204` (true, true) | `RELEASED/VERIFIED_CLEANUP`, was quarantined | none |
| 2 | 3 | `SUCCEEDED` | — | `…b3bc214e095a` / `…b3bc214e095a` (fence 3, revoked true) / `…b3bc214e095a` (fence 3, ended true) | `5a5dd1af01e9` (true, true) | `RELEASED/VERIFIED_CLEANUP` | seq 1 `…4bebc3088149` |

| # | UTC | Event | Reason |
|---|---|---|---|
| 1 | 13:23:59.806 | `JOB_ACCEPTED` | `Job accepted` |
| 2 | 13:24:00.139 | `JOB_DISPATCHING` | — |
| 3 | 13:24:07.528 | `ATTEMPT_STARTED` | `attempt_started` |
| 4 | 13:24:18.675 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 5 | 13:24:27.612 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 6 | 13:24:28.213 | `ATTEMPT_FAILED` | `RUNNER_UNAVAILABLE` |
| 7 | 13:24:28.435 | `ALLOCATION_RELEASED` | `VERIFIED_CLEANUP` |
| 8 | 13:24:29.690 | `RETRY_READY` | `BACKOFF_ELAPSED` |
| 9 | 13:24:29.834 | `JOB_DISPATCHING` | — |
| 10 | 13:24:30.763 | `CHECKPOINT_CORRUPT` | `CHECKPOINT_CHECKSUM_MISMATCH` |
| 11 | 13:24:30.779 | `CHECKPOINT_RESTORE_SELECTED` | `CHECKPOINT_RESTORED` |
| 12 | 13:24:31.418 | `ATTEMPT_STARTED` | `attempt_started` |
| 13 | 13:24:45.619 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 14 | 13:24:57.893 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 15 | 13:25:09.486 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 16 | 13:25:13.829 | `RESULT_RECOGNIZED` | `result_recognized` |
| 17 | 13:25:19.674 | `ALLOCATION_RELEASED` | `VERIFIED_CLEANUP` |

#### S4 corrupt all, fall back to input

Job `01a0dde4-456a-7241-a5ec-b16b9c71b944` ended `SUCCEEDED` (desired `RUNNING`): job fence 3, `event_sequence` 19, `checkpoint_sequence` 6, `retry_count` 1 of 2, coordinator epoch 1. Submit key `b14-docker-fallback-input` has 1 idempotency record. Ledger: 18 segments, charged 23.81131…. Checkpoints (sequence, attempt): 1 (attempt 1, `CORRUPT/CHECKPOINT_CHECKSUM_MISMATCH`), 2 (attempt 1, `CORRUPT/CHECKPOINT_CHECKSUM_MISMATCH`), 3 (attempt 2), 4 (attempt 2), 5 (attempt 2), 6 (attempt 2). Reservations: 6, all `COMMITTED`. Retry schedules: #1 `INFRASTRUCTURE`, closed. Result `01a0dde5-83a4-7c05-84c4-cf0a30658959` comes from attempt 2.

| Attempt | Fence | State | Failure | Incarnation: attempt / lease / grant(s) | Container (stopped, verified) | Allocation | Restore |
|---|---|---|---|---|---|---|---|
| 1 | 1 | `FAILED` | `INFRASTRUCTURE/RUNNER_UNAVAILABLE` | `…b3bc214e095a` / `…b3bc214e095a` (fence 1, revoked true) / `…b3bc214e095a` (fence 1, ended true) | `2e67310fad5d` (true, true) | `RELEASED/VERIFIED_CLEANUP`, was quarantined | none |
| 2 | 3 | `SUCCEEDED` | — | `…b3bc214e095a` / `…b3bc214e095a` (fence 3, revoked true) / `…b3bc214e095a` (fence 3, ended true) | `2907eb92172d` (true, true) | `RELEASED/VERIFIED_CLEANUP` | none |

| # | UTC | Event | Reason |
|---|---|---|---|
| 1 | 13:25:19.845 | `JOB_ACCEPTED` | `Job accepted` |
| 2 | 13:25:20.033 | `JOB_DISPATCHING` | — |
| 3 | 13:25:28.710 | `ATTEMPT_STARTED` | `attempt_started` |
| 4 | 13:25:41.037 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 5 | 13:25:50.014 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 6 | 13:25:50.607 | `ATTEMPT_FAILED` | `RUNNER_UNAVAILABLE` |
| 7 | 13:25:50.853 | `ALLOCATION_RELEASED` | `VERIFIED_CLEANUP` |
| 8 | 13:25:53.147 | `RETRY_READY` | `BACKOFF_ELAPSED` |
| 9 | 13:25:53.316 | `JOB_DISPATCHING` | — |
| 10 | 13:25:53.491 | `CHECKPOINT_CORRUPT` | `CHECKPOINT_CHECKSUM_MISMATCH` |
| 11 | 13:25:53.491 | `CHECKPOINT_CORRUPT` | `CHECKPOINT_CHECKSUM_MISMATCH` |
| 12 | 13:25:53.518 | `CHECKPOINT_FALLBACK_TO_INPUT` | `CHECKPOINT_FALLBACK_TO_INPUT` |
| 13 | 13:25:54.234 | `ATTEMPT_STARTED` | `attempt_started` |
| 14 | 13:26:05.617 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 15 | 13:26:14.589 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 16 | 13:26:25.820 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 17 | 13:26:36.323 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 18 | 13:26:43.452 | `RESULT_RECOGNIZED` | `result_recognized` |
| 19 | 13:26:49.387 | `ALLOCATION_RELEASED` | `VERIFIED_CLEANUP` |

#### S5a kill before the first checkpoint, then between

Job `01a0dde5-a543-7b40-88e3-7dc46190366d` ended `SUCCEEDED` (desired `RUNNING`): job fence 5, `event_sequence` 22, `checkpoint_sequence` 5, `retry_count` 2 of 2, coordinator epoch 1. Submit key `b14-docker-multi-kill` has 1 idempotency record. Ledger: 18 segments, charged 23.11279…. Checkpoints (sequence, attempt): 1 (attempt 2), 2 (attempt 3), 3 (attempt 3), 4 (attempt 3), 5 (attempt 3). Reservations: 5, all `COMMITTED`. Retry schedules: #1 `INFRASTRUCTURE`, closed, #2 `INFRASTRUCTURE`, closed. Result `01a0dde7-2c7b-7e85-bc24-cca17fe3ba34` comes from attempt 3.

| Attempt | Fence | State | Failure | Incarnation: attempt / lease / grant(s) | Container (stopped, verified) | Allocation | Restore |
|---|---|---|---|---|---|---|---|
| 1 | 1 | `FAILED` | `INFRASTRUCTURE/RUNNER_UNAVAILABLE` | `…b3bc214e095a` / `…b3bc214e095a` (fence 1, revoked true) / `…b3bc214e095a` (fence 1, ended true) | `5eb89395471c` (true, true) | `RELEASED/VERIFIED_CLEANUP`, was quarantined | none |
| 2 | 3 | `FAILED` | `INFRASTRUCTURE/RUNNER_UNAVAILABLE` | `…b3bc214e095a` / `…b3bc214e095a` (fence 3, revoked true) / `…b3bc214e095a` (fence 3, ended true) | `7b7e0437ceea` (true, true) | `RELEASED/VERIFIED_CLEANUP`, was quarantined | none |
| 3 | 5 | `SUCCEEDED` | — | `…b3bc214e095a` / `…b3bc214e095a` (fence 5, revoked true) / `…b3bc214e095a` (fence 5, ended true) | `6b2e67dfd652` (true, true) | `RELEASED/VERIFIED_CLEANUP` | seq 1 `…910b333b67ab` |

| # | UTC | Event | Reason |
|---|---|---|---|
| 1 | 13:26:49.921 | `JOB_ACCEPTED` | `Job accepted` |
| 2 | 13:26:59.904 | `JOB_DISPATCHING` | — |
| 3 | 13:27:01.389 | `ATTEMPT_STARTED` | `attempt_started` |
| 4 | 13:27:02.925 | `ATTEMPT_FAILED` | `RUNNER_UNAVAILABLE` |
| 5 | 13:27:03.129 | `ALLOCATION_RELEASED` | `VERIFIED_CLEANUP` |
| 6 | 13:27:05.683 | `RETRY_READY` | `BACKOFF_ELAPSED` |
| 7 | 13:27:10.136 | `JOB_DISPATCHING` | — |
| 8 | 13:27:11.205 | `CHECKPOINT_FALLBACK_TO_INPUT` | `CHECKPOINT_FALLBACK_TO_INPUT` |
| 9 | 13:27:11.829 | `ATTEMPT_STARTED` | `attempt_started` |
| 10 | 13:27:23.899 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 11 | 13:27:27.048 | `ATTEMPT_FAILED` | `RUNNER_UNAVAILABLE` |
| 12 | 13:27:27.262 | `ALLOCATION_RELEASED` | `VERIFIED_CLEANUP` |
| 13 | 13:27:30.407 | `RETRY_READY` | `BACKOFF_ELAPSED` |
| 14 | 13:27:35.591 | `JOB_DISPATCHING` | — |
| 15 | 13:27:35.915 | `CHECKPOINT_RESTORE_SELECTED` | `CHECKPOINT_RESTORED` |
| 16 | 13:27:36.623 | `ATTEMPT_STARTED` | `attempt_started` |
| 17 | 13:27:50.342 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 18 | 13:28:02.842 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 19 | 13:28:15.164 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 20 | 13:28:27.456 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 21 | 13:28:33.514 | `RESULT_RECOGNIZED` | `result_recognized` |
| 22 | 13:28:39.582 | `ALLOCATION_RELEASED` | `VERIFIED_CLEANUP` |

#### S5b kill right after publish

Job `01a0dde7-54a1-7b8d-a55c-93313412c55f` ended `SUCCEEDED` (desired `RUNNING`): job fence 3, `event_sequence` 16, `checkpoint_sequence` 5, `retry_count` 1 of 2, coordinator epoch 1. Submit key `b14-docker-kill-after-publish` has 1 idempotency record. Ledger: 16 segments, charged 21.45706…. Checkpoints (sequence, attempt): 1 (attempt 1), 2 (attempt 2), 3 (attempt 2), 4 (attempt 2), 5 (attempt 2). Reservations: 5, all `COMMITTED`. Retry schedules: #1 `INFRASTRUCTURE`, closed. Result `01a0dde8-7f97-7f1e-9c35-b3008690548a` comes from attempt 2.

| Attempt | Fence | State | Failure | Incarnation: attempt / lease / grant(s) | Container (stopped, verified) | Allocation | Restore |
|---|---|---|---|---|---|---|---|
| 1 | 1 | `FAILED` | `INFRASTRUCTURE/RUNNER_UNAVAILABLE` | `…b3bc214e095a` / `…b3bc214e095a` (fence 1, revoked true) / `…b3bc214e095a` (fence 1, ended true) | `ea31a6802eb6` (true, true) | `RELEASED/VERIFIED_CLEANUP`, was quarantined | none |
| 2 | 3 | `SUCCEEDED` | — | `…b3bc214e095a` / `…b3bc214e095a` (fence 3, revoked true) / `…b3bc214e095a` (fence 3, ended true) | `b8e81645fa1e` (true, true) | `RELEASED/VERIFIED_CLEANUP` | seq 1 `…65030986f152` |

| # | UTC | Event | Reason |
|---|---|---|---|
| 1 | 13:28:40.350 | `JOB_ACCEPTED` | `Job accepted` |
| 2 | 13:28:40.961 | `JOB_DISPATCHING` | — |
| 3 | 13:28:53.615 | `ATTEMPT_STARTED` | `attempt_started` |
| 4 | 13:29:08.617 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 5 | 13:29:09.194 | `ATTEMPT_FAILED` | `RUNNER_UNAVAILABLE` |
| 6 | 13:29:09.409 | `ALLOCATION_RELEASED` | `VERIFIED_CLEANUP` |
| 7 | 13:29:11.028 | `RETRY_READY` | `BACKOFF_ELAPSED` |
| 8 | 13:29:14.687 | `JOB_DISPATCHING` | — |
| 9 | 13:29:15.246 | `CHECKPOINT_RESTORE_SELECTED` | `CHECKPOINT_RESTORED` |
| 10 | 13:29:15.980 | `ATTEMPT_STARTED` | `attempt_started` |
| 11 | 13:29:27.899 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 12 | 13:29:36.795 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 13 | 13:29:47.064 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 14 | 13:29:56.533 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 15 | 13:29:58.696 | `RESULT_RECOGNIZED` | `result_recognized` |
| 16 | 13:30:04.539 | `ALLOCATION_RELEASED` | `VERIFIED_CLEANUP` |

#### S6 worker SIGKILL with an open reservation

Job `01a0dde8-9f0b-7d9f-a234-f789bd0feaf2` ended `SUCCEEDED` (desired `RUNNING`): job fence 1, `event_sequence` 9, `checkpoint_sequence` 4, `retry_count` 0 of 2, coordinator epoch 1. Submit key `b14-docker-worker-restart` has 1 idempotency record. Ledger: 13 segments, charged 15.86562…. Checkpoints (sequence, attempt): 1 (attempt 1), 2 (attempt 1), 3 (attempt 1), 4 (attempt 1). Reservations: 4, all `COMMITTED`. Result `01a0dde9-60d3-7d94-898c-fabed5e4a131` comes from attempt 1. The reservation that was open when the worker was killed ended `COMMITTED`.

| Attempt | Fence | State | Failure | Incarnation: attempt / lease / grant(s) | Container (stopped, verified) | Allocation | Restore |
|---|---|---|---|---|---|---|---|
| 1 | 1 | `SUCCEEDED` | — | `…88dbad23a40d` / `…88dbad23a40d` (fence 1, revoked true) / `…b3bc214e095a` (fence 1, ended true), `…88dbad23a40d` (fence 1, ended true) | `37c9fe9619ac` (true, true) | `RELEASED/VERIFIED_CLEANUP` | none |

| # | UTC | Event | Reason |
|---|---|---|---|
| 1 | 13:30:04.931 | `JOB_ACCEPTED` | `Job accepted` |
| 2 | 13:30:05.168 | `JOB_DISPATCHING` | — |
| 3 | 13:30:12.262 | `ATTEMPT_STARTED` | `attempt_started` |
| 4 | 13:30:22.369 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 5 | 13:30:35.115 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 6 | 13:30:44.608 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 7 | 13:30:52.215 | `CHECKPOINT_COMMITTED` | `CHECKPOINT_COMMITTED` |
| 8 | 13:30:56.322 | `RESULT_RECOGNIZED` | `result_recognized` |
| 9 | 13:31:03.064 | `ALLOCATION_RELEASED` | `VERIFIED_CLEANUP` |

#### Result checksums

The test downloads every result through `GET /v1/jobs/{job_id}/result` and the
output download, checks SHA-256 of the bytes against the artifact row, and
asserts `bytes == R0 bytes` and `checksum == R0 checksum`.

| Run | Scenario | Result id | Result checksum | Bytes and checksum equal to R0 |
|---|---|---|---|---|
| R0 | S1 | `01a0dde1-c6eb-749c-adb5-44d9accaa370` | `sha256:7de722a0578b29d9d1a8e672e67a6525deb61ef81cff16503e1eb6b7412a6f35` | (R0) |
| R1 | S2 | `01a0dde2-ea99-7f85-b71b-8ba61c70e84f` | `sha256:7de722a0578b29d9d1a8e672e67a6525deb61ef81cff16503e1eb6b7412a6f35` | yes |
| R2 | S3 | `01a0dde4-2644-7961-a733-8b63f4601720` | `sha256:7de722a0578b29d9d1a8e672e67a6525deb61ef81cff16503e1eb6b7412a6f35` | yes |
| R3 | S4 | `01a0dde5-83a4-7c05-84c4-cf0a30658959` | `sha256:7de722a0578b29d9d1a8e672e67a6525deb61ef81cff16503e1eb6b7412a6f35` | yes |
| R4 | S5a | `01a0dde7-2c7b-7e85-bc24-cca17fe3ba34` | `sha256:7de722a0578b29d9d1a8e672e67a6525deb61ef81cff16503e1eb6b7412a6f35` | yes |
| R5 | S5b | `01a0dde8-7f97-7f1e-9c35-b3008690548a` | `sha256:7de722a0578b29d9d1a8e672e67a6525deb61ef81cff16503e1eb6b7412a6f35` | yes |
| R6 | S6 | `01a0dde9-60d3-7d94-898c-fabed5e4a131` | `sha256:7de722a0578b29d9d1a8e672e67a6525deb61ef81cff16503e1eb6b7412a6f35` | yes |

Fairness scores read after each scenario (asserted non-decreasing): 15.4041, 34.9742, 56.3863, 80.1976, 103.310, 124.767, 140.633.
#### B09/B10/B11 Docker regression on the new images

Same variables with `--deselect` of the B14 test, 2026-09-26 20:32:26–20:36:16
+07: **14 passed, 1 skipped, 1 deselected** in 228.56 s. Passed:
`test_b11_vertical.py` (2), `test_b11_worker_ipc.py` (1),
`test_real_executor.py` (2), `test_real_runner.py` (9). Skipped:
`test_b10_worker_restart.py`, which needs `NEXA_B10_RUNTIME_EVIDENCE=1` and a
Linux host (the worker process shares the runner's monotonic clock); this host
is macOS. The `ASGI callable returned without completing response` line in
the B11 vertical output is its injected response loss.

## Self-review (mục 2d)

I read the whole `git diff` and every untracked file as an independent
reviewer would. Three read-only review passes followed: server/API,
worker, and runner/workload. None edited files, used Docker or set
`NEXA_DATABASE_URL`. Each line was checked against tenant isolation,
authority/fence/lease, lock order, Docker or filesystem I/O inside a
transaction, idempotency and replay, sensitive-content logging, row
invariants and additive migration. I traced every reported claim in the code
before acting on it.

**Fixed.** "Red" is the failure observed before the fix, on the listed test.

| # | Issue (severity) | Fix | Red → green evidence |
|---|---|---|---|
| S1 | Restore events at claim (`CHECKPOINT_RESTORE_SELECTED`, `…_FALLBACK_TO_INPUT`, `…_RESTORE_UNAVAILABLE`, `…_INCOMPATIBLE`, `…_CORRUPT`) incremented `jobs.version`. `state-machines.md` says claim makes no Job version change, so a client's `If-Match` from dispatch would get a false `412`. (Medium) | `_event(..., keep_version=True)` in `execution_cleanup.py`; `checkpoint_restore.py` uses it for every restore event. The event sequence and audit row still advance. | Five `test_checkpoint_restore_b14.py` tests now assert `(version, event_sequence)` before and after claim. Red: `(4, 3) == (3, 3)`, `(7, 6) == (4, 6)`, `(4, 3) == (2, 3)`. Green in the 40-test B14 integration run. |
| S2 | The Docker test expected `("CHECKPOINT_COMMITTED", None)`; the service stores reason `CHECKPOINT_COMMITTED`. S2 and every later scenario would have failed. (High for the evidence run) | Expected value fixed at 3 lines. | Not red-first: the Docker test could not run in round 1 (B14-R02). Checked against `checkpoint_service.py` and the integration assertions. Round 2: S1–S6 pass with this expectation. |
| S3 | A workload could replace `/output/state.json` with a FIFO. `read_state_file` opened it blocking while it held the runner lock, so the deadline watchdog stalled. (Low–Medium) | `os.open(..., O_RDONLY \| O_NOFOLLOW \| O_NONBLOCK)`; the regular-file check then rejects it. | `test_state_file_read_rejects_a_fifo_without_blocking`. Red: `assert not True` (the thread was still blocked after 2 s). Green. |
| S4 | A completion committed on the server whose response was lost could never be replayed once the container stopped. `_upload` re-read the output through `docker exec`, which raised, and the loop repeated forever. (Medium) | `result_flow.py` journals `completion_request` with `completion_callback_id` before sending. A replay sends the same request and callback without reading the container. | `test_lost_completion_ack_replays_after_the_container_stopped`. Red: `RuntimeError` from `read_output`. Green: two identical `(callback, body)` completions and `completed` true. |
| S5 | A worker killed mid-download left a partial `restore-state.json`. Replay trusted `exists()`, so an intact checkpoint became `INCOMPATIBLE/CHECKPOINT_RESTORE_UNAVAILABLE`. (Low–Medium) | `_matches_descriptor` checks size and SHA-256; a mismatching leftover is removed and fetched again. | `test_partial_restore_download_left_by_a_crash_is_fetched_again`. Red: a failure was recorded. Green: re-download, full bytes, launch restore step 20. |
| S6 | `_checkpoint_launch` (image digest check, `docker image inspect`, restore download) ran before the startup `try`. A digest mismatch then escaped with no attempt failure and no `NoContainerProof`, a regression from B11. (Low) | The launch selection moved inside the startup budget and failure handling. The failure request is built from the context without the restore, so nothing is journaled before prepare. | `test_configured_image_digest_mismatch_fails_the_attempt_without_a_container`. Red: no failure recorded. Green: `[("INTERNAL", "INVALID_RESULT")]`, `NO_CONTAINER`, 0 containers created. |
| S7 | A worker whose new incarnation had not yet reported inventory got a permanent `CHECKPOINT_COMPATIBILITY_MISMATCH`. | 503 `Retry-After: 1`. | `test_unreported_worker_inventory_is_transient_not_incompatible`, red first. |
| S8 | Docker test used wrong column or event names in places; docs named the wrong restore path and snapshot file. | Names and paths corrected (`/input/restore-state.json`, `/output/state.json`). | Checked by reading `schema.py` and the runner. The Docker test collects without errors. |
| S9 | Guards for "no pickle" and "no checkpoint content in logs" were missing. | `test_pickled_state_is_rejected_and_no_source_module_deserializes_pickle`, `test_checkpoint_cycle_logs_no_state_content`. | Not red-first: they pin existing behavior and passed on first run. |

**Kept unchanged, with reasons.**

| # | Report | Disposition |
|---|---|---|
| K1 | A due retry that stays blocked makes the unlocked probe return true, so each tick takes the leadership, policy, GLOBAL counter and Job locks and commits nothing. | Contention only, never a wrong result. The docstring in `retry.py` now states it. Listed in Limits. |
| K2 | `worker_architecture` does not check that the inventory belongs to the current incarnation. | Unreachable: registration of a new incarnation resets `current_inventory_version` to NULL, which now answers 503 (S7). |
| K3 | A worker architecture outside the template's list makes every checkpoint incompatible, so a `restart_safe` Job falls back to input. | Follows restore step 5, conflicts with the relocation wording. Recorded as B14-R04. |
| K4 | A deferred `RESULT_PREPARE` is acknowledged while the checkpoint cycle is open. | Safe: the deferral is journaled before the acknowledgment and its replay is idempotent. Wording-level only. |
| K5 | A disk or file error while the runner copies a checkpoint escapes as runner exit 124, classed `INFRASTRUCTURE/RUNNER_UNAVAILABLE`, not a checkpoint failure. | Fails closed, and the retry is bounded by `max_retries`. Listed in Limits. |
| K6 | A runner refusal (for example a replayed `REQUEST_CHECKPOINT` whose frozen deadline passed) fails the attempt instead of skipping one checkpoint. | A deadline older than 60 s implies the 45 s lease already expired, so the attempt has lost authority anyway. Listed in Limits. |
| K7 | `downloads/<attempt>/input.json` has the same partial-file pattern as S5 and ends `INVALID_INPUT`. | Existing B11 code path, not changed by B14. Recorded as B14-R05. |

After the fixes, the review checklist held for the whole diff:
- checkpoint list tenant/membership/scope isolation;
- authority re-checked inside every committing transaction;
- lock order unchanged: Job → Attempt → lease → allocation → grant, and policy → GLOBAL → Job → schedule;
- no Docker, blob or filesystem I/O inside a transaction;
- callback receipts and sequence reuse;
- the frozen restore choice;
- no logging of state, cursor, accumulator or manifest content;
- migration 0018 is additive, with its downgrade;
- no metric is added, so no new label (and no `job_id` label) exists.

### Round 2 (after review round 1)

Each blocking finding has a closure test. "Mutation" means the fix was
reverted in place, the closure test was run and failed as shown, and the file
was then restored byte for byte, after which the test passed again.

| Finding | Fix | Closure test | Mutation result |
|---|---|---|---|
| B14-R01 | `execution_service.py`: the poll offer fills `checkpoint` with the newest non-corrupt `CheckpointRecord`. `worker/execution.py`: the claim journals `offered_checkpoint`; if a checkpoint was offered, the claim froze no restore and the template snapshot is not `restart_safe`, the worker raises `RestoreUnavailable` before launch and fails the attempt `INCOMPATIBLE/CHECKPOINT_RESTORE_UNAVAILABLE` with a `NoContainerProof`. | P `test_non_restart_safe_recovery_without_valid_checkpoint_fails_before_create`: production `WorkerAgent` + `DockerExecutor` (fake Docker) against the real API and PostgreSQL. The offer carries the checkpoint; the Job ends `FAILED` with `retry_count` 0; the attempt is `INCOMPATIBLE/CHECKPOINT_RESTORE_UNAVAILABLE` with `started_at` null; there is no `STARTUP_TIMEOUT` event, no container create, no input or restore download; the allocation is `RELEASED` and the journal `TOMBSTONED`. P `test_non_restart_safe_recovery_refuses_start_without_restore`: a worker that skips the check still gets 409 at `startAttempt`. | Guard changed to `is 42`: 1 failed; the worker went on to launch and stopped at `ExecutorError: image inspection failed`. |
| B14-R06 | `trusted_runner.py`: the first progress frame is `fraction = step / iterations`, `step = restored step`. | U `test_first_progress_reports_the_restored_cursor` (4 cases: no restore, step 0, step 10, final step). | Old `emit_progress(fraction=0.0, step=0)`: 2 of 4 failed, `assert (1, 0) == (1, 10)` and `assert (1, 0) == (1, 50)` (rerun 20:41 +07; the other 2 start at step 0). |
| B14-R07 (non-blocking) | `_atomic_write` sets the mode with `os.fchmod` on the open descriptor; docs say 0440. | U: the two staging tests in `test_runner_checkpoint_b14.py` assert mode `0o440`. | Not mutation-checked: the assertion pins the mode, not the symlink case. |
| B14-R08 | `WorkerApiClient.upload_artifact` checks the committed answer and raises only `ValueError`; `checkpoint_flow._upload` maps it to `CheckpointProtocolError`; the result path maps it to `INTERNAL/INVALID_RESULT`. | U `test_invalid_committed_upload_answer_fails_only_that_attempt` (5 cases: `binding`, `missing_field`, `not_object`, `not_json`, `bad_id`). | `except ValueError` → `except LookupError`: 5 failed. |
| B14-R09 | `checkpoint_flow.reconcile_adoption` explains a result reservation committed before the journal: same `reservation_callback_id` as the server's `callback_id`, and the journaled reservation is none or the same. It then journals the server's reservation. | U `test_adoption_reconcile_explains_a_result_reserve_committed_before_the_journal`; U `test_result_reserve_crash_window_is_adopted_and_completes_once` (real `_adopt` of a restarted agent; one completion under the new incarnation); U `test_adoption_reconcile_refuses_unexplained_differences` (updated). | Assignment replaced by `return None`: 1 failed with `RuntimeError: adoption reservation snapshot mismatch`. |

Running S1–S6 for the first time found three defects in the Docker test
itself. They were fixed in the test only; no source file changed:

| # | Failure seen | Fix |
|---|---|---|
| H1 | 422 `validation_failed`: the fixture used `runtime_limit_seconds` 600; the schema maximum is 300. | 300 in the template bounds and in the submit spec. |
| H2 | Reconcile read allocation `HELD` right after `SUCCEEDED`. Release commits after the cleanup proof. | Wait up to 30 s for every allocation of the Job to be `RELEASED` before reading the timeline, as the B11 vertical test does. |
| H3 | `function sum(text) does not exist`: `charged_amount` is exact-decimal text. | Sum the segments as `Decimal` in Python. |

Review of the round-2 diff against the same checklist: the R01 check runs in
the worker before any Docker call and outside any transaction; the offer
reads only committed, non-corrupt records of the offered Job; R08 turns an
unexpected answer into a failure of that attempt only; R09 accepts only the
exactly explained case, and every other difference still refuses adoption.
No new metric, label or log field was added.

## Acceptance matrix

Layers: U = unit / fake Docker; P = PostgreSQL 17 + in-process API; C = Docker
containers on Docker Desktop. Status covers the B14 slice only.
`docs/acceptance.md` is not edited; every gate there stays `specified` until
Task Review.

Round 2, final source (see Verification): all U tests passed (default suite:
900 passed, 354 skipped). B14 PostgreSQL items: 43 collected with
`--run-postgres` (18 `test_checkpoint_b14`, 9 `test_checkpoint_restore_b14`,
2 `test_checkpoint_corruption_b14`, 12 `test_retry_b14`, 2 B14 migration
tests); **43 passed** in the PG B14 run, and again inside the full PostgreSQL
run (1238 passed). C rows come from the S1–S6 run and the B09/B10/B11 Docker
regression on the new images. "PG round 2" below means both PostgreSQL runs.
In round 1, 12 of the B14 items were `blocked` by the PID exhaustion
(B14-R02).

| Gate | B14 criterion | Layer | Tests | Evidence | Applicability | Status | Limits / other task |
|---|---|---|---|---|---|---|---|
| ACC-17 | Checkpoint files and manifest use the B07 upload path; binding needs committed same-tenant artifacts; publish is fenced; a partial local file is never trusted | U | `test_bytes_request_and_artifact_must_agree`, `test_file_rows_must_be_exact_committed_same_tenant_checkpoint_files`, `test_every_cycle_step_replays_the_same_checkpoint_identity`, `test_partial_restore_download_left_by_a_crash_is_fetched_again`, `test_invalid_committed_upload_answer_fails_only_that_attempt` (B14-R08) | default run | B14 slice | pass | — |
| ACC-17 | same | P | `test_checkpoint_uploads_are_gated_by_attempt_phase`, `test_wrong_authority_or_unavailable_storage_commits_nothing`, `test_publish_after_failure_is_stale_and_reservation_is_abandoned`, `test_each_publish_defect_is_rejected_durably` (6 of 7 cases) | PG round 2 | B14 slice | pass | — |
| ACC-17 | same | P | `test_publish_commits_the_complete_graph_once_and_replays`, `test_each_publish_defect_is_rejected_durably[foreign-manifest-…]` | PG round 2 | B14 slice | pass | Fault injection around fsync/rename/commit is B07 |
| ACC-17 | real filesystem under containers | C | S1–S6 | S1–S6 run, raw/B14-docker-timeline.json | B14 slice | pass | Docker Desktop VM, not bare Linux |
| ACC-18 (CPU) | Closed canonical manifest; provenance and compatibility validation; no pickle; cursor bound to state | U | `test_manifest_rejects_each_closed_schema_violation`, `test_strict_parser_rejects_ambiguous_json`, `test_each_provenance_field_must_match_the_database`, `test_each_compatibility_dimension_must_match_exactly`, `test_cpu_state_is_closed_canonical_and_matches_cursor`, `test_pickled_state_is_rejected_and_no_source_module_deserializes_pickle`, `test_restore_manifest_bytes_must_match_the_committed_checksum`, `test_restore_state_must_be_the_manifest_cursor`, `test_first_progress_reports_the_restored_cursor` (B14-R06) | default run | B14 slice | pass | — |
| ACC-18 (CPU) | Abandoned reservation never restorable; newest valid restore is frozen; incompatibility stops at identical compatibility; non-restart-safe never replays input; corruption row one-way | P | `test_publish_after_failure_is_stale_and_reservation_is_abandoned`, `test_claim_selects_the_newest_valid_checkpoint_immutably`, `test_incompatible_architecture_stops_at_identical_compatibility`, `test_non_restart_safe_recovery_without_valid_checkpoint_fails_before_create` and `test_non_restart_safe_recovery_refuses_start_without_restore` (B14-R01: `INCOMPATIBLE/CHECKPOINT_RESTORE_UNAVAILABLE`, no container, no input replay), `test_first_attempt_claim_has_no_restore_decision`, `test_corruption_mark_is_one_way_and_insert_only` | PG round 2 | B14 slice | pass | All-incompatible fallback (B14-R04) |
| ACC-18 (CPU) | Reserve/replay id + sequence before manifest; defect never reuses a sequence; ≥2 committed kept; corrupt newest → older; none valid → input only if restart-safe, with event | P | `test_reserve_allocates_a_job_locked_sequence_and_replays_exactly`, `test_deterministic_manifest_defect_rejects_reservation_and_never_reuses_sequence`, `test_every_published_checkpoint_stays_referenced_and_unreclaimable`, `test_corrupt_checkpoints_are_marked_once_and_older_is_restored`, `test_no_valid_checkpoint_falls_back_to_input_only_when_restart_safe` | PG round 2 | B14 slice | pass | — |
| ACC-18 (CPU) | Selection/restore timeline with real containers | C | S1–S6 | S1–S6 run, raw/B14-docker-timeline.json | B14 slice | pass | Non-restart-safe branch is P only (no Docker scenario) |
| ACC-18 | Inference carry-forward, RNG, CUDA | — | — | — | B16 | specified | B16 |
| ACC-19 (CPU) | CPU crash-resume byte-equal to an uninterrupted run | U | `test_resume_from_any_step_is_byte_equal_to_an_uninterrupted_run` (Hypothesis), `test_resume_applies_the_step_index_recurrence_exactly`, `test_entrypoint_snapshots_state_and_resumes_to_the_same_result`, `test_published_checkpoint_round_trips_into_a_verified_restore` | default run | B14 slice | pass | — |
| ACC-19 (CPU) | R1–R6 = R0 after real kills | C | S1–S6 | S1–S6 run: R1–R6 bytes and checksum = R0 | B14 slice | pass | One run per scenario on Docker Desktop |
| ACC-19 | PyTorch, sweep, inference chunks | — | — | — | B16 | specified | B16 |
| ACC-03 | Checkpoint list, publish binding and corruption rows stay within the tenant | U | `test_file_rows_must_be_exact_committed_same_tenant_checkpoint_files`, `test_job_checkpoints_reads_the_owned_job_page` | default run | B14 slice | pass | — |
| ACC-03 | same | P | `test_list_checkpoints_is_owned_paged_newest_first_and_marks_corrupt` (a member reading through another tenant's header gets 404), `test_corruption_mark_requires_same_tenant_committed_checkpoint` | PG round 2 | B14 slice | pass | UI is B17; manual-retry inheritance is B15 |
| ACC-03 | same | P | `test_each_publish_defect_is_rejected_durably[foreign-manifest-…]` | PG round 2 | B14 slice | pass | — |
| ACC-07 | Reserve/publish receipts replay the same body; the claim replay returns the frozen restore; worker replays reuse callback ids | U | `test_every_cycle_step_replays_the_same_checkpoint_identity`, `test_publish_conflict_keeps_the_reservation_and_retries_the_same_callback`, `test_lost_completion_ack_replays_after_the_container_stopped` | default run | B14 slice | pass | — |
| ACC-07 | same | P | `test_claim_selects_the_newest_valid_checkpoint_immutably` (claim replay) | PG round 2 | B14 slice | pass | — |
| ACC-07 | same | P | `test_reserve_allocates_a_job_locked_sequence_and_replays_exactly`, `test_publish_commits_the_complete_graph_once_and_replays` | PG round 2 | B14 slice | pass | Submit idempotency is B08 (unchanged) |
| ACC-13/14 | Reserve/publish need live exact authority (epoch, incarnation, fence, lease in DB time); stale publish rejected; adoption publishes under the new authority only; retry gets a new fence | U | `test_adoption_reconcile_explains_every_transferred_reservation`, `test_adoption_reconcile_refuses_unexplained_differences`, `test_adoption_reconcile_never_invents_a_reservation`, `test_adoption_reconcile_explains_a_result_reserve_committed_before_the_journal`, `test_result_reserve_crash_window_is_adopted_and_completes_once` (B14-R09), `test_checkpoint_request_fails_closed`, `test_checkpoint_request_after_result_reservation_fails_closed` | default run | B14 slice | pass | — |
| ACC-13/14 | same | P | `test_wrong_authority_or_unavailable_storage_commits_nothing`, `test_publish_after_failure_is_stale_and_reservation_is_abandoned`, `test_due_retry_is_promoted_once_then_dispatched_with_a_new_fence`, `test_concurrent_promotion_and_ticks_promote_each_retry_once` | PG round 2 | B14 slice | pass | — |
| ACC-13/14 | same | P | `test_reserve_requires_live_exact_authority_and_a_checkpointable_template`, `test_reserve_rejects_an_expired_lease`, `test_adopted_open_cycle_publishes_under_the_new_authority_only` | PG round 2 | B14 slice | pass | — |
| ACC-13/14 | Worker SIGKILL during an open reservation | C | S6 | S6 timeline | B14 slice | pass | Lease expiry and reaper are B15 |
| ACC-20/21 | Storage outage at restore fails closed with no mark; an unreported inventory is transient (503); journaled cycle survives a runner reload | U | `test_checkpoint_handshake_survives_runner_state_reload`, `test_reconcile_is_persisted_only_while_the_prior_authority_is_journaled` | default run | B14 slice | pass | — |
| ACC-20/21 | same | P | `test_storage_outage_fails_closed_without_marks_or_context`, `test_unreported_worker_inventory_is_transient_not_incompatible`, `test_wrong_authority_or_unavailable_storage_commits_nothing` (503 path) | PG round 2 | B14 slice | pass | Full restart, reboot and partition are B15/B22 |
| ACC-20/21 | same with a real worker restart | C | S6 | S6 timeline | B14 slice | pass | Full restart, reboot and partition are B15/B22 |
| ACC-22 (container crash → retry with restore only) | Dead container → typed failure → exact cleanup; `INFRASTRUCTURE` + budget + restorable → `RETRY_WAIT`; promotion once; claim restores | U | `test_dead_container_fails_attempt_then_cleans_exact_identity` (3 cases), `test_result_loop_detects_dead_container_behind_pending_result_frame`, `test_renew_is_kept_until_failure_is_acknowledged_and_cleanup_verified`, `test_running_container_channel_failure_is_not_a_container_exit` | default run | B14 slice | pass | — |
| ACC-22 (same) | same | P | `test_cleanup_retries_when_a_committed_checkpoint_or_restart_safe[True]`, `test_cleanup_fails_non_restart_safe_job_without_valid_checkpoint` (2), `test_cleanup_with_exhausted_budget_fails_even_with_checkpoint`, `test_due_retry_is_promoted_once_then_dispatched_with_a_new_fence`, `test_retry_promotion_requires_desired_running_without_recovery_intent` (2), `test_infeasible_retry_stays_waiting_with_a_visible_reason`, `test_concurrent_promotion_and_ticks_promote_each_retry_once`, `test_tick_promotes_due_retries_before_deciding` | PG round 2 | B14 slice | pass | — |
| ACC-22 (same) | same | P | `test_cleanup_retries_when_a_committed_checkpoint_or_restart_safe[False]`, `test_retry_waits_for_ready_at_and_live_leadership` | PG round 2 | B14 slice | pass | — |
| ACC-22 (same) | Real `docker kill` S2–S5b | C | S2, S3, S4, S5a, S5b | S1–S6 run, raw/B14-docker-timeline.json | B14 slice | pass | The rest of ACC-22 is B15 |
| ACC-22 | Worker crash matrix, kill at upload/commit, timeout/OOM/log flood | — | — | — | B15 | specified | B15 |
| ACC-23 | Checkpoint blobs have no delete or prune path; B07 staging cleanup leaves them | static | Code search: no `DELETE` or `delete()` on `checkpoints`, `checkpoint_corruptions` or `artifact_references`. New `unlink` calls touch only worker/runner local files (partial restore download, temp file, runner staging after upload). The corruption FK is `ON DELETE RESTRICT`. | this review | B14 slice | pass | GC, reference protection and disk-full are B19 |
| ACC-23 | same, proven on the database | P | `test_every_published_checkpoint_stays_referenced_and_unreclaimable` | PG round 2 | B14 slice | pass | B19 |
| ACC-25 | Restore mount read-only at a fixed path; launch spec v1 and B09 hardening unchanged; image adds only a label | U | `test_launch_spec_v1_is_unchanged_and_v2_is_closed`, `test_launch_command_adds_state_and_restore_only_for_checkpoint_specs`, `test_launch_spec_v2_and_binding_exist_only_for_a_checkpoint_launch`, `test_verified_restore_mounts_state_and_resumes_the_runner`, `test_entrypoint_rejects_oversized_or_symlinked_resume_state`, `test_state_file_read_rejects_a_fifo_without_blocking`, B09 `test_docker_config.py` | default run | B14 slice | pass | — |
| ACC-25 | B09 hardening unchanged on the new CPU image | C | `test_real_image_has_hardened_runtime_config`, `test_production_container_denies_rootfs_input_network_and_runner_control`, `test_production_container_enforces_cpu_pid_scratch_and_memory_bounds` and the rest of the B09/B11 Docker regression | Docker regression, round 2 | B14 slice | pass | Abuse and resource measurement are B20 |
| ACC-25 | `docker inspect` of a real checkpoint container with the restore mount | C | none: S1–S6 do not inspect container config (the round-1 matrix listed S1–S6 here in error) | — | B14 slice | not-run | U tests cover the checkpoint launch config |
| ACC-26 | Events/audit carry ids and reason codes only; no checkpoint, state, cursor or manifest content in logs; no new metric or label | U | `test_checkpoint_cycle_logs_no_state_content`; restore/retry P tests assert event types and reason codes. Audit rows are written in the same transaction (`_event`) but no B14 test asserts them. | default run, PG round 2 | B14 slice | pass | Audit-row assertions are not covered; metrics, alerts and monitoring outage are B19 |
| ACC-27 | `GET /v1/jobs/{job_id}/checkpoints` and `nexa job checkpoints`: owned, page ≤100, newest first, shows `CORRUPT` | U | `test_b14_checkpoint_operations_and_page_schema`, `test_job_checkpoints_reads_the_owned_job_page`, `test_api_route_contract.py`, `test_operation_matrix.py` | default run | B14 slice | pass | — |
| ACC-27 | same | P | `test_list_checkpoints_is_owned_paged_newest_first_and_marks_corrupt` | PG round 2 | B14 slice | pass | Playwright/UI is B17 |
| ACC-28 | Additive migration 0018 + `schema_v15`, upgrade/downgrade, offline SQL, insert-only trigger | P | `test_b14_checkpoint_corruption_upgrade_is_additive_and_downgrades`, `test_b14_offline_sql_creates_insert_only_corruption_table`, `test_corruption_mark_is_one_way_and_insert_only`, `test_corruption_mark_requires_same_tenant_committed_checkpoint`; U `test_schema_metadata.py` | PG round 2, default run | B14 slice | pass | Maintenance upgrade/restore is B21/B25; B13-R12 still open |
| ACC-39 | Ruff, format, default pytest + Hypothesis | U | commands in Verification | default run | B14 slice | pass | — |
| ACC-39 | Full PostgreSQL suite | P | `pytest --run-postgres` | Verification: 1238 passed, 16 skipped (opt-in Docker) | B14 slice | pass | — |
| ACC-39 | Image build with digest provenance | C | CPU and worker image builds | raw/B14-images.json | B14 slice | pass | Local builds, not pushed; scan/CI is B02/B25 |

## Limits

- Container evidence comes from Docker Desktop's `linuxkit` VM (linux/arm64)
  on a macOS host, one run per scenario. Nothing here is bare Linux, GPU,
  portability, load or soak evidence. The B10 worker-restart Docker test skips
  on a macOS host, so the B10 regression ran without it.
- ACC-22 covers only container crash → retry with restore. Lease expiry,
  reaper, pause/cancel interaction and the full recovery matrix are B15.
  A dead container found by a worker after a restart, where the pending renew
  belongs to another incarnation, is left to B15 recovery.
- An exit code 0 without a terminal frame is classed
  `INTERNAL/RUNNER_PROTOCOL_ERROR` and is not retried. After a completion was
  sent, a container exit raises no failure; the completion is replayed.
- `REQUEST_CHECKPOINT` with a frozen deadline older than 60 s is answered
  `INVALID` by the runner, and the attempt fails closed. Any runner refusal
  fails the attempt rather than skipping one checkpoint. Such a deadline
  implies the 45 s lease already expired (K6).
- A disk or file error while the runner copies a checkpoint exits the runner
  with 124. The attempt fails `INFRASTRUCTURE/RUNNER_UNAVAILABLE`, and a retry
  within `max_retries` follows, not a checkpoint-specific reason (K5).
- While a due retry stays blocked (quota or compatibility), every coordinator
  tick takes the leadership, policy, GLOBAL counter and Job locks once per
  second and commits nothing. This is contention, not a wrong result (K1);
  not measured under load (B22).
- A worker that crashes mid-download of `input.json` still turns an intact
  input into a terminal `INVALID_INPUT` (B14-R05, B11 path).
- Publish during cancel keeps retrying the same callback, as in B11; cancel
  is B15.
- No seeded template is checkpointable. An admin must register a template
  version with `checkpointable = true` and the B14 image digest. Tests use a
  fixture version.
- `CheckpointReference` is not written by B14. Manual-retry inheritance is
  B15; GC and reference protection are B19. ACC-23 is proven only as "no
  delete path".
- Inference carry-forward, RNG state, PyTorch, sweep and chunk inference
  (ACC-18/19 non-CPU parts) are B16.
- Both images are local builds on this host (`linux/arm64`), not pushed,
  signed or scanned (B25). Timing in S1–S6 is from this VM: commits were
  8–15 s apart with a 5 s interval, because the next cycle starts one interval
  after the previous one ends.

## Findings

Round-2 statuses are this implementer's claim; the reviewer decides closure.
R01–R05 are the implementer's IDs; R06–R10 and the observation come from
review round 1.

| ID | Status | Description | Root cause | Repro test | Closing condition | Closing evidence |
|---|---|---|---|---|---|---|
| B14-R01 | fixed in round 2, chờ Task Review | A non-restart-safe Job whose checkpoints all fail verification at claim was refused at `startAttempt` (409) and then failed `TIMEOUT/STARTUP_TIMEOUT`, which the contract reserves for startup > 30 s or runtime > 300 s. | The worker could not tell "restore required but missing" from a first start. Round 1 assumed a contract change was needed; the poll offer already has a `checkpoint` field, which the server always sent as `null`. | P `test_non_restart_safe_recovery_without_valid_checkpoint_fails_before_create`; P `test_non_restart_safe_recovery_refuses_start_without_restore` | End to end, the Job ends `FAILED` with a non-TIMEOUT class/reason and an event, and input is never replayed. | Offer sends the newest non-corrupt record; the worker fails `INCOMPATIBLE/CHECKPOINT_RESTORE_UNAVAILABLE` before any container. Both tests pass in the PG B14 run (43 passed, 20:12:55–20:13:44 +07) and in the full PostgreSQL run (1238 passed). Mutation: 1 failed. No contract, PLAN or schema change. See Self-review round 2. |
| B14-R02 | runtime evidence recorded in round 2, chờ Task Review | Round 1 had no container evidence: the worker image build, the Docker runs and PostgreSQL connections failed with `fork … resource temporarily unavailable`; the CPU digest was stale. | `nexa_b10_smoke3-caddy-1` held 30,865 PIDs in the Docker VM. It was not touched by B14. | `tests/docker/test_b14_checkpoint_restore.py` | Rebuild both images; PG B14 and Docker S1–S6 pass; evidence with the new digest; S2, S5a and S5b byte-identical to R0. | After the user restarted Docker Desktop: `--lf` rerun 58 passed, 1 skipped (20:07:23–20:08:48 +07); CPU image `sha256:e0e6222e…b6f` and worker image `sha256:c55c25da…8b42` rebuilt 20:09; PG B14 43 passed; Docker S1–S6 1 passed in 568.50 s; R1–R6 bytes and checksum equal to R0 (`sha256:7de722a0…6f35`), including S2, S5a and S5b. See [Docker scenarios](#docker-scenarios-timelines-and-checksums) and [raw/B14-docker-timeline.json](raw/B14-docker-timeline.json). |
| B14-R03 | open (B08 owner) | `listJobEvents` serializes `created_at` with microseconds; docs/contracts.md requires exactly 3 ms digits + `Z`. `getJob` and the new `listJobCheckpoints` are correct. | B08 route re-serializes through `response_model`. | probe on 2026-09-26: event `created_at` has 6 fractional digits | Owning task returns the wire body or a field serializer; a test asserts 3 digits. Not fixed in B14 (out of scope). | — |
| B14-R04 | open (contract clarification, Low) | On a `restart_safe` Job, when every checkpoint is incompatible with the claiming worker, restore falls back to input (`CHECKPOINT_FALLBACK_TO_INPUT`). `workloads-checkpoints.md` step 5 (line 154) allows this. The relocation paragraph (line 173) says incompatible Jobs "remain blocked with visible reason". | Restore runs at claim, after dispatch has already matched the worker. The steps do not say whether "none valid" includes "none compatible". Dispatch eligibility already keeps unsupported architectures away, so this needs a worker whose inventory changes between dispatch and claim. | `test_incompatible_architecture_stops_at_identical_compatibility` (scan stops at identical compatibility; the fallback event follows) | Contract owner decides between keeping the fallback for `restart_safe` and blocking with `waiting_for_compatibility`. PLAN/contract updated first if it changes; then a test for the chosen branch. | — |
| B14-R05 | open (owner B11/B15, Low) | A crash-truncated `downloads/<attempt>/input.json` is trusted on replay (`if not source.exists()`). The run then fails `INVALID_INPUT/INPUT_CHECKSUM_MISMATCH`, which is terminal, although the input blob is intact. | B11 download writes to the final name and replay checks existence only. B14 fixed the same pattern for `restore-state.json` (S5) but did not change the B11 input path. | none yet. It would copy `test_partial_restore_download_left_by_a_crash_is_fetched_again` for `input.json`. | Owning task applies the size/checksum re-fetch to the input download and adds that test. | — |
| B14-R06 | fixed in round 2, chờ Task Review | The runner's first progress frame always reported step 0, also after a restore; the contract says `completed_step / iterations`. | `emit_progress(fraction=0.0, step=0)` was hard-coded at start. | U `test_first_progress_reports_the_restored_cursor` | The first progress after a restore equals the restored step. | Fix in `trusted_runner.py`; 4 cases pass in the default suite; mutation: 2 of 4 failed. |
| B14-R07 | fixed in round 2 (non-blocking), chờ Task Review | `_atomic_write` used `os.chmod` on the path, which follows a symlink placed in `/output`; `trusted-runner.md` said "private 0440 copy". | Mode set by path instead of by descriptor. | U 0440 mode assertions in `test_runner_checkpoint_b14.py` | `fchmod` and corrected wording. | `os.fchmod(handle.fileno(), mode)`; `trusted-runner.md` says "a read-only (0440)" copy. No symlink-race test. |
| B14-R08 | fixed in round 2, chờ Task Review | `WorkerApiClient.upload_artifact` raised `ValueError`/`KeyError` on an unexpected committed answer; it escaped the checkpoint and result steps, wedged the loop, blocked READY and starved later attempts; the attempt was never failed. | No validation of the upload answer and no mapping of its error. | U `test_invalid_committed_upload_answer_fails_only_that_attempt` (5 cases) | A mismatch fails `INTERNAL/CHECKPOINT_PROTOCOL_ERROR` with cleanup, and other attempts keep running. | The test asserts that failure, one cleanup, nothing published, and that another adopted attempt still gets its turn. Mutation: 5 failed. The result path maps the same error to `INTERNAL/INVALID_RESULT`. |
| B14-R09 | fixed in round 2, chờ Task Review | The server committed the result reserve, the worker crashed before `ResultFlow` journaled it, and `reconcile_adoption` then raised `RuntimeError` on every adoption replay. | B14's strict adoption check did not explain the result-reservation window. | U `test_result_reserve_crash_window_is_adopted_and_completes_once`, `test_adoption_reconcile_explains_a_result_reserve_committed_before_the_journal`, `test_adoption_reconcile_refuses_unexplained_differences` | Explain the window as the checkpoint side does, or fail/stop with cleanup. | Explained only when the journaled `reservation_callback_id` equals the server's `callback_id` and no different answer was journaled; one completion under the new incarnation. Mutation: 1 failed (`adoption reservation snapshot mismatch`). S6 also adopts a live container with an open checkpoint reservation. |
| B14-R10 | open (non-blocking) | A checkpoint tick that races a stopping runner is labelled `CHECKPOINT_PROTOCOL_ERROR` instead of `WORKLOAD_EXIT_NONZERO`. | Reported by review round 1; not analysed further in round 2 (outside the blocking set). | — | A later fix labels the race `WORKLOAD_EXIT_NONZERO` and adds a test. | — |
| Observation (review round 1) | open (non-blocking) | A blob `not_found` at restore marks the checkpoint `CORRUPT` one-way, even when the storage root is missing or mounted late. | Reported by review round 1; not changed in round 2. | — | — | — |
| B13-R12 | open (unchanged) | Decimal `search_path` defect on `fairness_ledgers` ANALYZE/restore, rooted in B05. | see B13 evidence | see B13 evidence | user decision | — |

## Raw evidence

- [raw/B14-images.json](raw/B14-images.json): round-2 CPU and worker image
  builds (commands, times, digests/ids, labels, sizes, engine), the stale
  round-1 CPU digest and the old pre-B14 digest as history.
- [raw/B14-verification.json](raw/B14-verification.json): round-2 results
  (`round2`: lint, default suite, `--lf` rerun, PG B14, full PostgreSQL suite,
  Docker S1–S6 and the B09/B10/B11 Docker regression) and the round-1 history:
  suite counts per round, probe window, PostgreSQL log cause count, and the 59
  test IDs that were `blocked` in round 1 (all ran in round 2). It holds test
  IDs and counts only; no connection string or password.
- [raw/B14-docker-timeline.json](raw/B14-docker-timeline.json): written by
  the round-2 S1–S6 run (`NEXA_B14_EVIDENCE_OUT`): image, worker image,
  iterations, interval, fairness scores and, per scenario, the result
  checksum and the timeline (IDs, states, reason codes, times, checksums; no
  credential, input or checkpoint content).
