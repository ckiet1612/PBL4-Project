# B09 Docker Executor and Trusted Runner Evidence

> Status: B09 remediation R01-R17 is implemented and verified within the
> environment and ownership boundaries below. B09 is ready to be submitted to
> Task Review, but this document does not record Task Review approval.

## Revision and environment

| Field | Observation |
|---|---|
| Revision | `e73d7f62551f8a0156f97a42f5a72170f8ebf08a` base revision with uncommitted B09 working-tree changes |
| Client | macOS arm64; client capacity is not reported as workload capacity |
| Docker boundary | Docker Desktop 4.79.0 context `desktop-linux`; LinuxKit 6.12.76 guest, Docker Engine 29.5.3, cgroups v2, builtin seccomp, `aarch64` |
| Base image | `python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9` |
| B09 image | `nexa/cpu-iterative@sha256:341d943487940cb67f9e5ef61c2334593d8eab99619fc0f977e4f28a6d0e8e9a`, `linux/arm64` |
| Runtime boundary | Docker Desktop Linux VM development evidence, not a bare Linux deployment, two-host portability, GPU, release or public deployment |

Selected raw reports are under `docs/evidence/raw/`:

- `B09-image.json`: exact image identity, entrypoint and architecture.
- `B09-probe.json`: live Docker-host capability report and allocatable capacity.
- `B09-smoke.json`: deterministic CPU oracle output.
- `B09-container-config.json`: security/resource configuration and non-root UID model.
- `B09-docker-scenarios.jsonl`: eleven real-Docker scenario observations, timelines and bounds.

## Verification

| Check | Command | Fresh result |
|---|---|---|
| B09 unit/protocol/workload | `PYTHONPATH=src .venv/bin/pytest -q tests/worker tests/workloads` | `126 passed` |
| Full Python regression | `PYTHONPATH=src .venv/bin/pytest -q` | `436 passed, 195 skipped, 2 dependency deprecation warnings` |
| Real Docker suite | `NEXA_B09_IMAGE_REF='nexa/cpu-iterative@sha256:341d...8e9a' scripts/b09_docker_tests.sh --require` | `11 passed in 108.35s`; JSONL observations preserved |
| Image inspection | `docker image inspect 'nexa/cpu-iterative@sha256:341d...8e9a'` | exact digest above; entrypoint `python -m nexa.workloads.trusted_runner`, user `1000:1000`, `linux/arm64` |
| CPU smoke | exact digest in `IMAGE_REF`, then `scripts/b09_smoke.sh` | pass; `final_accumulator=915488392` |
| Discovery | exact digest in `NEXA_CPU_IMAGE_REF`, then `PYTHONPATH=src .venv/bin/python scripts/b09_probe.py` | verified Nexa labels/entrypoint/architecture; base Python image advertised no adapter/framework |
| Quality | `.venv/bin/ruff check .`; `.venv/bin/ruff format --check .`; `git diff --check` | pass |

`uv` is not available on the current shell PATH, so the approved fallback
`PYTHONPATH=src .venv/bin/...` was used. No lockfile claim is inferred from the
fallback. PostgreSQL tests that require `NEXA_TEST_DATABASE_URL` remain among
the documented skips and were not made green by changing their prerequisites.

## Remediation closure

| Finding | Status | Direct evidence |
|---|---|---|
| B09-R01 production executor to runner to CPU path, startup deadline and UID/control isolation | `pass` | Persisted-clock regressions cover registration at `29.999s`, `30.000s` and `31.000s`: only the pre-deadline case starts once. The watchdog retains the startup deadline after authority and the send boundary rechecks it. `START` precedes transition fsync; send failure rolls back the in-memory start transition. Missing `STARTED` with a live peer is terminated and confirmed, while disconnect after `START` fails closed in `STOPPING` without false `STOPPED`. Real barriers prove valid registration produces one `STARTED` at UID 1001, while delayed registration produces no `STARTED`/CPU and stops for `RUNTIME_LIMIT` within 35 seconds; no production UID-switch fallback |
| B09-R02 independent watchdog, current authority and bounded stop grace | `pass` | Initial/current deadline must be strictly future, registration rechecks expiry, and real SIGTERM-ignoring children are killed with requested grace `0` and `0.1s`; Docker controller-loss/deadline/log scenarios remain green |
| B09-R03 descendant survives parent SIGTERM | `pass` | `test_supervisor_kills_descendant_when_direct_parent_exits_first`; group liveness is checked before `STOPPED` |
| B09-R04 callback-bound timely deadline | `pass` | `tests/workloads/test_deadline.py`: first-send candidate, strict-before-candidate ACK, failed/late/replayed/no-revival cases |
| B09-R05 progress and result handshake | `pass` | unit fake peer and production Docker path through `RESULT_READY`; descriptor, binding and both manifest checksum layers validated |
| B09-R06 strict validation before sequence commit | `pass` | closed control and runner payload branches, UUID/int64/enum/non-finite checks, type+payload hash, conflict stop, malformed retry rejection |
| B09-R07 durable reconnect/lost-ACK replay | `pass` | Runner lock covers snapshot construction through atomic replace; stale-writer barrier cannot roll sequence/result state back; exact terminal `REQUEST_STOP` replay is `DUPLICATE` without a repeated effect |
| B09-R08 stable runtime identity | `pass` | digest uses immutable normalized identity/config; production create/start/inspect/cleanup succeeds with the same digest |
| B09-R09 observed `NO_CONTAINER` and initial `UNCLAIMED` tombstone | `pass` | authority/allocation/nonce/binding are compared before mutation; exact-label lookup under lock and sequence-one tombstone tests pass |
| B09-R10 create-bound is not start-confirmed | `pass` | Durable start phases plus exact-identity inspection; running `START_IN_FLIGHT` CPU replay fails closed because inspect cannot prove detached supervisor exec outcome, and a lost applied exec response is never re-executed |
| B09-R11 cleanup after remove/write or response loss | `pass` | structured object-not-found classification fails closed on daemon/socket errors; cleanup intent and stopped observation persist before remove |
| B09-R12 complete immutable execution binding | `pass` | architecture, adapter/framework, mounts, runtime/scratch/log and resource fields persist; parameterized changes are rejected |
| B09-R13 trusted input materialization | `pass` | descriptor-relative component traversal rejects ancestor symlinks; primary mount checksum is bound to authorized context checksum |
| B09-R14 one startup budget | `pass` | create/inspect/start share the 30-second deadline; both `CREATED` and `START_IN_FLIGHT` fail closed before Docker inspect/start/exec after clock-domain change, while inspect/cleanup remain available |
| B09-R15 closed discovery | `pass` | exact image digest, OS/architecture, labels and entrypoint are required; unlabelled base image advertises no adapter/framework |
| B09-R16 production Docker abuse/deadline harness | `pass` | Eleven scenarios include both registration barriers plus runtime, authority and log trigger-to-stop measurements entirely in runner/guest monotonic time; a deliberate 2-second host receive delay cannot reduce the authority measurement |
| B09-R17 bounded input streaming | `pass` | copy/hash uses a fixed buffer and rejects after at most declared size plus one byte instead of buffering the full source |

The real-Docker resource run observed CPU throttling, PID refusal before the
128-process cap, scratch `ENOSPC`, one cgroup OOM kill, delayed registration
stop `30.084825s` after runner readiness with no CPU start, runtime-watchdog
stop `1.033549s` after the runner `STARTED` timestamp, authority stop
`0.034789s` after its guest deadline despite a deliberate `2s` host receive
delay, and log stop `1.045714s` after the guest workload wrote its trigger
timestamp. These are bounded development-VM observations, not portable
performance targets.

## Gate status

| Gate/portion | Applicability | Status | Evidence or missing prerequisite |
|---|---|---|---|
| ACC-04 discovery/reserve/compatibility B09 portion | Direct B09 | `pass` | Unit tests plus exact-image Docker-host probe |
| ACC-13 runner deadline/watchdog portion | Direct B09 | `pass` | Injected-clock tests and real controller-loss/IPC-pressure timelines |
| ACC-13 DB lease renewal and worker/coordinator fencing | B10/B11, not implemented by B09 | `not-run` | Requires worker HTTP renewal and coordinator/API state |
| ACC-22 startup/runtime/OOM/failure B09 portion | Direct B09 | `pass` | Startup/runtime unit checks and real runtime/OOM/log scenarios |
| ACC-25 container isolation/resources on current Docker Linux VM | Direct B09 | `pass` | Config plus behavior tests for rootfs/input/network/socket/control/UID and CPU/RAM/PID/scratch/log bounds |
| ACC-25 bare Linux deployment acceptance | Later deployment environment | `blocked` | No bare Linux host was provided; Docker Desktop VM evidence is not promoted |
| ACC-23/ACC-31 local identity/cleanup/replay subset | Direct B09 | `pass` | Unit race/crash tests and production exact-identity cleanup |
| ACC-23/ACC-31 server reconciliation, allocation release and restart chain | B10/B11/B15 | `not-run` | Requires PostgreSQL/API worker reconciliation and state transitions |
| GPU support | B23 conditional | `not-run` | No NVIDIA provider, runtime or hardware evidence; no CUDA fallback is advertised |

## Process and trust boundary

Docker starts the trusted image as `1000:1000` with all capabilities dropped,
no added capabilities, `no-new-privileges`, read-only rootfs, network disabled
and bounded resources. The worker starts the fixed supervisor command as
`1001:1000` using detached `docker exec` against the exact full container ID.
The runner accepts only that peer through a one-shot registration socket after
validating Linux `SO_PEERCRED`, then unlinks the registration path. `/run/nexa`
is an internal tmpfs owned by UID 1000; control socket/state are mode `0600`.
The worker uses an exact-container-ID `docker exec --user 1000:1000` relay.
The CPU child does not inherit registration/control streams or runner
environment and cannot connect to control, signal the runner or access Docker.

## Handoff and limitations

- B10 consumes discovery, journal/lock, exact identity/cleanup proofs, durable
  IPC state and deadline primitives; it adds worker incarnation, heartbeat,
  reconcile-before-READY and real API renewal.
- B11 consumes immutable execution context, `STARTED`/`STOPPED`, result
  reservation/binding frames and proofs; it owns fenced API publish/release.
- B14 consumes the CPU oracle and explicit unsupported checkpoint boundary; it
  owns checkpoint reservation, persistence, restore, provenance and fallback.
- B15 consumes typed failures, deadline/stop/tombstone observations and cleanup
  proofs; it owns cancel/pause/resume/retry/reaper/quarantine/recovery state.
- B23 may add GPU capability/isolation, but no GPU support is claimed here.

No commit, push, branch/worktree, registry push, Task Review request, external
deployment or release was performed. B09 is implemented and verified within
the stated scope and is ready to be submitted to Task Review.
