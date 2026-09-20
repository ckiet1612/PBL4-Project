# B07 Artifact Storage

B07 provides a single-node filesystem blob store and tenant artifact API. PostgreSQL
is authoritative for artifact metadata, upload-session state, idempotency and durable
tenant byte counters; bytes never enter an idempotency snapshot.

## Layout and path safety

`NEXA_ARTIFACT_ROOT` is an absolute deployment path. The store creates two private
directories on the same filesystem:

- `staging/<server-generated-32-hex-id>` for an active upload;
- `committed/<server-generated-32-hex-id>` for a durable blob.

Client filenames, tenant slugs, media types, query strings and paths are never used
as keys. Blob keys are opaque internal values. `O_NOFOLLOW`, regular-file checks,
root-local key validation and directory fsync fail closed on unsafe access. Staging
keys are not accepted by `open` or `inspect` and are never returned by the API.

## Upload contract

Public uploads use `application/octet-stream` transport and require:
`X-Nexa-Tenant-Id`, `Idempotency-Key`, `X-Artifact-Checksum` (`sha256:` plus 64
lowercase hex), `X-Artifact-Size`, `X-Artifact-Kind` (`INPUT`, `DATASET`, or `MODEL`),
and `X-Artifact-Media-Type`. When present, `Content-Length` must equal the declared
size. The current narrow allowlist is:

| Kind | Allowed original media types |
|---|---|
| `INPUT` | `application/vnd.nexa.cpu-iterative-input+json`, `application/json` |
| `DATASET` | `application/vnd.apache.arrow.file`, `application/json` |
| `MODEL` | `application/octet-stream` (opaque fixture; template validation remains required) |

The core does not permit attempt/checkpoint/result/log kinds on this endpoint.
Future frozen templates may extend the adapter allowlist without weakening the core.

## Durable commit and replay

The service authenticates and revalidates ownership, checks disk pressure and the
tenant counter, reserves declared bytes, creates an `ACTIVE` UploadSession, then
streams bounded chunks while hashing. It requires exact size and checksum, fsyncs the
staging file, atomically renames within the artifact filesystem, fsyncs the committed
directory, and only then commits Artifact metadata, counter conversion,
UploadSession=`COMMITTED`, audit and idempotency response in one PostgreSQL
transaction. A crash after rename but before DB commit leaves an unreferenced orphan;
it is not downloadable or referenceable.

The idempotency scope is tenant + principal + `uploadArtifact` + key. The request hash
contains tenant, kind, original media type, declared size and checksum. A completed
same request replays the exact Artifact body/Location/ETag; a different request is
`409 idempotency_conflict`; a pending request is bounded by `Retry-After: 1`. Replay
does not reserve quota again.

Cancellation is terminalized like any other interrupted stream: the upload session is
marked `ABORTED`, the reservation is released, the idempotency record receives a safe
error snapshot, and the staging capability/file is closed. If cancellation races a
database outage, the bounded expiry/reconciliation path remains the durable fallback.

## Quota and pressure

`artifact_storage_counters` has one row per tenant with committed and reserved bytes.
Admissions lock this row before checking `committed + reserved + expected` against
`NEXA_TENANT_ARTIFACT_QUOTA_BYTES`. Reservations are released on stream failure and
converted atomically on metadata commit/dedup. Filesystem `statvfs` is checked before
streaming and immediately before durable commit. Defaults are 10 GiB per file, 100
GiB per tenant, 85% high watermark, 95% critical watermark, and 24-hour staging/orphan
TTLs. B07 supplies the primitive; B19 still owns operational metrics, alerts and full
GC/reconciliation.

Upload commit, abort, and expiry use the same PostgreSQL lock order: idempotency
record, tenant storage counter, then upload session. Expiry selects candidates without
taking upload locks, then acquires each lock class in deterministic order before
rechecking state and expiry.

## Read API

`GET /v1/artifacts` returns only committed rows for the authenticated tenant in
`created_at DESC, artifact_id DESC` keyset order (default 50, maximum 100). Cursors
are signed and bound to actor, tenant, filter and operation. Metadata and content
endpoints recheck membership/ownership on every request. Content always transports as
`application/octet-stream`, returns the original media type in
`X-Artifact-Media-Type`, checksum ETag, and supports one well-formed byte range with
`206`/`Content-Range`; malformed, multi-range and out-of-range requests are rejected.

Checkpoint/result publication, worker authority/fencing, reference reachability,
orphan sweep and production GC are downstream B10/B11/B14/B19 responsibilities.
