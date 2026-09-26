# B09 Worker Executor

These worker-side primitives do not make the local journal authoritative for
Job, Allocation, Lease, quota, ledger or result state. PostgreSQL and the B10/
B11 APIs remain the control-plane source of truth.

## Discovery and compatibility

`DockerProbeBackend` reads the Docker host that will execute workloads, not the
macOS client. It fails closed unless Docker reports Linux OS/architecture,
cgroups v2, seccomp, CPU/RAM and stable runtime data. Image capability is
advertised only when exact digest inspection verifies the expected Nexa image
labels, trusted entrypoint, OS and architecture. An unlabelled Python base image
therefore exposes no CPU adapter/framework capability.

`ResourceProvider.allocatable()` subtracts at least
`max(1000 millicores, ceil(20% host CPU))` and
`max(2 GiB, ceil(20% host RAM))`; larger configured reserves win. Results never
go negative or exceed raw host capacity. Compatibility is closed across
architecture, exact image digest, adapter/version, framework/device, GPU count
and resources, with no hidden CUDA-to-CPU fallback.

Run a report without changing worker readiness:

```sh
NEXA_CPU_IMAGE_REF='registry/name@sha256:<verified-digest>' \
  PYTHONPATH=src .venv/bin/python scripts/b09_probe.py
```

## Immutable prepare and input staging

`DockerExecutor` accepts only an authorized immutable `StartExecution`; it does
not accept client commands, environment, mounts or image tags. The journal
binding includes authority/allocation/attempt/startup nonce, architecture,
adapter/framework, resources, runtime/scratch/log bounds, CPU spec and every
input descriptor.

Prepare verifies that each source is a regular non-symlink file under the
trusted staging root, checks size/checksum, copies bytes into executor-owned
per-attempt staging and records the pinned copy. The pinned file is rechecked
immediately before Docker create. Missing files, checksum mismatch, symlink
escape and source substitution fail closed.

## Journal and startup state machine

Each attempt has a stable filesystem lock and atomic JSON journal. Writes use a
same-directory temporary file, file `fsync`, atomic replace and directory
`fsync`. Corruption or write failure is typed and never converted to success.

The durable phases are:

```text
PREPARED -> CREATE_IN_FLIGHT -> CREATED
         -> START_IN_FLIGHT -> STARTED
         -> CLEANUP_IN_FLIGHT -> TOMBSTONED
```

Intent is persisted before each Docker side effect. A create timeout remains
`CREATE_IN_FLIGHT`/unknown and is not blindly retried. Binding a full container
ID is not start success; replay inspects the exact bound identity and only
returns success after `STARTED` was durably observed. Create, inspect and start
share one injected monotonic 30-second startup deadline, each receiving only
the remaining budget. If the persisted clock domain changes, neither `CREATED`
nor `START_IN_FLIGHT` may inspect/start/exec under a fresh budget; start fails
closed as `START_OUTCOME_UNKNOWN`. Explicit inspect, stop and cleanup remain
available against the exact bound identity. A running CPU container still in
`START_IN_FLIGHT` also fails closed: Docker inspect cannot prove whether the
detached supervisor exec was applied, so replay never issues a second exec.

The runtime identity digest contains only immutable normalized data: full
container ID, image identity, exact Nexa labels and security/resource config.
Mutable Docker state fields cannot change it after a normal start.

## Container and control boundary

The create configuration uses the exact verified digest, read-only rootfs and
input mounts, `network=none`, Docker builtin seccomp, all capabilities dropped,
`no-new-privileges`, hard CPU/RAM/PID/tmpfs/log limits, memory swap equal to the
hard memory limit and restart policy `no`.

The entrypoint runs as `1000:1000`; all capabilities are dropped and none are
added. The worker starts the UID-1001/GID-1000 supervisor with a detached
exact-container-ID `docker exec`, and the runner accepts it only through a
one-shot registration socket after validating Linux `SO_PEERCRED`. `/run/nexa`
is a bounded internal tmpfs owned by UID 1000; only the immutable launch spec is
bind-mounted read-only at `/run/nexa-input`. The worker opens control through an
exact-container-ID `docker exec --user 1000:1000` relay. The workload has no
Docker socket, worker credential, control socket permission, registration FD or
runner signal permission.

## Stop, tombstone and cleanup

Stop, inspect and cleanup require the exact full container ID plus stable
runtime digest. A mismatch never targets another container. A server-confirmed
`UNCLAIMED` identity can create an initial sequence-one tombstone only after an
exact-label Docker lookup proves no matching container and no create is in
flight. Docker unavailable, uncertain inspection or an existing candidate
cannot produce `NO_CONTAINER`.

Cleanup persists `CLEANUP_IN_FLIGHT` and stopped observation before remove.
After a lost remove response, post-remove crash or journal write failure, replay
uses the durable observation plus confirmed absence to return the same exact
proof. Repeated cleanup is safe. Proof creation never releases allocation,
quota or ledger state; B10/B11/B15 own server reconciliation and release.

`signal_checkpoint()` stays a typed `UNSUPPORTED` error by design: a B14
checkpoint is requested only as `REQUEST_CHECKPOINT` over the fenced runner
control channel after a server reservation, never by a Docker signal.
`checkpoint_supported(image_digest)` reads the labels of the pinned image once
per process and returns true only for `io.nexa.runner.checkpoint=cpu-state-v1`.
Launches of older images carry no checkpoint block. A restore adds one
more exact read-only input mount, `/input/restore-state.json`, from the
attempt's private staging directory, plus `--resume-state`. The fixed path is
part of the launch contract and is rejected if changed. Other mounts,
UIDs, tmpfs bounds, network, capabilities, seccomp and restart policy `no` are
unchanged. `inspect(identity)` returns the Docker exit code and `OOMKilled` of
an exact identity, and the worker uses it to prove a workload container exited
without a runner terminal frame (see [worker agent](worker-agent.md)).

## Evidence boundary

Unit/fault tests cover locks, journal crash points, immutable replay, unknown
create/start, tombstones, input staging, identity and cleanup. The production
executor/runner path and resource/isolation scenarios ran on Docker Desktop's
Linux `aarch64` VM. Bare Linux deployment, reboot reconciliation, two-host
portability, GPU and release acceptance remain unverified.
