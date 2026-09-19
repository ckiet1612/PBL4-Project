# PostgreSQL physical database mapping

Tài liệu này mô tả schema generation `1` do B05 triển khai từ contract `1.0.0-b01`.
[PLAN.md](../PLAN.md), [domain model](contracts/domain-model.md),
[concurrency/recovery](contracts/concurrency-recovery.md) và
[invariants](invariants.md) vẫn là nguồn sự thật về hành vi. Schema cung cấp storage,
constraint, index và primitive transaction; nó không thay authorization, state-machine
orchestration, artifact durability hoặc recovery runtime.

## Storage classes

- **PostgreSQL-authoritative:** identity, ownership, job/session/attempt state, queue,
  allocation, quota/counter, lease, authority, fairness ledger, event/audit,
  idempotency và metadata/reference được công nhận.
- **Filesystem blob + PostgreSQL metadata:** bytes của input, checkpoint, result, chunk và
  log nằm dưới artifact root; `artifacts` và các reference/provenance liên quan là catalog
  authoritative. Client không cung cấp filesystem path.
- **Worker-local durable:** `ExecutorAttemptRecord` và state cần reconcile Docker nằm trên
  worker. Không có table `executor_attempt_records`; worker chỉ dùng REST `/v1`, không truy
  cập PostgreSQL.
- **Derived/cache:** scheduler heap/candidate window, worker availability cache và metric
  projection có thể dựng lại từ PostgreSQL. `queue_heads` là durable cursor/head hỗ trợ
  query, không phải heap authoritative trong process.

## Logical-to-physical mapping

`P` là PostgreSQL-authoritative; `F+P` là blob filesystem bất biến cùng metadata/reference
PostgreSQL; `W` là worker-local durable; `D` là derived/cache. Tên constraint/index dưới
đây là các điểm chính, không thay inventory DDL đầy đủ trong `schema_v1.py`.

### Identity and authority

| Logical entity | Class / table and key | Main constraints and indexes | Invariant and direct test | Remaining runtime obligation |
|---|---|---|---|---|
| `Tenant` | P `tenants(tenant_id)` | Lowercase slug check; unique `uq_tenants_slug_casefold`; positive `version`; deferred one-`MembershipSet` trigger | INV-01/07; `test_constraints_identity_jobs.py` insert/update normalization and empty aggregate | B06 checks principal scope, role and membership on every operation |
| `User` | P `users(user_id)` | Lowercase username, unique `uq_users_username_casefold`; Argon2 hash column only; positive `version` | INV-20; metadata secret/normalization checks | B06 hashes/verifies passwords, revokes sessions and applies login limits |
| `MembershipSet` | P `membership_sets(tenant_id)` | Exactly one aggregate per tenant, positive version, exists even with zero members; deferred trigger covers tenant and aggregate INSERT/UPDATE/DELETE | INV-01/07; empty aggregate commit and delete-rejection tests | B06 locks aggregate and advances version with membership mutations |
| `Membership` | P `memberships(tenant_id,user_id)` | FK to aggregate/user; closed tenant role enum; composite PK | INV-01; migration/metadata parity | B06 authorization and last-admin/business rules |
| `SystemRoleGrant` | P `system_role_grants(user_id,role)` | Closed `SYSTEM_ADMIN`, positive version, grantor FK | INV-20; metadata/migration parity | B06 grant/revoke policy and audit |
| `BrowserSession` | P `browser_sessions(browser_session_id)` | Unique opaque `secret_hash`; CSRF hash; user/expiry index | INV-20; no raw-secret metadata test | B06 cookie flags, CSRF, expiry/revocation and frozen-mode rules |
| `CliToken` | P `cli_tokens(token_id)` | Unique `token_hash`; scope JSON metadata; user/expiry index | INV-20; no raw-token metadata test | B06 one-time issuance, scope validation, expiry/revocation |
| `WorkerCredential` | P `worker_credentials(credential_id)` | Unique credential hash; worker/expiry index; no raw secret | INV-20; no raw-credential metadata test | B06/B10 bootstrap, rotation, scope and worker authentication |

Lowercase storage plus `CHECK (value = lower(value))` makes the ordinary unique index
case-insensitive for accepted values without requiring `citext` or a locale-dependent
expression index.

### Template, artifact, job and session

| Logical entity | Class / table and key | Main constraints and indexes | Invariant and direct test | Remaining runtime obligation |
|---|---|---|---|---|
| `Template` | P `templates(template_id)` | Current-version composite FK | INV-18; migration parity | Admin allowlist and capability policy in B06/B09 |
| `TemplateVersion` | P `template_versions(template_id,version)` | Immutable once referenced; image digest format and positive version | INV-18; trigger/migration tests | B06 admin lifecycle and B09 image/capability verification |
| `Artifact` | F+P `artifacts(artifact_id)` plus internal `artifact_reference_guards(tenant_id,artifact_id)` | Composite tenant/state identity; unique blob key and tenant digest tuple; checksum/state/size checks; only `COMMITTED` artifacts receive a guard; every authoritative consumer FK targets that guard | INV-01/14/16; cross-tenant, staging-guard and two-connection recognition/cleanup races | B07 implements bounded staging, checksum, fsync/rename/fsync-dir, watermark and GC |
| `JobSpec` | P `job_specs(job_id)` | Immutable row; composite job/artifact FKs; template version FK; resource and checksum bounds | INV-01/08; cross-tenant insert and immutability tests | B08 canonicalizes request and atomically accepts job/spec/session |
| `Job` | P `jobs(job_id)` | Composite tenant identity; same-tenant retry FK; closed states; version/sequence checks; monotone fence and terminal update trigger; queue/retry/keyset indexes | INV-01/08/10; incomplete job, retry ownership, fence-decrease and terminal tests | B08/B11/B15 state machine, If-Match and fence orchestration |
| `LogicalSession` | P `logical_sessions(session_id)` | Unique one per tenant/job; composite `(tenant,job,session)` provenance | INV-08/17; deferred completeness and delete test | B08 creates it with accepted job; B15 preserves it across recovery |
| `Attempt` | P `attempts(attempt_id)` | Unique `(job,attempt_number)` and startup nonce; composite job/fence/worker identity | INV-08/09; worker-mismatch and authority lineage tests | B11 grants authority atomically; B15 classifies/retries attempts |
| `RetrySchedule` | P `retry_schedules(job_id,retry_number)` | Bounded retry/jitter checks; partial ready index | INV-08/22; migration/index tests | B15 computes allowed failure class, backoff and retry budget |
| `SweepParent` | P `sweep_parents(sweep_id)` | Tenant identity, child/count bounds and outcome consistency | INV-07/19; metadata/migration parity | B16 expands bounded sweep; parent never receives allocation/slot |
| `SweepChild` | P `sweep_children(sweep_id,child_index)` | Same-tenant parent/job FKs; unique parameter hash; exactly one outcome | INV-01/19; metadata/migration parity | B16 per-child admission/idempotency/quota and aggregate outcome |

`BrowserSession` and `LogicalSession` are unrelated identities. Manual retry uses a new
`Job` and `LogicalSession` linked by `retry_of_job_id`; automatic recovery creates a new
`Attempt` under the existing job/session.

### Worker, inventory, allocation and execution authority

| Logical entity | Class / table and key | Main constraints and indexes | Invariant and direct test | Remaining runtime obligation |
|---|---|---|---|---|
| `Worker` | P `workers(worker_id)` | Composite current-incarnation FK; admin/health index; positive version | INV-12/13; metadata/migration parity | B10 heartbeat, health transitions and readiness |
| `WorkerIncarnation` | P `worker_incarnations(worker_incarnation_id)` | Unique sequence/process nonce; one current incarnation partial index | INV-09/12; authority adoption tests | B10 server-created incarnation and reconcile-before-READY |
| `Inventory` | P `worker_inventories(inventory_id)` | Unique worker/version; incarnation FK; host/allocatable bounds and checksum | INV-02/03; GPU inventory-version test | B09/B10 real discovery, reserve subtraction and capability checks |
| `GpuDevice` | P `gpu_devices(worker_id,inventory_version,gpu_uuid)` | Composite inventory FK; memory bound | INV-02/03; inventory-version race test | B23 real NVIDIA discovery/isolation; no GPU claim from B05 alone |
| `CoordinatorLeadership` | P singleton `coordinator_leadership` | Singleton key and monotone epoch | INV-09; migration parity | B11 DB-time lease acquisition/renewal and stale leader rejection |
| `Allocation` | P `allocations(allocation_id)` | One allocation/attempt; composite attempt/fence/worker authority identity; resource/state checks; unreleased tenant/worker indexes | INV-02/09/13; worker-mismatch, GPU/release and transaction tests | B11 locks capacity/quota aggregates and commits dispatch; B15 releases only after cleanup proof |
| physical GPU claim | P `allocation_gpu_claims(allocation_id,gpu_uuid)` | Partial unique active `(worker_id,gpu_uuid)` independent of inventory version; deferred allocation/claim consistency on claim INSERT/UPDATE/DELETE | INV-02/13; two-transaction race, delete rejection, quarantine, version and atomic release tests | B11 creates claim with allocation; B15 marks both released in one transaction |
| `AttemptLease` | P `attempt_leases(lease_id)` | One active lease/attempt; composite attempt/allocation/fence/worker identity plus current incarnation owned by that worker; expiry index | INV-09/12/13; lease INSERT/UPDATE worker-mismatch and lineage tests | B10/B11 renew with DB time; B15 expiry/reaper/quarantine protocol |
| `AttemptAuthorityGrant` | P `attempt_authority_grants(grant_id)` | One current grant/job including `CREATED`; unique callback and `(attempt,worker incarnation)`; exact worker-bound attempt/allocation/lease/fence FKs | INV-08/09/11; mismatch, repeated A-to-B-to-A incarnation and valid A-to-B adoption tests | B11/B15 revoke/adopt under locks and validate every publish |
| `ContainerIdentity` | P `container_identities(attempt_id,container_id)` | Unique startup nonce; exact attempt/allocation identity; runtime digest | INV-12/13; metadata/migration parity | B09/B10 reconcile exact Docker identity and cleanup proof |
| `ExecutorAttemptRecord` | W, no PostgreSQL table | Durable local mapping of attempt/startup nonce/container/create sequence | INV-12/13; B05 classification only | B09/B10 implement local store; worker still never queries DB |

Capacity and quota are aggregate predicates across rows. B05 deliberately does not claim
that a per-row `CHECK` prevents oversubscription; B11/B13 must lock the relevant policy,
inventory, allocation, counter and ledger rows in contract order and recheck sums before
commit.

### Policy, quota and fairness

| Logical entity | Class / table and key | Main constraints and indexes | Invariant and direct test | Remaining runtime obligation |
|---|---|---|---|---|
| `PolicyVersion` | P `policy_versions(policy_version)` | One current version partial unique; mode/outstanding checks | INV-07; metadata/migration parity | B06/B13 validated policy change and drain-before-tighten rules |
| `TenantPolicy` | P `tenant_policies(tenant_id,version)` | One current/tenant; canonical exact positive Decimal text for weights/rates; independent positive tenant/user outstanding limits; quota/concurrency bounds | INV-04/07; Decimal and distinct outstanding-limit round-trip/rejection tests | B06 writes versioned policy; B13 applies it under lock |
| `AdmissionCounter` | P `admission_counters(scope_type,scope_id)` | Closed global/tenant/user scope; nonnegative counters and version | INV-07/10; atomic rollback/CAS foundations | B08 updates atomically with submit/control/idempotency |
| `RateBucket` | P `rate_buckets(scope_type,scope_id)` | Exact finite token/rate/capacity values; version | INV-07/10; metadata and Decimal checks | B06/B08 DB-time refill and replay-safe consumption |
| `FairnessLedger` | P `fairness_ledgers(tenant_id)` | Exact nonnegative score, accounted-through time, version; bounded-prefix score index | INV-04; 50-digit/close/5,000-digit/policy-order tests | B13 accounts held allocation at events and tick <=1 s without double charge |
| virtual floor | P singleton `fairness_state` | Exact nonnegative `virtual_floor` and version | INV-04; round-trip test | B13 advances floor and initializes newly active tenant correctly |
| `AllocationLedgerSegment` | P `allocation_ledger_segments(segment_id)` | One open segment/allocation; policy/tenant/allocation FKs; exact share/weight/charge and time-order checks | INV-02/04; metadata/index tests | B13 opens/closes segment at allocation transitions and reconstructs after restart |
| `Reservation` | P `reservations(reservation_id)` | One active local slot partial unique; job/policy FK; invalidation pair | INV-05/06; metadata/index coverage | B13 selects weighted tenant, drains, dispatches/invalidate with event reason |
| `QueueHead` | P `queue_heads(tenant_id,priority)` | Candidate job composite FK, bounded priority and sequence | INV-05/06; metadata/migration parity | B13 maintains/rebuilds bounded cursor and heap |
| scheduler heap/candidate cache | D, process memory | Rebuilt from jobs/heads/ledger/reservation; never correctness authority | INV-04/17; B05 classification only | B13 implements bounded retrieval and 100k queue evidence later |

All B04 arithmetic fields use `ExactDecimalText`, encoded canonically as
`sign:digits:exponent` and decoded directly to the original Python `Decimal` tuple. This
avoids PostgreSQL `NUMERIC` exponent limits without quantization or float conversion, and
preserves trailing zeros as well as values such as `1E-20000`. Python and PostgreSQL both
require the raw exponent to be within `decimal.MIN_ETINY..decimal.MAX_EMAX`; for a nonzero
coefficient they also require `exponent + digit_count - 1 <= decimal.MAX_EMAX`. Trailing zeros
count because they remain part of the exact tuple, while zero itself remains valid at raw
`MAX_EMAX`. Immutable PostgreSQL helpers validate sign/domain, compare encoded values, and
derive zero rank, adjusted exponent and normalized significand for deterministic ordering.
Exact queries may sort on the full normalized significand; the B-tree stores only its first 256
characters with `COLLATE "C"`, so a valid 5,000-digit significand cannot exceed the index-row
limit. The rate-bucket capacity check uses the exact compare helper. Domain validation and DB
checks reject negative, non-finite or undecodable values where forbidden.

### Events, deduplication and recognized data

| Logical entity | Class / table and key | Main constraints and indexes | Invariant and direct test | Remaining runtime obligation |
|---|---|---|---|---|
| `Event` | P `events(event_id)` | Append-only; unique `(job_id,sequence)`; a job event must carry the same tenant while global events use both fields `NULL`; job sequence keyset index | INV-01/10/21; tenant/job pairing, duplicate sequence and atomic rollback tests | B08/B11/B15 advance job counter and insert event in same transaction |
| `AuditRecord` | P `audit_records(audit_id)` | Append-only; tenant/global history indexes; version-domain checks | INV-10/21; atomic rollback and index tests | Each application use case writes safe metadata without secrets |
| `IdempotencyRecord` | P `idempotency_records(idempotency_id)` | Non-null unique `(context,principal,operation,key)`; closed `GLOBAL`, `BOOTSTRAP`, tenant UUID or `WORKER:<uuid>`; expiry index | INV-07/10; null/invalid/global duplicate tests | B06/B08/B15 replay before `If-Match`, payload hash and one-time-secret behavior |
| `CallbackReceipt` | P `callback_receipts(receipt_id)` | Unique `(worker_id,operation_id,callback_id)` and payload hash | INV-10/11; duplicate callback test | B10/B11/B15 persist acknowledgment with transition |
| `CheckpointReservation` | P `checkpoint_reservations(checkpoint_id)` | Unique job sequence across attempts and callback; one `RESERVED` per attempt; exact authority FK; deferred final-state check keeps a recognized checkpoint source `COMMITTED` | INV-11/14/15; cross-attempt sequence, uniqueness, uncommitted-recognition and post-recognition mutation tests | B14 reserve/replay response and publish only after durable blob |
| `Checkpoint` | F+P `checkpoints(checkpoint_id)` | Immutable recognized row; unique `(job,sequence)`; exact reservation/attempt/artifact provenance; manifest checksum must equal the committed artifact checksum; job index | INV-11/14/15; checksum/provenance, committed positive and reserved negative tests | B14 compatibility, retention >=2 and fallback |
| `CheckpointReference` | P `checkpoint_references(target_job_id,source_checkpoint_id)` | Same-tenant job/checkpoint composite FKs | INV-01/15/16; metadata/migration parity | B14/B15 validate compatibility and protect reference during GC/restore |
| `ResultReservation` | P `result_reservations(result_id)` | One `ACTIVE` per job; unique callback; exact authority FK; deferred final-state check keeps a recognized result source `COMMITTED` | INV-11/14; active-result, uniqueness and post-recognition mutation tests | B11 reserve/replay and fenced publish |
| `Result` | F+P `results(result_id)` | Immutable; unique one/job; exact reservation/attempt/artifact provenance; manifest checksum must equal the committed artifact checksum | INV-10/11/14; checksum/provenance, final-result uniqueness and committed-source tests | B11 validates lease/fence/desired state at publish |
| `RecognizedChunk` | F+P `recognized_chunks(recognized_chunk_id)` | Immutable; unique `(job,chunk)`; exact session/source attempt/fence/artifact FKs; artifact must be `COMMITTED` with exact checksum | INV-01/11/15; wrong-session/source, staging-artifact and checksum-mismatch tests | B16 chunk carry-forward and output assembly |
| `LogSegment` | F+P `log_segments(log_segment_id)` | Immutable; unique attempt/start offset; exact attempt/artifact FKs; range checks; artifact must be `COMMITTED` with exact checksum | INV-01/16/21; staging-artifact and checksum-mismatch tests | B09/B19 bounded/truncated log streaming and retention |
| `ArtifactReference` | P `artifact_references(...)` | Closed owner type; artifact FK targets the committed guard; generated UploadSession owner key has a composite FK; reference-side trigger validates other polymorphic owners; owner index | INV-01/16; committed artifact, two-connection owner delete/tenant-transfer races, INSERT/UPDATE and unreferenced cleanup tests | B07/B14/B19 reference creation and GC lock/recheck |
| `UploadSession` | P `upload_sessions(upload_id)` | Composite tenant/upload key for conditional reference FK; optional attempt provenance; unique staging key; active expiry index; size/checksum/state checks | INV-14/16; concurrent owner/reference and cleanup tests | B07 bounded stream, expiry/orphan cleanup and atomic publish |

Broad cascade delete is not used. Historical, audit, result and provenance FKs use
`RESTRICT`; immutable triggers protect accepted specifications and recognized records.
Filesystem retention, fsync, checksum scan and GC safety remain runtime work even though
the reference graph is present.

`JobSpec` may reference only `COMMITTED` input/model artifacts. An artifact trigger maintains
one guard row only while the artifact is `COMMITTED`; every accepted spec, checkpoint, result,
recognized chunk, log segment and generic artifact reference has a composite FK to that row.
Before tenant, kind, checksum, blob key, size or state changes, or before deletion, the trigger
must delete the guard. PostgreSQL FK locking therefore makes reference creation and cleanup
serialize across independent transactions: either recognition wins and mutation is rejected,
or cleanup wins and recognition is rejected. Rows with no authoritative reference remain
eligible for cleanup.

The checkpoint/result reservation update triggers are deferred and query the final row at
commit. A transaction may perform intermediate state changes, but it cannot commit a
recognized checkpoint or result whose reservation is no longer `COMMITTED`. Composite
foreign keys separately preserve the recognized tenant/job/attempt identity; the authority
grant binding remains eligible for the same-attempt adoption flow defined by the contract.

## IDs, timestamps and transactions

- `new_uuid7()` creates RFC 9562 UUIDv7 in the server process using Unix milliseconds,
  cryptographic randomness and a process lock that preserves monotone ordering. PostgreSQL
  has no assumed `uuidv7()` extension and Python 3.12 has no assumed `uuid.uuid7()`.
- Durable timestamps use `timestamptz`. Lease decisions must call `clock_timestamp()`
  after waiting for locks; `transaction_timestamp()` is intentionally stable inside a
  transaction.
- `create_database_engine()` accepts only `postgresql+psycopg`, uses `READ COMMITTED`,
  `pool_pre_ping=True` and hides parameters. `create_session_factory()` disables autoflush
  and expiry-on-commit.
- `lock_rows()` sorts canonical IDs before `SELECT ... FOR UPDATE`; callers must continue
  the global lock order in the concurrency contract.
- `compare_and_swap()` requires the expected positive version and increments it exactly
  once. The caller writes state/event/counter/idempotency in the same transaction.
- `run_transaction()` retries the whole side-effect-free transaction only for SQLSTATE
  `40001` and `40P01`, at most three attempts with fresh 10-50 ms jitter. Every failed
  session is rolled back and closed. Data conflicts, disconnect/unknown commit outcomes
  and business errors are not retried.

No Docker, filesystem or network side effect belongs inside a retry body.
Direct PostgreSQL tests commit and roll back job state/version, event, admission counter and
idempotency as one `run_transaction()` unit; the losing CAS writer is also checked to leave
no event or counter side effect.

## Migration and schema compatibility

Alembic head is `20260919_0001`. The revision imports immutable `schema_v1` metadata;
current `schema.py` clones that snapshot so a future table/column cannot change the old
revision. Online migration takes a PostgreSQL session advisory lock, commits the lock
acquisition so Alembic owns its DDL transaction, commits migration DDL, then unlocks.
Two independent migration processes therefore serialize.

`inspect_schema_compatibility()` returns `missing`, `old`, `current`, `new` or `unknown`.
`require_current_schema()` fails closed; neither module import nor schema guard migrates
the database. Later readiness/write/resume integration belongs to B11/B15/B21.

Local PostgreSQL 17 integration mode is explicit:

```sh
NEXA_TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@127.0.0.1:PORT/nexa_b05_test_NAME' \
  uv run --no-sync pytest -q --run-postgres
```

The destructive fixture rejects non-psycopg URLs, non-loopback hosts outside the CI
`postgres` service, database names without `nexa_b05_test_`, a wrong PostgreSQL major
version, and reuse of `NEXA_DATABASE_URL`. It validates the target and server version before
any schema cleanup. Alembic honors an explicitly supplied config URL; the runtime
`NEXA_DATABASE_URL` is only a fallback when no explicit migration target is configured.

Upgrade a configured database explicitly:

```sh
NEXA_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST/DATABASE' \
  uv run --no-sync alembic upgrade head
```

Do not downgrade a database after product writes. B05 verifies downgrade/re-upgrade only
for the supported pre-write lifecycle and does not promise lossless rollback of newer data.

## B06 and later handoff

B06 should construct one engine with `create_database_engine`, build a session factory,
wrap a complete application mutation in `run_transaction`, acquire only required rows in
the documented lock order, use `compare_and_swap` for versioned aggregates, and map
infrastructure exceptions outside this package. Before serving writes it must require a
`CURRENT` schema but must never auto-migrate during import or a request.

Schema support does not complete these obligations:

- **B06:** ownership/membership/principal authorization, password/token/session lifecycle,
  role/policy mutation and safe audit.
- **B08:** durable submit/admission, exact idempotency replay before `If-Match`, and atomic
  counters/events.
- **B11:** coordinator epoch, capacity/quota aggregate recheck, fence/state-machine
  orchestration and fenced result publish.
- **B13:** <=1-second held-allocation accounting loop, restart reconstruction, reservation
  operation and queue/load evidence.
- **B14:** staged blob durability, checkpoint provenance/compatibility, retention >=2,
  restore fallback and GC protection.
- **B15:** lease/reaper/cancel/pause/recovery races, quarantine and cleanup proof before
  allocation/GPU release.
