# B11 coordinator, dispatch and fenced result implementation plan

> Execute in this task with Superpowers executing-plans and TDD. No commit,
> push, branch, worktree, PR, external deployment or PLAN changes.

**Goal:** Connect accepted API jobs to real CPU execution, recognized results and
proof-based release, using the existing B04/B08/B09/B10 contracts.

**Architecture:** Separate DB-backed coordinator process proposes through B04 and
rechecks under global lock order. Backend owns authority and transactions;
worker owns Docker, durable IPC replay and artifact transport. Recognition and
release remain separate commits.

**Stack:** Python 3.12, SQLAlchemy/psycopg, PostgreSQL 17, FastAPI, Docker,
existing trusted CPU runner. Source baseline `a7d215a`; initial tree clean.

**Specification:** User's B11 execution brief (2026-09-22), PLAN B11,
`docs/contracts/{internal-interfaces,concurrency-recovery,state-machines}.md`
and `docs/contracts/openapi.yaml`. Latest user confirmation approves B01–B10;
older evidence status does not override it.

## Work packages

- [ ] Leadership and dispatch: create `src/nexa/coordinator/leadership.py`,
  `service.py`, `accounting.py`, `main.py`; first add PostgreSQL behavioral tests
  for competing holders, takeover/stale dispatch, atomic allocation and held
  quota. Use existing singleton, policy and exact Decimal schema.
- [ ] Policy snapshot: bounded 16 normal candidates plus oldest and reservation,
  fair tenant continuation, durable eligibility/floor/segments. Exercise every
  B04 decision and capacity/quota invalidation under locks.
- [ ] Execution API: committed poll offer, immutable claim context, graph-only
  input download, exact start acknowledgment; tests for replay/stale authority,
  graph mismatch and startup identity. Changes in application execution services,
  worker routes and schemas; reuse B10 authentication/callback receipts.
- [ ] Result API: reserve server ID, fenced durable attempt upload, canonical
  runner manifest validation, completion/result query. Test upload/complete
  response loss, provenance, stale authority and unique result.
- [ ] Worker CPU orchestration: add bounded lifecycle module and client calls;
  persist callback/message bindings, materialize inputs, execute B09, renew new
  attempts, bind result artifacts and finalize using runner bytes. Unit tests
  cover IPC replay, tar/path limits and deadline semantics before implementation.
- [ ] Failure/release: typed failure linearization and narrow cleanup proof;
  release allocation/GPU/ledger/counters once with continuation facts. Preserve
  B10 pending cleanup crash windows and timeout operation serialization.
- [ ] Real integration: API input upload/submit for two tenants, coordinator and
  worker subprocesses, real Docker runner, recognized download and CPU oracle,
  allocation/counter/ledger reconciliation and coordinator/API restart.
- [ ] Runtime packaging: `nexa-coordinator` entrypoint and development Compose
  service without Docker socket. Frozen dependency checks and startup smoke.
- [ ] Final verification: focused races, B09/B10 regression, full Python suite,
  lint/format/migration checks and one consolidated review (at most two repair
  batches). Update usage/evidence/README/ROADMAP to measured scope only.

## Single requirements and evidence checklist

| Requirement | Source | Module | Transaction/invariant | Test | Evidence | Status |
|---|---|---|---|---|---|---|
| Leadership/healthy turnover | brief §4; ACC-12 | coordinator leadership | DB time 15/5, epoch, recheck after locks | coordinator B11 integration | B11 evidence | not-run |
| B04 snapshot/reservation/accounting | brief §5; INV-02/04 | coordinator snapshot/accounting | held quarantine, exact Decimal, tick ≤1s | coordinator B11 integration | B11 evidence | not-run |
| Atomic allocation/dispatch | brief §5.3; ACC-04/12 | coordinator service | global order, fence, unique grant, capacity | allocation races | B11 evidence | not-run |
| Poll/claim/graph/start | brief §6; ACC-03/13/16 | execution service/worker lifecycle | immutable context, lease/start receipt | API and worker B11 | B11 evidence | not-run |
| Runner result handshake/upload | brief §7; ACC-14/17 | worker lifecycle/result service | descriptor keys, durable bytes before metadata | result replay/fault tests | B11 evidence | not-run |
| Recognized result/query | brief §8; ACC-03/20 | result service/job routes | current authority, unique Result, terminal atomic | result concurrency/isolation | B11 evidence | not-run |
| Failure and proof release | brief §9; ACC-05/21 | execution service/accounting | quarantine; exact proof; release once | cleanup races and B10 regression | B11 evidence | not-run |
| Real CPU vertical slice | brief §10F; ACC-07/08/28/39 portion | API/coordinator/worker/runner | accepted ID→result→release reconciliation | Docker B11 E2E two tenants | B11 raw reports | not-run |
| Runtime/docs | brief §11 | packaging/Compose/docs | fail closed, source provenance | lock/build/config/startup | B11 evidence | not-run |

Separate guarded databases are used for independent integration suites;
destructive fixtures are never concurrent within one database. Docker Desktop
Linux VM evidence does not prove bare Linux, GPU, portability or release.
