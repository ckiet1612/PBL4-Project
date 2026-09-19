# B05 PostgreSQL schema, migration and constraint evidence

- **Task:** B05 — PostgreSQL physical schema, Alembic migration, constraints, indexes and transaction helpers
- **Implementation status:** the latest independent review closed B05-R02/R04 and kept only R06 open; adjusted-exponent validation is now remediated and locally verified, awaiting independent re-review
- **Baseline revision:** `744e9077b3461c92b4358e7a1ebac96dc7bcb5d5`
- **Working context:** existing `main` checkout, no branch/worktree/commit/push/reset
- **Evidence date:** 2026-09-20, Asia/Ho_Chi_Minh
- **Contract:** `1.0.0-b01`; schema generation `1`; Alembic head `20260919_0001`
- **Direct B05 blocker:** independent Task Review approval has not been granted

## Scope delivered

| Area | Files / behavior | Evidence |
|---|---|---|
| Physical model | `schema_v1.py` freezes the B05 generation; `schema.py` clones the current metadata | 52 tables cover every required logical group plus schema metadata, GPU claims, fairness state and committed-artifact reference guards |
| Migration | Alembic config/environment and revision `20260919_0001` | Clean/repeat upgrade, pre-write downgrade/re-upgrade, failure rollback, one head, frozen revision and metadata parity |
| Identity and ownership | Lowercase unique identity, membership aggregate, composite tenant/job/session/attempt FKs, allocation-ledger and nullable-event ownership, generated UploadSession owner FK | Direct SQL failures plus two-connection owner delete/tenant-transfer races, valid global events and unreferenced-upload cleanup |
| Authority and resources | Worker-bound attempt/allocation/lease/grant lineage, unique attempt/incarnation, one current grant, per-device GPU claim | Worker mismatch INSERT/UPDATE and repeated incarnation fail; valid A-to-B adoption passes; GPU race/quarantine/version coverage remains |
| Recognized data | Every JobSpec/checkpoint/result/chunk/log/reference FK targets a guard that exists only for a `COMMITTED` artifact; identity/checksum triggers remain | Two-connection input/checkpoint/result recognition-vs-`DELETING` races serialize; staging guard injection and referenced mutation fail; unreferenced cleanup remains valid |
| Fairness persistence | Canonical exact Decimal text with shared Python/PostgreSQL exponent bounds, exact helpers and a bounded-prefix B-tree key | Exact tuple/trailing-zero round-trip, `1E-20000`, two hard-to-compress 5,000-digit scores, exact ordering and invalid exponent rejection |
| Transaction primitives | Engine/session, sorted row locking, CAS, DB clocks, bounded retry | Helper-driven atomic commit/rollback covers job, event, admission counter and idempotency; CAS loser has no event/counter side effects |
| Schema compatibility | Guard reports missing/old/current/new/unknown and raises fail-closed | Guard tests verify no auto-migration |
| CI configuration | PostgreSQL 17 service in `.github/workflows/ci.yml` | Read-only workflow runs frozen Python install, Ruff and `pytest -q --run-postgres`; hosted execution not observed |
| Documentation | `docs/database.md`, README, project structure and ROADMAP | Logical-to-physical mapping, commands, limits and B06 handoff |

The complete entity/storage/key/constraint/index/runtime-obligation mapping is in
[database.md](../database.md). `ExecutorAttemptRecord` is explicitly worker-local durable
state and is not a PostgreSQL table.

## PostgreSQL 17 environment

| Component | Observed value |
|---|---|
| Host used for development/tests | macOS/Darwin arm64; not Linux runtime acceptance evidence |
| Database container | `nexa-b05-postgres-17`, image `postgres:17`, task-owned |
| Network exposure | container port 5432 bound only to `127.0.0.1:55432` |
| Server | `PostgreSQL 17.11 (Debian 17.11-1.pgdg13+2) on aarch64-unknown-linux-gnu`, 64-bit |
| `server_version_num` | `170011` |
| Test database | `nexa_b05_test_main`; isolated user/password used only for this task |
| Python | CPython 3.12.13 |
| `uv` | 0.12.15 isolated binary at `/tmp/nexa-b05-uv.W3wP0r/extracted/uv-0.12.15.data/scripts/uv`; system PATH unchanged |
| Locked database packages | SQLAlchemy 2.0.54, psycopg 3.3.5, Alembic 1.18.5 |

The guarded fixture accepts only `postgresql+psycopg`, a loopback/CI `postgres` host,
database names starting with `nexa_b05_test_`, PostgreSQL major 17, and a target different
from `NEXA_DATABASE_URL`. It drops/recreates only the `public` schema of that guarded test
database before and after each integration test.

## Requirement-to-test evidence

| Requirement / source | Implementation | Direct test/evidence |
|---|---|---|
| Complete B01 PostgreSQL model; user §5 | 52-table metadata plus filesystem/worker/cache classification | `test_schema_metadata.py`; migration parity; `docs/database.md` |
| UUIDv7/Python 3.12 compatibility; user §6 | RFC 9562 application-server generator using `time_ns`, `secrets` and process-monotone ordering | `test_ids.py` verifies RFC version/variant and 1,024 sequential values are unique and monotone; no concurrency/boundary claim |
| UTC/int domains/hash-only credentials | `timestamptz`, signed int64 checks and hash columns | metadata tests plus direct SQL constraint suite |
| Lowercase unique tenant/user identity and empty membership aggregate | normalized checks/unique indexes; deferred tenant/membership-set triggers | normalized INSERT/UPDATE, empty aggregate commit and aggregate delete-rejection tests |
| Cross-tenant and exact provenance on INSERT/UPDATE | composite unique/FKs, nullable-event pairing, polymorphic validation and generated UploadSession owner FK | allocation-ledger mismatch; event tenant/job pairing; reference INSERT/UPDATE plus two-connection owner delete/tenant-transfer races |
| One job/logical session/spec; monotone fence; terminal/spec immutable | deferred completeness and job/spec update triggers | incomplete insert/delete, fence decrease, immutable spec and terminal job tests |
| One current attempt authority/job and exact adoption lineage | partial current-grant unique, worker-bound grant/lease/allocation/fence FKs and unique `(attempt,worker incarnation)` | worker mismatch INSERT/UPDATE, repeated A-to-B-to-A incarnation rejection and valid A-to-B adoption tests |
| Active GPU UUID exclusive across inventory versions and quarantine | `allocation_gpu_claims` partial unique `(worker_id,gpu_uuid)` plus deferred release consistency on INSERT/UPDATE/DELETE | deterministic two-connection race, delete rejection, quarantine/version and release-together tests |
| Reservation/result/checkpoint/event uniqueness and recognition | partial uniques, job-wide checkpoint sequence, committed-source triggers and artifact guard FKs | reservation/sequence cases, generic/chunk/log checks, post-recognition mutation, staging-guard rejection and input/checkpoint/result two-connection races |
| Exact idempotency and callback scopes | non-null closed context with unique four-tuple; callback three-tuple | global/null/invalid/tenant/worker/bootstrap and callback duplicate tests |
| B04 Decimal compatibility | canonical `sign:digits:exponent` text; exact tuple decode; DB validation/comparison/order helpers; bounded index prefix; no float/quantization | `MIN_ETINY`, one/two-digit, trailing-zero and zero-at-`MAX_EMAX` boundaries; adjusted-exponent rejection; `1E-20000`; two 5,000-digit significands and exact order |
| Independent tenant/user outstanding limits | `outstanding_limit` and positive `user_outstanding_limit` on each tenant policy | metadata plus PostgreSQL distinct-value round-trip and zero rejection |
| Lock/CAS/atomicity/retry/time/lifecycle | helpers under `persistence/` | helper-driven full commit/rollback, CAS-loser side-effect check, stable/advancing DB clocks, session close and pool return tests |
| Queue/recovery lookup indexes | named partial/keyset/access-pattern indexes | reflection plus representative `EXPLAIN` using `ix_jobs_queue_head` |
| Migration lifecycle and serialization | transactional DDL, immutable v1 snapshot, session advisory lock | clean/repeat/down/up/failure, frozen future metadata test, two process runners waiting on advisory lock |
| Schema mismatch fail-closed | generation and Alembic head inspection only | missing/old/current/new/unknown tests; no auto-migration |

## Important physical decisions

- UUIDv7 is generated in server-side Python. No PostgreSQL extension or unavailable
  `uuid.uuid7()` is assumed, and UUIDv4 is not used as fallback.
- Historical revision behavior is frozen in `schema_v1.py`; future live metadata changes
  cannot alter initial clean-install DDL. A regression test mutates current metadata with a
  sentinel table and verifies the B05 revision does not create it.
- Migration runners use a session advisory lock. Lock acquisition is committed before
  Alembic begins DDL so the DDL transaction commits before unlock; two real Python/Alembic
  processes are observed waiting on PostgreSQL `advisory` locks before release.
- An explicit Alembic config URL cannot be replaced by `NEXA_DATABASE_URL`. The destructive
  fixture verifies the PostgreSQL major version before any `DROP SCHEMA` cleanup.
- Tenant provenance uses composite FKs rather than relying on ORM checks. `ArtifactReference`
  has a generated UploadSession-only owner key and composite FK, so reference INSERT and owner
  delete/tenant transfer serialize correctly even across independent transactions. Triggers
  continue to validate the other closed polymorphic owner types.
- Attempt, allocation, lease and grant identities carry the same worker, while each
  `(attempt_id, worker_incarnation_id)` may receive authority only once.
- A physical `allocation_gpu_claims` row enforces per-device exclusivity. Quarantine remains
  unreleased; allocation and claim release must commit together.
- Exact scheduler values use canonical Decimal text and immutable PostgreSQL helpers. Database
  validation and the Python codec share raw exponent bounds and the nonzero adjusted-exponent
  ceiling; trailing zeros count toward the exact tuple and zero remains valid at `MAX_EMAX`.
  The fairness B-tree stores only a 256-character significand prefix while exact queries retain
  the full order helper. Values with 5,000-digit significands therefore remain valid. B05
  neither changes B04 arithmetic nor sums per-allocation dominant shares into a false tenant
  aggregate.
- Delete policy is restrictive and history-oriented. Accepted specs/events/audits and
  recognized checkpoint/result/chunk/log rows are immutable. A committed-artifact guard row
  is the shared FK serialization point: protected mutation must delete it first, which fails
  when any reference exists, while unreferenced cleanup remains possible. Broad cascade delete
  is absent.
- Deferred triggers re-evaluate membership aggregates, GPU claim counts and recognized
  reservation state from the transaction's final rows at commit. A nonterminal job update
  may keep or increase `job_fence`, but cannot decrease it.
- Capacity/quota totals, authorization, fencing decisions, fsync/retention/GC, ledger tick,
  replay-before-`If-Match` and recovery state machines remain service/runtime obligations.

## Verification commands and observed results

The post-latest-review R06 remediation verification used the frozen non-editable package
required by repository bootstrap policy:

| Command/check | Observed result |
|---|---|
| `uv lock --check` | pass; 33 packages resolved |
| `uv sync --frozen --all-groups --no-editable --reinstall-package nexa` | pass; local package rebuilt and reinstalled |
| `uv run --no-sync ruff check .` | pass; all checks passed |
| `uv run --no-sync ruff format --check .` | pass; 115 files already formatted |
| Focused Decimal unit/PostgreSQL regression | pass; `39 passed, 8 deselected in 4.98s` after the expected RED run |
| Migration lifecycle/parity plus fairness persistence | pass; `31 passed in 9.67s` |
| `NEXA_TEST_DATABASE_URL=... uv run --no-sync pytest -q --run-postgres` | pass; `332 passed in 43.66s` |
| `pnpm --dir web install --frozen-lockfile` | pass; lockfile already up to date with pnpm 11.9.0 |
| `pnpm --dir web run typecheck` | pass; `tsc -b` |
| `pnpm --dir web run build` | pass; Vite 8.3.0 transformed 16 modules and emitted ignored `web/dist/` |
| PostgreSQL identity query | pass; version 17.11, `server_version_num=170011` |
| `git diff --check` | pass |
| Git status/untracked audit | pass; every B05 file is visible and all untracked files were reviewed separately |
| `git check-ignore -v --no-index .env` | pass; dedicated `.gitignore:42:.env` rule applies |

These are local results. GitHub-hosted CI was not run or observed, and the workflow
configuration result is not reported as hosted evidence.

## Acceptance scope

Applicability and project status remain separate. B05 supplies evidence for only the
database portion of these gates; `docs/acceptance.md` remains unchanged.

| Gate | B05 evidence | Project status / missing scope |
|---|---|---|
| ACC-07 | Correct durable idempotency/callback scope and uniqueness | `specified`; replay-before-`If-Match`, API responses, retention and one-time-secret behavior remain B06/B08/B15 |
| ACC-08 | Atomic transaction/CAS/rollback primitives and exact ledger persistence | `specified`; runtime counters, <=1 s accounting loop and restart reconciliation remain B08/B11/B13 |
| ACC-11 | Queue/keyset/retry indexes and representative query plan shape | `specified`; 100,000-job latency, bounded candidate retrieval and heap rebuild remain B13/B22 |
| ACC-14 | One current authority and one final result schema constraints | `specified`; live publish fencing/lease/desired-state races remain B11/B15/B20 |
| ACC-16 | Quarantine retains allocation/GPU claim and release consistency | `specified`; reconcile/cleanup proof and host-loss runtime remain B10/B15 |
| ACC-17/18 | Metadata/reference/provenance and committed reservation constraint | `specified`; filesystem durability, retention >=2, restore/fallback and GC remain B07/B14/B19 |
| ACC-28 | Clean/repeat/down/up/failure migration, parity, schema guard, constraints and concurrency | B05 subset `pass`; project gate remains `specified` until Linux readiness, maintenance upgrade/restore, backup compatibility and release-candidate evidence |
| ACC-39 | Local Ruff, full Python/PostgreSQL integration and unchanged UI checks | B05 subset `pass`; hosted CI, Playwright, images/scans, Linux/release checks remain future scope |

No simulator or schema test is used to claim Linux isolation, GPU support, recovery,
100,000-job load, soak, chaos, portability, backup/restore or release completion.

## Review status

Independent Task Review first ran 263 tests on PostgreSQL 17 and rejected B05 with
B05-R01–R07. The first remediation closed R01/R03/R05/R07 when the second independent review
ran 308 tests, but R02/R04/R06 remained open. The next remediation closed R02/R04 when the
latest independent review ran 317 tests; R06 remained open because SQL checked only the raw
exponent. The current R06 remediation again used a focused RED/GREEN cycle before the final
full suite:

| Finding | Remediation and direct evidence |
|---|---|
| B05-R01 migration target / fixture safety | Explicit Alembic URL wins over `NEXA_DATABASE_URL`; rejected PostgreSQL versions execute no destructive cleanup |
| B05-R02 ownership gaps | Allocation-ledger and event fixes remain; a generated UploadSession-only owner key plus composite FK now serializes uncommitted reference creation with owner delete/tenant transfer in either transaction order |
| B05-R03 worker authority gaps | Attempt/allocation/lease/grant identities agree on worker; `(attempt,worker incarnation)` cannot be reused; valid A-to-B adoption remains possible |
| B05-R04 mutable recognized artifacts | A committed-artifact guard is now the FK target for every consumer; input/checkpoint/result recognition racing `DELETING` cannot both commit, guard injection for `STAGING` fails, and unreferenced cleanup remains valid |
| B05-R05 missing user outstanding limit | `user_outstanding_limit` is persisted independently, round-trips distinctly and rejects zero |
| B05-R06 Decimal representation domain | The bounded score index and 5,000-digit round-trip/order remain; DB/codec now share raw exponent bounds plus the nonzero adjusted-exponent ceiling, including one/two-digit, trailing-zero, zero-at-`MAX_EMAX` and `MIN_ETINY` boundaries; raw SQL rejects `0:12:999999999999999999` |
| B05-R07 missing direct evidence | Added helper-driven transaction commit/rollback, CAS-loser side effects, reservation uniqueness, cross-attempt checkpoint sequence and sweep ownership INSERT/UPDATE cases |

The R02 tests first reproduced both owner mutations committing ahead of an uncommitted
reference. The R04 tests reproduced the same invalid final state independently for JobSpec
input, Checkpoint and Result. Earlier R06 regressions reproduced PostgreSQL error `54000` with
an index row of 5,040 bytes and a raw exponent escaping DB validation. The latest R06
regression reproduced SQL accepting `0:12:999999999999999999` while Python raised the wrapped
decode error; the SQL validator and direct `fairness_state` insert both failed RED, then passed
after adjusted-exponent validation. All boundary cases and preserved-domain cases were GREEN
in the final suite. This remediation record is not independent Task Review approval; B05 still
requires re-review.

## B06 handoff

1. Build one engine with `create_database_engine(NEXA_DATABASE_URL)` and a session factory
   with `create_session_factory(engine)`. Call `require_current_schema(engine)` during
   readiness before writes; never auto-migrate on import or request.
2. Run one complete domain mutation through `run_transaction`. The callback must contain
   only retry-safe database work. Acquire rows with `lock_rows` in contract order and use
   `compare_and_swap` for versioned aggregates.
3. Map database/infrastructure errors outside persistence. Do not add FastAPI/HTTP concepts
   to this layer, and do not allow worker code to import/use it.
4. For tests, provision PostgreSQL 17 with a dedicated `nexa_b05_test_*` database, set only
   `NEXA_TEST_DATABASE_URL`, run `alembic upgrade head` or use the migrated fixture, and
   execute pytest with `--run-postgres`.
5. B06 must still implement principal/tenant authorization, Argon2/session/token behavior,
   role/policy mutation, audit and secret handling. Schema constraints are defense in depth,
   not authorization.

B05 approval would close the direct dependency for B06. This implementation does not send
or create another task and does not claim that B06 or any runtime task is complete.
