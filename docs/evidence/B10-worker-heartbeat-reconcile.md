# B10 worker heartbeat/reconcile evidence

Date: 2026-09-22. Source checkout: `main` at `81395e6` with uncommitted B10
changes; no branch, commit, push, PR or external deployment was performed.
The controlling host is macOS. PostgreSQL 17 and the B10 runtime scenario ran
inside Docker Desktop's Linux arm64 VM, **not** on a bare Linux server. All
source tests used `PYTHONPATH=src` to load this checkout.

The latest runtime test used image `nexa/b10:review` for dependencies, mounted
the checkout read-only at `/project`, and loaded `/project/src`; it did not
claim that the image alone contains the latest changes. PostgreSQL was reached
through `--network container:nexa_b10_review_pg` at port 5432. The runtime temp
directory was moved to the host Trash after the test; existing Compose stacks
were left unchanged.

Review snapshot SHA-256 (uncommitted source):

```text
2e3049ab52b88185554efa4716ee215faf0fea68028ea59362dd3b9a759809fc  src/nexa/worker/agent.py
1f08e8744ff218e7a6bc510658610f31bebf043965d1d71e7b6959c44a341df7  src/nexa/worker/state.py
af180b193253308a6888f0831e2afe7f2c8be08156b882330a35b33ac9971a7f  src/nexa/worker/main.py
071a981cc6cf668bf55941baee32360a4bd36626d1cd650131cc0d65318b1787  src/nexa/worker/journal.py
```

| Requirement/gate | Command or assertion | Result | Evidence boundary |
|---|---|---|---|
| OS singleton/private credential/pending callback | `PYTHONPATH=src .venv/bin/python -m pytest -q tests/worker/test_local_state_b10.py tests/worker/test_client_b10.py` | pass in latest focused/full run | Two local processes contend; file modes, first-send persistence, boot mismatch, no implicit HTTP retry. |
| B10 wire shapes | `PYTHONPATH=src .venv/bin/python -m pytest -q tests/application/test_worker_schemas_b10.py` | pass in worker/application run | Request bounds and closed shapes; not API authority. |
| B10 API/PostgreSQL | `NEXA_TEST_DATABASE_URL='postgresql+psycopg://nexa_test:nexa_test_password@127.0.0.1:15437/nexa_b05_test_b10_review' PYTHONPATH=src .venv/bin/python -m pytest -q --run-postgres tests/integration/test_worker_api_b10.py tests/integration/test_worker_auth_b10.py tests/integration/test_worker_authority_b10.py tests/api/test_operation_matrix.py` | pass: 29, 2 warnings, 32.90s | Real migrated PostgreSQL 17 + FastAPI. Fixture seeds authorized attempt/allocation/lease; fixture is not a production allocator. |
| Pagination and false READY | 101 fixture allocations, page 100+1; cursor replay after drain returns 409; heartbeat without page remains STARTING | pass | Real API/DB; not worker-side reconciliation of these rows. |
| Adoption/renewal | Exact prior Authority/container fixture; reservation IDs/callbacks transfer; same callback replays stored ack; progress conflict/expiry returns 409 | pass | Real API/DB fixture; this row alone is not the live-container proof. |
| B09 executor/runner regression | `NEXA_B09_IMAGE_REF='nexa/cpu-iterative@sha256:341d943487940cb67f9e5ef61c2334593d8eab99619fc0f977e4f28a6d0e8e9a' scripts/b09_docker_tests.sh --require` | latest: 11 passed, 1 skipped, 102.59s; earlier rerun: 10 passed, 1 failed, 1 skipped, 121.56s; isolated resource-bounds rerun: 1 passed, 56.62s | Docker Desktop Linux VM B09 scenarios; the rerun hit a 15-second PID-probe subprocess timeout after reporting `True:123`. Isolated and final full reruns passed; the earlier timeout remains recorded and no B09 test bound was relaxed. |
| Worker/runner unit regression | `PYTHONPATH=src .venv/bin/python -m pytest -q tests/worker tests/workloads tests/application/test_worker_schemas_b10.py` | pass: 185, 10.90s | Unit/fault tests include pagination, ARM64 identity comparison, pending heartbeat replay, runner ACK, serialized pending state, readiness fail-closed, timeout convergence and real agent adoption/renewal with a simulated API clock advancing 50 seconds during the remaining scan (45-second lease). |
| Integrated worker kill/restart with live B09 runner | Linux container: `NEXA_B10_RUNTIME_EVIDENCE=1 NEXA_B09_IMAGE_REF='nexa/cpu-iterative@sha256:341d943487940cb67f9e5ef61c2334593d8eab99619fc0f977e4f28a6d0e8e9a' NEXA_TEST_DATABASE_URL='postgresql+psycopg://nexa_test:nexa_test_password@127.0.0.1:5432/nexa_b05_test_b10_review' pytest -q -s --run-postgres tests/docker/test_b10_worker_restart.py` | pass: 1, 22.62s | Linux container ran FastAPI/PostgreSQL, real B09 Docker runner and worker subprocess in one monotonic clock domain. Worker adopted and renewed a live attempt, was SIGKILLed, restarted with a new incarnation, rebound the journal, renewed again, returned READY, preserved credential hash, kept one active grant, and left the runner container running. This is Docker Desktop Linux VM evidence, not bare-Linux portability or independent review. |
| Historical Compose syntax and startup/restart smoke (2026-09-21) | `docker compose -f compose.yaml config --quiet`; recreate `api`/`worker` with the exact verified CPU digest, then SIGKILL the worker and `docker compose up -d worker` | pass | Project `nexa_b10_smoke3`: API/Caddy/DB healthy; worker PID `7511` at incarnation sequence `12` was killed and relaunched as PID `26765` at sequence `13`; health returned `READY`, heartbeat/reconciliation/poll returned HTTP 200, and credential hash `1ccdf4584bf54473b67c3acbfd8a49e88c6f17cb6fb342c2b375ec99a3ea0109` was unchanged. Docker Desktop Linux VM only; not clean-host or release acceptance. |
| Historical B10 image and frozen lock | `docker build -f deploy/b10/Dockerfile -t nexa/b10:local .`; `docker run --rm --mount type=bind,source=<checkout>,target=/project,readonly --workdir /project nexa/b10:local uv lock --check`; `docker run --rm nexa/b10:local uv sync --frozen --all-groups --no-editable` | pass | Local arm64 image/build and ephemeral uv environment; Compose smoke is recorded separately and bare-Linux runtime remains unproven. |
| Historical full Python/PostgreSQL suite (before R23/R27 follow-up) | `NEXA_TEST_DATABASE_URL='postgresql+psycopg://nexa_test:nexa_test_password@127.0.0.1:15437/nexa_b05_test_b10_review' PYTHONPATH=src .venv/bin/python -m pytest -q --run-postgres` | prior snapshot: 692 passed, 12 skipped, 3 warnings, 167.60s; earlier: 689 passed, 12 skipped, 1 failed, 159.64s | Earlier B08 cursor-tamper failure passed in isolation and on the latest full run. Non-canonical base64 decoding remains a separate B08 finding; a green rerun does not prove it fixed. |
| Lint/format/diff | `.venv/bin/ruff check .`; `.venv/bin/ruff format --check .`; `git diff --check` | pass: 255 files formatted and no diff errors | Local source verification, not runtime. |
| ACC-13/16/21 scoped | API authority, heartbeat/pagination, worker journal rebind and live runner restart assertions above | not-run (full shared gate) | The B10 slice has runtime evidence in Docker Desktop Linux VM. These shared gates remain **not-run** as complete acceptance gates; the rows above cover B10 subchecks only. Bare-Linux portability, GPU, full cleanup/reaper and independent Task Review remain open. Do not mark shared ACC gates PASS. |

Early test failure: an initial database name without the guarded
`nexa_b05_test_` prefix was rejected by `tests/conftest.py` before setup; the
guard was preserved and a correctly named isolated database was created.
A subsequent 503 from `max(uuid)` in PostgreSQL was reproduced directly and
fixed to aggregate the string representation. The no-page READY test failed
first, then passed after adding durable reconciliation progress in migration
`20260921_0004`. A second-incarnation inventory test also failed first on
duplicate inventory version 1, then passed after allocating from the maximum
stored version under the Worker lock.

## Remaining B10 limits

- B11/B15 failure, cleanup, reaper, result publication and release endpoints
  remain outside this task. The worker keeps unresolved authority pending and
  never treats process exit or a missing container as release proof.
- The integrated restart proof uses Docker Desktop's Linux VM. Bare-Linux
  cgroups, clean-host portability, GPU and release acceptance are not proven.
- `uv` is absent from the macOS host PATH. The rebuilt B10 image passed the
  frozen lock/sync checks; this is not a host `uv` run.

**Handoff:** B11 can consume the authenticated B10 incarnation, pagination,
heartbeat, null-poll, adoption and renewal API service. B15 still owns
failure/cleanup/reaper state transitions and release acknowledgment. B10 has
implementation and scoped runtime evidence, but is not approved: `B10 đã triển
khai và kiểm chứng trong phạm vi đã ghi nhận; chờ Task Review độc lập.`

## Review remediation map

- **R16:** shielded in-flight operations converge before another thread starts;
  shutdown awaits retained work; race tests assert `peak_active == 1` and late
  completion cannot restore READY.
- **R19–R20:** reconciliation resets readiness, validates immutable labels,
  catches loop errors, and treats a missing CREATE_IN_FLIGHT container as
  unresolved.
- **R21/R24:** pending callbacks use a serialized locked writer and runner state
  uses an atomic updater under the attempt lock.
- **R22–R23:** unverified cleanup remains durable across replay; orphan cleanup
  uses the executor proof/journal path and exact identity.
- **R25:** `run()` starts renewal supervision with reconciliation. Live identities
  are inspected/adopted as pages arrive, before the full orphan scan; cleanup
  requiring a full inventory remains deferred. Adoption/pending replay and
  renewal share the attempt lock. The regression failed before this ordering
  change and passed afterwards: actual agent renewal callbacks extend a mock
  API lease while its clock advances 50 seconds through the remaining scan.
  This is deterministic unit evidence, not a 50-second real PostgreSQL outage
  test; unavailable adoption/renewal still fails closed. Live restart passed
  again on the latest source mounted read-only into the Linux test container.
- **R26:** `docs/project-structure.md` and `docs/worker-agent.md` now describe
  the implemented nonempty reconciliation/adoption/runner-control lifecycle.

## R23/R27 follow-up after independent review

The user closed R16, R19, R20, R21, R22, R24, R25 and R26; this follow-up only
changes cleanup recovery and completed-operation timeout retry. R23/R27 are
implemented and awaiting independent re-review, not self-approved.

`PYTHONPATH=src .venv/bin/python -m pytest -q tests/worker/test_agent_b10.py
tests/worker/test_reconciliation_b10.py -k 'docker_timeout_retries or revoked_cleanup'`
passed **11 tests** (0.47s). The initial five reproductions failed before the
fixes: Docker discovery was called once instead of retrying; revoked cleanup
fell through to failure; both crash windows failed to recover cleanup. A
further identity-mismatch test failed before adding the observed-identity guard.

- **R23, success:** revoked authority takes a cleanup-only path. A strict peer
  rejects stale failure with 409; no such request is emitted. Verified cleanup
  drains pending state and removes the deleted container from the readiness
  inventory. Missing evidence or mismatched expected/observed identity stays
  unresolved and sends no cleanup proof.
- **R23, crash/restart:** inject process-like abort after Docker removal but
  before `finish_cleanup`, and after `TOMBSTONED` but before pending callback
  persistence. Reopen `ExecutionJournal` and `PendingOperationStore`; the real
  B09 executor resumes `CLEANUP_IN_FLIGHT` or reconstructs the persisted stopped
  proof from `TOMBSTONED`. No second stop occurs. Test both immediate verified
  release and `verified:false` followed by another restart and verified release;
  the latter reuses one durable callback ID and blocks readiness until verified.
- **R27:** an operation that raises `TimeoutError` has completed and is cleared
  for retry. A still-running operation remains shielded and retained. Tests
  cover immediate Docker timeout and a slow operation that outlives the wait
  timeout before raising its own timeout; each retries successfully, restores
  local readiness, and keeps `peak_active == 1`.

These are worker unit/fault tests with real durable local state and executor,
an injected Docker backend and a cleanup contract peer. They do not establish
production B11/B15 cleanup/release transactions or full ACC-16/21 acceptance.
The current focused, API/PostgreSQL and Docker regressions are listed above;
the earlier full PostgreSQL run remains explicitly historical.
