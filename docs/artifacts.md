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

### Store identity (B14-OBS-01)

The root also holds `store-identity`, a mode `0600` regular file containing exactly
`nexa-artifact-store v1 <uuid>` and a newline. It is created once through a temporary
file and `link`, so an existing identity is never replaced. PostgreSQL records the same
UUID in the insert-only singleton `artifact_store_identity` (migration
`20260929_0022`). At startup the API binds its store only when the identity at the root
equals the recorded one. On the first start, including the upgrade to 0022, the API
records the identity that is already at the root, or creates a new one. It refuses
when PostgreSQL references committed blobs while `committed/` is empty, because that
is an empty or wrong volume.

`open`/`inspect` report `not_found` only when all of these hold at that moment: the
store is bound; the root identity still equals the bound identity; `committed/` is a
real directory; and the blob is absent. Every other case is `storage_unavailable`
(temporary, HTTP 503): a missing root, a missing `committed/`, a re-created tree
without an identity, another store's identity, or a malformed or symlinked identity.
So only `not_found` can make restore mark a checkpoint `CORRUPT` with
`CHECKPOINT_BLOB_MISSING`. The worker READY readiness probe also requires the verified
identity, so nothing dispatches while the store is unbound. Restoring the right volume
takes an API restart. A deployment whose storage was really lost is outside the
failure scope and has no automatic re-initialization.

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

Result publication and worker authority/fencing are B10/B11 responsibilities.
Reference reachability, orphan sweep and production GC remain B19.

## CPU checkpoint artifacts (B14)

Checkpoint bytes use the worker attempt upload path, never the tenant upload
endpoint above. Only kinds `CHECKPOINT_FILE` and `CHECKPOINT_MANIFEST` with
`application/json` are accepted, for a checkpoint reservation that is still
`RESERVED` under live authority. Each upload goes through the same bounded
staging, checksum, fsync, atomic rename, directory fsync and metadata commit as
above. Publish commits the `checkpoints` row and its references (owner
`CHECKPOINT`: the manifest as `CHECKPOINT_MANIFEST/manifest`, each file as
`CHECKPOINT_FILE/<logical_name>`) in one transaction, only after every blob is
committed. A crash or rejection before that leaves unreferenced orphans that
can never be restored. Restore reads blobs through the attempt's execution graph
and verifies size and checksum against the committed metadata. A mismatch marks
the checkpoint corrupt (insert-only `checkpoint_corruptions`) and never rewrites
or deletes the blob. No B14 path deletes, prunes or overwrites a committed
checkpoint artifact, so every committed checkpoint, and therefore at least the two
newest, stays referenced. Operational GC that must honour these references is
B19. Checkpoint content is never written to logs, events or error bodies.

## AI workload artifacts (B16; đã triển khai, chờ Task Review)

The worker attempt upload allowlist is now per adapter
(`nexa.domain.workload_adapters.AdapterDescriptor.upload_media_types`). Before
the transaction a kind/media pair must appear in the union of all descriptors;
inside the transaction the job's `template_versions.(adapter_id, adapter_version)`
narrows it to that adapter, otherwise `422 validation_failed`:

| Adapter | `RESULT_FILE` | `RESULT_MANIFEST` | `CHECKPOINT_FILE` | `CHECKPOINT_MANIFEST` | `CHUNK_OUTPUT_MANIFEST` |
|---|---|---|---|---|---|
| `cpu.iterative` 1.0.0 | `application/vnd.nexa.cpu-iterative-result+json`, `application/json` | `application/json` | `application/json` | `application/json` | — |
| `pytorch.cifar10` 1.0.0 | `application/octet-stream` (`model.safetensors`), `application/json` (`metrics.json`) | `application/json` | `application/octet-stream` (`model/optimizer/rng.safetensors`), `application/json` (`training-state.json`) | `application/json` | — |
| `batch.inference` 1.0.0 | `application/json` (`summary.json`), `application/x-ndjson` (`chunk-%08d.jsonl`), `application/vnd.apache.parquet` (`chunk-%08d.parquet`) | `application/json` | `application/json` (`inference-state.json`) | `application/json` | `application/json` |

Checkpoint kinds are accepted only while the attempt is `CHECKPOINTING`, other
kinds only while it is `RUNNING`. For the chunked `batch.inference` adapter,
chunk files and the chunk-output manifest are also accepted while
`CHECKPOINTING`, because the checkpoint cycle binds them (B16-R19). Tenant
uploads for inputs use the existing endpoint: `DATASET` as
`application/vnd.apache.arrow.file` for both AI templates, and `MODEL` as
`application/octet-stream` for `batch-inference`. Submit rejects any other
pair. Bounds: every checkpoint tensor file is at most 1 MiB, and each chunk
file and chunk-output manifest is at most 1 MiB − 16 KiB, with at most 2048
chunks per job (B16-R17). Every upload goes through the same bounded staging and
durable commit as above.

`RecognizedChunk` rows reference `CHUNK_OUTPUT` artifacts that are `COMMITTED`
with the exact checksum. A later attempt may only carry a chunk forward if its
row matches exactly (same artifact, checksum, source attempt and fence).
Restore selection re-reads every chunk blob the candidate checkpoint
references; a chunk missing from the verified store (see Store identity) or a corrupt
chunk marks the checkpoint `CORRUPT` and falls back (B16-R07). No B16 path deletes or overwrites a committed artifact. GC is
still B19. Chunk, tensor, dataset and model bytes are never written to logs,
events or error bodies. See [B16 evidence](evidence/B16-pytorch-sweep-inference.md).
