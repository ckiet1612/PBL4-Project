# Requirements traceability

Snapshot contract `1.0.0-b01`, ngày 2026-09-17. Mapping này nối requirement đã duyệt tới nội dung thực tế; liên kết không tự chứng minh runtime gate. [B01 evidence](evidence/B01-contract-review.md) review nội dung/coverage; B02–B25 tạo evidence implementation/runtime.

## PLAN requirements

| Source | Requirement | Contract/schema/operation/transition | INV | ACC | Backlog | Planned verification |
|---|---|---|---|---|---|---|
| §1–§2 | Single-node, self-hosted, multi-tenant batch; API/CLI/Web UI; one v1 release | `contracts.md`; ADR-0001; OpenAPI tags Auth/Jobs/Admin/Worker | 01,18–20 | 01–03,24–27,32,37–39 | B02,B06,B12,B17–18,B21,B25 | Contract/import boundary; API/CLI/Playwright; clean install/release |
| §2 | Failure scope process/network/reboot with durable storage; no disk-loss/exactly-once claim | `concurrency-recovery.md` Failure scope/matrix; ADR-0002/0003 | 10–17 | 14–23,28,31,33 | B07,B11,B14–15,B19–22 | Crash/response loss/reboot; paired backup/restore; accepted-ID reconciliation |
| §2/§7/§9 | No arbitrary code/shell/image/mount/external side effects | `workloads-checkpoints.md`; `concurrency-recovery.md` auth/security; `Executor` contract | 18–20 | 19,24–25 | B06,B09,B16,B20 | Negative API/schema, image allowlist and container abuse inspection |
| §3 | Modular monolith; API/coordinator separate; worker-only Docker and worker-only execution data plane | ADR-0001; `internal-interfaces.md`; `workerDownloadExecutionArtifact`; ExecutionContext | 09,12,18–19 | 01,12,24–25 | B02,B09–11 | Dependency/import tests; claim/materialization authorization; network/socket/container inspection |
| §3 | PostgreSQL authority and immutable filesystem blob | ADR-0002; domain authority table; blob linearization | 10,14,16–17 | 07–08,17,20,23,28 | B05,B07–08,B13–14,B19 | Transaction/fsync faults, restart/reconcile, backup/restore |
| §3 | Job/session/attempt identity, immutable spec, manual retry lineage | `domain-model.md`; `state-machines.md`; operations `submitJob`, `getLogicalSession`, `retryFailedJob` | 08 | 01,03,15,20 | B05,B08,B15 | Constraints; control/idempotency races; lineage/ownership tests |
| §3 | Tenant ownership every query/download/composite reference | OpenAPI `TenantContext`; domain composite keys; auth matrix; audited `adminListJobs`/`adminGetJob` global-grant reads | 01 | 02–03,24,27 | B06–08,B17,B20 | Cross-tenant positive/negative matrix on every surface and global-role-without-membership cases |
| §3/§5 | Submit after atomic commit; 409/412/422/429/503; replay before If-Match | contract index idempotency/version/error; `submitJob`; transaction table | 07,10 | 05–08 | B06,B08,B13,B15 | Same-key concurrency/response loss/rate/queue/capability/version tests |
| §4 | Hardware discovery/reserve; closed runtime/adapter/image/framework/CUDA/driver/GPU capabilities; two Linux configs; stopped relocation | `ResourceProvider`; WorkerInventory schemas; environment inventory; failure matrix relocation | 03,17 | 04,28,32–34 | B09,B21,B23 | Valid CPU/CUDA inventory; missing-capability rejection; host limits; two-host restore/compatibility |
| §5 | Weighted dominant resource-time, ledger, floor and tie-break; pure policy selects job while coordinator assigns fence/device | `SchedulerPolicy` algorithm and deterministic CPU/GPU decisions; domain ledger | 04–05 | 08–09 | B03–04,B13,B24 | Rational-reference properties, pure no-I/O decisions, locked deterministic GPU selection, ≥5 seeds |
| §5 | Aging 60 s, reservation 120 s, one local reservation | `SchedulerPolicy`; Job transitions; Reservation entity | 05–06 | 10 | B04,B13 | Virtual-clock/property and production reservation timelines |
| §5 | Bounded candidates/head/heap, keyset pagination, no full scan | scheduler integration; OpenAPI cursors; index groups | 05–06 | 11 | B05,B13,B22 | Query plans at 100k, candidate bound/cursor fairness tests |
| §5 | Hard quota/outstanding/concurrency/rate defaults/versioning | domain policy/counter/rate; `adminGetGlobalPolicy`/`adminUpdateGlobalPolicy`; tenant/user rate defaults; config limits | 02,07 | 04–08 | B05–06,B08,B13 | Boundary/concurrent admission/restart/policy update tests |
| §6 | Mandatory load/fairness/soak evidence profile | traceability only; acceptance ACC-09/10/11/29/30/35 | 04–07,21 | 09–11,29–30,35 | B03–04,B13,B22,B24 | Real PostgreSQL/API runs, ≥3 runs, raw IDs/latency/query plans; B01 does not claim pass |
| §7 | Four managed workloads and typed parameters | OpenAPI typed JobSpec/Sweep; `workloads-checkpoints.md` | 14–16,18–19 | 18–19,23,25 | B09,B14,B16 | Adapter fixtures, exact CPU, PyTorch comparison, sweep replay, chunk dedup |
| §7/§9 | Checkpoint provenance/safe format/≥2/fallback/compatibility | JSON Schema; workloads restore; exact checkpoint/result → current-attempt chunk manifest → current/prior-attempt recognized chunk references; ADR-0004 | 14–17 | 17–19,23,28,33–34 | B14,B16,B19,B21,B23 | Schema + transitive kind/tenant/job/session/source-attempt/fence/checksum/GC checks + corrupt fallback + relocation/GPU compatibility |
| §8 | Browser session/CSRF, CLI token, admin/user flows and page bounds | OpenAPI Auth/Admin/Jobs; exact CLI scope-to-operation mapping; explicit tenant context/global grant; one-time-secret replay rule; admin queue; auth matrix; cursor limits | 01,20 | 02–03,24,27 | B06,B12,B17–18,B20 | Per-scope allow/deny API matrix, secret response-loss, global-role/membership separation and Playwright/backend flows |
| §9 | State/control races, terminal immutability, closed pause-crash recovery | `state-machines.md`; pause `CHECKPOINT_FOR_PAUSE` branch; race table; callback-deduplicated `workerFailAttempt` before cleanup | 08–13 | 12–16,20–22,31 | B11,B14–15,B20,B22 | Concurrent cancel/complete/failure/pause before/after checkpoint/reaper/callback timelines |
| §9 | Lease DB time, acknowledgment-gated send-time monotonic runner deadline, server incarnation/adoption, no-container cleanup | ADR-0003; `workerCreateIncarnation`; `workerAdoptAttempt`; reconciliation; proof union | 09,12–13 | 12–16,21–22 | B09–11,B15,B20,B22 | Bootstrap/restart/adoption; failed/delayed/replayed renew; pre-create failure/delayed start; agent kill |
| §9 | Security/isolation/observability bounded cardinality | Executor/auth/config contract; audit/event model | 18–21 | 24–26 | B06,B09,B19–20 | Container/network/resource abuse, log/metric schema/cardinality |
| §10 | Readiness fail closed; bootstrap admin/worker from secret; environment gates | `bootstrapInitialAdmin`; `bootstrapLocalWorker`; reconcile; operational-mode freeze/reopen with existing-SA session/audit recovery exceptions; environment inventory | 12,17,20–21 | 21,24,26,32,39 | B02,B06,B10,B19,B21 | Initial-admin uniqueness, frozen-mode login/audit/recovery allowlist, worker rotation, config/secret/readiness faults and clean-host smoke |
| §11–§14 | Six stages/25 tasks/DoD/release; conditional GPU | this matrix and acceptance rows | all | 01–39 | B01–B25 | Task-specific evidence; runtime/release gates remain specified after B01 |

## Invariant coverage

| INV | Contract location | Primary ACC | Implementer |
|---|---|---|---|
| INV-01 | domain composite ownership; tenant context/global grant; authorization matrix; OpenAPI security | ACC-02,03,24,27 | B06–08,B17,B20 |
| INV-02 | Allocation entity/state machine; cleanup/quarantine | ACC-04,16 | B04,B09,B11,B13,B15 |
| INV-03 | `ResourceProvider`, closed WorkerInventory capabilities, deterministic coordinator GPU choice, relocation | ACC-04,32–34 | B09,B21,B23 |
| INV-04 | `SchedulerPolicy` accounting/floor; ledger entities | ACC-08,09,35 | B04,B13,B24 |
| INV-05 | deterministic tenant/job ordering | ACC-09,10 | B04,B13 |
| INV-06 | reservation algorithm/entity/transitions | ACC-10,11 | B04,B13 |
| INV-07 | policy/counter/rate/idempotency transaction | ACC-05–08 | B05–08,B13 |
| INV-08 | Job/Session/Attempt/lineage; terminal/control table | ACC-14,15 | B05,B11,B15 |
| INV-09 | three authority domains; server incarnation/adoption; dispatch fence/device recheck | ACC-12,14 | B11,B20 |
| INV-10 | transaction/lock/idempotency/event contract | ACC-07,08,12,15 | B05,B08,B11,B15,B20 |
| INV-11 | publish guards/result uniqueness/cancel race; fenced failure callback | ACC-14,15,17,22 | B11,B14–15,B20 |
| INV-12 | acknowledgment-gated first-send deadline, start/adopt/renew responses and reconciliation | ACC-13,16,21 | B09–10,B15 |
| INV-13 | lease reaper/quarantine and `NoContainerProof`/`ContainerStoppedProof` cleanup | ACC-13,16,21–22 | B10–11,B15,B22 |
| INV-14 | ArtifactStore and durable publish sequence | ACC-17,23 | B07,B14,B19 |
| INV-15 | exact artifact-bound manifest/restore/fallback/retention including job-scoped cross-attempt chunk carry-forward graph | ACC-18,19 | B14,B16 |
| INV-16 | upload/storage/log/scratch bounds and GC races | ACC-17,23,25 | B07,B09,B19–20 |
| INV-17 | authoritative data/restart/backup/relocation | ACC-20,28,31–33 | B13,B15,B21–22 |
| INV-18 | template/adapter/image allowlist and Executor isolation | ACC-19,24,25 | B09,B16,B20 |
| INV-19 | Executor resource limits/runner/restart policy | ACC-22,25,34 | B09,B20,B23 |
| INV-20 | Auth schemes, exact CLI scopes, bootstrap, frozen recovery authentication, TLS/network boundary | ACC-02,24,27 | B06,B17–18,B20–21 |
| INV-21 | Event/audit/log/metric contract including append-only audit during maintenance freeze | ACC-08,20,26 | B11,B19,B22 |

## Acceptance coverage

All statuses below remain project status from [acceptance](acceptance.md). ACC-01 is `pass` after R-03, R-05 and R-09 remediation, focused rereview and fresh verification; ACC-02–ACC-39 require later implementation/runtime evidence and remain `specified`. B02 is no longer contract-blocked.

| ACC | Contract/evidence prepared by B01 | Runtime owner and verification |
|---|---|---|
| ACC-01 | Entire contract set, ADR-0001..0004, this matrix, B01 review | B01: parse/schema/example/cross-contract review |
| ACC-02 | Authorization matrix, exact CLI scope groups, tenant context and separate global-role grant | B06/B20 per-operation scope/role/membership allow/deny tests |
| ACC-03 | Composite ownership constraints and 404 policy | B06–08/B20 cross-tenant every surface |
| ACC-04 | Resource vector/provider/allocation state | B04/B09/B11/B13 property+DB+Linux/GPU |
| ACC-05 | TenantPolicy/update guards/counters | B06/B13 boundary/race/audit |
| ACC-06 | Submit errors/Retry-After/waiting reason | B08/B13 API/restart/queue-full |
| ACC-07 | Canonical idempotency/callback retention plus one-time-secret non-persistence exception | B06–08/B11/B15 ordinary/secret response-loss and concurrency |
| ACC-08 | Atomic effects/ledger/floor | B08/B11/B13 transaction/restart timelines |
| ACC-09 | Scheduler score/tie-break/Jain mapping | B04/B13/B24 ≥5 seeds and real benchmark |
| ACC-10 | Aging/reservation semantics | B04/B13 deterministic and production traces |
| ACC-11 | Candidate/cursor/index contract | B13/B22 raw query plans at ≥100k |
| ACC-12 | Dispatch lock/epoch/no Docker transaction | B11 two-leader and renew-over-turnover |
| ACC-13 | Start/lease/renew/deadline defaults | B09–10/B15 delayed response/agent kill |
| ACC-14 | Authority tuple/result uniqueness | B11/B15/B20 stale/duplicate completion |
| ACC-15 | Full job/attempt transition tables and `workerFailAttempt` linearization | B15/B20 control/failure/reaper races |
| ACC-16 | Allocation/worker state/reconciliation API | B10/B15 container identity/quarantine |
| ACC-17 | ArtifactStore/publish sequence | B07/B14 filesystem fault injection |
| ACC-18 | Artifact-bound manifest schema/restore order and checkpoint/result → current-attempt chunk manifest → recognized source-attempt chunk graph | B14/B16 corrupt/missing/kind/job/session/source-fence/GC/compatibility |
| ACC-19 | Four template contracts | B14/B16 fixture/baseline/resume reports |
| ACC-20 | Authority/restart durability semantics | B11/B15/B22 accepted-ID reconciliation |
| ACC-21 | Readiness/dependency failure contract | B10/B11/B15 partition/readiness faults |
| ACC-22 | Failure classes/retry/runtime bounds and fenced failure callback before cleanup | B09/B15 real kill/OOM/timeout/log flood plus replay/stale failure callback |
| ACC-23 | Config storage bounds/GC race | B07/B19 disk/full/stream/GC tests |
| ACC-24 | Session/token/bootstrap/security policy including WRITE_FROZEN existing-SA recovery authentication | B06/B20/W/L security review/tests |
| ACC-25 | Executor isolation contract | B09/B20 Linux abuse/inspection |
| ACC-26 | Event/audit/log/metric boundaries including frozen-mode read audit | B19 metric/log/alert/outage/freeze tests |
| ACC-27 | OpenAPI operations and page/log bounds | B12/B15/B17–18 Playwright + CLI/API |
| ACC-28 | Domain constraints/schema compatibility | B05/B21/B25 migration/upgrade/restore |
| ACC-29 | Required profile mapped, not claimed | B22 ≥3 real production-API runs |
| ACC-30 | Failure/observability scope mapped | B22 8-hour soak raw evidence |
| ACC-31 | Failure matrix rows above | B20/B22 scenario-to-evidence matrix |
| ACC-32 | Inventory/reserve/architecture contract | B21/B25 two Linux clean installs |
| ACC-33 | Backup/relocation sequence/compatibility | B21 two-host restore cases |
| ACC-34 | GPU UUID/compatibility conditional contract | B23 real NVIDIA ≥3 runs/window |
| ACC-35 | Scheduler/evidence provenance requirements | B03–04/B22/B24 reproducibility review |
| ACC-36 | Demo dependencies and API states mapped | B24 rehearsal/video/snapshot provenance |
| ACC-37 | Contract version/release bundle dependencies | B25 published artifacts/fresh install |
| ACC-38 | No runtime claim in B01; all gates retained | B24–25 final gate reconciliation |
| ACC-39 | Strict schemas and future tool surfaces named | B02+ actual lint/typecheck/test/build/scan |

## Coverage boundary

B01 establishes how later evidence is produced; it does not assign `pass` to load, Linux, Docker, Web UI, GPU, portability, migration, soak, release or implementation gates. Internal details that cannot change behavior—SQL table names, Python class layout, Docker SDK choice, cache implementation—remain with owning tasks. Any detail that changes API/schema/state/authorization/fairness/failure guarantee returns to PLAN/contract review rather than being hidden as an implementation choice.
