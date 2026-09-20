# B07 Artifact Store Evidence

## Revision and scope

- Working tree: implementation changes are uncommitted; no user changes were present
  at the start of the task (`git status --short --untracked-files=all`).
- Scope: filesystem store, bounded public upload, metadata/list/download API, tenant
  reservation counter and idempotency seam. No job/worker/checkpoint/GC runtime claim.
- Database: PostgreSQL 17 container `postgres:17`, test database name prefixed with
  `nexa_b05_test_`; migration head `20260920_0003`.
- Host: macOS development host. This is not Linux filesystem portability evidence.

## Commands and results

| Command | Result |
|---|---|
| `PYTHONPATH=src .venv/bin/pytest -q` | `292 passed, 163 skipped` (PostgreSQL-marked tests skipped) |
| `NEXA_TEST_DATABASE_URL='postgresql+psycopg://nexa:nexa-test@127.0.0.1:55432/nexa_b05_test_b07' PYTHONPATH=src .venv/bin/pytest -q --run-postgres` | `455 passed` on PostgreSQL 17 |
| `.venv/bin/ruff check .` | passed |
| `.venv/bin/ruff format --check .` | passed |
| `git diff --check` | passed |

The repository bootstrap command using `uv` was not available in the shell PATH;
the checked-in `.venv` was used for the commands above. Full frozen `uv sync` and
hosted CI were not re-run in this task.

## Direct behavior evidence

- Store tests cover exact checksum/size, bounded append, missing/symlink/traversal
  keys, full and single-range reads, fsync fault, rename fault, directory-fsync
  fault, and expired/mismatched GC claims.
- PostgreSQL tests cover one committed artifact, durable counter conversion,
  same-key replay, quota rejection, tenant isolation, storage-write failure replay,
  expiry reservation release, cancellation cleanup, expiry lock order, API/OpenAPI
  binary/schema contracts and the additive B07 migration using independent
  SQLAlchemy transactions.
- Metadata is inserted only after the filesystem commit sequence; staging keys are
  rejected by `open`/`inspect`; response bodies contain no blob key or filesystem path.
- Orphan cleanup uses an internal capability registry plus a service-side database
  reachability check; persistent claim/recheck serialization for committed references
  remains a B19 GC responsibility and is not claimed as complete here.

## Gate status

- Direct B07 unit/API/persistence evidence: pass for the scenarios listed above.
- ACC-03, ACC-06, ACC-07, ACC-17, ACC-23, ACC-24, ACC-28 and ACC-39: implementation
  evidence is present for the B07 slice, but the full project gates remain `specified`
  until their downstream tasks and independent review complete.
- Not run: Linux portability, Docker/worker authority, checkpoint/restore, B19
  metrics/alerts/full GC, load/soak/chaos, hosted CI and release evidence.
- ROADMAP status: B07 implementation evidence recorded; awaiting independent Task
  Review. B01-B06 statuses are unchanged.
