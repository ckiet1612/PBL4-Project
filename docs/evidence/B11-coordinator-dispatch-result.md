# B11 coordinator, dispatch and fenced result — remediation evidence

Status: **local remediation verified; independent re-review pending, not approved**.
The original review of `HEAD a7d215a` rejected B11-R01–R16. This record
describes the current uncommitted working tree on that same HEAD; it does not
authorize a commit, deployment or release acceptance.

## Environment and checks

- PostgreSQL 17.11 (`server_version_num=170011`) in the isolated, destructive
  `nexa_b05_test_b11_review` database on the existing `nexa_b10_review_pg`
  container. Tests use migrations 0001–0007 and run sequentially. The local
  `/tmp/nexa_b11_test_runner.py` obtains this container's test connection,
  sets `NEXA_TEST_DATABASE_URL` and `PYTHONPATH=src`, then invokes Pytest with
  `--run-postgres`. It is an environment-local runner, not a repository script.
- Linux arm64 Docker Desktop VM on macOS; CPU workload image
  `nexa/cpu-iterative@sha256:341d943487940cb67f9e5ef61c2334593d8eab99619fc0f977e4f28a6d0e8e9a`.
  Worker image `nexa/b11-local:review`, image ID
  `sha256:dfc0b8e147681ecb956e6db257b200b9f154ee08a6ab6fdbb270f9951a8b7fd6`.
  This environment is not bare Linux or GPU hardware evidence.
- `PYTHONPATH=src .venv/bin/python -m pytest -q`: **544 passed, 255 skipped**,
  two Starlette/AnyIO dependency deprecation warnings. The default guard skips
  PostgreSQL and opt-in Docker tests.
- Guarded PostgreSQL B11 integration, OpenAPI, result validation and B10 worker
  auth/API/authority plus B07 artifacts integration ran **sequentially** via
  `/tmp/nexa_b11_test_runner.py -q tests/integration/test_*b11.py
  tests/api/test_openapi_b11.py tests/application/test_result_validation_b11.py
  tests/coordinator/test_runtime.py tests/integration/test_worker_auth_b10.py
  tests/integration/test_worker_api_b10.py
  tests/integration/test_worker_authority_b10.py
  tests/integration/test_artifacts_b07.py`: **70 passed**, two dependency
  warnings, 46.22 s. Their lock-order assertions require policy → receipt →
  worker, matching the deadlock regression.
- Real Docker IPC regression: `NEXA_B11_RUNTIME_EVIDENCE=1` with the pinned CPU
  image, `PYTHONPATH=src .venv/bin/python -m pytest
  tests/docker/test_b11_worker_ipc.py -q -x --tb=short -s`: **1 passed**.
  Worker reconciliation/dispatch and OpenAPI focused tests are covered by the
  default suite; targeted late-start and replay cases are described below.
- Real Docker vertical test, `tests/docker/test_b11_vertical.py`,
  with the same opt-in flag, pinned CPU image, worker image above and guarded
  PostgreSQL runner: **2 passed** in 96.22 s, two dependency warnings. The
  deliberate post-commit lost response logs
  `ASGI callable returned without completing response`; this is the injected
  fault, not an unexpected assertion failure.
- `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `git diff --check`
  and `docker compose config --quiet` with required placeholder values: pass.
  Compose validation starts no services.

The first Docker case submits two CPU jobs through the real FastAPI app for
distinct memberships/tenants, runs a separate coordinator process and Linux
worker container, claims and downloads each input graph, then uses the trusted
Docker runner. Both `GET /jobs/{id}/result` calls return recognized Results,
and both output downloads equal independently computed CPU adapter bytes. One
`/claim` and one `/complete` response have their bodies discarded **after API
commit**; the same worker incarnation retries the original callbacks. The
database contains precisely two claim receipts, two completion receipts and
two Results. Cleanup releases both allocations. Each allocation's
ledger segments form a continuous, closed chain from `held_at` to `released_at`;
per-tenant score and segment charges agree within `1e-40` (Decimal rounding at
50 digits). GLOBAL/TENANT/USER outstanding and active counters are zero. The
test restarts the worker into a new incarnation and restarts the coordinator,
then verifies the recognized Result IDs, scores, charged segments and counters
have not changed. The second case kills the worker after completion commits
while the allocation is still held, before cleanup; a new incarnation verifies
the committed completion receipt against the journal, cleans the original
container under its stored grant and reaches READY with the allocation released.

The preceding failures were reproduced before their fixes: callback receipt
insert before the policy lock deadlocked against dispatch; 100 ms Docker relay
receive missed real runner messages; `docker cp` could not see runner tmpfs
output; periodic reconciliation reset READY before each heartbeat; renewal of a
succeeded attempt could starve another held allocation; and simultaneous IPC
receive/result control connections contested the runner's serial socket. The
worker now reads a bounded exact output through `docker exec tar`, serializes
control on the attempt lock and waits for a persisted result frame to finish
before reopening IPC. Focused regressions went red before their respective fixes.
The latest reconciliation regression forced the page snapshot to precede a
local journal bind/start and Docker discovery. It initially stopped that live
container; after the fix, the container remains running, its journal stays
`STARTED`, and reconciliation holds readiness until the next server snapshot.
Old-incarnation orphan cleanup remains covered separately. Renewal continues
in its independent loop during long scans.
Another regression simulated Docker start crossing the dispatch-based 30-second
server deadline. Before the fix, `/start` rejected it while the worker left
the container and allocation unresolved. The HTTP/PostgreSQL test now checks
that a live claimed attempt's failure atomically binds the exact first-observed
container, fences/quarantines, rejects a different stopped proof and releases
once after valid cleanup. Worker tests check an exact stop/removal when `/start`
returns 409, pending `202` cleanup replay, and local stop without a release
claim if failure reporting itself is rejected. The Docker vertical exercises
the ordinary start path, not this injected late-start fault. The PostgreSQL poll
regression expires the committed lease, confirms `200 {offer: null}`, restores
a future DB-time expiry, and confirms the same committed offer is then returned.
A further HTTP/PostgreSQL regression reproduced a server exception when an
identical cleanup callback replayed a committed `202`. Both variants are now
verified: pending → identical `202` → committed failure classification → same
callback `200`, and pending → another callback releases → original receipt
promotes to `200`. In both, a final replay preserves the verified acknowledgment,
changed payload conflicts, and the allocation, GLOBAL/TENANT/USER counters,
ledger rows and release event remain unchanged after the first release. This
fixture injects the recovery classification boundary; it does not claim to test
the B15 reaper. The two variants failed on the original `202` replay before the
fix and passed through the real app and PostgreSQL afterward.
An independent review then identified a policy `FOR SHARE` → `FOR UPDATE`
conversion deadlock between concurrent failure and cleanup callbacks. A
barrier-controlled PostgreSQL regression produced `DeadlockDetected` before
repair. Failure and cleanup now take `FOR UPDATE` before the receipt insert;
completion already took the exclusive policy/counter lock first. The three
fail/cleanup, fail/fail and cleanup/cleanup schedules pass without retry,
including unchanged counters. The callback policy-before-receipt exception
is now explicit in the concurrency contract. The guarded PostgreSQL and Docker
checks above were rerun after this change. The scoped independent reviews
found no further Important/Critical issue in either the `202` receipt or
policy-lock fixes; they
did not independently run the destructive database or Docker fixtures.

## Review closure map

| Finding | Local closure evidence |
|---|---|
| R01 | Docker vertical exercises offer → claim → graph download → start → renew → result prepare/batch/ready → cleanup without replacing orchestration methods. A committed lost claim response replays under the original callback; a rejected late start stops the local container and is fenced before proof-based release. The Docker IPC regression checks actual runner messages and output checksum. |
| R02 | `create_app`/PostgreSQL returns a typed closed `DispatchOffer`; poll replay returns the same offer, not a second attempt, and an expired lease returns `offer: null` until DB-time expiry is extended. The Docker vertical consumes the live offer. |
| R03 | `POST /fail` through the real app fences/quarantines, binds the first pre-start container observation if Docker created it before a rejected `/start`, validates exact cleanup proof, releases and replays without double-decrement; PostgreSQL completion/cleanup race test runs concurrently. HTTP/PostgreSQL tests also cover stable pending `202` replay and atomic promotion to `200`, including release by a different callback first; three controlled failure/cleanup callback races avoid the policy-lock upgrade deadlock. |
| R04 | Claimed input graph download verifies checksum, size, media and range; HTTP tests reject out-of-graph, cross-tenant and stale Authority. Docker worker downloads the real input. |
| R05 | Canonical manifest, output kind, same-attempt upload lineage, binding and reference graph validation reject malformed/foreign input; real runner result bytes and independent oracle agree. Competing completion callbacks recognize only one final Result. |
| R06 | `GET /jobs/{id}/result` hides mere reservation/upload, then returns the recognized Result with membership, scope and tenant checks; Docker vertical downloads both outputs. |
| R07 | Completion decrements outstanding once across all three scopes; cleanup alone releases active/held allocation. Concurrent completion/cleanup tests and Docker post-restart reconciliation check this. |
| R08 | Upload idempotency includes attempt/Authority lineage; cross-attempt replay conflicts, same-attempt adoption preserves Artifact ID, stale incarnation fails. |
| R09 | Complete → cleanup → identical callback returns the original acknowledgment; a new callback conflicts. Docker test drops one committed completion response and confirms worker replay. |
| R10 | Dispatch/reservation event names validate the schema; HTTP `GET /jobs/{id}/events` succeeds through transitions in PostgreSQL tests. |
| R11 | Seventeenth high-priority job wins the bounded window; quota/concurrency/capability blocks do not age, while occupied capacity ages. Reservation transition is exercised. |
| R12 | Capacity-changing heartbeat closes/rebases held segments at DB time; integration test verifies old/new share and recreated coordinator, while the Docker test compares ledger after restart. |
| R13 | Identical reservation callback replays original ID; a distinct callback conflicts; adoption retains reservation identity. |
| R14 | Claim/replay leave Job version/event sequence unchanged; start performs the transition. |
| R15 | First two retry continuation deadlines use `min(30, 2^(retry−1)) + jitter[0,1]` and commit with counters/state in HTTP/DB tests. The later B15 recovery loop is outside B11. |
| R16 | Updated operation matrix, guarded PostgreSQL races, two-tenant real CPU vertical, independent download oracle, post-commit claim/completion response loss, worker restart both after and before cleanup, coordinator restart and Result/allocation/counter/ledger reconciliation above. Snapshot/dispatch and rejected late-start interleavings have targeted regressions; the latter is HTTP/PostgreSQL plus worker/Docker-backend test, not a Docker-container fault run. |

This is B11 implementation and local integration evidence, **not independent
review approval**. Broader ACC-13/16/17/20/21/31 fault matrices and bare-Linux,
GPU, checkpoint, CLI, recovery and release gates have not been demonstrated by
these runs; they retain their separately defined applicability/status. A lost
claim response can be replayed by the **same** incarnation, as the Docker fault
shows. If the process dies after claim commit but before Docker create, a new
incarnation has no live grant to adopt and keeps the allocation held and READY
blocked; it cannot infer no-container proof from lease expiry or an empty scan.
Automatic fencing/reaper and subsequent recovery for that old claim belong to
B15, not this B11 slice. This availability limit remains an explicit handoff,
not a claim of the broader ACC-16 recovery gate.
