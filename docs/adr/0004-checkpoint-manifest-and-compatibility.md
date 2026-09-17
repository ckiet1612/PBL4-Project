# ADR-0004: Checkpoint manifest, safe restore and compatibility

- **Date:** 2026-09-17
- **Status:** accepted
- **Decision source:** PLAN §4, §7, §9 and §14; B01 defines schema/version serialization and exact fallback ordering.

## Context

Application recovery needs portable provenance and enough workload state without unsafe arbitrary deserialization. A newest checkpoint may be corrupt, and relocation may change architecture/framework/GPU capability. Compute can repeat but only one final result may be recognized.

## Options considered

1. Versioned JSON manifest plus safe tensor/data files, strict provenance/compatibility and older-checkpoint fallback.
2. Framework-native pickle/object serialization. Flexible but unsafe and tightly coupled to code/imports.
3. Restart every job from input. Simpler but does not meet checkpoint recovery requirements.

## Decision

Use option 1. Canonical JSON Schema `schema_version=1` defines checkpoint/result/chunk manifests. Checkpoint binds tenant/job/session/attempt/fence, input/spec, template/adapter/image, architecture/device/framework/CUDA/driver, cursor/state components and every file checksum/size. PyTorch includes model/optimizer/RNG/sampler state through non-executable safe formats; arbitrary pickle is prohibited.

Every manifest file entry binds the exact committed `artifact_id` together with unique logical name, media type, byte size and checksum. The trusted runner is the sole final manifest creator: it first closes and describes staged files; worker uploads them, persists exact bindings copied from successful/idempotently replayed Artifact responses, and returns bounded binding batches; only then may runner construct canonical checkpoint/result manifests. Runner emits a closed descriptor for the final canonical bytes, and worker uses the same descriptor-derived idempotency/Authority-lineage replay rule before taking the final manifest Artifact ID from the `201` response. Direct checkpoint/result entries must belong to the publishing attempt. Batch-inference chunk entries additionally bind source attempt/fence and may carry forward only the immutable recognized `(job_id,chunk_id)` artifact from a prior attempt in the same tenant/job/logical session. Publish verifies permitted kind, identity, checksum/size and the complete reference graph; logical names are not filesystem paths.

Restore checks committed newest-to-oldest. Corrupt/incompatible candidates emit reasoned events. Only after no valid checkpoint may a template explicitly marked `restart_safe` restart immutable input. At least two committed checkpoints remain referenced. CPU comparison is exact; PyTorch fixture/tolerances are frozen by B16 before measurement and scoped to device/image.

## Consequences

- Image or environment changes can block resume; there is no silent device fallback.
- Manual retry can reference a same-tenant compatible checkpoint but creates a new job/session.
- Chunk inference uses deterministic job-scoped chunk IDs, source-attempt/fence provenance and immutable recognized-chunk uniqueness to tolerate repeated compute and recovery across attempts.
- Missing/corrupt durable storage remains a backup incident, not something retries can guarantee.

## Transition and rollback

B14 implements CPU manifests/restore, B16 freezes training fixtures and completes adapters, B21 validates relocation. Manifest-breaking changes require a new schema version and explicit compatibility/migration path.

## Acceptance

ACC-01, ACC-17–19, ACC-23, ACC-28, ACC-31, ACC-33 and conditional ACC-34. Evidence includes schema/example validation, crash/resume comparisons, corrupt-newest fallback, safe-format review and incompatible relocation.
