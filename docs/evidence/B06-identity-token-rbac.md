# B06 identity, token and RBAC evidence

- **Task:** B06 - Identity, token, RBAC
- **Implementation status:** implemented and locally verified; `chờ Task Review`
- **Baseline revision:** `89034671ef5b97c0ad4bc502682a8f0d5e48a2ca`
- **Working context:** existing `main` checkout; no branch, worktree, commit, push, reset or Git-history change
- **Evidence date:** 2026-09-20, Asia/Ho_Chi_Minh
- **Contract:** `1.0.0-b01`; schema generation `2`; Alembic head `20260920_0002`
- **Direct B06 blocker:** independent Task Review approval has not been granted

This report records only the B06 backend slice and local verification observed in this
checkout. It does not claim workload, artifact-store, worker protocol, scheduler,
recovery, Web UI, Linux isolation, GPU, hosted CI or release acceptance.

## Scope delivered

| Area | Implementation | Direct evidence |
|---|---|---|
| Security configuration | Public HTTPS origin, deployment secret files, stable installation/local-worker identity, maintenance/trusted proxy networks, Argon2/session/token/bootstrap/cursor/idempotency bounds | `test_config.py`, `test_primitives.py`, loadable `.env.example`, redacted settings representation |
| Durable identity state | Immutable schema generation 2 adds `auth_control`, bounded/retained `login_rate_limits` and one-current-worker-credential index without changing frozen generation 1; B05 tenants receive policy v1 defaults during upgrade | metadata/parity/schema-guard tests; B05-to-B06 upgrade with worker credentials, tenant-policy backfill and casefold collision fail-closed coverage |
| Browser authentication | Argon2id login, source+case-folded-username durable rate window with bounded retention/cap, generic failures, secure cookie, absolute/idle TTL, live user/grant/membership recheck, bound CSRF and logout revocation | identity-service and real FastAPI/PostgreSQL tests, including restart, first-row concurrency, frozen-mode metadata and lock/expiry races |
| CLI tokens | Opaque hash-only tokens, exact non-hierarchical scopes, own-token list/create/revoke, TTL/revocation/disabled-user checks and one-time-secret behavior | identity-service, HTTP contract and operation-matrix tests |
| Bootstrap | Durable admin latch/window and local-worker window/binding; atomic admin grant/audit/idempotency; worker rotation without incarnation, inventory or READY fabrication | bootstrap, migration, maintenance CLI and two-request race tests |
| RBAC and administration | SYSTEM_ADMIN plus exact `admin:read` or `admin:write`; tenant/user/membership CRUD, membership-set ETag/CAS, last-admin protection and credential invalidation | service tests, per-operation negative matrix and full scoped/unscoped CLI SA matrix |
| Versioned policy | Immutable global/tenant versions, Decimal/JCS normalization, quota/counter/allocation guards, legal operational-mode transitions and fail-closed recovery proof boundary | policy unit/integration tests plus real counter/freeze transaction races |
| Audit and pagination | Audit rows commit with accepted mutations; actor/filter-bound signed keyset cursors | admin identity, policy, token and HTTP audit tests |
| HTTP boundary | 24 approved operation IDs, bounded duplicate-safe JSON, RFC 8785 request hashing, mixed-credential rejection, safe error mapping, request ID, ETag/Location/cookie headers | full API registration/contract/operation-matrix suite |

## Locked design resolutions

| Issue | Implemented resolution | Boundary retained |
|---|---|---|
| CLI introspection conflict | `GET /v1/auth/session` remains browser-cookie-only because the approved OpenAPI security declaration is authoritative | No bearer access was added and no browser session/CSRF is fabricated for a CLI token |
| CSRF durability | CSRF is deterministically derived from the session identity and raw session secret with server secret material; only its hash is stored | GET session works across restart without storing a raw CSRF value |
| Bootstrap durability | `auth_control` persists separate admin/worker windows, permanent admin completion latch and deployment/local-worker identity | No in-memory correctness latch and no shared default secret |
| Policy without inventory | Migration creates global policy version 1; tenant creation uses zero CPU/RAM/GPU limits as a fail-closed placeholder | No development-host hardware or worker READY state is fabricated |
| JSON/JCS numbers | Strict decoding rejects duplicate members, invalid Unicode, integers outside the RFC 8785 exact domain and non-finite/out-of-binary64 numbers | Policy Decimal values normalize to the same JCS/binary64 behavior used for request hashes |

## PostgreSQL 17 environment

| Component | Observed value |
|---|---|
| Host used for local verification | macOS/Darwin arm64; not Linux runtime acceptance evidence |
| Database container | `nexa-b06-pg`, image `postgres:17`, task-owned |
| Network exposure | container port 5432 bound only to `127.0.0.1:55432` |
| Server | PostgreSQL 17.11, Debian build, aarch64, 64-bit |
| `server_version_num` | `170011` |
| Test database | `nexa_b05_test_worker_expiry`; isolated B05-guarded test database |
| Python / uv | CPython 3.12.13; uv 0.12.15 isolated under `/tmp/nexa-b06-uv/` |
| Database packages | SQLAlchemy 2.0.54, psycopg 3.3.5, Alembic 1.18.5 |
| API/security packages | FastAPI 0.141.1, Pydantic 2.13.5, argon2-cffi 25.1.0, httpx 0.28.1, Uvicorn 0.41.0, rfc8785 0.1.4 |

PostgreSQL tests were run serially. The guarded fixture recreates only the `public` schema
of the dedicated test database, so concurrent pytest processes against this database are
not supported and were not used.

## Requirement-to-operation evidence

| Requirement / source | Operations or service boundary | Direct test/evidence | Later dependency |
|---|---|---|---|
| Browser session and CSRF; INV-20, ACC-24 | `loginBrowserSession`, `getBrowserSession`, `logoutBrowserSession` | restart, idle/absolute expiry, cookie flags, cross-session/wrong CSRF, forged Origin/Host, durable rate limit and first-row race | UI B17/B18 and TLS deployment B21 |
| Own CLI token lifecycle; ACC-07/24 | `listCliTokens`, `createCliToken`, `revokeCliToken` | hash-only metadata, exact scope, own-user isolation, expiry/revoke/disable, same/different key and response-loss behavior | Product CLI B12 |
| Initial admin bootstrap | `bootstrapInitialAdmin` | durable window/latch, source/secret/expiry rejection, two different first-admin requests, same-key replay and strong user ETag | Deployment secret delivery B21/B25 |
| Local worker credential bootstrap | `bootstrapLocalWorker`, maintenance reopen command | installation/fingerprint binding, stable worker ID, current credential rotation, one-time replay, WRITE_FROZEN and explicit reopen | Worker protocol/incarnation B10 after B09 |
| Tenant/user/membership RBAC; ACC-02 | 11 tenant/user/membership admin operations | full anonymous/member/worker/bootstrap rejection; every operation exercised with unscoped, `admin:read` and `admin:write` CLI SYSTEM_ADMIN tokens; browser SA positive flows; CAS and revoke/disable races | Ownership guards B07/B08 |
| Policy and mode; ACC-05 | 4 global/tenant policy operations | default/bound/Decimal/ETag/idempotency tests, counter/allocation guards, policy-versus-counter and freeze-versus-initialize races, fail-closed proof provider | Admission/scheduler/recovery B08/B10/B13/B21 |
| Audit history | `adminListAuditRecords` and atomic audit writers | append-only mutation audit plus actor/filter/cursor tamper/expiry binding | Observability B19 |
| Shared HTTP/idempotency | all 24 B06 operation IDs | bounded strict JSON, UUIDv7 request ID, safe errors, replay before `If-Match`, one-time-secret replay conflict and no raw secret snapshot | Reuse by B07/B08/B10/B12 |

## Authorization matrix evidence

The real FastAPI/PostgreSQL operation matrix distinguishes authentication, credential
scope and live global role. It does not infer read from write.

| Caller | Admin reads | Admin mutations |
|---|---|---|
| Anonymous | rejected `401` for every operation | rejected `401` for every operation |
| Browser MEMBER / TENANT_ADMIN without SYSTEM_ADMIN | rejected `403` for every operation | rejected `403` for every operation |
| Browser SYSTEM_ADMIN | permitted by role; target/precondition rules still apply | permitted by role; target/precondition/mode rules still apply |
| CLI SYSTEM_ADMIN with no admin scope | rejected `403` for every operation | rejected `403` for every operation |
| CLI SYSTEM_ADMIN with exact `admin:read` | permitted for every read operation | rejected `403` for every mutation |
| CLI SYSTEM_ADMIN with exact `admin:write` | rejected `403` for every read operation | permitted for every mutation; target/precondition rules still apply |
| Worker credential | rejected as user authentication for every operation | rejected as user authentication for every operation |
| Bootstrap header alone | rejected as user authentication for every operation | rejected as user authentication for every operation |

An admin-scoped CLI token also requires its user to retain an active SYSTEM_ADMIN grant;
scope does not replace the live role check.

## Internal review and remediation

The first internal review reported eight Important findings. Each was verified against the
code and contract before remediation:

| Finding | Resolution and direct regression evidence |
|---|---|
| First login-rate row race | PostgreSQL `INSERT ... ON CONFLICT DO NOTHING RETURNING`, followed by row lock; `test_first_concurrent_login_attempts_serialize_without_losing_rate_charge` |
| Startup write through `WRITE_FROZEN` | Shared global-policy lock plus fail-closed uninitialized deployment identity; initialize/freeze transaction race test |
| Browser live revalidation omitted idle expiry | Idle deadline rechecked inside mutation transaction; idle-expired mutation test |
| Retry exhaustion mapped to 500 | API and maintenance CLI map `TransactionRetryExhausted` to safe dependency failure; dedicated HTTP/CLI tests |
| JSON outside RFC 8785 domain mapped to 500 | Decoder rejects oversized integers and invalid Unicode/surrogates before route hashing; unit and HTTP tests |
| Migration failed with multiple B05 worker credentials | Deterministically keep newest unrevoked credential and revoke older rows before unique partial index; B05-to-B06 upgrade test |
| Admin bootstrap lacked ETag | Strong user-version ETag is stored in completed idempotency headers and replayed; HTTP bootstrap ETag test |
| Missing broad evidence and race coverage | Added freeze/policy, disable/token and grant/mutation races plus per-operation authorization matrices |

The scoped internal re-review then found two remaining Important evidence gaps: exact CLI
scope coverage was only representative, and this evidence file did not yet exist. The
per-operation CLI matrix and this report remediate those gaps. The final scoped internal
re-check reported no remaining Critical or Important finding. That re-check is still
distinct from and cannot replace independent Task Review.

The rejected Task Review round identified seven additional findings. They are remediated
in this checkout and covered by fresh regression tests. A follow-up review then identified
the worker-credential expiry race recorded below; independent Task Review is still required:

| Finding | Remediation and direct regression evidence |
|---|---|
| B06-R01 stale expiry time before row/control lock | Browser session, CLI token, admin bootstrap and worker bootstrap now read `clock_timestamp()` after the relevant PostgreSQL locks; four deterministic blocker probes prove an expiry committed while waiting is rejected |
| B06-R02 Argon2 before authorization/idempotency | User create/update hash only after live authorization, replay, mutable-mode, auth-control and version/last-admin preconditions; spy-count test proves denied, replay and stale-ETag paths perform zero extra hashes |
| B06-R03 missing tenant-policy backfill | Revision `20260920_0002` inserts policy v1 defaults only for tenants with no policy and leaves existing rows unchanged; B05 upgrade test verifies the defaults |
| B06-R04 unbounded login-rate cardinality | Rate rows prune after one hour, creation is serialized with a PostgreSQL advisory transaction lock and a fixed 10,000-bucket cap fails closed with `429`; bounded-cap/stale-prune test verifies row count |
| B06-R05 frozen-mode metadata write | Mode and live user/grant are checked before rate mutation; unknown, disabled and non-admin frozen login attempts do not add/update rows, while the existing enabled SYSTEM_ADMIN path remains allowed |
| B06-R06 resource bounds | Request schema and service enforce CPU `100000000`, memory signed int64 maximum and GPU `1`; boundary tests cover over-limit `422` behavior |
| B06-R07 username case folding | Login/bootstrap/admin normalization uses `strip().casefold().lower()` to align with the frozen lowercase index; migration normalizes existing rows and aborts on collisions; `straße`/`STRASSE` regression and migration collision tests are included |
| P1 worker credential stale expiry time | `resolve_worker_credential()` locks the credential row before reading `clock_timestamp()`; `test_worker_credential_expiry_is_checked_after_row_lock` commits expiry while the resolver waits and proves the credential is rejected |

## Verification commands and observed results

The bootstrap/UI commands below were recorded from the frozen non-editable install in the
isolated uv environment. This recheck used the existing `.venv` with `PYTHONPATH=src` so
the worktree sources were exercised; `uv` is not installed in the current shell.

| Command/check | Observed result |
|---|---|
| `uv lock --check` | pass; 44 packages resolved |
| `uv sync --frozen --all-groups --no-editable --reinstall-package nexa` | pass; local package rebuilt and reinstalled |
| `uv run --no-sync ruff check .` | pass; all checks passed |
| `uv run --no-sync ruff format --check .` | pass; 158 files already formatted |
| `pytest -q` (worktree `src/` on `PYTHONPATH`) | pass; `279 passed, 155 skipped, 2 warnings in 7.83s`; skips are PostgreSQL-marked tests exercised by the next command |
| Full B06-focused serial group before final suite | pass; `165 passed, 2 warnings in 39.12s` |
| Full admin operation matrix after scoped-review remediation | pass; `6 passed, 2 warnings in 5.36s` |
| `NEXA_TEST_DATABASE_URL=... pytest --run-postgres -q` | pass; `434 passed, 2 warnings in 64.70s` with the worker-credential expiry race regression |
| `alembic heads` | pass; one head `20260920_0002` |
| `pnpm --dir web install --frozen-lockfile` | pass; lockfile already up to date with pnpm 11.9.0 |
| `pnpm --dir web run typecheck` | pass; `tsc -b` |
| `pnpm --dir web run build` | pass; Vite 8.3.0 transformed 16 modules and emitted ignored `web/dist/` |
| PostgreSQL identity query | pass; PostgreSQL 17.11, `server_version_num=170011` |
| `git diff --check` and `git diff --cached --check` | pass |
| Git status/untracked audit | pass; tracked and untracked B06 files are visible; `.env` remains ignored by dedicated `.gitignore` rule |

The two warnings are upstream TestClient deprecations: Starlette's compatibility layer
warns about `httpx` and the AnyIO `BlockingPortal` alias. They do not skip tests or expose
credentials. No hosted GitHub Actions run was observed, so workflow configuration is not
reported as hosted CI evidence.

## Acceptance scope

`docs/acceptance.md` remains unchanged. B06 supplies local subset evidence but does not by
itself complete project gates that also require later product/runtime environments.

| Gate | B06 evidence | Project status / missing scope |
|---|---|---|
| ACC-02 | Real backend role, exact CLI scope and per-operation rejection/allowance matrix | `specified`; B07/B08/B20 ownership and end-to-end authorization surfaces remain |
| ACC-05 | Versioned policy bounds, ETag/idempotency and local PostgreSQL races | `specified`; runtime admission/scheduler quota enforcement remains B08/B13 |
| ACC-07 | B06 mutation idempotency, replay-before-`If-Match` and one-time-secret exception | `specified`; jobs/callbacks/result flows remain B07/B08/B11/B15 |
| ACC-24 | Argon2id, cookie/CSRF/session/token/worker credential and WRITE_FROZEN subset | `specified`; TLS/network/image/abuse and browser acceptance remain B20/B21 |
| ACC-28 | Generation-2 clean/repeat and B05-data upgrade coverage, one head and fail-closed guard | `specified`; maintenance upgrade/restore and release compatibility remain B21/B25 |
| ACC-39 | Local lock, Ruff, Python/PostgreSQL, UI typecheck/build and Git hygiene | `specified`; hosted CI, Playwright, image/scan and release gates remain later scope |

No simulator, schema test or macOS local run is used to claim Linux isolation, GPU,
worker/container recovery, load/soak/chaos, backup/restore, portability or release
completion. B07 is not unblocked until independent Task Review approves B06.

## Handoff

- B07/B08 should reuse live principal revalidation, exact idempotency ordering and tenant
  ownership guards; they must add artifact/job ownership surfaces and quota consumption.
- B10 should consume the stable local worker credential through REST and add incarnation,
  heartbeat, inventory, reconcile and READY semantics without direct PostgreSQL access.
- B12 should use the same REST operations and exact scopes; `/v1/auth/session` remains
  cookie-only unless the approved contract is changed first.
- B17/B18 must use secure browser cookies plus returned CSRF and must not reproduce backend
  authorization logic in the UI.

B06 remains `chờ Task Review`.
