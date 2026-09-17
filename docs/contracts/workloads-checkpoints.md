# Managed workloads and checkpoint contract

Dẫn xuất từ [PLAN](../../PLAN.md) §7/§9. Bốn template dưới đây là toàn bộ workload v1. User chỉ chọn template/version, committed artifact, typed parameters và resource vector; không cung cấp code, command, image, mount hay privileged environment.

Schema manifest chuẩn duy nhất là [workload-manifests.schema.json](schemas/workload-manifests.schema.json). OpenAPI tham chiếu trực tiếp schema đó.

## Contract chung cho template

Mỗi `TemplateVersion` bất biến khóa parameter schema, adapter ID/version, OCI image digest, architecture/device/framework compatibility, min/max resource, runtime max, checkpoint interval range, `checkpointable`, `restart_safe`, progress schema và failure mapping. Validation xảy ra khi submit và được recheck khi dispatch/restore. Dataset/model/input đều là same-tenant committed artifact hoặc admin-published immutable shared fixture được materialize thành tenant-authorized reference.

| Bound | Default / valid range | Source |
|---|---|---|
| Runtime attempt | default/max 300 s | PLAN §9 |
| Startup | max 30 s | PLAN §9 |
| Graceful stop | max 5 s, then kill | PLAN §9 |
| Checkpoint interval | default 30 s; 5–60 s | PLAN §9 |
| GPU request | 0 or 1 whole device | PLAN §2–§5 |
| Progress | typed `{fraction,step,epoch,item_cursor}` snapshot, monotonic sequence within attempt; restore may begin a new attempt at an earlier committed value | B01 concretization |
| Manifest files | ≤64 checkpoint files, ≤1024 result files; each bounded by configured artifact limit | B01 abuse bound, not benchmark result |

Runner progress message and REST renewal use the same closed snapshot: `fraction` plus nullable `step`, `epoch` and `item_cursor`. The trusted runner is the sole assigner of a strictly increasing `progress_sequence` per Attempt; worker validates it against durable local state and forwards the unchanged sequence/full snapshot in `RenewRequest`. Runner envelope `message_sequence` orders and deduplicates every IPC frame, while `progress_sequence` orders only new progress snapshots; neither is derived from the other. Duplicate/out-of-order progress sequences do not overwrite newer state. Example training update is runner progress sequence 41 `{fraction:0.42,step:4200,epoch:4,item_cursor:200}` → worker renew sequence 41 → API atomically stores it with the lease acknowledgment. After restore, a new Attempt may start at `{fraction:0.38,step:3800,epoch:3,item_cursor:800}` from the selected checkpoint; UI shows rollback provenance and the new attempt ID rather than mutating the old attempt's monotonic history.

## `cpu-iterative` version 1

Purpose: portable deterministic CPU/checkpoint oracle.

### Input and parameters

Input media type `application/vnd.nexa.cpu-iterative-input+json`, strict schema:

```json
{"initial_value": 17}
```

`initial_value` is integer `0..2147483647`. Parameters are `iterations: 1..1_000_000_000`, `seed: 0..2147483647`, `modulus: 2..2147483647`. Resource: CPU only (`gpu_count=0`), at least 100 millicores and 64 MiB; template version may set a portable higher minimum but cannot embed development-host capacity.

### Deterministic computation

Let `a_0=(initial_value+seed) mod modulus`. For step `k` from 0 through `iterations-1`:

```text
a_(k+1) = (a_k * 1664525 + 1013904223 + k) mod modulus
```

Integer arithmetic is exact and implementation must avoid platform overflow by using semantics equivalent to arbitrary precision before modulus. Progress is `completed_step/iterations`. Checkpoint cursor stores `step=completed_step`, `accumulator=a_step`, input/spec checksums and common provenance. Restore continues at `k=step`.

Result JSON media type `application/vnd.nexa.cpu-iterative-result+json` contains exactly `iterations`, `final_accumulator`, `input_checksum`, `spec_checksum`. Crash/resume result must match uninterrupted result byte-for-byte after canonical JSON serialization and checksum; no tolerance.

Failure classification: malformed input/parameter → `INVALID_INPUT`; runtime deadline → `TIMEOUT`; executor/resource failure → `INFRASTRUCTURE`; unexpected adapter invariant → `INTERNAL`. Template is `checkpointable=true`, `restart_safe=true`.

Transport example: tenant uploads the input body as `Content-Type: application/octet-stream`, `X-Artifact-Kind: INPUT`, `X-Artifact-Media-Type: application/vnd.nexa.cpu-iterative-input+json`; the stored Artifact media type is the latter. Worker uploads the result file as octet-stream with kind `RESULT_FILE` and `X-Artifact-Media-Type: application/vnd.nexa.cpu-iterative-result+json`. Content type is never used as a second, conflicting source of original media metadata.

## `pytorch-cifar10-cnn` version 1

Purpose: small managed training workload on CPU, and CUDA only after real GPU capability/gate.

### Dataset, model and parameters

CIFAR-10 subset is prepared offline as a safe tensor/array artifact plus JSON metadata. The metadata records source, selected indices, class counts, preprocessing, checksum and seed. Workload has no Internet. B16 must create and freeze:

- `tests/fixtures/workloads/pytorch-cifar10-v1/fixture.json`: dataset checksum, selected indices checksum, model architecture ID, preprocessing version, seeds and exact library/image digest;
- `tests/fixtures/workloads/pytorch-cifar10-v1/tolerance.json`: comparison metrics, absolute/relative tolerance values, device class and rationale **before** any acceptance measurement.

Those target paths specify ownership/location for B16; B01 does not create them or invent hardware-validated tolerance.

Parameters: `epochs 1..100`, `batch_size 1..512`, `learning_rate (0,1]`, `seed 0..2147483647`, `subset_size 100..50000`. Model is the admin-defined small CNN architecture frozen by template version; user cannot supply model code. CPU requires `gpu_count=0`; CUDA requires `gpu_count=1`, matching architecture/image/framework/CUDA/driver/compute capability.

Dataset upload example uses octet-stream transport, kind `DATASET`, and a template-allowlisted original media type such as `application/vnd.apache.arrow.file` for the safe array plus `application/json` for its metadata artifact. A CSV dataset, when a future frozen template version explicitly allows it, still uses octet-stream transport with `X-Artifact-Media-Type: text/csv`; media type is included in idempotency and cannot be changed under the same key.

### Checkpoint and comparison

Checkpoint must include safe tensor files for model and optimizer tensors and non-executable JSON/binary-safe state for:

- global step, epoch, batch/item cursor and sampler state;
- Python, NumPy, PyTorch CPU RNG and every allocated CUDA device RNG when CUDA;
- workload parameters, model/preprocessing identity, dataset/input/spec checksum;
- image digest, adapter/version, framework/device/architecture/CUDA/driver compatibility;
- per-file checksum/size and common provenance.

Arbitrary pickle (`torch.save` object graphs or `pickle.load`) is prohibited. Adapter must reconstruct only allowlisted model/optimizer types from the template and load tensor/state values through safe formats.

Comparison uses same fixture/image/device class/seed and uninterrupted baseline. Required assertions: final step/epoch/cursor equal; no sample omission beyond intentional at-least-once replay since last checkpoint; scalar loss/accuracy within pre-frozen absolute tolerances; selected model/optimizer tensor norms within pre-frozen relative tolerance; provenance/checksums exact. Cross-device CPU↔CUDA or architecture change is not assumed bitwise comparable and restore occurs only when the template compatibility rule explicitly allows it.

Failure: invalid dataset/spec → `INVALID_INPUT`; CUDA/driver/architecture mismatch → `INCOMPATIBLE`; OOM → `OOM`; deadline → `TIMEOUT`; worker/runtime loss → `INFRASTRUCTURE`; numerical non-finite result → `INTERNAL` and no blind retry. `checkpointable=true`; `restart_safe=true` only from original immutable input/seed/template, recorded by the concrete version.

## `hyperparameter-sweep` version 1

Sweep is a finite grouping/admission operation, not an executor workload. It has no Allocation, Attempt or resource slot.

Request contains one allowlisted training base spec and `1..16` dimensions, each with `1..100` typed values. Cartesian product must produce `1..100` children after canonical de-duplication; larger/empty product is `422`. Parameter names must exist in the child template schema and each resulting child must validate independently.

### Partial acceptance and replay

1. Parent idempotency transaction stores canonical request hash and immutable ordered expansion. Order is dimension request order, then value request order; `child_index=0..n-1`.
2. Child parameter JSON uses RFC 8785 hash. Internal child idempotency key is deterministically derived from `(sweep_id, child_index, parameter_hash)` and has normal tenant/principal/operation scope.
3. Every child calls the same submit use case: auth, input ownership, schema, rate, outstanding/concurrency/resource quota and queue checks. No bypass/bulk counter path.
4. Each outcome is durably `ACCEPTED(job_id)` or `REJECTED(error snapshot)`. A crash resumes unfinished indexes; completed indexes replay rather than create duplicates.
5. Response `207` exposes every child outcome. Parent counts are derived from immutable outcomes. Replaying parent key/hash returns identical mapping and does not spend rate/counter again for completed children.

Rejected child can be resubmitted only through a new parent request/key or normal job submit after conditions change. Parent cancellation does not implicitly cancel accepted child jobs; user controls each job, avoiding a hidden second state machine.

## `batch-inference` version 1

Input dataset and model are committed allowlisted artifacts. Parameters: `chunk_size 1..100000`, `batch_size 1..4096`, output `JSONL` or `PARQUET`. Model format/loading is template-specific safe format; no arbitrary executable model object. CPU or one compatible CUDA GPU is explicit and never falls back silently.

Dataset item count `N` is fixed by input manifest. Chunk index `i` covers `[i*chunk_size, min(N,(i+1)*chunk_size))`; deterministic ID is `chunk-%08d`. A chunk file is immutable and records chunk ID/range, input/model/spec checksum and content checksum. Database uniqueness `(job_id,chunk_id)` recognizes at most one output across all attempts of the same job; duplicate compute may reuse identical checksum or reject conflicting checksum as `INTERNAL`/stale.

Checkpoint cursor is the next unrecognized chunk index. The checkpoint manifest separately carries the exact committed chunk-output manifest artifact ID and checksum. That manifest is published by the current live attempt but may carry forward already recognized chunks from prior attempts of the same tenant/job/logical session; every entry binds its source attempt ID and source job fence. On restore, adapter revalidates every referenced recognized chunk; missing/corrupt referenced output triggers recovery failure/fallback, never silent skipping. Final result carries the same kind of current-attempt chunk-output manifest reference and is recognized only when its sorted non-overlapping ranges cover `[0,N)` without gaps. Template is checkpointable; restart-safe from immutable input/model.

## Manifest contract

All manifest JSON is UTF-8 canonicalized by RFC 8785 for `manifest_checksum`; checksum field itself is omitted while computing that checksum. Every file entry contains an exact committed `artifact_id`, logical name, media type, size and checksum. Logical names are unique within a manifest and never contain absolute/relative filesystem paths.

### Common provenance

Required: tenant/job/logical session/attempt IDs, job fence, input/spec checksum, template ID/version, adapter ID/version and image digest. Publish additionally validates worker incarnation, attempt, allocation, lease and desired state from HTTP Authority; these live authority fields are not duplicated into long-lived manifest provenance.

### Checkpoint

`kind=CHECKPOINT`, `schema_version=1`, UUID, sequence, created timestamp, provenance, compatibility, cursor, explicit state-component list, 1..64 file entries and manifest checksum. Each entry references a committed same-tenant, same-attempt `CHECKPOINT_FILE` artifact; duplicate artifact IDs or logical names are rejected. Training must declare MODEL, OPTIMIZER, RNG and SAMPLER components applicable to the device. CPU must declare ACCUMULATOR. Inference declares INFERENCE_CURSOR and must include `chunk_output_manifest={artifact_id,checksum}` resolving to a committed `CHUNK_OUTPUT_MANIFEST` published by the same current tenant/job/session/attempt; non-inference manifests must omit that field. The chunk manifest's entries may reference recognized outputs from prior attempts only under the carry-forward rules below.

Before constructing a checkpoint manifest, worker calls `workerReserveCheckpoint`. Under the Job lock the server generates `checkpoint_id` and reserves the next monotonic, never-reused sequence for exact Authority; response loss replays the same pair. At most one reservation remains `RESERVED` per Attempt. Worker sends `REQUEST_CHECKPOINT` with the exact reservation and deadline. Runner closes staged bytes and emits `CHECKPOINT_FILES_READY`; worker uploads each descriptor with its descriptor-derived key, validates and persists the exact `201 Artifact`, then returns `CommittedArtifactBinding` through `BIND_ARTIFACT_BATCH`. For batch inference, current-attempt chunk files follow the same rule as `RESULT_FILE`; runner creates the canonical `CHUNK_OUTPUT_MANIFEST` only from committed chunk bindings, and worker uploads/binds it before top-level finalization. After `FINALIZE_CHECKPOINT_MANIFEST`, runner verifies the binding-set checksum, writes canonical checkpoint JSON with the reserved ID/sequence and committed entries, then emits `CHECKPOINT_READY` carrying a closed `FinalManifestArtifact`. Worker verifies its kind/checksum/size and uploads the exact canonical bytes with the final-manifest descriptor-derived key; only the successful or replayed `201 Artifact` supplies `manifest_artifact_id`, which is persisted before `workerPublishCheckpoint`. A lost final-upload response replays the same key/bytes/headers; after same-Attempt adoption, exact Authority lineage and immutable metadata checks return that original Artifact rather than create another. Lost IPC frames reuse sequence/payload hash, and any descriptor/binding mismatch fails the Attempt. Adoption also rebinds the unchanged reservation and reuses durable runner state. Rejected, abandoned or staging reservations never become a `Checkpoint`, are never restorable and cannot be repurposed; gaps are ignored by restore ordering. At least the newest two committed checkpoints remain referenced, and quota/watermark cannot delete an in-use committed checkpoint.

### Result and chunk output

Runner emits `RESULT_PREPARE` with its immutable completion token after closing result files and before creating a manifest. Worker persists the token/message/callback binding, reserves the replayable `result_id`, and sends `PREPARE_RESULT`. Runner emits 1..1024 direct files through ordered `RESULT_FILE_BATCH` frames of at most 64 descriptors; worker uploads each using the descriptor-derived key and persists only the successful/replayed `201 Artifact` before `BIND_ARTIFACT_BATCH`. Lost HTTP/IPC responses reuse the same key or sequence/hash. Batch-inference chunk files follow the same bounded cycle; runner creates `CHUNK_OUTPUT_MANIFEST` only from committed chunk bindings, and worker uploads/binds it before finalization. After `FINALIZE_RESULT_MANIFEST`, runner validates the binding-set checksum, constructs canonical success JSON with exact `result_id` and committed descriptors, then emits `RESULT_READY` carrying a closed `FinalManifestArtifact`. Worker verifies it and uploads the exact canonical bytes with the final-manifest descriptor-derived key; only the successful or replayed `201 Artifact` supplies `result_manifest_artifact_id`, persisted before `workerCompleteAttempt`. Lost final-upload response, including across same-Attempt adoption, replays through the stable key plus exact Authority lineage/metadata checks and cannot create another Artifact. Worker acknowledges the original `RESULT_PREPARE` only after `PREPARE_RESULT` is accepted. Adoption rebinds the unchanged reservation and requires durable completion/control/descriptor state to match. Other revoke permits only a later Attempt to abandon the old reservation and reserve a new ID. Reservation alone is not success or downloadable and changes no terminal counter. The success-only manifest contains immutable files and bounded scalar metrics; every direct file is a same-tenant, same-attempt `RESULT_FILE`. Failure is an Attempt/Event record. Batch inference requires a current-attempt `chunk_output_manifest`; non-inference omits it. `ChunkOutputManifest` binds full provenance, deterministic ranges and exact source attempt/fence/file entries; implementation additionally validates order, coverage and uniqueness rules beyond JSON Schema.

Carry-forward is job-scoped, not arbitrary cross-attempt access. For each chunk entry, API verifies source attempt belongs to the same tenant/job/logical session as the publishing manifest, `source_job_fence` equals that attempt's immutable authorized fence, the exact artifact is a committed `RESULT_FILE` produced by that source attempt, and input/model/spec checksums match the immutable job.

Recognition occurs only inside a fenced checkpoint/result publish transaction under the Job and artifact/reference locks. If `source_attempt_id` and `source_job_fence` equal the current publishing Authority, the transaction insert-or-verifies immutable `RecognizedChunk(job_id,chunk_id,range,artifact_id,checksum,source_attempt_id,source_job_fence)`: absent row is inserted with its reference edge; an existing byte-for-byte identical row is reused for callback replay or concurrent duplicate compute; any different range/artifact/checksum/source returns `409 state_conflict` and commits no checkpoint/result. If the source is a prior attempt, an exact existing `RecognizedChunk` row is mandatory and no new prior-attempt recognition can be invented by the current worker. Thus the first checkpoint can recognize newly produced chunks, while later attempts carry forward only previously fenced facts without copying bytes.

Publishing a checkpoint/result creates reference edges to the exact current-attempt chunk-output manifest and from that manifest to every current/prior-attempt chunk file. Restore and final recognition traverse those edges and recheck artifact kind, tenant, job/session, source attempt/fence, checksum and stored size. GC treats the whole reachable graph as live while any committed checkpoint/result/reference remains; deleting or replacing a manifest artifact ID cannot silently retarget a reference.

## Durable publish sequence

1. Reserve server-generated checkpoint ID/sequence or result ID using the exact live Authority; persist the replayable reservation unchanged.
2. Trusted runner closes each output file, computes its expected checksum/size and emits bounded `StagedArtifact` batches; it does not create the final checkpoint/result manifest yet.
3. For each descriptor, worker creates a bounded `UploadSession` owned by tenant/attempt, checks quota/watermark and streams the exact staged bytes as `application/octet-stream` with declared kind/media type/size/checksum. Commit order is checksum/size → file fsync → atomic same-filesystem rename → directory fsync → Artifact metadata/idempotency response. Interruption leaves expiring staging.
4. Worker treats the successful or idempotently replayed `201 Artifact` as the sole source of committed ID/kind/media type/size/checksum, durably maps it to the descriptor, and returns the exact binding to runner. Response loss reuses the same idempotency key/headers/body; IPC loss reuses the same sequence/payload hash.
5. After all file bindings exist, runner constructs any required chunk-output manifest from committed chunk bindings; worker uploads it with exact kind `CHUNK_OUTPUT_MANIFEST` and returns its committed binding. Runner then constructs the final checkpoint/result manifest from the complete binding set and emits its closed `FinalManifestArtifact`. Worker uploads the exact canonical bytes with the final-manifest descriptor-derived key, persists the successful/replayed Artifact binding, and only then uses that response's ID for publish/complete. Attempt upload cannot declare user input kinds, and public tenant upload cannot declare attempt-output kinds.
6. In a DB transaction, authenticate worker, lock job/attempt/lease then reservation/artifact/reference rows, check current incarnation/attempt/allocation/fence/lease/desired state, validate reserved identity/sequence, and validate the top-level manifest artifact and direct files against the current attempt. For each transitive chunk, insert-or-verify `RecognizedChunk` when its source is the current Authority; for a prior-attempt source require an exact existing row. Check same tenant/job/session, source fence, permitted kind/media type/size/checksum, range and unique logical-name rules, then insert checkpoint/result metadata, complete reference graph, event/callback receipt and commit atomically.
7. Crash before step 6 leaves an uncommitted reservation and possibly orphan bytes. Crash after commit is replayable from callback receipt. Metadata never references partial bytes, and runner never fabricates an artifact ID.

No Docker/filesystem mutation occurs inside the DB transaction; read-only metadata/checksum validation may occur before, with immutable identity rechecked at commit.

## Restore selection and fallback

For a new Attempt in the same Job (or explicit same-tenant manual retry reference):

1. Query committed checkpoints newest sequence first; exclude metadata marked corrupt.
2. Verify referenced blobs exist and checksums match, then provenance input/spec/template/adapter/image and environment compatibility.
3. On checksum/corruption, mark evidence/event and try the next older committed checkpoint.
4. On incompatibility, emit exact reason and try an older checkpoint only if it could differ meaningfully; otherwise stop scanning with reason.
5. If none valid and adapter version says `restart_safe=true`, emit `CHECKPOINT_FALLBACK_TO_INPUT` and start immutable input from step/cursor zero.
6. Otherwise Job fails or remains blocked during relocation with `waiting_for_compatibility`; no unsafe deserialize or silent fallback.

Automatic recovery uses retry budget; resume after PAUSED does not. Restore failure caused by corrupt checkpoint can move to older checkpoint within the same attempt creation decision; it does not consume extra retry until an Attempt actually fails.

## Compatibility rules

| Dimension | Exact / allowed rule |
|---|---|
| Schema | `schema_version=1` supported explicitly; unknown version rejected |
| Tenant/job/session | Exact for automatic recovery/resume; manual retry permits new job/session only through explicit same-tenant CheckpointReference |
| Input/spec | Exact checksums; manual retry keeps immutable spec/input |
| Adapter/template | Exact ID and compatible declared version range; default is exact version |
| Image | Exact digest by default; replacement digest only via future approved migration contract, not B01 |
| Architecture | Exact declared supported architecture; no assumption amd64/arm64 equivalence |
| Framework | Exact compatible major/minor range frozen by template; safe state loader only |
| Device | CPU checkpoint restores to CPU. CUDA restore requires declared CUDA/driver/compute capability and device-state support; no silent CPU fallback |
| Files | All required files, size/checksum and manifest checksum exact |

Relocation runs the same compatibility evaluator after restore and new inventory. Incompatible jobs remain blocked with visible reason; they are not auto-failed merely because the destination temporarily lacks capability, unless an administrator explicitly cancels or policy declares permanent failure.

## Failure classification and retry

| Class | Examples | Automatic same-spec retry |
|---|---|---|
| `INFRASTRUCTURE` | worker/container unexpected death, transient executor/DB/network loss after accepted attempt | Yes, max 2 after initial attempt, after cleanup/quarantine and backoff |
| `TIMEOUT` | startup >30 s or runtime >300 s | No |
| `OOM` | cgroup/GPU OOM | No |
| `INVALID_INPUT` | schema/data/checksum/model validation | No |
| `INCOMPATIBLE` | image/architecture/framework/CUDA/driver mismatch | No automatic attempt; visible blocked/failure decision |
| `USER_CANCEL` | committed cancel | No; terminal CANCELLED |
| `INTERNAL` | adapter invariant, conflicting chunk/result, unsafe state | No blind retry; fail and audit |

The trusted worker reports any classified attempt failure through callback-deduplicated `workerFailAttempt` before cleanup. The callback includes exact live Authority, immutable container identity, typed observations and an allowlisted safe reason code; it never carries raw stderr, input, path, credential or checkpoint content. Commit stores the class/reason, revokes lease, increments fence and quarantines allocation. Separate exact-identity cleanup is still required before release and retry/terminal resolution.

Application-level failure can still be manually retried as a new job if the user deliberately chooses and admission passes; source terminal state remains immutable.
