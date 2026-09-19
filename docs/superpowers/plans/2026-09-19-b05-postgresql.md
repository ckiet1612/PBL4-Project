# B05 PostgreSQL Schema, Migration, and Constraint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Deliver the complete B05 PostgreSQL 17 physical schema, Alembic migration, database constraints, transaction helpers, real-PostgreSQL integration tests, and evidence required to unblock B06.

**Architecture:** SQLAlchemy 2 Core metadata under `nexa.infrastructure.persistence` is the application-side schema map; a source-controlled Alembic revision creates the same objects and PostgreSQL trigger functions. Tenant provenance is enforced with composite foreign keys, GPU ownership with per-device claims, and cross-row invariants with deferred constraint triggers where a row-local `CHECK` is insufficient. The database layer exposes only engine/session, transaction, lock/CAS, database-clock, schema-guard, UUIDv7, and explicit scheduler-enum/Decimal conversion helpers; application services remain responsible for authorization, capacity/quota sums, and state-machine orchestration.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0, psycopg 3, Alembic, PostgreSQL 17, Pytest, Hypothesis, uv.

**Spec:** `PLAN.md` B05; `docs/contracts/domain-model.md`; `docs/contracts/concurrency-recovery.md`; `docs/contracts/state-machines.md`; user B05 implementation request attached to this task.

## Global Constraints

- Work in the current checkout and current `main` branch as explicitly authorized; do not create a branch/worktree, commit, push, reset, or rewrite history.
- PostgreSQL 17 is the only acceptance database; SQLite and mocks do not prove constraints, locking, migration, or concurrency.
- Domain and scheduler modules must not import SQLAlchemy, psycopg, Alembic, FastAPI, Docker, or PyTorch.
- Worker code must not access PostgreSQL; no module import may connect to a database or run a migration.
- Use server-generated UUIDv7 compatible with Python 3.12; do not call unavailable `uuid.uuid7()` and do not substitute UUIDv4.
- Use UTC `timestamptz`, signed 64-bit counters/versions/fences with domain checks, and canonical exact Decimal text plus database comparison/order helpers so PostgreSQL exponent limits cannot narrow B04 values; reject non-finite values explicitly.
- PostgreSQL isolation is `READ COMMITTED` plus explicit row locks, CAS, unique constraints, and bounded whole-transaction retry for SQLSTATE `40001`/`40P01` only.
- Transaction retry is at most three attempts with fresh 10-50 ms jitter; rollback and close the failed session before retry; never retry data/business conflicts or unknown commit outcomes.
- Migration execution is explicit and serialized with a PostgreSQL advisory lock; importing application modules never migrates.
- Destructive integration fixtures accept only a loopback test URL whose database name starts with `nexa_b05_test_`, and must reject the runtime `NEXA_DATABASE_URL`.
- Preserve existing B01-B04 behavior and evidence; B04 is approved according to `ROADMAP.md`, even though older README/evidence wording predates the final review.

## Requirement/Test Map

| Requirement | Source | Planned implementation | Direct evidence |
|---|---|---|---|
| Full B01 logical model and storage classification | Domain model; user §5 | `schema.py` tables plus `docs/database.md` entity map | Metadata inventory and migration parity tests |
| UUIDv7, UTC, integer domains, credential hashes | Contract index; user §6 | `ids.py`, UUID/timestamp columns, `CHECK` constraints, no raw-secret columns | UUID unit/property tests; SQL negative tests |
| Case-insensitive slug/username and empty membership aggregate | Domain model identity section | normalized lowercase columns with `lower(value)=value`, unique indexes, deferred tenant/membership-set trigger | Direct SQL insert/update tests |
| Tenant/provenance isolation | INV-01; domain uniqueness group 1 | composite unique keys/FKs and owner-validation trigger | Cross-tenant and wrong job/session/attempt insert/update tests |
| One job/session/spec; terminal/spec immutability | INV-08; state machines | deferred job-completeness trigger and immutability triggers | Commit-time positive/negative lifecycle tests |
| One current execution authority/job including CREATED offer | INV-08/09; dispatch contract | authority-grant lineage table with partial unique current job grant and exact composite FKs | Concurrent authority and wrong-lineage SQL tests |
| Allocation/GPU exclusivity including quarantine | INV-02; user §7c | per-device `allocation_gpu_claims`, active physical-device unique index, deferred allocation/claim consistency trigger | Two-transaction race, quarantine, inventory-version, atomic release tests |
| Capacity/quota sums remain transactional obligations | INV-02/07 | resource columns, counter/lock indexes, lock/CAS helpers; explicit docs limitation | Lock/CAS tests and database mapping notes |
| Reservation/result/checkpoint/event uniqueness | Domain model; user §7d | partial unique indexes and composite provenance constraints | SQL positive/negative uniqueness tests |
| Idempotency/callback exact scopes | Contract index; user §7e | non-null context/principal/operation/key unique scope; callback unique tuple | GLOBAL/BOOTSTRAP/WORKER and callback tests |
| B04 Decimal compatibility | B04 accounting; user §8 | canonical exact Decimal text, finite/non-negative checks, explicit Decimal conversion and immutable database comparison/order helpers | exact tuple/trailing-zero, `1E-20000`, close scores, invalid and ordering round-trip tests |
| Lock/CAS/atomicity/retry/DB clocks | Concurrency contract; user §9 | `database.py`, `transactions.py`, `locking.py` | independent-connection CAS/race, rollback, retry classification/limit/session disposal, clock semantics tests |
| Index access patterns | Domain index groups; user §10 | named queue/allocation/lease/history/idempotency/ledger indexes | reflection assertions and representative `EXPLAIN` shape checks |
| Alembic lifecycle and schema guard | PLAN B05; user §10 | `alembic.ini`, `migrations/env.py`, one reviewed head, schema metadata and guard | clean/repeat upgrade, downgrade/re-upgrade, failure rollback, one-head, guard-state tests |
| PostgreSQL 17 test mode and CI | PLAN §10/§13; user §11-13 | guarded pytest fixture, isolated Docker/CI service, explicit command | Local PostgreSQL 17 run and CI workflow configuration review |
| Documentation/evidence/roadmap | User §14/§16 | `docs/database.md`, README/structure/ROADMAP/evidence updates | Requirement-to-test table and final command record |

---

### Task 1: Dependency, UUIDv7, Decimal, and Persistence Package Foundation

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/nexa/infrastructure/__init__.py`
- Create: `src/nexa/infrastructure/persistence/__init__.py`
- Create: `src/nexa/infrastructure/persistence/ids.py`
- Create: `src/nexa/infrastructure/persistence/values.py`
- Test: `tests/persistence/test_ids.py`
- Test: `tests/persistence/test_values.py`

**Interfaces:**
- Produces: `new_uuid7() -> uuid.UUID`, `decimal_to_db(Decimal) -> str`, `decimal_from_db(str) -> Decimal`, `allocation_state_to_db(AllocationState) -> str`, `allocation_state_from_db(str) -> AllocationState`.
- Consumes: B04 `AllocationState` and Decimal arithmetic without changing domain code.

- [x] **Step 1: Write failing UUIDv7 and value-conversion tests**

```python
def test_new_uuid7_uses_rfc9562_version_variant_and_monotonic_order():
    values = [new_uuid7() for _ in range(32)]
    assert all(value.version == 7 and value.variant == uuid.RFC_4122 for value in values)
    assert values == sorted(values)


def test_decimal_to_db_rejects_non_finite_or_negative_values():
    for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-1")):
        with pytest.raises(PersistenceValueError):
            decimal_to_db(value)
```

- [x] **Step 2: Run the focused tests and confirm import/behavior failures**

Run: `.venv/bin/pytest -q tests/persistence/test_ids.py tests/persistence/test_values.py`

Expected: failure because the persistence modules and functions do not exist.

- [x] **Step 3: Add Alembic to the runtime manifest/lock and implement minimal helpers**

Implement UUIDv7 from a 48-bit Unix-millisecond timestamp plus monotonic 74-bit randomness under a process lock; use `secrets.randbits`, set version 7 and RFC 4122 variant bits, and never connect to external state. Keep persistence enum strings uppercase while mapping B04 lowercase `StrEnum` values explicitly.

- [x] **Step 4: Re-run focused tests**

Run: `.venv/bin/pytest -q tests/persistence/test_ids.py tests/persistence/test_values.py`

Expected: all focused tests pass.

### Task 2: SQLAlchemy Physical Schema Metadata

**Files:**
- Create: `src/nexa/infrastructure/persistence/schema.py`
- Test: `tests/persistence/test_schema_metadata.py`

**Interfaces:**
- Produces: `metadata: sqlalchemy.MetaData`, closed table constants, naming convention, and `SCHEMA_GENERATION = 1`.
- Consumes: UUID/value helpers from Task 1.

- [x] **Step 1: Write failing metadata inventory and boundary tests**

Assert the required table groups exist, all tenant-scoped reference tables expose composite foreign keys, fairness/rate columns use `ExactDecimalText`, secrets have only hash columns, and neither `nexa.domain` nor `nexa.scheduler` imports infrastructure dependencies.

- [x] **Step 2: Run metadata tests and confirm failure because `schema.py` is absent**

Run: `.venv/bin/pytest -q tests/persistence/test_schema_metadata.py`

- [x] **Step 3: Define focused SQLAlchemy tables and named constraints/indexes**

Create the authoritative tables for identity, template/job/session, worker/resource, policy/fairness, event/idempotency, and recognized data. Use composite `(tenant_id, id)` uniques for ownership; use typed resource columns instead of arrays; use `JSONB` only for closed snapshots/metadata whose structure is owned outside B05; use `ondelete=RESTRICT` for history/provenance and no broad cascade.

- [x] **Step 4: Re-run metadata tests**

Run: `.venv/bin/pytest -q tests/persistence/test_schema_metadata.py`

Expected: table inventory, type, FK, and dependency-boundary tests pass.

### Task 3: Alembic Environment and Initial PostgreSQL Revision

**Files:**
- Create: `alembic.ini`
- Replace: `migrations/.gitkeep`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/20260919_0001_b05_initial.py`
- Create: `tests/integration/conftest.py`
- Create: `tests/integration/test_migrations.py`

**Interfaces:**
- Produces: one Alembic head, explicit `upgrade()`/`downgrade()`, PostgreSQL advisory migration lock, and guarded `postgres_engine` fixture.
- Consumes: Task 2 metadata only for parity comparison, not to call `metadata.create_all()`.

- [x] **Step 1: Write failing migration lifecycle tests**

Tests call Alembic against the guarded PostgreSQL URL and assert clean upgrade, repeated upgrade, one head, downgrade-to-base/re-upgrade, transactional rollback of a deliberately failing temporary revision, and reflected metadata parity.

- [x] **Step 2: Run the PostgreSQL migration tests and confirm expected missing-Alembic/migration failure**

Run: `NEXA_TEST_DATABASE_URL=postgresql+psycopg://... .venv/bin/pytest -q --run-postgres tests/integration/test_migrations.py`

- [x] **Step 3: Implement the explicit migration**

Create every table, constraint, index, trigger function, and trigger through Alembic operations/DDL. Acquire a stable session advisory lock before `run_migrations()`. Seed only the singleton schema metadata row; do not seed product tenants/users/policies.

- [x] **Step 4: Re-run migration lifecycle tests**

Expected: clean/repeat/downgrade/re-upgrade/failure rollback/parity checks pass on PostgreSQL 17.

### Task 4: Provenance, Immutability, Authority, and GPU Constraint Tests

**Files:**
- Modify: `migrations/versions/20260919_0001_b05_initial.py`
- Modify: `src/nexa/infrastructure/persistence/schema.py`
- Create: `tests/integration/test_constraints_identity_jobs.py`
- Create: `tests/integration/test_constraints_authority_gpu.py`
- Create: `tests/integration/test_constraints_artifacts.py`

**Interfaces:**
- Produces: database-enforced composite ownership, deferred completeness, current authority, GPU claim, recognized-data, and polymorphic artifact-reference invariants.
- Consumes: migrated test database fixture.

- [x] **Step 1: Add direct-SQL failing tests for INSERT and UPDATE violations**

Cover cross-tenant and same-tenant wrong job/session/attempt references; terminal/spec immutability; missing JobSpec/LogicalSession at commit; retry/sweep provenance; two current grants; wrong grant lineage; final result/checkpoint/event/reservation uniqueness; and polymorphic artifact-owner validation.

- [x] **Step 2: Run focused constraint tests and confirm each missing constraint fails for the expected reason**

Run: `... pytest -q --run-postgres tests/integration/test_constraints_identity_jobs.py tests/integration/test_constraints_artifacts.py`

- [x] **Step 3: Add minimal named constraints/deferred triggers**

Implement deferred constraint triggers for tenant membership aggregate, job completeness, allocation/GPU claim consistency, and artifact owner/provenance validation. Use a partial unique current authority index that covers the grant created with a `CREATED` attempt, not only running attempt states.

- [x] **Step 4: Add deterministic two-connection GPU race tests, then implement/fix claim exclusivity**

Coordinate independent transactions with barriers and lock timeouts. Assert one active `(worker_id,gpu_uuid)` claim wins, quarantine retains it, inventory version cannot bypass it, and only a transaction that releases both allocation and claims may commit before reallocation.

- [x] **Step 5: Re-run all focused constraint tests**

Expected: negative SQL is rejected and positive adoption/history/cleanup cases commit.

### Task 5: Transaction, Lock/CAS, Database Time, Retry, and Schema Guard Helpers

**Files:**
- Create: `src/nexa/infrastructure/persistence/database.py`
- Create: `src/nexa/infrastructure/persistence/transactions.py`
- Create: `src/nexa/infrastructure/persistence/locking.py`
- Create: `src/nexa/infrastructure/persistence/schema_guard.py`
- Create: `tests/persistence/test_transactions.py`
- Create: `tests/persistence/test_schema_guard.py`
- Create: `tests/integration/test_transactions.py`

**Interfaces:**
- Produces: `create_database_engine`, `create_session_factory`, `run_transaction`, `lock_rows`, `compare_and_swap`, `transaction_timestamp`, `clock_timestamp`, `inspect_schema_compatibility`.
- Consumes: SQLAlchemy engine/session and Alembic head metadata.

- [x] **Step 1: Write failing unit tests for URL validation, redaction, retry classification, retry limit, and session disposal**

Use synthetic SQLAlchemy `DBAPIError` values only for retry classification; assert only SQLSTATE `40001` and `40P01` retry, at most three attempts, and every failed session rolls back/closes before fresh jitter.

- [x] **Step 2: Implement minimal engine/session/retry helpers and make unit tests pass**

The engine uses `READ COMMITTED`, `pool_pre_ping=True`, and no connection at import. Errors remain infrastructure exceptions with no HTTP dependency.

- [x] **Step 3: Write failing PostgreSQL tests for deterministic row locks, CAS, atomic rollback, and DB clock semantics**

Use independent sessions and barriers. Assert two writers with one expected version yield one winner; the loser commits no event/counter; an intentional exception rolls back state/event/idempotency together; `transaction_timestamp()` is stable within a transaction while `clock_timestamp()` advances after a lock wait.

- [x] **Step 4: Implement lock/CAS/time helpers and re-run integration tests**

Canonicalize UUID/string IDs before locking and issue `SELECT ... FOR UPDATE` in sorted order. CAS includes `version=expected` and increments exactly once.

- [x] **Step 5: Write and satisfy schema-guard tests**

Classify `MISSING`, `OLD`, `CURRENT`, `NEW`, and `UNKNOWN` from the schema generation row and Alembic version. The helper reports/raises only; it never migrates.

### Task 6: Fairness Persistence and Query Index Evidence

**Files:**
- Create: `tests/integration/test_fairness_persistence.py`
- Create: `tests/integration/test_indexes.py`
- Modify: `src/nexa/infrastructure/persistence/values.py`
- Modify: `src/nexa/infrastructure/persistence/schema.py`
- Modify: migration revision if tests expose missing checks/indexes

**Interfaces:**
- Produces: exact B04 Decimal round-trip/order behavior and reflected/index-plan evidence.
- Consumes: B04 `advance_accounting` and scheduling value objects.

- [x] **Step 1: Write failing Decimal round-trip and policy-order tests**

Persist/reload a 50-digit `1/3`, two scores differing in the last digit, virtual floor, large positive values near PostgreSQL practical boundaries, and invalid negative/NaN/Infinity values. Build equivalent scheduling snapshots before/after persistence and assert the same selected tenant/job.

- [x] **Step 2: Run the fairness integration test and confirm missing schema/conversion behavior**

- [x] **Step 3: Implement exact numeric validation/conversion and re-run**

Do not quantize or cast through float. Store ledger/floor/segment weight/share/charge as canonical exact Decimal text; immutable database helpers validate and compare values and derive deterministic index keys without converting to `NUMERIC`.

- [x] **Step 4: Write index reflection and representative `EXPLAIN` tests**

Assert the named queue, oldest-eligible, unreleased allocation/GPU, lease expiry, retry ready, keyset history/event, idempotency expiry, artifact/checkpoint/result ownership, ledger, and audit indexes exist. Use a fixture large enough for PostgreSQL to choose representative indexes with sequential scans disabled only as a diagnostic, not as a load claim.

- [x] **Step 5: Re-run fairness and index tests**

Expected: exact values/order survive persistence and index shapes are usable.

### Task 7: CI, Documentation, Evidence, and Status Synchronization

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `ROADMAP.md`
- Modify: `docs/project-structure.md`
- Create: `docs/database.md`
- Create: `docs/evidence/B05-postgresql.md`

**Interfaces:**
- Produces: reproducible PostgreSQL 17 CI/local commands, physical mapping, B06 handoff, and ACC-28/related evidence scoped to B05.
- Consumes: actual final migration revision, tests, commands, and environment output.

- [x] **Step 1: Add a PostgreSQL 17 service and explicit integration command to CI**

Keep `permissions: contents: read`; use a CI-only database/password and run `pytest -q --run-postgres` after frozen install. Do not use production secrets, `continue-on-error`, or integration skips.

- [x] **Step 2: Document the physical mapping and runtime obligations**

For every logical entity, record storage class, table/key/reference, constraint/index, invariant/test, and what B06/B08/B11/B13/B14/B15 must still enforce. Include engine/session/transaction/CAS usage and guarded test-database setup.

- [x] **Step 3: Update repository structure and status wording**

Replace stale README B04 status with the ROADMAP-approved status; mark B05 implemented and awaiting independent Task Review only after fresh verification. Do not mark B05 Task Review approved or change B01-B04 roadmap status.

- [x] **Step 4: Write evidence from observed outputs only**

Record PostgreSQL server version, Docker/local context, exact commands/results, migration/constraint/concurrency/precision coverage, requirement-to-test mapping, hosted-CI status, limitations, and B06 handoff. Keep project-level gates `specified` where B05 proves only a subset.

### Task 8: Bounded Review, Fix Round, and Fresh Verification

**Files:**
- Review all B05 diffs and untracked files.
- Modify only files needed to close verified Critical/Important findings.

**Interfaces:**
- Produces: one scoped review report, at most two review-fix rounds, and final evidence-backed status.

- [x] **Step 1: Request a read-only B05 code review**

Provide the reviewer the user request, this plan, base `744e907`, current diff, and scope: contract violations, correctness/isolation/concurrency, migration/data-loss risk, and missing direct tests/evidence.

- [x] **Step 2: Verify each finding against source and tests before changing code**

Apply `receiving-code-review`: fix valid blockers in severity order, push back with contract/test evidence where a suggestion is incorrect, and do not expand into B06+ runtime features.

- [x] **Step 3: Run focused tests after each fix batch, then the complete fresh verification suite**

Run:

```sh
uv lock --check
uv sync --frozen --all-groups --no-editable --reinstall-package nexa
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
NEXA_TEST_DATABASE_URL=... uv run --no-sync pytest -q --run-postgres
pnpm --dir web install --frozen-lockfile
pnpm --dir web run typecheck
pnpm --dir web run build
git diff --check
git status --short --untracked-files=all
git ls-files --others --exclude-standard
git check-ignore -v --no-index .env
```

Expected: every applicable local check exits zero; `.env` is ignored by the dedicated rule; PostgreSQL reports major version 17. Hosted CI is reported separately and is not claimed unless observed.

- [x] **Step 4: Reconcile the plan/requirement map and evidence**

Confirm every requirement row points to implemented code and passing direct evidence, or is explicitly recorded as a B06+ runtime obligation/blocker. Do not claim Linux/GPU/load/recovery/runtime acceptance from B05.

### Task 9: Independent Task Review Remediation B05-R01–R07

**Files:**
- Modify: `migrations/env.py`
- Modify: `migrations/versions/20260919_0001_b05_initial.py`
- Modify: `src/nexa/infrastructure/persistence/schema_v1.py`
- Modify: `src/nexa/infrastructure/persistence/values.py`
- Modify: `tests/conftest.py`
- Modify: `tests/integration/` and `tests/persistence/`
- Modify: `docs/database.md`, `docs/evidence/B05-postgresql.md`, `README.md`, `ROADMAP.md`

**Interfaces:**
- Preserve Alembic head `20260919_0001`, schema generation `1`, the B04 domain API and the worker/API boundary.
- Replace PostgreSQL `NUMERIC` fairness storage with exact canonical Decimal text plus immutable database comparison/order helpers; expose explicit Decimal bind/read and SQL ordering helpers.
- Keep cleanup possible only for rows with no authoritative references.

- [x] **Step 1: R01 migration target and destructive-fixture safety**

Add failing regressions proving an explicit Alembic target wins over `NEXA_DATABASE_URL` and a rejected server version executes no `DROP SCHEMA`; then fix URL precedence and gate cleanup on successful validation.

- [x] **Step 2: R02 ownership preservation**

Add failing direct-SQL INSERT/UPDATE tests for allocation-ledger tenant mismatch and nullable event ownership, plus owner-side UploadSession reference tests; add composite FKs/checks and reverse owner triggers while preserving global events and unreferenced upload cleanup.

- [x] **Step 3: R03 authority worker identity and incarnation uniqueness**

Add failing lease/grant worker mismatch and A-to-B-to-A adoption tests; extend composite authority keys through Attempt, Allocation, Lease and Grant and enforce unique `(attempt_id, worker_incarnation_id)` while retaining valid A-to-B adoption.

- [x] **Step 4: R04 referenced artifact immutability**

Add failing SQL tests for STAGING JobSpec inputs, generic/chunk/log committed references, exact recognized checksums and post-recognition input/checkpoint/result artifact mutation; add final-state validation and referenced-artifact protection while allowing unreferenced cleanup.

- [x] **Step 5: R05 per-user outstanding policy**

Add failing metadata/PostgreSQL tests for independent tenant/user outstanding limits and invalid values; add `user_outstanding_limit` to generation-1 metadata/migration and all fixtures.

- [x] **Step 6: R06 exact Decimal representation**

Add failing unit/PostgreSQL tests for `1E-20000`, large exponents, exact tuple round-trip and pre/post-persistence ordering; implement canonical text storage, DB validation/comparison/order functions and matching score index without changing B04 arithmetic.

- [x] **Step 7: R07 missing direct evidence and final verification**

Add PostgreSQL tests for helper-driven commit/rollback, CAS-loser side effects, local/result reservation uniqueness, cross-attempt checkpoint sequence and sweep ownership INSERT/UPDATE. Correct UUID evidence wording, run migration parity and the complete frozen verification suite, then record only observed results.

### Task 10: Independent Re-review Remediation B05-R02, R04, and R06

**Files:**
- Modify: `migrations/versions/20260919_0001_b05_initial.py`
- Modify: `src/nexa/infrastructure/persistence/schema_v1.py`
- Modify: `src/nexa/infrastructure/persistence/values.py`
- Modify: `tests/persistence/test_schema_metadata.py`
- Modify: `tests/persistence/test_values.py`
- Modify: `tests/integration/test_constraints_ownership.py`
- Modify: `tests/integration/test_constraints_artifacts.py`
- Modify: `tests/integration/test_fairness_persistence.py`
- Modify: `tests/integration/test_indexes.py`
- Modify: `docs/database.md`, `docs/evidence/B05-postgresql.md`, `README.md`, `ROADMAP.md`

**Interfaces:**
- Preserve Alembic head `20260919_0001`, schema generation `1`, cleanup of unreferenced uploads/artifacts, exact Decimal tuple round-trip, and all closed R01/R03/R05/R07 behavior.
- Add declarative PostgreSQL serialization points: a conditional generated UploadSession owner key and a committed-artifact guard referenced by every artifact consumer.
- Keep the fairness index key bounded while exact ordering remains available through the immutable Decimal comparison/order helpers.

- [x] **Step 1: Reproduce R02 and add two-connection owner regressions**

Add parameterized PostgreSQL tests where an uncommitted `UPLOAD_SESSION` artifact reference races first with owner deletion and then with owner tenant transfer. Verify the owner mutation cannot commit before the reference transaction resolves, the losing mutation raises `IntegrityError`, the final reference retains a matching owner, and the existing unreferenced-owner cleanup test still commits.

- [x] **Step 2: Implement declarative UploadSession ownership serialization**

Add `upload_session_owner_id uuid GENERATED ALWAYS AS (CASE WHEN owner_type = 'UPLOAD_SESSION' THEN owner_id END) STORED`, a composite unique owner key on `upload_sessions(tenant_id, upload_id)`, and a composite FK from `artifact_references(tenant_id, upload_session_owner_id)`. Retain polymorphic validation for the other owner types and re-run the focused ownership tests.

- [x] **Step 3: Reproduce R04 for input, checkpoint, and result recognition**

Add parameterized two-connection tests where a `COMMITTED` artifact is changed to `DELETING` without committing while another transaction creates a JobSpec input, committed Checkpoint, or final Result. Verify only one path commits and the database cannot end with recognized data pointing at a non-committed artifact.

- [x] **Step 4: Implement artifact guard serialization**

Add `artifact_reference_guards(tenant_id, artifact_id)` with a composite FK to `artifacts`, replace every authoritative artifact consumer FK so it targets the guard, and maintain guard rows with triggers. A protected artifact mutation or delete must remove the guard before changing identity/state; PostgreSQL FK locking must reject that mutation when references exist, while committed inserts/updates recreate the guard and unreferenced cleanup remains valid.

- [x] **Step 5: Reproduce R06 Decimal domain and index failures**

Add unit and PostgreSQL regressions for two hard-to-compress 5,000-digit significands, exact round-trip/order, successful fairness-ledger insertion, raw encoding `0:1:1000000000000000000`, and codec rejection without leaking `decimal.InvalidOperation`.

- [x] **Step 6: Bound the index key and align DB/codec exponent validation**

Change `ix_fairness_ledgers_score` to index only a fixed-length normalized-significand prefix after zero rank and adjusted exponent; keep full normalized-significand ordering in explicit queries. Validate exponents in both PostgreSQL and Python against `decimal.MIN_ETINY` through `decimal.MAX_EMAX`, then re-run Decimal and index tests.

- [x] **Step 7: Run migration parity, full verification, and update evidence**

Run the focused regressions first, then migration lifecycle/parity and the full frozen PostgreSQL 17, Ruff, UI, and Git hygiene commands. Record fresh observed counts and describe R02/R04/R06 as remediated locally and awaiting independent re-review; do not claim B05 approval.

### Task 11: Final B05-R06 Adjusted-Exponent Remediation

**Files:**
- Modify: `migrations/versions/20260919_0001_b05_initial.py`
- Modify: `tests/persistence/test_values.py`
- Modify: `tests/integration/test_fairness_persistence.py`
- Modify: `docs/database.md`, `docs/evidence/B05-postgresql.md`, `README.md`, `ROADMAP.md`

**Interfaces:**
- Preserve all findings already closed by independent review and the bounded fairness index.
- Make PostgreSQL accept exactly the tested Python Decimal tuple boundaries for raw and adjusted exponents without normalizing away trailing zeros.

- [x] **Step 1: Reproduce the remaining SQL/Python domain mismatch**

Add codec and PostgreSQL boundary cases for one/two-digit coefficients, trailing zero, zero at `MAX_EMAX`, and `MIN_ETINY`. Add a raw `fairness_state` insert proving `0:12:999999999999999999` commits before the fix.

- [x] **Step 2: Align PostgreSQL validation with Python tuple construction**

After validating the raw exponent, require `exponent + digit_count - 1 <= MAX_EMAX` for every nonzero coefficient. Count trailing zeros as stored tuple digits and retain the zero exception.

- [x] **Step 3: Re-run focused, migration, and complete verification**

Observe the SQL validator and raw insert tests RED, then GREEN; retain `MIN_ETINY`, `1E-20000`, 5,000-digit round-trip/order and migration parity. Run the frozen non-editable PostgreSQL 17, Ruff, UI and Git checks, then record only fresh observed results without claiming independent approval.
