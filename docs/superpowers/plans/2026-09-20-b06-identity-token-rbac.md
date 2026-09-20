# B06 Identity, Token, RBAC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This task is explicitly authorized for inline execution in the current checkout; do not create a branch/worktree or commit.

**Goal:** Deliver the real `/v1` identity, browser-session, CLI-token, bootstrap, SYSTEM_ADMIN, tenant/membership, versioned-policy and audit backend slice required by B06.

**Architecture:** FastAPI owns HTTP parsing, credentials, request limits and response mapping; application services own live authorization, idempotency, transaction orchestration and policy guards; domain modules define principals, exact scopes and mode transitions without FastAPI/SQLAlchemy imports; infrastructure owns Argon2id, opaque-secret/HMAC handling and PostgreSQL tables. All accepted mutations linearize at one PostgreSQL commit through the B05 engine/session/transaction helpers.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy 2 Core, psycopg, PostgreSQL 17, Alembic, argon2-cffi, RFC 8785 JCS, Uvicorn, Pytest, HTTPX.

**Spec:** `PLAN.md`, `docs/contracts/openapi.yaml`, `docs/contracts/domain-model.md`, `docs/contracts/concurrency-recovery.md`, `docs/contracts/state-machines.md`, and the direct B06 task request dated 2026-09-20.

## Global Constraints

- Preserve approved B01-B05 behavior and immutable `schema_v1.py` / `20260919_0001_b05_initial.py`.
- Use `/v1` operation IDs and response/error semantics from the approved OpenAPI contract.
- PostgreSQL 17 is authoritative; no in-memory correctness state, SQLite replacement, import-time connection or request-time migration.
- Store only password/credential/session/CSRF hashes; never persist raw one-time secrets in audit, logs or idempotency responses.
- Authenticate and live-authorize before idempotency replay; replay before `If-Match`; commit mutation, audit and completed idempotency snapshot atomically.
- Browser cookie is always `Secure; HttpOnly; SameSite=Lax; Path=/`; cookie mutations require bound CSRF and configured-origin/host checks.
- Exact CLI scopes are non-hierarchical; admin scopes also require a current active SYSTEM_ADMIN grant.
- No B07 job/artifact implementation, B10 worker protocol/incarnation, scheduler runtime, UI or release claim.
- No commit, push, branch, worktree or Git history change.

## Early Design Resolutions

| Issue | Resolution | Verification |
|---|---|---|
| CLI introspection | Keep `GET /auth/session` cookie-only until the approved contract contradiction is resolved; do not fabricate a browser session/CSRF for bearer credentials. The application principal resolver remains reusable by B12. | OpenAPI/security review plus negative bearer test; update if user approves a contract correction. |
| CSRF | Cookie value is `session_id.secret`; derive CSRF with HMAC-SHA256 over the session identity and raw session secret using the stable server-secret file, store only `sha256(csrf)` and recompute on GET/restart. | Restart, cross-session, wrong-token and revoked-session tests. |
| Bootstrap durability | Add a singleton auth-control row with separately persisted admin/worker windows, permanent admin latch, configured installation ID and local worker binding. Bootstrap secrets and server signing material come only from validated files. | Migration, restart, expiry, latch and two-request races. |
| Policy without inventory | Migration creates global policy v1. Tenant creation creates policy v1 with contractual count/rate defaults and zero CPU/RAM/GPU resource limits when no verified inventory exists. Zero is a fail-closed placeholder; B10/inventory-aware administration must explicitly replace it. | Tenant creation and no-inventory policy tests; no READY/inventory fabrication. |
| JSON numbers and JCS | Reject duplicate members; parse number lexemes without an intermediate float, normalize policy decimals to the RFC 8785 finite binary64 domain, store the canonical decimal value through `ExactDecimalText`, and hash the same normalized representation. | Distinct-behavior hash tests, exact DB/HTTP round trip and out-of-domain rejection. |

## Requirement / Operation / Evidence Map

| Requirement | Source | Endpoint/service | Invariant | Test/evidence | Later dependency |
|---|---|---|---|---|---|
| Initial admin bootstrap | OpenAPI `bootstrapInitialAdmin`; concurrency contract | `POST /v1/internal/admin-bootstrap`, `IdentityService.bootstrap_admin` | durable latch, maintenance source/window, one admin under race, atomic grant/audit/idempotency | PostgreSQL concurrent bootstrap and restart tests | Compose secret delivery B21/B25 |
| Browser login/session/logout | OpenAPI auth operations; INV-20 | `/v1/auth/login`, `/session`, `/logout` | Argon2id, durable rate limit, absolute/idle TTL, cookie/CSRF/origin, live enabled user | unit security tests plus real app/PostgreSQL tests | UI B17/B18 |
| Own CLI tokens | OpenAPI token operations; ACC-07/24 | `/v1/tokens`, principal resolver | exact scopes, own-user ownership, hash-only, one-time secret, revoke/expiry/live authority | response-loss, scope, revoke/disable and concurrent same-key tests | CLI B12 |
| Tenant/user/membership/SYSTEM_ADMIN | OpenAPI admin identity operations; auth matrix | `/v1/admin/tenants`, `/users`, membership endpoints | active SA + admin scope, MembershipSet ETag/CAS, last-admin serialization, credential invalidation | per-operation matrix, stale ETag and PostgreSQL race tests | ownership guards B07/B08 |
| Versioned policy/mode | OpenAPI admin policy; INV-07; mode state machine | `/v1/admin/policy`, tenant policy endpoints | immutable versions, Decimal, quota/allocation guards, legal mode edges, fail-closed proof | bound/default/conflict/quota/race/mode tests | admission/scheduler B08/B13 |
| Worker bootstrap credential | OpenAPI `bootstrapLocalWorker` | `POST /v1/internal/worker-bootstrap` | exact configured installation/worker, current credential rotation, separate type/TTL, no READY/incarnation | binding, rotation, response-loss/replay, expiry tests | worker protocol B10 after B09 |
| Audit and pagination | OpenAPI `adminListAuditRecords` | `/v1/admin/audit`, audit writer, cursor signer | append-only atomic audit, signed actor/filter-bound keyset cursor | tamper/cross-actor/filter/expiry tests | observability B19 |
| Shared HTTP/idempotency/error layer | OpenAPI common schemas; concurrency contract | middleware, request decoder, idempotency service | UUIDv7 request ID, body bound, duplicate/unknown reject, replay order, no sensitive errors | status/header/body/atomic rollback tests | B07/B08/B10/B12 |

---

### Task 1: Security configuration and primitives

**Files:**
- Modify: `pyproject.toml`, `uv.lock`, `.env.example`, `src/nexa/config.py`
- Create: `src/nexa/infrastructure/security.py`
- Create: `tests/security/test_primitives.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces `PasswordHasher.hash/verify`, `SecretCodec.issue/parse/hash`, `CsrfCodec.issue/verify`, `CursorCodec.encode/decode`, and validated `Settings` fields for public origin, secret files, bootstrap identity/windows, TTLs, Argon2 and trusted networks.

- [x] Write failing tests for OWASP-floor Argon2id validation, redacted settings repr, 32-byte secret-file bounds, opaque secret parsing, session-bound CSRF and signed cursor tamper/expiry/binding.
- [x] Run focused tests and confirm failures are caused by missing fields/modules.
- [x] Add only `argon2-cffi`, `rfc8785`, `uvicorn`, and test HTTP client dependencies; refresh lockfile.
- [x] Implement the minimal validated configuration and security primitives; use constant-time comparison and no raw secret repr.
- [x] Run focused tests and Ruff for the touched files.

### Task 2: B06 schema generation and migration

**Files:**
- Create: `src/nexa/infrastructure/persistence/schema_v2.py`
- Modify: `src/nexa/infrastructure/persistence/schema.py`, `src/nexa/infrastructure/persistence/schema_guard.py`
- Create: `migrations/versions/20260920_0002_b06_identity_runtime.py`
- Modify: `tests/persistence/test_schema_metadata.py`, `tests/integration/test_migrations.py`, `tests/integration/test_schema_guard.py`

**Interfaces:**
- Produces current metadata generation 2 with `auth_control`, `login_rate_limits`, worker installation binding/current credential constraints and any indexes required by B06.

- [x] Write failing metadata/migration tests proving v1 remains frozen, B05 data survives v1→v2, one Alembic head exists, bootstrap/rate state is durable and the schema guard treats generation 1 as old.
- [x] Run unit and PostgreSQL migration tests to observe RED.
- [x] Build `schema_v2` by cloning v1 metadata and adding only B06 tables/columns/constraints; create the second Alembic revision and update metadata generation.
- [x] Seed only global policy version 1 and the auth-control singleton in the migration; do not seed users, worker readiness, inventory or credentials.
- [x] Run focused migration/parity/guard tests to GREEN.

### Task 3: Principal, authorization, JSON/JCS and HTTP foundation

**Files:**
- Create: `src/nexa/domain/identity.py`, `src/nexa/domain/policy.py`
- Create: `src/nexa/application/errors.py`, `src/nexa/application/json_codec.py`, `src/nexa/application/idempotency.py`
- Create: `src/nexa/api/http.py`, `src/nexa/api/dependencies.py`
- Create: `tests/domain/test_identity.py`, `tests/application/test_json_codec.py`, `tests/application/test_idempotency.py`

**Interfaces:**
- Produces immutable `Principal`, `CredentialKind`, exact `TokenScope`, `require_admin/read/write`, tenant membership guards, mode-transition validation, duplicate-safe bounded JSON decoding, JCS request hashing, ETag parsing and idempotency begin/complete/replay.

- [x] Write failing tests for scope non-hierarchy, mixed credential rejection, SA-without-membership behavior, legal/illegal mode edges, duplicate JSON members, body bounds, Decimal/JCS normalization, same/different idempotency hashes and replay-before-ETag.
- [x] Run focused tests and confirm expected RED.
- [x] Implement domain rules without FastAPI/SQLAlchemy imports and application helpers over B05 tables/transactions.
- [x] Run focused tests to GREEN and refactor only after green.

### Task 4: Authentication, session and own-token application service

**Files:**
- Create: `src/nexa/application/identity_service.py`
- Create: `tests/integration/test_auth_sessions.py`, `tests/integration/test_cli_tokens.py`

**Interfaces:**
- Produces `IdentityService.login`, `resolve_browser_session`, `resolve_cli_token`, `logout`, `list_tokens`, `create_token`, `revoke_token`, and live principal recheck methods for later B06/B07/B08 services.

- [x] Write PostgreSQL tests for real Argon2 login, durable source+username rate counting, generic failure, disabled user, session fixation, cookie material, absolute/idle expiry and restart durability.
- [x] Observe RED, then implement rate-count commit before password verification and session creation in a separate transaction with live user/mode recheck.
- [x] Write CSRF/origin/cross-session/revocation tests; observe RED; implement derived CSRF validation and normal-mode idle refresh without extending absolute expiry.
- [x] Write token TTL/scope/ownership/hash-only/expiry/revoke/disabled-user and one-time-idempotency tests; observe RED; implement own-token operations with exact browser/bearer authority rules.
- [x] Run the complete auth/token integration group to GREEN.

### Task 5: Bootstrap admin and local worker credential

**Files:**
- Modify: `src/nexa/application/identity_service.py`
- Create: `tests/integration/test_bootstrap.py`

**Interfaces:**
- Produces `bootstrap_admin` and `bootstrap_worker` using durable windows/latch, configured maintenance networks and exact installation binding.

- [x] Write failing tests for missing/wrong secret, disallowed source, expired/closed windows, restart persistence, two different first-admin keys, same-key replay, and last enabled SA state.
- [x] Implement atomic admin user/grant/audit/idempotency/latch creation under the auth-control row lock.
- [x] Write failing worker tests for installation/fingerprint mismatch, exact stable worker ID, credential type separation, atomic current-credential rotation, old credential revoke and one-time replay.
- [x] Implement worker bootstrap without creating incarnation, inventory or READY state.
- [x] Run bootstrap tests to GREEN.

### Task 6: Admin identity and membership use cases

**Files:**
- Create: `src/nexa/application/admin_service.py`
- Create: `tests/integration/test_admin_identity.py`, `tests/integration/test_admin_races.py`

**Interfaces:**
- Produces list/create/get/update tenant/user and list/upsert/delete membership operations, all using live SA/scope authorization, audit, idempotency and ETags.

- [x] Write failing positive/negative authorization matrix tests for anonymous, MEMBER, TENANT_ADMIN, browser SA, scoped/unscoped CLI SA, worker and bootstrap credentials.
- [x] Implement admin reads with append-only audit and actor/filter-bound keyset pagination.
- [x] Write failing mutation tests for normalization, uniqueness, tenant membership-set/policy creation, no implicit user membership, stale/missing ETag and credential invalidation.
- [x] Implement mutations and immutable response snapshots in the same transaction as audit/idempotency.
- [x] Write real two-connection tests for membership CAS, last-admin disable/revoke across two different users, disable racing session/token creation and authorization racing a privileged mutation.
- [x] Add the required singleton/auth-control serialization and live recheck until committed outcomes satisfy the contract.

### Task 7: Versioned policy and operational mode

**Files:**
- Create: `src/nexa/application/policy_service.py`
- Create: `tests/integration/test_policy.py`, `tests/integration/test_policy_races.py`

**Interfaces:**
- Produces global/tenant policy reads and updates with immutable version insertion, current-pointer swap, exact Decimal values, quota/counter/allocation guards and a proof-provider boundary for mode changes.

- [x] Write failing default/bound/Decimal/ETag/idempotency tests and prove replay is evaluated before stale/missing `If-Match`.
- [x] Implement policy reads and updates using B05 Decimal codecs and documented lock order.
- [x] Write failing tests for decreases below global/tenant/user outstanding, active attempts and HELD/QUARANTINED resource totals.
- [x] Implement aggregate guards across all relevant rows, not a single allocation/state.
- [x] Write failing legal/illegal mode transition and missing-proof tests; implement a `RecoveryProofProvider` interface whose B06 default fails closed for transitions requiring B10/B21 proof.
- [x] Write two-connection policy/counter race tests and ensure only one current version survives.

### Task 8: FastAPI app and all B06 routes

**Files:**
- Create: `src/nexa/api/schemas.py`, `src/nexa/api/routes_auth.py`, `src/nexa/api/routes_admin.py`, `src/nexa/api/routes_bootstrap.py`, `src/nexa/api/app.py`, `src/nexa/api/main.py`
- Modify: `src/nexa/api/__init__.py`
- Create: `tests/api/test_http_contract.py`, `tests/api/test_operation_matrix.py`

**Interfaces:**
- Produces `create_app(settings, engine=None) -> FastAPI` and `nexa.api.main:app`; startup checks schema compatibility, constructs one engine/session factory and never migrates or seeds principals.

- [x] Write failing real-app/PostgreSQL tests for every B06 operation ID, status, ETag, Location, cookie flags, request ID, error shape and pagination bounds.
- [x] Implement strict request-body middleware/decoding, credential extraction, trusted source/origin checks and route wiring.
- [x] Add exception mapping for 400/401/403/404/409/412/422/428/429/503 with safe messages and required headers.
- [x] Verify cookie mutations need CSRF while bearer-only mutations do not; reject mixed credentials without merging authority.
- [x] Run the entire API integration group to GREEN using HTTPS ASGI base URLs.

### Task 9: Documentation, evidence and regression

**Files:**
- Create: `docs/authentication.md`, `docs/evidence/B06-identity-token-rbac.md`
- Modify: `README.md`, `ROADMAP.md`, `docs/project-structure.md`, `docs/database.md`, `.github/workflows/ci.yml`
- Modify: `docs/contracts/openapi.yaml` only if the user approves the CLI introspection correction.

**Interfaces:**
- Documents B07/B08/B10/B12/UI handoff, secret-file/bootstrap/API startup, role/scope/operation matrix and exact limitations.

- [x] Document actual commands/config and the zero-resource fail-closed policy bootstrap; do not claim runtime inventory/readiness.
- [x] Update B01-B05 status from the latest user confirmation and mark B06 implemented/local-verified but awaiting independent Task Review.
- [x] Add B06 PostgreSQL/API tests to the real CI path without skips or `continue-on-error`.
- [x] Fill evidence with requirement→operation/test mapping and only observed command results.

### Task 10: Review and verification

**Files:** all B06 changes.

- [x] Run one full internal review for contract correctness, authorization/isolation, secret exposure, concurrency/atomicity and missing evidence.
- [x] Verify every reviewer finding against the code/contract, then fix Critical/Important findings with a new RED/GREEN regression cycle; perform at most two review-fix rounds.
- [x] Run `uv lock --check` and frozen non-editable reinstall.
- [x] Run Ruff check/format, unit suite, PostgreSQL 17 full suite, UI typecheck/build when applicable, migration parity/upgrade and Git hygiene commands.
- [x] Record PostgreSQL identity and command results, inspect full tracked/untracked diff, and report remaining contract blocker or unverified runtime scope without lowering gates.
