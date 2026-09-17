# B01 contract review evidence

- **Task:** B01 — lock contract/ADR, failure scope and environment inventory
- **Review time:** 2026-09-17, Asia/Ho_Chi_Minh
- **Base revision:** `ac8a0e5` (`main`, `origin/main` at task start); evidence covers the uncommitted B01 working tree
- **Contract version:** `1.0.0-b01`
- **Environment class:** D (macOS development/documentation); no Linux/GPU/runtime claim
- **Applicable gate:** ACC-01
- **Result:** R-03, R-05 and R-09 closed by remediation, focused independent rereview and fresh verification; ACC-01 `pass`, B02 no longer contract-blocked

## Scope reviewed

The review covers [contract index](../contracts.md), [OpenAPI](../contracts/openapi.yaml), domain/state/internal/workload/recovery contracts, canonical workload-manifest schema/examples, ADR-0001..0004, [traceability](../requirements-traceability.md), [inventory](../environment-inventory.md), invariants and acceptance mapping.

No product source, test suite, migration, dependency manifest/lockfile, CI, Compose or runtime configuration was created. PLAN, project skills and Codex configuration were not changed. B02 or later implementation was not performed.

## Validator evidence

Commands ran from repository root. `npx --yes` used isolated npm cache resolution; it did not add a package manifest, lockfile, `node_modules` or product dependency.

| Check | Tool/command | Result |
|---|---|---|
| OpenAPI tool version | `npx --yes @redocly/cli --version` | Redocly `2.53.2` |
| OpenAPI semantic lint | `npx --yes @redocly/cli@2.53.2 lint docs/contracts/openapi.yaml` | exit 0, valid, zero warning |
| External/local `$ref` resolution | `npx --yes @redocly/cli@2.53.2 bundle docs/contracts/openapi.yaml --output /tmp/nexa-openapi-bundle.yaml` | exit 0; bundle created outside repository |
| JSON Schema strict compile | `npx --yes ajv-cli@5.0.0 compile -s docs/contracts/schemas/workload-manifests.schema.json --spec=draft2020 --strict=true` | exit 0, schema valid |
| Checkpoint example | AJV validate strict against the canonical schema | exit 0, valid |
| Result example | AJV validate strict against the canonical schema | exit 0, valid |
| Chunk example | AJV validate strict against the canonical schema | exit 0, valid |
| CPU WorkerInventory example | Extract exact Markdown payload and AJV validate strict against the OpenAPI schema closure | exit 0, valid |
| CUDA WorkerInventory example | Extract exact Markdown payload and AJV validate strict against the OpenAPI schema closure | exit 0, valid |
| Missing-capability negative | Remove required `frameworks` from the exact CPU payload and AJV validate strict | exit 1 as required: missing required property `frameworks` |
| Structural/semantic cross-check | `ruby /tmp/nexa_contract_checks.rb` | exit 0: 62 paths, 72 operations/unique IDs, 140 schemas, 5 security schemes, all 72 required surfaces, R-01–R-11 guards, INV-01..21 and ACC-01..39 found, zero errors |
| Markdown local links | `ruby /tmp/nexa_link_checks.rb` | exit 0: 20 Markdown files, 72 local links, zero missing target |
| Diff whitespace | `git diff --check` plus `git diff --no-index --check /dev/null <untracked-file>` for every untracked path | exit 0 for tracked and all 17 untracked B01 files |

An initial Redocly run correctly failed because unquoted comma-containing descriptions in YAML flow maps became unexpected properties and JobSpec `allOf` placed `additionalProperties: false` on the derived branch. The fixes quote flow descriptions and use JSON Schema 2020 `unevaluatedProperties: false` on composed request schemas. AJV strict rejected union `type` syntax and later caught missing object typing in conditional chunk-reference rules; the canonical manifest schema now uses portable `oneOf` and fully typed conditional branches. Final commands above are fresh passing runs; no ignore file or warning suppression is used.

System Ruby/Psych parse and JSON parse also succeeded, but those parser results are not substituted for semantic validation. Redocly validates the embedded login/job example; AJV validates all external manifest examples.

## Independent review and remediation

Independent contract review ran in multiple focused rounds. Every reported Critical/Important finding and the concrete Minor consistency issues were verified against PLAN and the contract set before changes. The remediation is:

| Finding group | Resolution |
|---|---|
| Tenant identity and global administration | Every public tenant route now requires `X-Nexa-Tenant-Id`; cursor/audit/idempotency bind it. `SYSTEM_ADMIN` is a separate global grant, `/admin/jobs` supplies audited read-only queue/detail, and the global grant cannot impersonate a tenant membership. |
| One-time credential replay | CLI/worker raw secrets are never persisted in response snapshots. Same-key replay returns `409 one_time_secret_unavailable` plus a locator; CLI revokes before a new key, while worker bootstrap atomically rotates within an explicit window. |
| Result/accounting lifecycle | Result recognition decrements outstanding only. Allocation-active accounting and dominant resource-time charging continue until exact cleanup/release proof. |
| Manifest identity and upload bounds | Every file entry requires exact `artifact_id`; publish verifies tenant/attempt/kind/checksum/size and logical-name uniqueness. Public and worker uploads require declared `X-Artifact-Size`. |
| Worker startup/recovery | Added bounded `workerGetReconciliation` and a `StartResponse` carrying initial lease expiry/duration/renew/margin. Current incarnation adopts an exact live prior container or stops/cleans it before READY; only timely acknowledged start/adopt/renew may update runner deadline. Added callback-deduplicated `workerFailAttempt`, which commits typed failure, revokes/fences/quarantines and requires separate cleanup before retry/terminal release. |
| Errors, policy and resource vectors | Reusable JSON errors expose server-generated `X-Request-Id`; every operation has 400/500/503 and callback/idempotency 409 uses the shared conflict envelope. Global outstanding and tenant/user rate defaults are versioned; request vectors require positive CPU/RAM while capacity vectors allow zero. |
| Documentation status | README/project structure statements were corrected; ADRs and traceability now include tenant/global authority, one-time secrets, artifact binding, reconciliation/start and policy surfaces. |
| Bootstrap and schema closure | Added maintenance-only initial-admin bootstrap required by INV-20. Replaced unsatisfiable closed-base `allOf` extensions for token/start responses and added a structural regression guard. |
| CLI scopes and pagination | Added a normative exact/non-hierarchical scope-to-operation mapping in the authorization contract and OpenAPI security scheme. Token metadata listing is keyset-paginated; self-management cannot mint grants or membership. |
| Artifact kind and inference reachability | Split public versus attempt-upload kinds. Checkpoint and result manifests bind an exact current-attempt `CHUNK_OUTPUT_MANIFEST`; each chunk binds source attempt/fence. Fenced publish atomically insert-or-verifies current-Authority recognized chunks, while carry-forward requires an exact existing `(job_id,chunk_id)` row from the same tenant/job/session. Restore/GC validate the full cross-attempt kind/checksum/size graph. |
| Upload replay authority | Canonical upload hashes include original `X-Artifact-Media-Type`, size, checksum, artifact kind and, for worker upload, normalized attempt path plus the full Authority tuple. Current credential/incarnation/live authority are rechecked before replay. The only cross-incarnation exception is a completed same-Attempt upload whose stored Authority is in the immutable adoption lineage and whose attempt/allocation/lease/fence plus upload metadata match exactly; stale authority gains no upload/publish ability. |
| Operational recovery | Added worker enable and versioned `NORMAL/ADMISSION_OFF/WRITE_FROZEN` transitions. Frozen mode permits only existing-enabled-SA session/rate metadata, append-only read audit, domain-free replay and guarded recovery, preventing authentication lockout without reopening bootstrap or mutating workload authority. |
| Wire consistency | Response-only secrets are `readOnly`; JobSpec discriminator has explicit mappings; login/logout/request-ID semantics match the documented server behavior. |
| R-01 deadline acknowledgment | Start/adopt/renew bind first send-monotonic per callback but update runner only after a valid acknowledgment arrives before the candidate deadline. Failed, delayed and replayed callbacks have explicit no-extension timelines. |
| R-02/R-03 incarnation and pre-create lifecycle | Added `workerCreateIncarnation`, `workerAdoptAttempt`, server-generated monotonic incarnation sequence, atomic same-Attempt reservation transfer with complete replay snapshot, durable runner-binding reconciliation, executor startup nonce/operation lock/tombstone, and `NoContainerProof`/`ContainerStoppedProof` cleanup union. Startup nonce is committed at dispatch; reconciliation returns it with `claim_state`, so cancel-before-claim creates a server-corroborated sequence-1 tombstone, blocks delayed claim/start and releases once under repeated cleanup. |
| R-04/R-05 worker execution/capability | Claim now replays a complete `ExecutionContext`; `workerDownloadExecutionArtifact` is limited to the exact live execution graph. WorkerInventory has closed runtime/adapter/image/framework/CUDA/driver/GPU schemas. Exact CPU/CUDA examples now use schema-valid millisecond timestamps; both pass strict AJV and removing required `frameworks` fails. |
| R-06 scheduler boundary | `SchedulerPolicy` returns tenant/job decision only. Coordinator deterministically assigns concrete free compatible GPU UUID and increments job fence under dispatch locks. |
| R-07 pause recovery | Pause-crash with a checkpoint reaches PAUSED without retry; without one, restart-safe work consumes one infrastructure retry under `CHECKPOINT_FOR_PAUSE`; unsafe/exhausted work reaches FAILED with exact counter effects. |
| R-08/R-09 identity, runner progress and artifact binding | Added replayable server checkpoint/result reservations, closed runner envelopes/payload/acks with units and bounds, reserve-before-`REQUEST_CHECKPOINT`, and `RESULT_PREPARE` → reserve → `PREPARE_RESULT`. Runner emits staged file batches; worker uploads them and returns only committed `201 Artifact` bindings; runner alone creates final manifests after `FINALIZE_*`. `CHECKPOINT_READY`/`RESULT_READY` carry closed final-manifest descriptors whose stable upload keys support response-loss/adoption-lineage replay before publish/complete. REST present/absent progress and per-attempt runner-owned sequence semantics remain explicit. |
| R-10/R-11 membership/media | Tenant `MembershipSet` starts at version 1 and owns list ETag/If-Match for all mutations. Upload transport is only octet-stream; required original media type is stored, allowlist-validated and included in idempotency hash. |

Earlier focused rounds closed R-01, R-02, R-04, R-06, R-07, R-08, R-10 and R-11. The rejection then reopened R-03, R-05 and R-09. Remediation closed all three, and a final focused independent rereview found no Critical, Important or concrete Minor issue. The reviewer explicitly approved ACC-01 promotion and B02 contract unblock while preserving the boundary that no implementation/runtime gate is implied.

## Cross-contract review

| Review point | Evidence/result |
|---|---|
| HTTP surface | Browser/CLI auth, template catalog, user artifact, job/session/attempt/checkpoint/event/log/progress/result/control, sweep, global/tenant policy, audited admin queue/detail, tenant/user/membership, worker/capacity/allocation/drain/disable/enable, fairness/recovery/audit, initial-admin/local-worker bootstrap and worker incarnation/reconciliation/claim/adopt/start/renew/execution-artifact/reservation/checkpoint/complete/failure/cleanup all have method/path/unique operation ID/security/request/response/error contract |
| Authorization | Five security schemes plus explicit tenant context, exact CLI scope mapping and authorization matrix; membership and global grant are distinct; auth/membership-or-global-role/scope/ownership occurs before replay; tenant-hidden objects return 404; worker/bootstrap/global admin cannot impersonate tenant users |
| Strict schema | Core request/response objects are typed with required fields, enum, bounds/default/unit naming and unknown-field rejection; response-only secrets are read-only; manifest schema is canonical and externally referenced |
| Idempotency/version | Scope/hash/pending/concurrency/response loss/retention fixed; completed replay before If-Match; one-time-secret non-persistence/replay/rotation fixed; checkpoint/result reservation replay fixed; MembershipSet collection ETag/version increment and 428/412 semantics fixed |
| Pagination/bounds | Signed actor/filter-bound keyset cursor; job/event max 100; logs max 1 MiB response; aggregate range 31 days/1000 buckets |
| State | Job/attempt/allocation/worker tables include actor, guard, atomic effect, error and invalid/repeat semantics; waiting reason is not state; terminal immutable |
| Scheduler | Snapshot/clock/bounded candidate/order/tie-break/floor/ledger/aging/reservation output and transaction recheck are explicit; pure policy selects only job, coordinator assigns fence/device under locks; baseline algorithms remain simulator-only |
| Worker/runner | Server-generated incarnation/adoption, complete claim ExecutionContext/data graph, lost/late/repeat response, acknowledgment-gated deadline, typed runner/progress/failure messages, startup tombstone/proof union, stop/cleanup idempotency and reconcile-before-READY fixed |
| Workload/checkpoint | Four templates, typed parameters/progress, replayable checkpoint/result identity reservation, exact artifact-bound safe manifest, transitive inference chunk reachability, uniform octet-stream/original-media metadata, exact CPU oracle, sweep partial acceptance and deterministic chunks fixed |
| Failure/security | Linearization/lock order/deadlock policy, race outcomes, exact scope matrix, frozen-mode recovery/audit exceptions, hard-limit config, all PLAN §9 fault rows, backup/relocation and no-exactly-once/disk-loss boundary fixed |
| Traceability | PLAN requirement groups map to contract/INV/ACC/backlog/evidence; individual tables cover INV-01..21 and ACC-01..39 |

## Scenario walkthroughs

### 1. Submit to result

1. Actor authenticates, supplies explicit tenant context and uploads a same-tenant input with declared size/checksum; bounded stream/fsync/rename/directory-fsync precede Artifact metadata commit.
2. `submitJob` validates committed input/template/spec/capability/total feasibility, locks durable rate/counters, then commits Job/LogicalSession/JobSpec/event/idempotency together and only then returns `202`.
3. Scheduler policy selects tenant/job deterministically; coordinator commit rechecks leader/policy/job/quota/capacity, chooses concrete compatible free GPU UUID when needed, increments fence and atomically creates Attempt/Allocation/Lease.
4. Worker poll is only an offer; exact claim returns immutable session/template/adapter/image/allocation/artifact/restore context. Worker materializes only graph artifacts through its live Authority, then start binds startup nonce/sequence and container identity. Runner deadline changes only after timely start/renew acknowledgment.
5. Worker reserves server result ID. Runner emits immutable staged descriptors; worker uploads each and returns committed `201 Artifact` bindings; runner constructs the canonical final manifest only after all bindings, then emits its closed descriptor. Worker uploads that exact final JSON with a stable descriptor-derived key and uses only the returned `RESULT_MANIFEST` Artifact ID in `workerCompleteAttempt`, which checks reservation, identity/kind/checksum, chunk graph and live Authority before committing unique Result/job and `SUCCEEDED`.
6. Allocation remains held/charged until exact cleanup proof commits RELEASED. Outcome preserves INV-02/08–14 and prepares ACC-06/12–17/20 evidence.

### 2. Response loss and idempotency replay

The first submit transaction commits its complete response snapshot, but HTTP response is lost. Retry authenticates/authorizes, finds same scope/key/hash, returns stored `202`/Job/Location/ETag before evaluating the now-stale If-Match or spending rate/counter again. Different payload returns `409`; concurrent PENDING waits at most 5 s then returns replay or `409` + `Retry-After: 1`. No duplicate Job/session/event/counter exists.

For one-time CLI/worker credentials, the raw secret is intentionally absent from the replay snapshot. Response loss followed by the same key returns `409 one_time_secret_unavailable` and the metadata locator, not a second secret. CLI recovery revokes the unknown token before using a new key; worker bootstrap with a new key atomically rotates the unknown current credential inside the explicit rotation window.

### 3. Cancel versus complete

Both lock the Job. If cancel commits first, desired state becomes CANCELLED, authority is fenced/quarantined and later complete fails `stale_authority`; cleanup reaches terminal CANCELLED. If complete commits first, unique Result and SUCCEEDED become immutable; a new cancel returns `409 state_conflict`. Replaying the actual winning idempotent request returns its original response. No stale/duplicate final result is recognized.

### 4. Pause, crash and resume

Pause commits desired PAUSED and state PAUSING. A committed checkpoint alone is insufficient; PAUSED requires verified cleanup/release. If worker crashes after a compatible checkpoint, cleanup moves the same job/session to PAUSED without retry. If it crashes before the first checkpoint, restart-safe work with budget consumes exactly one infrastructure retry, persists `CHECKPOINT_FOR_PAUSE`, runs only to create a checkpoint, then requires cleanup before PAUSED. Unsafe or exhausted recovery reaches FAILED and decrements counters exactly once. Resume uses a new Attempt and no infrastructure retry.

### 5. Lease expiry, quarantine, cleanup and recovery

After bootstrap, a process takes the local singleton and calls `workerCreateIncarnation`; server generates the new sequence/ID and old callbacks become stale. Reconciliation drains all pages and either adopts an exact still-live prior container or stops/cleans it before READY. Start/adopt/renew record first send-monotonic, but only a valid acknowledgment arriving before `first_send + 45 - 5` updates runner; failure, delayed response and same-callback replay do not rebase. DB-time reaper fences/quarantines while capacity/quota/resource-time remain charged. Cleanup uses exact `ContainerStoppedProof` or tombstoned `NoContainerProof` and closes ledger/counter once.

For dispatch → cancel-before-claim, dispatch has already committed the immutable startup nonce. Cancel revokes/fences and quarantines before a claim receipt exists. Reconciliation returns the exact revoked Authority, `startup_nonce`, `claim_state=UNCLAIMED` and null container; worker creates a durable sequence-1 tombstone without Docker create and submits `NoContainerProof`. API verifies no claim/start/container row, releases once, rejects delayed claim/start, and replays repeated cleanup without a second ledger close or counter decrement.

### 6. Corrupt newest checkpoint

Restore queries committed checkpoints newest first. The newest blob checksum fails, producing corruption/fallback event and no deserialize. The next older checkpoint matches input/spec/schema/adapter/image/environment, so it restores. If all fail, immutable input restart occurs only when the concrete adapter is `restart_safe`; otherwise failure or relocation block is explicit. Two newest committed references are retained and GC cannot remove an in-use candidate.

### 7. Sweep partial acceptance and replay

A canonical expansion of at most 100 children fixes child indexes/hashes. Each child traverses ordinary admission/idempotency/quota and independently commits ACCEPTED(job ID) or REJECTED(error). Parent holds no allocation. Crash resumes unfinished indexes; unique `(sweep,index/hash)` and derived child keys replay completed outcomes. `207` exposes every result, and replay of the parent returns the same mapping without duplicate children/counters.

### 8. Incompatible stopped relocation

Admission is disabled, jobs drain or pause/checkpoint, containers are verified stopped, writes freeze and paired DB/blob manifest backup is taken. During freeze, workload/configuration writes stay blocked, but an existing enabled SA can establish/revoke a browser session, allowed admin reads append audit, and the guarded recovery mode transition remains possible; no bootstrap, token, password, grant or membership mutation is reopened. Old execution remains off. Destination restores same release, creates a new incarnation, inventories/reconciles and verifies checksums. A job whose image/architecture/framework/CUDA/driver or requested resource is incompatible stays blocked with `waiting_for_compatibility` and a reason; it does not silently fall back or resume. Compatible jobs resume with same job/session and new attempt only after readiness.

### 9. Classified failure before cleanup

Trusted runner reports typed `FAILED`; worker maps it to an allowlisted reason and either exact container observation or pre-create `NoContainerProof`. `workerFailAttempt` rechecks live Authority and atomically stores failure, revokes lease, increments fence, moves the job to RECOVERING and allocation to QUARANTINED, and stores callback acknowledgment. A local executor tombstone blocks delayed create/start. Duplicate same-payload callback replays; stale/mismatched callbacks fail. Only proof-based cleanup can release capacity and choose retry or terminal outcome.

### 10. Checkpoint/result artifact binding and response loss

Runner closes output bytes and emits `StagedArtifact` batches; worker streams each exact descriptor through `workerUploadAttemptArtifact`. The successful or idempotently replayed `201 Artifact` is the sole source of `artifact_id`, kind, media type, size and checksum. Worker persists and returns `CommittedArtifactBinding`; only then does runner construct any chunk-output manifest and the final checkpoint/result manifest. `CHECKPOINT_READY`/`RESULT_READY` carry a closed `FinalManifestArtifact`, whose descriptor checksum fixes the stable upload key. Worker persists the final manifest Artifact response before publish/complete. If either file or final-manifest response is lost, the same key/bytes/headers replay the original ID; after same-Attempt adoption, current Authority plus immutable grant lineage and exact metadata permit that replay without granting the predecessor new authority or creating a second Artifact.

## ACC-01 assessment

ACC-01 is satisfied for contract `1.0.0-b01` on this working tree:

- requirement ↔ API/schema/state/invariant/interface/acceptance mapping exists and was structurally checked;
- all five interfaces have logical signatures, ownership, pre/postconditions and typed errors;
- OpenAPI/manifest version and wire formats are concrete and validators pass;
- LogicalSession is distinct from BrowserSession and has no client-writable state machine;
- decisions cite PLAN or are labeled B01 concretization; ADRs do not change PLAN;
- R-03, R-05 and R-09 are remediated and closed by focused independent rereview with no remaining Critical, Important or concrete Minor finding.

ACC-01 is `pass`. ACC-02..ACC-39 remain `specified`: B01 defines their contracts/planned evidence but supplies no product implementation/runtime proof. In particular, macOS documentation validation is not Linux/cgroups, Docker execution, PostgreSQL transaction, browser, load, portability, release or NVIDIA evidence.

## Environment and blockers

The current machine inventory is recorded separately. `uv`, `psql`, Linux/cgroups v2, two portability hosts, durable storage characteristics, NVIDIA stack, CI/GHCR permissions and personnel assignments are not confirmed. B02 is no longer contract-blocked, but it must provision/confirm its own tool prerequisites and cannot claim any runtime gate from B01 evidence.

## Handoff

R-03, R-05 and R-09 are closed; ACC-01 is `pass` and B02 may proceed. B02 must implement the locked state, identity, idempotency, authority, artifact-binding and failure guarantees without treating B01 documentation validation as product/runtime evidence.
