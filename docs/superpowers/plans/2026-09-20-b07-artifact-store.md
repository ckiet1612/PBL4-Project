# B07 Artifact Store and Durable Upload Plan

## Scope and acceptance boundary

B07 adds the filesystem `ArtifactStore`, the application upload/list/metadata/download
service, and the public tenant artifact API. It consumes the frozen B05/B06
`artifacts`, `upload_sessions`, `artifact_references`, and idempotency schema. It
does not implement job submission, worker authority, checkpoint publication,
restore, metrics/GC operations, or Docker execution.

## Boundaries and modules

- `src/nexa/infrastructure/artifacts/store.py`: server-generated opaque keys,
  root-safe staging/committed layout, bounded append, checksum/size inspection,
  fsync/rename/directory-fsync, safe open/range and token-gated deletion.
- `src/nexa/infrastructure/artifacts/policy.py`: central public-kind/media-type
  allowlist and header normalization.
- `src/nexa/application/artifact_service.py`: principal/tenant revalidation,
  idempotency scope/hash, quota reservation, upload orchestration, metadata
  transaction, keyset list and download authorization. It never exposes paths.
- `src/nexa/api/routes_artifacts.py`: HTTP parsing and response mapping only.
- `src/nexa/infrastructure/persistence/schema_v3.py` plus migration
  `20260920_0003_b07_artifact_storage.py`: durable per-tenant committed/reserved
  byte counters and a DB-issued GC claim token seam; prior snapshots remain
  immutable.
- Existing config/app wiring, `.env.example`, docs, ROADMAP and B07 evidence are
  updated without changing PLAN or B01-B06 decisions.

## Upload state machine and commit order

`ACTIVE -> COMMITTED` is the success path. Stream/validation failures transition
to `ABORTED` or leave an expiring `ACTIVE` staging record for cleanup; expiry is
`ACTIVE -> EXPIRED`. Staging and committed blobs are never API-visible.

1. Authenticate and revalidate the principal, tenant membership and exact
   `artifacts:write` scope for CLI credentials; require CSRF for browser mutation.
2. Validate content headers, public kind/media allowlist, declared size, checksum,
   idempotency key, optional Content-Length, filesystem health and watermark.
3. Lock/create the tenant storage counter and reserve expected bytes, rejecting
   quota or critical watermark before any stream bytes are accepted.
4. Insert `UploadSession(ACTIVE)` and idempotency `PENDING` in one transaction.
5. Append chunks to a server-created staging file while hashing and enforcing both
   declared size and configured hard limit.
6. Require exact final byte count/checksum, fsync the staging file, atomically rename
   within the artifact root/filesystem, then fsync the committed directory.
7. Start a new DB transaction, recheck tenant/idempotency/counter state, deduplicate
   an existing same-tenant committed artifact when present, otherwise insert the
   immutable Artifact, convert reservation to committed bytes, mark UploadSession
   `COMMITTED`, and complete idempotency/audit together; commit before returning.

If DB commit is uncertain after rename, the blob remains an unreferenced orphan;
no blind delete or synthetic committed metadata is attempted. Cleanup is a later
DB-claimed operation and never removes committed/referenced data.

## Quota, reservation and watermark strategy

`artifact_storage_counters` is one canonical row per tenant with `committed_bytes`
and `reserved_bytes`. All admissions lock this row (`FOR UPDATE`) before reading
or updating counters. A reservation is released on stream/validation failure and
converted atomically on metadata commit; dedup releases it without double-counting.
Filesystem `statvfs` is checked before admission and again before commit. High and
critical watermark settings are validated centrally. B07 supplies the primitive
and evidence but does not claim B19 operational alerts, metrics, or full GC.

## Fault injection and tests

- Pure store tests use a temporary real filesystem and injectable `fsync`, rename,
  directory-fsync and statvfs calls. They cover checksum/size, bounded append,
  missing/symlink/traversal keys, full/range reads, and deletion token identity.
- Service tests cover allowlist, reservation release/commit, dedup and idempotency
  behavior with fakes for the store and database transaction boundary.
- PostgreSQL 17 integration tests use the guarded `NEXA_TEST_DATABASE_URL`, run the
  new migration, and cover tenant counter constraints, ownership, race-safe
  reservations, idempotency replay/conflict and metadata never pointing at staging.
- API tests cover auth/scope/CSRF, header mapping, 201/404/409/413/422/429/503,
  list keyset binding and full/single-range downloads.

## Acceptance mapping

ACC-03: tenant-bound queries and revalidated membership; ACC-06: bounded stream,
reservation and watermark admission; ACC-07: tenant/principal/operation idempotency;
ACC-17: fsync/rename/DB ordering and orphan boundary; ACC-23: file/tenant/storage
limits with B19 operational GC explicitly out of scope; ACC-24: opaque keys,
symlink/traversal and secret/path-safe errors; ACC-28: additive migration and schema
parity; ACC-39: reproducible unit, API and PostgreSQL evidence.

## Explicit limitations

Native macOS evidence cannot claim Linux filesystem portability or production
watermark/GC readiness. Worker attempt uploads, checkpoint/result provenance,
restore and B19 reconciliation remain downstream seams.
