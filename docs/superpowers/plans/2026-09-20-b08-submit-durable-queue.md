# B08 Submit, Idempotency, Durable Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the B08 submit path so an authenticated tenant member can durably accept a validated job into PostgreSQL exactly once, query its job/session/event records, and replay the committed response after retries or response loss.

**Architecture:** Reuse the frozen B05/B06 PostgreSQL tables, transaction retry helper, JCS hashing, cursor codec, identity revalidation, artifact ownership guard, and policy rows. A new application service owns one atomic submit transaction that locks policy and admission rows in contract order, writes Job/LogicalSession/JobSpec/Event/Audit/Idempotency together, and exposes read-only keyset query methods. FastAPI routes remain thin and enforce tenant context, exact scopes, browser CSRF, strict JSON parsing, and response headers.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, PostgreSQL 17, Alembic metadata, Pytest/TestClient.

**Spec:** `PLAN.md` sections 3, 5, 6, 9, 10 and 13; `docs/contracts/openapi.yaml`; `docs/contracts/state-machines.md`; `docs/contracts/concurrency-recovery.md`; B08 request in the task attachment.

## Global Constraints

- PostgreSQL is the source of truth for accepted jobs, queue state, counters, rate buckets, events, audit and idempotency.
- `202` is returned only after the Job/LogicalSession/JobSpec/Event/counter/rate/idempotency transaction commits.
- Submit starts `QUEUED`, desired state `RUNNING`, version `1`, fence `0`, event sequence `1`, with no Attempt/Allocation/Lease.
- Idempotency scope is tenant context + authenticated principal + operation id + key; replay is checked after current authorization and before business mutation.
- Same key and payload replays the stored status/body/headers; a different payload returns `409 idempotency_conflict` without side effects.
- Lock order is policy mode, global/tenant/user counters and rate buckets, then Job/LogicalSession and artifact/reference rows as applicable; transaction retry is bounded to three attempts.
- Query/list/session/event surfaces are tenant-scoped; cursors are signed and bound to actor, tenant, operation and filter shape.
- Do not implement executor, worker, coordinator, scheduler ledger, control lifecycle, UI, load, or release work in B08.

### Task 1: Define B08 wire models and service boundary

**Files:**
- Modify: `src/nexa/api/schemas.py`
- Create: `src/nexa/application/job_service.py`
- Modify: `src/nexa/api/dependencies.py`

**Interfaces:**
- Consumes: `Principal`, B06 identity/policy helpers, B07 committed artifact rows, persistence tables and `run_transaction`.
- Produces: strict `JobSubmitRequest`/discriminated specs, `Job`, `LogicalSession`, `EventPage`, `JobPage` models and `JobService.submit/list/get/get_session/list_events` methods used by routes.

- [x] **Step 1: Write failing model/service tests** covering UUIDv7 fields, required spec members, unknown fields, template discriminator, resource bounds, and missing service methods.
- [x] **Step 2: Run the focused tests and confirm they fail for missing B08 models/service.**
- [x] **Step 3: Add strict Pydantic models matching the frozen OpenAPI JobSpec, Job, LogicalSession, page and Event shapes; add the service constructor and typed result dataclasses without side effects.**
- [x] **Step 4: Run the focused model tests and confirm they pass.**

### Task 2: Implement atomic submit and durable admission

**Files:**
- Modify: `src/nexa/application/job_service.py`
- Test: `tests/integration/test_jobs_b08.py`

**Interfaces:**
- Consumes: Task 1 models; `begin_idempotency`/`complete_idempotency`; `jcs_request_hash`; `identity.revalidate_principal`; `require_tenant_membership`; `jobs`, `job_specs`, `logical_sessions`, `events`, `audit_records`, `admission_counters`, `rate_buckets`, `policy_versions`, `tenant_policies`, `artifacts`, `artifact_reference_guards`, `template_versions`, `workers`, `worker_inventories`.
- Produces: atomic `submit(principal, tenant_id, spec, idempotency_key, request_hash)` returning status/body/headers and durable accepted-ID reconciliation query.

- [x] **Step 1: Add failing PostgreSQL tests for happy-path submit, initial state, immutable spec, no attempt/allocation/lease, and accepted-ID ledger reconciliation.**
- [x] **Step 2: Add failing tests for same-key replay, same-key conflict, tenant/principal isolation, mode `ADMISSION_OFF`/`WRITE_FROZEN`, quota/rate/queue errors, first-use counter/bucket races, and transaction rollback after each write group.**
- [x] **Step 3: Run those tests in guarded PostgreSQL mode and confirm expected failures.**
- [x] **Step 4: Implement authorization and strict template/artifact/spec/capability checks; use DB time and policy rows, lock/create global/tenant/user counter and rate rows canonically, refill durable buckets without exceeding capacity, and reject only infeasible requests versus queueable requests.**
- [x] **Step 5: Implement the single transaction that inserts Job, LogicalSession, immutable JobSpec, committed artifact references/guards where required, event sequence 1, outstanding/rate updates, audit and completed idempotency snapshot; do not create attempts or allocations.**
- [x] **Step 6: Return stored replay snapshots before admission/If-Match checks, map rate/quota/queue/dependency errors with `Retry-After`, and keep active/terminal idempotency retention timestamps contract-compliant.**
- [x] **Step 7: Run the focused PostgreSQL tests and confirm green.**

### Task 3: Implement tenant-scoped query, session and event APIs

**Files:**
- Modify: `src/nexa/application/job_service.py`
- Create: `src/nexa/api/routes_jobs.py`
- Modify: `src/nexa/api/app.py`
- Modify: `src/nexa/api/dependencies.py`
- Test: `tests/api/test_jobs_b08.py`

**Interfaces:**
- Consumes: Task 1/2 service methods and B06/B07 request/auth/error helpers.
- Produces: operation IDs `submitJob`, `listJobs`, `getJob`, `getLogicalSession`, `listJobEvents` under `/v1` with exact tenant header, security, status and response headers.

- [x] **Step 1: Add failing API/OpenAPI tests for route registration, operation IDs, security, headers, and response schemas.**
- [x] **Step 2: Add failing HTTP tests for browser CSRF, exact CLI scopes, cross-tenant 404, submit `202` Location/ETag, replay headers, list keyset ordering/cursor binding, session derived state and event `after_sequence`.**
- [x] **Step 3: Implement thin routes using strict JSON parsing, `resolve_principal`, `X-Nexa-Tenant-Id`, `run_in_threadpool`, and `JSONResponse`/headers without duplicating authorization logic.**
- [x] **Step 4: Implement signed cursor binding for list filters and bounded event pagination; serialize timestamps/UUIDs through `json_wire_value`.**
- [x] **Step 5: Run focused API tests and confirm green.**

### Task 4: Update docs, evidence and regression coverage

**Files:**
- Modify: `tests/api/test_operation_matrix.py`
- Modify: `docs/database.md`
- Modify: `docs/project-structure.md`
- Modify: `README.md`
- Modify: `ROADMAP.md`
- Create: `docs/submit.md`
- Create: `docs/evidence/B08-submit-durable-queue.md`

**Interfaces:**
- Consumes: verified implementation/test commands and accepted-ID reconciliation output.
- Produces: B08 handoff documentation with explicit evidence boundaries and downstream B11/B12/B13/B15 notes.

- [x] **Step 1: Extend operation-matrix regression expectations for the five B08 operations and run the matrix.**
- [x] **Step 2: Document transaction/lock order, durable queue semantics, replay/retention, query cursors and accepted-ID ledger; record PostgreSQL version, commands, pass/skip/failure status, restart/response-loss evidence and limitations.**
- [x] **Step 3: Update README/ROADMAP only to mark B08 implemented and awaiting independent review after evidence exists; leave later milestones unchanged.**
- [x] **Step 4: Run repository regression tests, lint/format checks, migration/schema guard checks and `git diff --check`.**

## Acceptance and Evidence Mapping

- ACC-03: tenant-scoped job/session/event/artifact reference tests.
- ACC-06: durable rate/quota/backpressure and `202`-after-commit tests.
- ACC-07: concurrent replay/conflict/scope/retention tests.
- ACC-08: atomic state/event/counter/idempotency and rollback/race tests.
- ACC-20: accepted-ID reconciliation before/after response loss and API restart.

The plan does not claim dispatch, execution, production fairness ledger, worker/coordinator recovery, GPU/Linux portability, load, hosted CI, or release gates.

## Handoff to Later Tasks

- B11 consumes accepted Job/Session/Spec/reference/event/counter rows for dispatch, and still depends on B10 worker readiness.
- B12 consumes these REST operations for CLI submit/status/events.
- B13 integrates the durable queue with production fairness and admission accounting.
- B15 owns control/terminal lifecycle and completes terminal retention/cleanup semantics.
- B16 owns sweep expansion and workload execution.
