# Internal interfaces and worker protocol

Dẫn xuất từ [PLAN](../../PLAN.md) §3–§5, §7, §9–§10. Pseudo-signature là logical contract, không khóa tên module/class Python. Domain/policy không phụ thuộc FastAPI, SQLAlchemy, Docker hay PyTorch; adapter hạ tầng phụ thuộc các contract này.

## Shared logical types

```text
type Instant = DB-backed UTC timestamp for persisted decisions
type MonotonicInstant = process-local deadline clock, never serialized
type Result<T, E> = Ok(T) | Err(E)
type ResourceRequestVector = {cpu_millis: positive int, memory_bytes: positive int, gpu_count: 0|1}
type ResourceCapacityVector = {cpu_millis: nonnegative int, memory_bytes: nonnegative int, gpu_count: nonnegative int}
type AllocationVector = ResourceRequestVector + {gpu_uuids: tuple[str, ...]}
type Digest = "sha256:" + 64 lowercase hex
type Version = positive int64
```

Inputs/outputs are immutable values. Interface implementations must not mutate domain objects supplied by the caller. Expected typed errors are returned, not collapsed into generic exceptions; unexpected failures fail closed at the application boundary.

## `SchedulerPolicy`

```text
SchedulerPolicy.decide(
    snapshot: SchedulingSnapshot,
    now: Instant,
) -> Result[SchedulingDecision, PolicyError]
```

### Input

```text
SchedulingSnapshot {
  policy_version: Version
  allocatable_capacity: ResourceCapacityVector
  held_allocations: tuple[HeldAllocation, ...]       # includes quarantine
  tenant_ledgers: tuple[TenantLedger, ...]
  tenant_limits: tuple[TenantPolicySnapshot, ...]
  candidates_by_tenant: map[TenantId, CandidateWindow]
  active_reservation: ReservationSnapshot | None
  virtual_floor: Decimal
}

CandidateWindow {
  normal: tuple[Candidate, ...]      # <=16, indexed head/aging/retry order
  oldest_eligible: Candidate | None  # extra reservation candidate
  continuation_cursor: bytes | None
}

Candidate {
  job_id, tenant_id, user_id, job_version, ready_sequence,
  resources, base_priority: 0|1|2,
  eligible_wait_seconds, retry_ready_at,
  template_version, required_capabilities,
  tenant_active_attempts, user_active_attempts
}
```

Snapshot is read-consistent enough to make a proposal but **not** authority to allocate. Coordinator rechecks every relevant value in a transaction. `now` is supplied explicitly; tests use virtual time, production uses a DB-time sample tied to snapshot accounting. Policy performs no I/O and does not read a global clock.

### Deterministic algorithm

1. Account each held allocation, including quarantine, to `now`: `d_i=max_r(A_i,r/C_r)` and `V_i += d_i*elapsed/w_i`. Zero-capacity dimensions are excluded. Arithmetic/rounding is deterministic and property-tested against a rational reference.
2. Policy-eligible means queued, retry ready, template/worker compatible, request fits total allocatable capacity and hard resource quota, and tenant/user concurrency permits another attempt. Lack of **currently free** resources does not stop eligible waiting age; permanent infeasibility does.
3. On a tenant transition from no eligible demand to eligible demand, set `V_i=max(V_i, virtual_floor)`. `virtual_floor` is the persisted monotonically nondecreasing minimum score of tenants with eligible demand after accounting; when none exist it retains its prior value.
4. Effective priority is `min(2, base_priority + floor(eligible_wait_seconds/60))`. Within tenant: effective priority descending, then `ready_sequence` ascending, then `job_id` ascending.
5. Tenant key is `(V_i ascending, current_dominant_share_i/weight_i ascending, oldest_ready_sequence ascending, tenant_id ascending)`.
6. A normal dispatch may skip a head that does not fit **currently free** resources and consider another bounded candidate. Skipping never resets eligible age.
7. At `eligible_wait_seconds >=120`, candidate `oldest_eligible` of the tenant selected by the same tenant key may create/retain the sole local reservation. If it does not fit free resources, decision is `DRAIN_FOR_RESERVATION` and no new dispatch is proposed. When it fits, dispatch it and clear reservation.
8. Cancelled, no-longer-eligible, policy-incompatible or capability-incompatible reservation returns `INVALIDATE_RESERVATION(reason)`; reason is persisted by coordinator.
9. Tie-breaking never uses random values, container utilization, wall-clock arrival outside supplied fields, or FIFO across all tenants.

### Output and errors

```text
SchedulingDecision =
  | Dispatch {job_id, expected_job_version, expected_policy_version,
              reservation_id?}
  | CreateReservation {job_id, tenant_id, reason}
  | DrainForReservation {reservation_id, job_id}
  | InvalidateReservation {reservation_id, reason}
  | NoDecision {reason, continuation_cursors}

PolicyError = InvalidSnapshot | AccountingRegression | DuplicateGpuUuid |
              NonPositiveWeight | CandidateBoundExceeded
```

Postcondition: output references only candidates in the snapshot, never exceeds free vector in the snapshot for `Dispatch`, and contains only versions available to pure policy input. Policy does not invent a job fence or concrete GPU UUID. A proposal may lose a race normally; coordinator reloads rather than forcing it.

## Transactional scheduler integration

Coordinator retrieval uses tenant/priority heads plus at most 16 normal candidates per tenant and one oldest eligible candidate. Heap key is rebuildable from DB; batches carry a fair continuation cursor so the same low-ID tenants do not monopolize retrieval. Expected decision cost is `O(TK + T log T)` plus indexed lookups, `K<=16`.

Dispatch transaction lock order is defined in [concurrency/recovery](concurrency-recovery.md). It rechecks live coordinator lease/epoch, job/policy versions, desired/state, counters, quota, capacity, allocation/GPU uniqueness and reservation. Under those locks, coordinator increments the persisted job fence and, for a GPU request, chooses the lexicographically lowest healthy compatible free GPU UUID from the current WorkerInventory after excluding every unreleased Allocation UUID. It then creates attempt/allocation/lease and appends the event atomically. Docker is invoked only after commit through worker protocol.

Deterministic examples:

- CPU candidate `J1` selected by policy produces `Dispatch{J1,job_version=7,policy_version=4}`. Coordinator rechecks and assigns no GPU UUID, then changes fence `2→3` in the same dispatch commit.
- CUDA candidate `J2` selected by policy produces no device value. With compatible free UUIDs `GPU-b` and `GPU-a`, coordinator selects `GPU-a`; a concurrent allocation of `GPU-a` makes this transaction lose/reload rather than select an already-held device.

## `ResourceProvider`

```text
ResourceProvider.discover() -> Result[HostInventory, DiscoveryError]
ResourceProvider.allocatable(
    inventory: HostInventory,
    reserve: ReservePolicy,
) -> Result[CapabilitySnapshot, CapacityError]
ResourceProvider.compatible(
    requirement: WorkloadRequirement,
    capability: CapabilitySnapshot,
) -> CompatibilityResult
```

Preconditions: runs on local worker; reads host/runtime only; no queue-derived capacity. Discovery records architecture, logical CPU, RAM bytes, Docker/OCI/cgroups/kernel/seccomp, exact adapter versions, locally verified image digests, framework/device/CUDA-runtime support, GPU UUID/model/memory/compute capability/driver/API version and timestamp. Capability arrays are closed declarations: no matching entry means unsupported.

Postconditions: CPU reserve is at least `max(1000 millicores,20% host)`; RAM reserve at least `max(2 GiB,20% host)`; configured reserve may increase but never decrease these floors. Allocatable values never negative/exceed host. GPU devices are whole and keyed by actual UUID. `compatible` returns structured reason (`ARCHITECTURE`, `IMAGE`, `ADAPTER`, `FRAMEWORK`, `CUDA`, `DRIVER`, `GPU_COUNT`, `RESOURCE`) and never silently falls back CUDA to CPU.

Valid closed inventory shapes (timestamps/checksums illustrative):

```json
{"architecture":"linux/amd64","host_cpu_millis":8000,"host_memory_bytes":17179869184,"allocatable":{"cpu_millis":6400,"memory_bytes":13743895347,"gpu_count":0},"runtime":{"docker_version":"27.3.1","oci_runtime":"runc","oci_runtime_version":"1.2.3","cgroups_version":2,"kernel_release":"6.8.0","seccomp_available":true},"adapters":[{"adapter_id":"cpu.iterative","adapter_version":"1.0.0"}],"images":[{"image_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","architecture":"linux/amd64","verified":true}],"frameworks":[{"framework":"NEXA_CPU","framework_version":"1.0.0","device":"CPU","cuda_runtime_version":null}],"gpu_devices":[],"discovered_at":"2026-09-17T00:00:00.000Z"}
```

```json
{"architecture":"linux/amd64","host_cpu_millis":16000,"host_memory_bytes":34359738368,"allocatable":{"cpu_millis":12800,"memory_bytes":27487790694,"gpu_count":1},"runtime":{"docker_version":"27.3.1","oci_runtime":"runc","oci_runtime_version":"1.2.3","cgroups_version":2,"kernel_release":"6.8.0","seccomp_available":true},"adapters":[{"adapter_id":"pytorch.cifar10","adapter_version":"1.0.0"}],"images":[{"image_digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","architecture":"linux/amd64","verified":true}],"frameworks":[{"framework":"PYTORCH","framework_version":"2.5.1","device":"CUDA","cuda_runtime_version":"12.4.0"}],"gpu_devices":[{"uuid":"GPU-00000000-0000-0000-0000-000000000001","model":"NVIDIA-example","memory_bytes":17179869184,"compute_capability":"8.6","driver_version":"550.54.15","cuda_driver_api_version":"12.4.0","healthy":true}],"discovered_at":"2026-09-17T00:00:00.000Z"}
```

Both examples are declarations, not hardware evidence. Removing any required adapter/image/framework/runtime field is schema-invalid; a valid payload with no matching entry is capability-incompatible and blocks dispatch/restore.

Errors: unsupported OS/cgroups, invalid reserve, Docker unavailable, duplicate/unstable GPU UUID, runtime probe failure. Any error prevents READY/dispatch; it does not invent zero-capacity READY.

## `Executor`

```text
Executor.prepare(request: StartExecution) -> Result[PreparedExecution, ExecutorError]
Executor.start(prepared: PreparedExecution) -> Result[ContainerIdentity, ExecutorError]
Executor.signal_checkpoint(identity, reason, deadline) -> Result[SignalAck, ExecutorError]
Executor.stop(identity, grace_seconds: int) -> Result[StopObservation, ExecutorError]
Executor.inspect(identity) -> Result[ContainerObservation, ExecutorError]
Executor.cleanup(identity) -> Result[CleanupProof, ExecutorError]
```

`StartExecution` contains an authorized immutable execution context, allocation, exact image digest, read-only artifact mounts expressed as internal artifact IDs, startup nonce, scratch/log limits, trusted-runner deadline channel, and no raw client path/command/env. Executor serializes `prepare/start/stop/cleanup` through a local per-attempt lock and persists a monotonic executor operation sequence. `prepare/start` are idempotent for exact `(attempt_id, allocation_id, startup_nonce)`; stop/cleanup additionally bind exact container identity when one exists.

Postconditions: non-root UID, read-only rootfs/input, network disabled, capabilities dropped, no-new-privileges, seccomp, CPU/RAM/PID/log/scratch/runtime bounds, restart policy `no`, no host namespace/socket/worker credential. Before Docker create, executor records `CREATE_IN_FLIGHT` under the attempt lock. A stop/failure before create atomically advances the operation sequence and writes a tombstone before emitting `NoContainerProof`; delayed prepare/start with an older or equal sequence is rejected locally. After create, identity is bound before releasing the lock. `start` cannot recognize result or alter DB allocation. `cleanup` emits either `NoContainerProof` (no managed container and no create in flight for the startup nonce) or `ContainerStoppedProof` (exact identity stopped/absent); only API transaction releases allocation.

Errors classify `IMAGE_UNAVAILABLE`, `IDENTITY_MISMATCH`, `START_TIMEOUT`, `RUNTIME_ERROR`, `OOM`, `STOP_TIMEOUT`, `INSPECTION_UNAVAILABLE`, `RESOURCE_LIMIT`, `INTERNAL`. Identity mismatch never targets a different container.

## `WorkloadAdapter`

```text
WorkloadAdapter.describe() -> AdapterDescriptor
WorkloadAdapter.validate(spec, inputs, capability) -> Result[ValidatedWorkload, ValidationError]
WorkloadAdapter.restore_plan(validated, checkpoints, capability) -> RestorePlan
WorkloadAdapter.launch_spec(validated, restore_plan) -> RunnerLaunchSpec
WorkloadAdapter.parse_progress(message) -> Result[Progress, ProtocolError]
WorkloadAdapter.validate_checkpoint(manifest, files, validated, capability)
    -> Result[CheckpointState, CompatibilityError]
WorkloadAdapter.validate_result(manifest, files, validated)
    -> Result[ResultState, ValidationError]
```

Adapter owns only one allowlisted template/version family and declares parameter/resource bounds, exact image digest, architecture/device/framework range, `checkpointable`, `restart_safe`, progress fields and failure classification. It may import PyTorch inside workload adapter/image; scheduler/domain may not.

Restore order: newest committed checkpoint first, verify all provenance/checksum/schema/adapter/image/environment fields, then older committed checkpoint, then restart input only when descriptor says restart-safe. Every reject/fallback yields a typed event. Safe tensor format + JSON only; arbitrary pickle deserialization is prohibited.

## `ArtifactStore`

```text
ArtifactStore.begin_staging(owner, expected_size, expected_checksum, media_type)
    -> Result[StagingHandle, ArtifactError]
ArtifactStore.append(handle, bytes) -> Result[Progress, ArtifactError]
ArtifactStore.commit_blob(handle) -> Result[DurableBlob, ArtifactError]
ArtifactStore.open(blob_key, byte_range?) -> Result[BoundedReader, ArtifactError]
ArtifactStore.inspect(blob_key) -> Result[BlobStat, ArtifactError]
ArtifactStore.delete_unreferenced(blob_key, gc_token) -> Result[DeleteResult, ArtifactError]
```

`commit_blob` ordering is checksum/size verify → file fsync → atomic rename on same filesystem → directory fsync. It returns an opaque internal `blob_key`, never a client path. Application creates metadata in a later DB transaction; crash before that produces an orphan. Store does not decide tenant authorization, fencing or reference reachability. GC caller supplies a DB-issued token proving no committed reference/active upload/restore; implementation rechecks token freshness before delete.

Errors include byte limit, checksum mismatch, disk watermark/full, interrupted stream, symlink/path escape, fsync/rename failure and missing blob. Partial/staging content is never returned as committed.

## Worker HTTP protocol

OpenAPI owns wire schemas and operations. This section owns ordering, loss, authority and acknowledgment semantics.

| Message | Required identity | Precondition and commit effect | Lost/late/repeat behavior |
|---|---|---|---|
| bootstrap | installation ID + bootstrap secret | Maintenance-network, configured local worker only; issue hashed opaque credential | Raw credential is one-time and not persisted. Same key/hash after commit returns `one_time_secret_unavailable` + locator; a new key rotates/revokes the unknown current credential; no arbitrary enrollment |
| create incarnation | worker credential + worker ID + idempotency key + process-start nonce | Under Worker lock generate server incarnation ID, increment sequence, end prior incarnation, set new current incarnation STARTING | Same key/hash returns the same ID/sequence. A newer commit makes every old-incarnation callback stale; no client-selected incarnation ID |
| heartbeat | worker + incarnation + callback | Incarnation current; inventory valid; reconcile flag controls READY; commit heartbeat DB time | Repeat same callback returns ack; old incarnation rejected; missing heartbeat changes health but not release |
| poll | worker + incarnation | READY+ENABLED; return ≤1 committed dispatch offer or no-work | Response loss: poll again; offer alone never authorizes execution |
| claim | full Authority + callback | Offer/lease live, incarnation current, desired RUNNING or explicit `CHECKPOINT_FOR_PAUSE`; Attempt CLAIMED; snapshot immutable `ExecutionContext` | Same callback returns the same acknowledgment/context; response loss safe; late/stale rejected |
| execution artifact read | full live Authority + attempt/artifact | Artifact is in exact ClaimResponse input/model/dataset/restore graph | Each range request rechecks current authority and graph; arbitrary same-tenant artifact is 404/denied; no tenant credential or DB read |
| start | Authority + startup nonce + executor sequence + exact container identity + callback | Non-tombstoned startup record, no other identity, within startup limit; commit Attempt/Job RUNNING and reset DB expiry to commit DB time +45 s; response stores exact expiry/duration/renew/margin | Worker binds first send-monotonic to callback before the first send. It applies the candidate runner deadline only after valid ack arrives before it. Same-callback retry reuses the first instant/original ack; late response never rebases or starts compute |
| adopt | prior live Authority + current incarnation + exact container + callback | During STARTING reconciliation, lock Job→Attempt/Lease→reservations; atomically append the prior→current Authority grant lineage, move Attempt/Lease and every still-active reservation for that Attempt to the current incarnation, extend from DB time, and return the exact transferred checkpoint/result reservation or null | Reservation IDs, upload keys/responses, original reservation callback IDs and durable runner sequences/payloads do not change. Same adoption callback replays the full transfer snapshot. A descriptor-key retry under current Authority may replay a completed upload from a lineage predecessor only when attempt/allocation/lease/fence and immutable upload metadata match exactly. Apply a later runner deadline only after valid ack before candidate; failure/expiry/server-local binding mismatch/late response means fail or stop+cleanup before READY |
| renew | Authority + progress union + callback | DB-time lease live, fence/incarnation/desired state valid; extend expiry. Before first runner progress accept only `(sequence=0,snapshot=null)` and leave progress absent; afterward accept sequence ≥1 with complete snapshot, same sequence only for byte-equivalent snapshot, greater sequence for new progress | Record first send-monotonic per callback, but do not update runner before acknowledgment. Only a valid ack received before `first_send + duration - margin` may apply that candidate; failure, delayed response and replay never extend/rebase unconfirmed authority |
| reserve checkpoint | Authority + callback | Under Job lock reserve server checkpoint ID and next sequence, without creating a committed Checkpoint; at most one RESERVED row/Attempt | Same callback returns same reservation. Successful same-Attempt adoption rebinds and returns it; deterministic publish rejection marks REJECTED, while failure/control/reaper revoke marks ABANDONED. It is never restorable/committed and ID/sequence cannot be repurposed |
| checkpoint | Authority + reserved ID/sequence + manifest artifact + manifest + callback | Runner-created final manifest contains exact committed bindings obtained from attempt-upload responses; durable blob and manifest valid; reservation/publish fence/lease/desired checks; commit metadata/event | Repeat returns same checkpoint; late/stale is rejected, reservation remains uncommitted and blob may become GC orphan |
| reserve result | Authority + callback | Under Job lock reserve the active server result ID for this Authority; no Result recognized yet | Same callback returns same reservation; distinct reservation under the same live Authority conflicts. Same-Attempt adoption rebinds and returns the reservation without changing identity; other revoke permits only a later Attempt to supersede the abandoned ID; no abandoned ID is ever a Result |
| complete | Authority + reserved result manifest + callback | Runner-created final manifest contains exact committed bindings obtained from attempt-upload responses; exact live authority, desired RUNNING, no final result; commit unique Result/terminal event | Cancel committed first rejects; repeat same callback returns accepted result; distinct duplicate conflicts/stale |
| fail | Authority + typed observation + callback | Exact live authority; observation may contain container identity or `NoContainerProof`; commit failure/fence/quarantine | Replay returns original ack. Pre-create failure writes executor tombstone before proof, so delayed start is rejected locally and by stale API authority |
| cleanup | cleanup identity + proof union + callback | May target revoked attempt; verify `ContainerStoppedProof` or `NoContainerProof`. For `UNCLAIMED`, exact startup nonce comes from reconciliation and server also verifies no claim/start/container receipt; release only after proof | Remains callable after renew/publish revoke. `202` means quarantine retained; repeat after release returns the original release ack without changing counters again |

`Authority` is `(worker_id, worker_incarnation_id, attempt_id, allocation_id, lease_id, job_fence)`. `dispatch_coordinator_epoch` is recorded for audit/dispatch recheck but not required to keep a healthy attempt alive after leader turnover. A trusted-runner `FAILED` message is normalized to the typed `FailureClass`, allowlisted safe reason code and typed observation/proof, then sent through `workerFailAttempt`; worker never substitutes cleanup for this failure linearization.

### Complete request chains

- CPU submit→result: submit references committed `INPUT` → dispatch/claim returns logical session, immutable CPU template, allocation and input descriptor → worker downloads only that input through `workerDownloadExecutionArtifact` → runner reports progress → worker reserves result ID → runner stages result bytes → worker uploads them and returns committed Artifact bindings → runner constructs the final manifest with those bindings → worker uploads that manifest and completes with the exact reservation.
- GPU assignment: policy selects job only → coordinator locks inventory/allocation rows, chooses deterministic free compatible GPU UUID and increments job fence → claim returns that UUID inside Allocation plus exact image/adapter/framework requirements → executor binds the whole GPU; no scheduler-side device lookup exists.
- Checkpoint restore: recovery chooses committed compatible checkpoint → claim returns checkpoint record, canonical manifest and referenced file descriptors → worker fetches only those graph artifacts using live Authority → adapter verifies provenance/capability/checksums before launch; no tenant route, user credential or direct DB access is used.

## Trusted runner channel

Worker starts a trusted runner as image entrypoint; workload is a less-trusted child. Runner receives validated launch spec and an acknowledged control channel that workload cannot write. `SET_AUTHORITY_DEADLINE` is sent only after a valid start/adopt/renew API acknowledgment received before the callback's candidate deadline; its absolute monotonic deadline is still computed from that callback's first send instant. Runner kills/stops workload when the last accepted deadline passes even if worker agent dies or blocks.

Every frame is length-prefixed UTF-8 JSON on a private local IPC endpoint, maximum 64 KiB. Unknown field/type/schema version is invalid. Logical primitives are closed as follows:

```text
Sequence = int64 1..9223372036854775807
MonotonicNs = int64 0..9223372036854775807
  # nanoseconds on the runner/worker shared host monotonic clock; never persisted as DB/API time
UuidV7 = lowercase canonical UUIDv7 string
SafeCode = ASCII /^[A-Z][A-Z0-9_]{0,63}$/
StagingName = ASCII /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$/ with no slash/path semantics
CompletionToken = UuidV7 generated by runner once per completed workload execution
LogicalName = ASCII /^[a-z][a-z0-9_.-]{0,127}$/ with no path semantics
Checksum = ASCII /^sha256:[0-9a-f]{64}$/
ArtifactKind = CHECKPOINT_FILE | RESULT_FILE | CHUNK_OUTPUT_MANIFEST | CHECKPOINT_MANIFEST | RESULT_MANIFEST
StagedArtifact = closed {staging_name:StagingName, logical_name:LogicalName, kind:ArtifactKind,
  media_type:string 1..127, size_bytes:int64 0..artifact_max_file_bytes, checksum:Checksum}
FinalManifestArtifact = closed {staging_name:StagingName, logical_name:LogicalName,
  kind:CHECKPOINT_MANIFEST|RESULT_MANIFEST, media_type:const application/json,
  size_bytes:int64 1..artifact_max_file_bytes, checksum:Checksum}
DescriptorChecksum = Checksum over RFC 8785 canonical JSON of the complete closed descriptor
  (`StagedArtifact` or `FinalManifestArtifact`); it is not the checksum of staged bytes
CommittedArtifactBinding = closed {staging_name:StagingName, logical_name:LogicalName,
  artifact_id:UuidV7, kind:ArtifactKind, media_type:string 1..127,
  size_bytes:int64 0..artifact_max_file_bytes, checksum:Checksum}
```

The trusted runner is the sole final manifest creator. It can compute expected checksum/size for staged bytes, but it cannot invent an `artifact_id`: every `CommittedArtifactBinding` must be copied from the successful or idempotently replayed `workerUploadAttemptArtifact` response. Worker rejects a response whose kind/media type/size/checksum differs from the staged descriptor, persists the descriptor→Artifact mapping before acknowledging it to runner, and never derives a committed identity from a staging name.

For each staged descriptor, the HTTP idempotency key is the 64-character lowercase hexadecimal SHA-256 of RFC 8785 canonical JSON `{version:1,attempt_id,purpose,reservation_callback_id,source_message_sequence,staging_name,descriptor_checksum}`. It is therefore stable across response loss and same-Attempt incarnation adoption. `binding_set_checksum` is `sha256:` plus SHA-256 over RFC 8785 canonical JSON of all accepted `CommittedArtifactBinding` objects ordered by source message sequence and then descriptor order; reservation/completion identity is already carried beside it in the finalize control.

Runner-to-worker envelope is the closed object `{schema_version:1, message_sequence:Sequence, type, payload}` and `payload` is exactly one branch:

| Type | Closed payload |
|---|---|
| `STARTED` | `{startup_nonce:UuidV7, pid:int 1..4194304, started_monotonic_ns:MonotonicNs}` |
| `PROGRESS` | `{progress_sequence:Sequence, fraction:number 0..1, step:int64 0..max|null, epoch:int64 0..max|null, item_cursor:int64 0..max|null}`; progress sequence starts at 1 per Attempt and increases only for a new snapshot |
| `CHECKPOINT_FILES_READY` | `{reservation_callback_id:UuidV7, checkpoint_id:UuidV7, checkpoint_sequence:int64 1..max, batch_index:int 0..0, batch_count:const 1, artifacts:array[1..64] of StagedArtifact}`; each artifact kind is `CHECKPOINT_FILE`, except current-attempt batch-inference chunk bytes use `RESULT_FILE`; emitted after bytes are closed for mutation and before a checkpoint manifest exists |
| `CHECKPOINT_READY` | `{reservation_callback_id:UuidV7, checkpoint_id:UuidV7, checkpoint_sequence:int64 1..max, manifest:FinalManifestArtifact}`; manifest kind is `CHECKPOINT_MANIFEST`, and its checksum covers complete canonical bytes including the embedded `manifest_checksum` field |
| `RESULT_PREPARE` | `{completion_token:CompletionToken}`; files are closed for mutation, but no result manifest exists yet |
| `RESULT_FILE_BATCH` | `{completion_token:CompletionToken, reservation_callback_id:UuidV7, result_id:UuidV7, batch_index:int 0..15, batch_count:int 1..16, artifacts:array[1..64] of StagedArtifact}`; batches cover 1..1024 direct `RESULT_FILE` entries exactly once and are emitted in increasing batch order after `PREPARE_RESULT` |
| `CHUNK_FILE_BATCH` | `{purpose:CHECKPOINT|RESULT, reservation_callback_id:UuidV7, reserved_id:UuidV7, batch_index:int 0..1562, batch_count:int 1..1563, artifacts:array[1..64] of StagedArtifact}`; optional batch-inference current-attempt chunk files are `RESULT_FILE` and cover at most the schema's 100000 chunks |
| `AUXILIARY_MANIFEST_READY` | `{purpose:CHECKPOINT|RESULT, reservation_callback_id:UuidV7, reserved_id:UuidV7, artifact:StagedArtifact}`; only `kind=CHUNK_OUTPUT_MANIFEST`, emitted after all referenced chunk bindings are accepted |
| `RESULT_READY` | `{completion_token:CompletionToken, reservation_callback_id:UuidV7, result_id:UuidV7, manifest:FinalManifestArtifact}`; manifest kind is `RESULT_MANIFEST`, and its checksum covers complete canonical bytes including the embedded `manifest_checksum` field |
| `FAILED` | `{failure_class:INFRASTRUCTURE|TIMEOUT|OOM|INVALID_INPUT|INCOMPATIBLE|INTERNAL, reason_code:SafeCode, exit_code:int -1..255|null, oom_killed:boolean, runtime_limit_reached:boolean}`; no raw stderr/path/input |
| `STOPPED` | `{reason:PAUSE|CANCEL|LEASE_DEADLINE|FAILURE|RUNTIME_LIMIT|SHUTDOWN, exit_code:int -1..255, stopped_monotonic_ns:MonotonicNs}` |

Worker-to-runner control envelope is the closed object `{schema_version:1, control_sequence:Sequence, type, payload}` and `payload` is exactly one branch:

| Type | Closed payload and ordering |
|---|---|
| `SET_AUTHORITY_DEADLINE` | `{source_callback_id:UuidV7, deadline_monotonic_ns:MonotonicNs}`; worker sends only after timely API ack; runner accepts only a nondecreasing deadline for the current Authority |
| `REQUEST_CHECKPOINT` | `{reason:INTERVAL|PAUSE|RUNTIME_END, reservation_callback_id:UuidV7, checkpoint_id:UuidV7, checkpoint_sequence:int64 1..max, checkpoint_deadline_monotonic_ns:MonotonicNs}`; worker must obtain `workerReserveCheckpoint` first, and runner copies the exact reservation into manifest/`CHECKPOINT_READY` |
| `PREPARE_RESULT` | `{completion_token:CompletionToken, reservation_callback_id:UuidV7, result_id:UuidV7}`; worker sends only after replayable `workerReserveResult`; runner may emit immutable file batches but cannot yet construct the final manifest |
| `BIND_ARTIFACT_BATCH` | `{purpose:CHECKPOINT|RESULT|CHUNK_OUTPUT, reservation_callback_id:UuidV7, reserved_id:UuidV7, source_message_sequence:Sequence, bindings:array[1..64] of CommittedArtifactBinding}`; worker sends one exact binding for every staged descriptor in the referenced runner batch, in the same order, only after durable local mapping |
| `FINALIZE_CHECKPOINT_MANIFEST` | `{reservation_callback_id:UuidV7, checkpoint_id:UuidV7, checkpoint_sequence:int64 1..max, binding_set_checksum:Checksum}`; accepted only after every checkpoint/chunk descriptor has one matching committed binding and any auxiliary chunk manifest is itself bound |
| `FINALIZE_RESULT_MANIFEST` | `{completion_token:CompletionToken, reservation_callback_id:UuidV7, result_id:UuidV7, binding_set_checksum:Checksum}`; accepted only after all declared result/chunk descriptors and any auxiliary chunk manifest have exact committed bindings |
| `REQUEST_STOP` | `{reason:PAUSE|CANCEL|LEASE_DEADLINE|FAILURE|RUNTIME_LIMIT|SHUTDOWN, grace_deadline_monotonic_ns:MonotonicNs}`; runner rejects a deadline earlier than receipt time only by immediately stopping |

Both directions use the same closed acknowledgment `{schema_version:1, ack_sequence:Sequence, accepted:boolean, code:ACCEPTED|DUPLICATE|OUT_OF_ORDER|INVALID|STALE_AUTHORITY}`; `ack_sequence` equals the received message/control sequence. `accepted=true` iff code is `ACCEPTED` or `DUPLICATE`. Receiver stores sequence plus payload hash for the attempt: same sequence/hash returns `DUPLICATE` without a second effect, same sequence/different hash is `INVALID` and fails the attempt, lower unseen sequence is `OUT_OF_ORDER`, and stale Authority is `STALE_AUTHORITY`. Senders retry an unacknowledged frame with the same sequence and byte-equivalent payload; they never allocate a new reservation because an IPC ack was lost.

Checkpoint handshake is reserve API → `REQUEST_CHECKPOINT` accepted → runner stages immutable bytes and emits `CHECKPOINT_FILES_READY` (plus `CHUNK_FILE_BATCH` when needed) → worker uploads each descriptor using `workerUploadAttemptArtifact` and a deterministic idempotency key → the returned `Artifact` becomes the sole source of its `CommittedArtifactBinding` → worker persists and sends `BIND_ARTIFACT_BATCH`. For batch inference, runner uses accepted chunk bindings to create the canonical chunk-output manifest, emits `AUXILIARY_MANIFEST_READY`, and receives its committed binding the same way. Only after all bindings exist does worker send `FINALIZE_CHECKPOINT_MANIFEST`; runner validates the binding-set checksum, constructs the canonical checkpoint manifest with those exact IDs/checksums, and emits `CHECKPOINT_READY` with a closed `FinalManifestArtifact`. Worker validates the reserved identity/kind, computes its `DescriptorChecksum`, uploads the exact canonical bytes as `CHECKPOINT_MANIFEST` with the descriptor-derived stable key, and durably stores the returned binding before fenced publish.

Result handshake is runner `RESULT_PREPARE` → worker persists `(attempt_id,completion_token,message_sequence,reservation_callback_id)` before calling `workerReserveResult` → worker replays that callback after response loss → `PREPARE_RESULT` accepted → runner emits ordered `RESULT_FILE_BATCH` frames (and optional chunk frames) → worker uploads/binds them exactly as above → optional chunk-output manifest is created/uploaded/bound → `FINALIZE_RESULT_MANIFEST` accepted → runner constructs the canonical result manifest and emits `RESULT_READY` with a closed `FinalManifestArtifact`. Worker validates completion/reservation/kind, computes its `DescriptorChecksum`, uploads the exact canonical bytes as `RESULT_MANIFEST` with the descriptor-derived stable key, and durably stores the returned binding before completion. Worker acknowledges the original `RESULT_PREPARE` only after `PREPARE_RESULT` is accepted; repeated `RESULT_PREPARE` before that point reuses the persisted callback/reservation. Ordinary revoke abandons the mapping and only a later Attempt receives a new result ID. Successful same-Attempt incarnation adoption rebinds server reservations, appends the exact Authority lineage and, under the local attempt lock, reuses the durable IPC sequences, payload hashes, staged/final-descriptor keys/mappings and committed bindings unchanged. If any file, auxiliary manifest or final manifest upload committed but its response was lost before local persistence, the adopted worker retries the stable descriptor key under current Authority; server authorizes replay only through that lineage with matching attempt/allocation/lease/fence and immutable metadata, then returns the original Artifact response. A mismatch between `AdoptResponse` and the local runner record forbids publish and forces fail/stop/cleanup.

Upload response loss never allocates a second artifact: retry streams the same closed staged or final-manifest bytes with the same descriptor-derived idempotency key and identical Authority/kind/media type/size/checksum headers, so `workerUploadAttemptArtifact` replays the original `201 Artifact`; worker then persists the same binding. The final manifest Artifact ID used by publish/complete comes only from that response, never from the staging name. IPC response loss retries the same sequence and byte-equivalent payload. A duplicate binding/finalize frame with the same sequence/hash returns `DUPLICATE`; changed bindings, missing descriptors, duplicate artifact/logical names or a different binding-set checksum are `INVALID` and fail the Attempt. At most 64 descriptors/bindings appear in one frame, so direct result output uses at most 16 batches despite the 64 KiB frame bound.

Before any `PROGRESS`, renew uses `progress_sequence=0` with `progress=null`; server extends only the lease and keeps REST progress `available=false`. Trusted runner is the sole assigner of `progress_sequence`, which is monotonic per Attempt and starts at one; worker validates and forwards it unchanged with the complete latest snapshot. Envelope `message_sequence` orders/deduplicates every runner frame, whereas `progress_sequence` orders only new progress snapshots and is independent of the envelope counter. Same progress sequence is valid only with a byte-equivalent snapshot and does not rewrite progress, while a greater sequence advances it. A recovery Attempt begins its own progress sequence at one and may display a lower fraction/step only with the new attempt ID plus restore-checkpoint provenance; it never rewrites the prior Attempt's progress.

Workload output is treated as data and validated by adapter; it cannot emit control-plane credentials or directly call API. Invalid/flooding messages fail the attempt with bounded audit logging.

## Reconciliation

On worker start/incarnation change:

1. Acquire local singleton, create one process-start nonce, call `workerCreateIncarnation`, and remain STARTING. Bootstrap only provides credential/worker ID; the server-generated incarnation response supplies the current incarnation ID/sequence.
2. Enumerate managed containers by immutable Nexa labels and compute runtime identity digests.
3. Fetch server-known allocations/attempt identities through the worker-scoped reconciliation API; every item includes the dispatch-committed startup nonce and `claim_state=UNCLAIMED|CLAIMED|STARTED`, so cleanup never depends on having received a ClaimResponse. Worker never queries DB.
4. Stop/quarantine unknown, revoked, stale-fence or mismatched containers. For an exact server-known live container from the prior incarnation, call `workerAdoptAttempt`; ordinary renew with the new incarnation is not valid before adoption.
5. Bind the adoption callback's first send-monotonic. Under the server transaction, transfer Attempt/Lease plus every active checkpoint/result reservation and return the exact reservation snapshot. Under the local attempt lock, compare it with durable runner bindings and reuse the same IPC sequences/payload hashes. Only an exact adoption acknowledgment received before the candidate deadline permits continuation and may update the runner deadline. Missing/mismatched binding, expired lease, response after candidate deadline or uncertain identity means fail or stop and cleanup; same-callback replay returns the original transfer and cannot rebase.
6. For a revoked `UNCLAIMED` row with no local record, initialize the exact attempt/startup nonce directly as a durable `TOMBSTONED` executor record at operation/tombstone sequence 1, inspect exact Nexa labels while holding the attempt lock, and submit `NoContainerProof`; no prepare/create call is made. Other rows with no container use the existing executor record/tombstone, and created containers use `ContainerStoppedProof`. Server corroborates claim state plus claim/start/container receipts and releases only after proof verification.
7. Publish closed capability inventory and `reconcile_complete=true`; server may mark this same incarnation READY only after every page/identity is resolved and DB/storage/capability checks pass.

The fetch in step 3 is `GET /workers/{worker_id}/reconciliation`, a signed-cursor page bound to worker identity/incarnation, maximum 100 rows/page. It returns page `server_time` and every unreleased Allocation known to the server for that worker with current/revoked Authority, `lease_expires_at`, desired job state, allocation state, dispatch-committed `startup_nonce`, derived `claim_state`, and expected container identity when known. Worker drains all pages and performs required adopt/stop/cleanup before asserting `reconcile_complete=true`; a cursor/snapshot conflict restarts the bounded scan.

Host unresponsive leaves allocations quarantined. Cleanup reporting authority is deliberately narrower and longer-lived than renew/publish authority, preventing revoked attempts from publishing while still allowing capacity to converge safely.
