# B08 submit, idempotency và durable queue evidence

## Trạng thái và phạm vi

- Task: B08, working tree chưa commit; không tạo branch/commit/push.
- Scope: submit/query/session/events, atomic admission, strict template/artifact checks,
  idempotency replay/conflict, durable queue và accepted-ID reconciliation.
- Không claim Task Review đã duyệt; không claim B09/B10/B11/B13/B15/B16, Linux/GPU,
  load/soak/chaos, hosted CI hay release gates.
- PostgreSQL test container: `nexa-b07-postgres`, image `postgres:17`, guarded database
  `nexa_b05_test_b07` trên `127.0.0.1:55432`. Fixture chỉ cho phép database prefix
  `nexa_b05_test_` và PostgreSQL major 17; version quan sát trong remediation là `17.11`.

## Implementation evidence

`src/nexa/application/job_service.py` thực hiện authorize/revalidate, template/version
artifact media compatibility, fail-closed parameter/resource/runtime/checkpoint bounds,
configured idempotency retention, DB-time rate refill, locked counters/buckets, atomic
Job/Session/Spec/artifact-reference/Event/Audit/idempotency commit và signed keyset query.
Routes ở
`src/nexa/api/routes_jobs.py` đăng ký `submitJob`, `listJobs`, `getJob`,
`getLogicalSession`, `listJobEvents`; wiring nằm ở app/dependencies. Không có migration mới;
B05/B06/B07 tables và committed-artifact guard đáp ứng B08 storage needs.

Lock order được kiểm tra qua code review: idempotency → global policy → tenant policy →
global/tenant/user counters → tenant/user rate buckets → template/artifact/inventory reads
→ job/session inserts and remaining durable rows. `run_transaction` retries SQLSTATE
`40001`/`40P01` at most three attempts.

## Commands and results

Commands dùng `.venv/bin/pytest` và `PYTHONPATH=src` vì `uv` không nằm trong PATH của
checkout này. Chạy focused B08 sau review remediation:

```text
NEXA_TEST_DATABASE_URL=postgresql+psycopg://.../nexa_b05_test_b07 \
  PYTHONPATH=src .venv/bin/pytest -q --run-postgres \
  tests/integration/test_jobs_b08.py
20 passed, 2 warnings

PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_config.py tests/application/test_job_service_b08.py \
  tests/application/test_job_schemas_b08.py tests/api/test_jobs_b08.py \
  tests/api/test_operation_matrix.py
54 passed, 7 skipped, 2 warnings
```

Final full-suite commands after the current fixes:

```text
PYTHONPATH=src .venv/bin/pytest -q
310 passed, 184 skipped, 2 warnings

NEXA_TEST_DATABASE_URL=postgresql+psycopg://.../nexa_b05_test_b07 \
  PYTHONPATH=src .venv/bin/pytest -q --run-postgres
494 passed, 3 warnings
```

## Scenarios covered

- Happy path accepts CPU input and returns `202`, `Location`, `ETag`, initial queued state,
  session identity and event sequence 1; no attempt/allocation/lease exists.
- Same key/same payload replays the exact stored snapshot; same key/different payload is
  `409 idempotency_conflict` with one Job/Spec/Session/Event only.
- Two independent clients racing the same key both receive the one committed snapshot and
  the database contains one accepted Job/Spec/Event.
- Different-key races against a single global queue slot and a one-token durable bucket
  commit exactly one Job and reject the other with `queue_full` or `rate_limited`; counters
  never exceed one.
- Missing browser CSRF is rejected; queue full returns `503 queue_full` and `Retry-After: 1`.
- Cross-tenant object reads return `404` after valid membership context; CLI `jobs:read` and
  `jobs:write` are exact, non-hierarchical scopes. Runtime OpenAPI publishes the frozen
  browser/CSRF/CLI security alternatives for all five B08 operations.
- Wrong CPU artifact kind/media and wrong training/inference dataset/model media are
  `422 infeasible_request`; Job/Spec/Session/Event/reference/counter/rate/audit/submit-
  idempotency effects remain absent.
- Null, list, empty, missing-field and wrong-type template bounds return safe
  `422 infeasible_request`; no unbounded submit path or comparison `500` remains.
- `NEXA_IDEMPOTENCY_TERMINAL_RETENTION_DAYS` defaults to 30, rejects values below 30 or values
  whose expiry would exceed Python `datetime.max` (including the prior failing value
  `999999999`), and is consumed by submit; a configured 45-day value is persisted within the
  tested 44-46 day window. B15 still owns active-resource-aware terminal extension and cleanup.
- An injected PostgreSQL trigger failure before the initial event, after the primary submit
  inserts and durable rate consumption, returns fail-closed `503` and rolls back Job,
  Session, Spec, references, counters, buckets, audit and submit idempotency.
- Rate bucket versions advance once per accepted submit, and replay does not consume another
  rate token. Existing buckets adopt tightened capacity/refill policy under lock before
  consuming, and counter updates match exact `(scope_type, scope_id)` pairs.
- USER admission and rate scopes use canonical `tenant_id:user_id`; a decoy cross-scope row
  remains unchanged. Accepted input/model artifacts receive atomic `JOB_SPEC` references,
  and B07 GC token issuance rejects the referenced artifact with `state_conflict`.
- Capability matching treats declarations as closed, requires canonical adapter/image
  bindings, rejects malformed shapes, and only treats ENABLED workers with a fresh heartbeat
  as READY. Offline configured CPU capacity remains queueable with `waiting_for_worker`.
- Cursor signatures bind actor/tenant/filter shape, reject malformed/tampered values, require
  timezone-aware timestamps and UUIDv7 positions, enforce expiry, and preserve keyset
  ordering across an insert between pages.
- A test-only ASGI wrapper terminates the first Uvicorn OS process at successful
  `http.response.start`, after the submit transaction has committed but before response bytes
  reach the client. The client observes a transport failure, PostgreSQL contains the completed
  idempotency snapshot and accepted Job/Spec/Event, and a fresh normal API process queries the
  same Job and replays the exact stored body, `Location` and `ETag`. This is post-commit
  response-loss evidence, not a host reboot or storage-loss test.
- The frozen capacity schema continues to allow up to 64 configured GPUs while one Job
  request remains bounded to one GPU.

## Accepted-ID reconciliation

The accepted-ID ledger is the intersection of `jobs`, `logical_sessions`, `job_specs`,
committed `artifact_references`, `events(sequence=1)`, admission counters and
`idempotency_records(state=COMPLETED, resource_id=job_id)`. In the happy-path and
response-loss/restart scenarios the durable entity counts were `1/1/1/1/1`, counters
`[1,1,1]`, rate buckets `2`, and replayed `job_id` matched the original;
missing, duplicate and mismatch counts were all zero. A request that received `503` or
`422` left no partial accepted rows.

## Gate mapping and limits

Evidence covers the B08 portions of ACC-03 (tenant-scoped surfaces), ACC-06 (commit-before-
202, quota/rate/backpressure), ACC-07 (idempotency replay/conflict) and ACC-08 (atomic
state/event/counter/idempotency). It covers the accepted-job durability portion of ACC-20
through a post-commit/pre-response process crash, database reconciliation and fresh-process
replay.

Not covered: dispatch/attempt/lease/fencing, production fairness ledger and reservation,
worker/coordinator restart, filesystem-loss recovery, terminal retention sweep, UI/CLI,
Linux/GPU portability, load objective ACC-29, hosted CI and release. Independent Task Review
is still required.
