# B10/B11/B14/B15/B16 worker agent: implementation boundary

This is the B10 worker implementation with scoped verification evidence; it is
not an accepted production deployment. The REST API persists incarnation,
bounded reconciliation, heartbeat/inventory, dispatch poll, exact adoption and
lease renewal in PostgreSQL. The local agent drains nonempty pages, discovers
only its installation-labelled containers, rebinds an exact live journal
authority, applies a first-send runner deadline after ACK, renews adopted
attempts, follows B15 pause/cancel control from renewal answers and keeps
unresolved failure/cleanup work pending. Bare-Linux portability,
release acceptance and independent Task Review remain open.

## Bootstrap and local state

`nexa-worker --bootstrap` takes an OS `flock` on the stable
`$NEXA_WORKER_STATE_ROOT/worker.lock` inode **before** reading the credential,
calling the API or discovering Docker. A second process exits with code 75.
The lock file is not removed; its FD is close-on-exec. The one-time B06 worker
bootstrap is attempted only if `credential.json` is absent. Its idempotency key
is fsynced in `bootstrap-intent.json` before sending. The credential is saved
mode 0600 with file and directory fsync. With a lost bootstrap response, the
intent remains and startup refuses to issue a fresh key: a local operator must
confirm the old credential/rotation state through the approved maintenance
procedure. Neither replay nor another automatic rotation can recover the raw
one-time secret. Never delete the intent merely to make startup proceed.

`nexa-worker` without `--bootstrap` requires the credential file. Protect the
state volume and the separate B09 `journal/` tree; never expose them to a
workload. `agent-state.json` stores callback identity/payload and Linux boot
clock domain but not credentials. Pending unacknowledged callbacks must not be
discarded by an operator. A boot-ID mismatch with pending state fails closed.
All paths are configured; the entrypoint rejects non-Linux execution because
the runner and worker need one Linux monotonic clock domain.

## Lifecycle and health

The entrypoint creates a server-generated incarnation in STARTING, checks the
local journal and exact installation-labelled Docker container set, drains the
worker-scoped reconciliation pages, and probes the real Docker host through
the B09 `ResourceProvider`. An exact live identity is adopted and rebound in
the journal; a runner deadline is applied only after the API acknowledgment
and trusted-runner ACK. Unresolved identity, cleanup or failure work stays
pending and blocks READY. The server also records completed page traversal and
rechecks active authority, observed containers and inventory before READY. The
server ages missing heartbeats by DB time: 15 seconds SUSPECT, 30 seconds
UNAVAILABLE. Those health states differ from ENABLED/DRAINING/DISABLED admin
state and do not release capacity or prove a container stopped. A `DRAINING`
worker gets no new dispatch, but poll still returns an offer committed before
the drain, which then claims, runs and completes as usual (SM:103, B15-R22). A
`DISABLED` worker is held at `STARTING`; each heartbeat stores in
`worker_incarnations.ready_checked_at` whether it passed every READY check
other than the admin state, and enable requires the latest heartbeat to have
passed (SM:98, B15-R07). A heartbeat that only moves `last_heartbeat_at` keeps
the worker version (ETag); a change of health, inventory version or `ready_at`
bumps it (contracts.md:100, B15-R08). The agent re-discovers inventory on every
heartbeat; the inventory checksum excludes `discovered_at`, so only a change in the
discovered content creates a new inventory version, and the admin `discovered_at`
is the first observation of that content (B18-R22). Worker routes return timestamps with
millisecond precision (B15-R29). Callbacks of one worker lock the worker row
before inserting their receipt, so two concurrent callbacks cannot deadlock on
a lock upgrade (B15-R27). A local Linux
operator can inspect the lock holder with `fuser
/var/lib/nexa-worker/worker.lock` and query the worker/incarnation through
authorized admin tools; do not infer authority from a PID file or Docker name.

The worker sends heartbeat every five seconds while healthy, renews adopted
attempts independently, and requests new offers only when READY+ENABLED. A
current-incarnation pending claim retries its original callback after a full
identity scan even while readiness is blocked by that claim; an unclaimed
offer with no local work does not itself block the first poll. A Docker identity
created after a reconciliation page snapshot is kept unresolved if its journal
still binds a current-incarnation execution; the worker waits for a fresh API
snapshot instead of stopping that live container as an orphan. A pending
heartbeat callback is replayed with its original callback ID and payload
before a new inventory sample is taken. Startup supervision launches
reconciliation, heartbeat and renewal together. It inspects and adopts live expected identities as pages
arrive, allowing renewal during the remaining pages and full orphan scan.
Cleanup requiring a full inventory is deferred until that scan completes.
Adoption/pending replay and renewal serialize under the attempt lock. A
pending failure or cleanup callback is sent and acknowledged under that lock
too, re-read after the lock is taken, so the reconciliation replay and the
result thread never POST it twice and a callback finished by the other sender
counts as resolved; the journal records the acknowledged failure and the
verified cleanup before the operation is dropped (B15-R39). A renewal that
waited on that lock re-reads the journal and skips an attempt whose cleanup is
already verified: its lease ended, so a new renewal could only be rejected and
would block READY (REM-R05). The same holds for every other path that could act
on such an attempt again: a reconciliation scan that waited on the lock does not
re-adopt it, the IPC and result loops drop it, a failure is not sent for it and
a replayed offer is not claimed (REM-R06). Journal reads take
the same lock as writes, so a reader never observes a record hidden by an
in-flight replace, for example on a Docker Desktop bind mount. An unreadable
record is logged with its cause, byte length and 16-hex-digit SHA-256 prefix
only (B15-OBS-01). Lock order is the attempt's journal lock, then the operation
store's internal lock; no thread holds two attempts' locks at once. A
timed-out operation is allowed to converge before that loop starts another
one. SIGTERM/SIGINT stops the loops. A failed dependency clears
local reconciliation readiness; the entrypoint returns code 75 only when the
supervised run exits with an unrecovered startup/runtime error. This entrypoint
deliberately has no unsafe fallback for an unknown B09 create/start outcome or
a missing failure/cleanup acknowledgment.

Revoked authority uses cleanup only. If the process dies after removal and
before saving the cleanup callback, the worker resumes `CLEANUP_IN_FLIGHT` or
reconstructs the stopped proof from `TOMBSTONED` through the executor, after
matching the journal to the expected identity. A missing container alone is
insufficient. Unverified cleanup reuses its pending callback and blocks READY;
verified release completes reconciliation without a stale failure callback.
For a recognized Result, reconciliation can return the committed completion
receipt. The worker compares callback, reserved Result and manifest Artifact
binding with its journal before durably restoring a lost acknowledgment and
discarding any pending renewal. The subsequent cleanup still needs the exact
stopped-container proof and original grant lineage. An old-incarnation claim
without a known container remains unresolved while its Authority is live;
missing Docker identity alone never proves safe release. Once reconciliation
reports that claim `REVOKED`/`CLAIMED` with no committed container identity,
for example after the reaper expired the lease of a process that died between
the claim commit and its journal (B11-H01), the executor proves the release
under the attempt lock. It checks any local record against the Authority,
startup nonce and resources; stops and removes a bound exact container through
cleanup; removes an unbound one only if Docker reports it created and never
started; tombstones the record, or initializes a `CLAIMED` reconciliation
tombstone when none exists; and repeats the exact-label scan before emitting
`NoContainerProof`. Any identity mismatch, started unbound container or Docker
uncertainty keeps the attempt unresolved. A verified cleanup also ends that
attempt's pending claim, renewal and failure callbacks, and a later container
from the dead process's delayed create is removed unstarted by the orphan scan.
If the server rejects `/start` after the exact Docker container was created,
the worker reports its identity in the failure callback, then stops it and
submits a stopped-container proof. A pending `202` cleanup retains its callback;
reconciliation retries the identical payload and receives the stored `202` until
the release transaction promotes that receipt to verified `200`. It only discards
the rejected start after verified release. A failed failure callback still
stops the locally bound container, but keeps the allocation unresolved rather
than fabricating release.
An operation's own completed `TimeoutError` is retried; a wait timeout retains
the running operation until it finishes so retries cannot overlap.

## B14 checkpoint cycle, restore and container exit

B14 adds a checkpoint cycle for attempts whose launch spec carries a checkpoint
block (template `checkpointable` and a runner image labelled
`io.nexa.runner.checkpoint=cpu-state-v1`). `worker/checkpoint_flow.py` keeps the
cycle in the journal (`runner_state.checkpoint_flow`) and records every server
identity and byte binding before the next effect. In order: reserve callback ID,
reservation, frozen `REQUEST_CHECKPOINT` control, file descriptors, per-file
upload keys and artifact IDs, the binding and finalize controls, the manifest
descriptor and artifact, and the publish callback ID. A crash at any step
therefore replays the same checkpoint ID and sequence instead of reserving
another. A cycle starts only on the monotonic interval (spec
`checkpoint_interval_seconds`, clamped to 5-60 s). It also needs no pending runner
frame, no result flow or deferred `RESULT_PREPARE`, no failure resolution, and
progress below 1.0. A restarted worker waits one full interval before a new
cycle but resumes an open one. Reserve `409` skips to the next interval, and
`503` or a network error retries the same callback. Publish `422` means the
server rejected that identity: checkpoints stop for the attempt while the workload keeps
running. A runner protocol mismatch fails the attempt
`INTERNAL/CHECKPOINT_PROTOCOL_ERROR`; the B11 fail path abandons the open
reservation. Adoption accepts the server's transferred checkpoint reservation
only when the journal explains it, and never replays the old publish callback
under the new authority. The API returns the adopt body exactly as stored, with
three-digit millisecond timestamps like the reserve answer, so a reservation
the worker journaled compares equal field by field (B15-R19). Checkpoint, cursor and manifest bytes are never logged.

For an attempt whose claim froze `execution_context.restore_checkpoint`, the
worker re-verifies the manifest against that record, the job input and its
architecture before any Docker work. It downloads only the listed state file
through the attempt's execution graph into its private staging directory and
checks it against the manifest cursor; a leftover file that does not match the
descriptor size and checksum (a download cut short by a worker crash) is removed
and fetched again. The executor then mounts it read-only at
`/input/restore-state.json` and passes `--resume-state`; the entrypoint validates
it again before resuming. If the frozen restore cannot be verified or
downloaded, the attempt fails `INCOMPATIBLE/CHECKPOINT_RESTORE_UNAVAILABLE` and
never silently restarts from the input. A claim without a restore record
starts from the input only for the first Attempt of a Job without a
CheckpointReference, or for a `restart_safe` template (B14-R11). For a later or
inherited Attempt of a template that is not `restart_safe`, the claim records
`CHECKPOINT_RESTORE_UNAVAILABLE`. When the poll offered a checkpoint, the worker
fails that Attempt `INCOMPATIBLE/CHECKPOINT_RESTORE_UNAVAILABLE` with a
no-container proof. In every other case the API refuses the start callback
with 409, before the runner accepts an authority deadline, which it needs
before any compute. The worker then fails the Attempt `TIMEOUT/STARTUP_TIMEOUT`
and removes the container, so the input is never replayed. Launch selection runs inside the startup budget and its
failure handling, so a configured image digest that differs from the context
still fails the attempt with a no-container proof, as before B14.

A workload container that exits without a runner terminal frame is detected
when its control relay fails. The IPC and result loops then inspect the exact
container identity; a relay error on a running container is not treated as
an exit. The observed exit is journaled (`runner_state.container_exit`) and the
attempt fails with a Docker-proven observation: `OOMKilled` becomes
`OOM/CONTAINER_OOM`, and exit code 0 becomes `INTERNAL/RUNNER_PROTOCOL_ERROR`
(the runner exits 0 only after a terminal frame that was lost). A runner stop
exit status (90–96, [trusted runner](trusted-runner.md#protocol-and-durable-state))
maps like the `STOPPED` frame with that reason. Any other exit
becomes `INFRASTRUCTURE/RUNNER_UNAVAILABLE`, which can be retried.

The same Docker observation applies at startup (B15-R11). When the runner does
not accept its start authority deadline, the worker inspects the bound container
before it replays. A container that has exited is classified at once: a
`FAILED` or `STOPPED` frame read on the deadline connection names the cause,
otherwise the exit status does. A killed runner therefore becomes a retryable
`RUNNER_UNAVAILABLE` rather than waiting out the 30-second budget as
`STARTUP_TIMEOUT`. A runner that is still running, or cannot be inspected, keeps
the replay. `STARTUP_TIMEOUT` stays for a runner that never accepts within the
budget, and for the runner's own `STARTUP_LIMIT` stop. Elapsed time never
proves an exit. A failed send of the deadline control is a control failure like
a failed read, and no longer escapes as a relay error. Cleanup then
removes the exact stopped container and sends its stopped-container proof;
nothing is stopped twice. An acknowledged renewal whose runner deadline could not
be delivered to the dead container is discarded only after the failure is
acknowledged and cleanup is verified, so readiness recovers without losing
authority work. After a completion callback has been sent, a container exit
does not fail the attempt; the completion is replayed from the request journaled
with its callback ID, without reading the stopped container's output again. A
dead container found after a worker restart can still have another
incarnation's pending renewal. Since B15 the reaper revokes that lease at DB
expiry. The new incarnation then sees the attempt `REVOKED`, proves the exact
stopped container and releases the allocation. The pending renewal is
discarded only after that cleanup is verified (B15-R09). A failure the worker
recorded while the reaper fenced the attempt, for example after a network loss
during which the runner stopped at its deadline, is rejected as stale (`409`)
and can never be accepted. It is discarded the same way, only after the exact
stopped container's cleanup is verified, so it cannot keep reconciliation
incomplete (B15-R17).

## PyTorch adapters and chunk uploads (B16; đã triển khai, chờ Task Review)

**Images.** The worker takes the legacy `NEXA_CPU_IMAGE_REF` plus the optional
`NEXA_WORKLOAD_IMAGE_REFS`, a comma-separated list where every entry is pinned
`@sha256`. It probes each image separately. An image advertises an adapter and
framework only when its labels match one registry descriptor exactly: runner
protocol, adapter ID and version, framework and version, and checkpoint
format. The executor launches the image whose digest equals
`ExecutionContext.image_digest`.

**Launch.** For `pytorch.cifar10` and `batch.inference`,
`worker/adapter_dispatch.py` turns the claimed execution graph into launch spec
v3. It checks the provenance against the context, that `threads` matches the
allocation, and the fixed input mounts. The CPU iterative graph still goes
through `dispatch.py`.

**Restore.** A restore downloads the files listed in the frozen checkpoint
into private staging. For inference it also downloads the chunk-output
manifest. Each file is checked against the manifest and mounted read-only under
`/input/restore/`. The worker refuses an inherited restore for a chunked
adapter (B16-R20).

**Recognized chunks (B16-R21).** A claim for a chunked adapter carries
`ExecutionContext.recognized_chunks`: the chunks the server recognized beyond
the restore cursor, or from chunk 0 after a fallback to input, each with its
original source attempt and fence. Before creating any container the worker
checks that the list is closed, contiguous and starts at the restore cursor. A
`null` value (a recognized chunk the server cannot read) or an invalid list
fails the attempt `INTERNAL/CHUNK_OUTPUT_UNAVAILABLE`, without retry and
without a container. A non-empty list becomes the launch spec key
`recognized_chunks`. The worker then downloads each listed file through the
execution graph into `recognized/` of its private staging directory, checks its
size and checksum, and mounts it read-only under `/input/recognized/`; a file
that cannot be fetched or does not match fails the attempt the same way, before
any container. The workload takes these chunks over from the files and never
recomputes them (RV03). A claim without the key comes from a server before this
change. The attempt then recomputes and a real conflict ends as below.

**Training checkpoint cycle.** It uses the B14 flow, with the four files taken
in adapter-rule order (B16-R16).

**Inference checkpoint cycle and result handshake.** Both run in
`worker/inference_flow.py`:

1. Each `CHUNK_FILE_BATCH` is uploaded once. Every binding is journaled
   (`runner_state.inference_chunks`, per frame in `inference_chunk_batches`)
   before the `BIND` control is sent. The runner unlinks bound chunk files, so a
   replayed frame reuses the journal and never re-reads the container.
2. The worker binds the state (or summary) file only when every chunk before
   the document's cursor is bound.
3. It binds the chunk-output manifest only when that manifest lists the
   restored prefix, then the recognized chunks carried forward unchanged, then
   exactly this attempt's journaled chunks.

The runner never uploads a carried chunk. Chunk indexes of this attempt start
after the restored and carried chunks (B16-R21).

Upload keys are derived from attempt, callback, message sequence and
descriptor, so a crash that repeats an upload gets the same artifact back.
Chunk bytes are never logged.

**Chunk conflicts.** A publish or complete that names a recognized chunk
range with other bytes gets `409 state_conflict` with the safe
`reason: CHUNK_OUTPUT_CONFLICT`. `WorkerApiError` keeps that code and reason,
never the message, so the worker classifies it deterministically: it fails the
attempt `INTERNAL/CHUNK_OUTPUT_CONFLICT` without retry, and no row is
re-recognized. A `409` without a reason is still replayed as before. The runner
reports the same failure itself when the workload writes output for a carried
chunk (B16-R21; before the remediation the attempt ended
`INTERNAL/RUNNER_PROTOCOL_ERROR` after its runtime limit).

## Pause, cancel and runner stop reasons (B15)

Every renewal ACK carries the Job `desired_state`, which the agent journals
as `runner_state.control_desired`. Only `RUNNING` clears an earlier pause
(`pause_checkpoint`, `pause_aborted`). A `CHECKPOINT_FOR_PAUSE` Attempt starts
with `control_desired = PAUSED`. While `PAUSED` is desired, the agent runs
the following steps:

1. It opens one checkpoint cycle for the pause and journals
   `pause_checkpoint` when the server commits it. A server abort of that
   cycle journals `pause_aborted`, and the cycle is not retried.
2. It freezes `pause_stop` = `REQUEST_STOP{reason: PAUSE, grace 5 s}` and
   sends it. A replay resends the same control bytes.
3. After the runner's `STOPPED{PAUSE}` frame, or an observed container exit
   after `pause_stop`, it proves the stopped container. The cleanup callback
   then turns the Job `PAUSED`, the Attempt `CANCELLED/PAUSE`, and releases
   the allocation.

Once the pause checkpoint commits, the server holds the Attempt `STOPPING`
and accepts no failure for it (SM:75). Whatever then ends the workload, a
`STOPPED{PAUSE}` or other terminal frame, an observed exit, or the 40 s
deadline, the agent force-stops and proves the exact container, and cleanup
pauses the Job at no retry cost (B15-R21). A `CHECKPOINT_FOR_PAUSE` Attempt
whose pause checkpoint the server rejects (for example a rejected manifest)
cannot run on: the server keeps it `PAUSING`, and the agent fails it
`INTERNAL/CHECKPOINT_PROTOCOL_ERROR` at once instead of waiting for the
deadline and spending a retry (B15-R28).

A pause that cannot finish within 40 s of monotonic time and has no committed
pause checkpoint fails the Attempt `INFRASTRUCTURE/RUNNER_UNAVAILABLE`. This timer lives in process memory, so a
worker restart starts it again. The server then applies the recovery rules in
[coordinator](coordinator.md#b15-reaper-retry-promotion-and-retention-sweep):
with a committed checkpoint the Job becomes `PAUSED` at no retry cost;
without one, a `restart_safe` template consumes one retry as
`CHECKPOINT_FOR_PAUSE`, and any other template ends `FAILED`.

Cancel does not use a runner control. A renewal answered 409
`stale_authority` ends the authority (SM:59; B15-R30 replaced the earlier
`state_conflict` for a revoked or expired lease or a changed desired state).
Only the attempt's own worker is told that the cancel is committed; another
worker's callback gets the same `stale_authority` answer before and after the
cancel (F8). The next reconciliation page reports the attempt `REVOKED`, and the
worker stops and removes the exact container before its cleanup proof. The
cleanup callback then commits `CANCELLED`, at most one reconciliation interval
(≤5 s) after the agent learns of the cancel.

Rejected stale callbacks and reconcile outcomes commit nothing, so they are not
recovery events: `RECOVERY_EVENT_TYPES` stays closed (B15-R33). They show up in
the API response and in one bounded JSON log line each.
- The API logs every `409 stale_authority` at WARNING as
  `{"event":"worker_callback_rejected", …}`. The line has the method, route
  template, status, code, fixed message and request ID, plus the path `worker_id`
  or `attempt_id`.
- The worker logs a reconcile outcome at INFO as
  `{"event":"worker_reconcile","complete":…,"items_seen":…,"adopted":…,"unresolved":…}`.
  It logs an incomplete scan, a scan that adopted an attempt, and the first
  complete scan after start or after an incomplete one. A steady healthy scan is
  silent.

Neither line carries a credential, callback body, authority, lease, allocation or
workload content. B19 adds worker metrics for these outcomes (loop failures by operation,
execution outcomes and readiness checks) on the ops listener; see
[observability](observability.md) §1.3.

A worker can be `READY` while it holds live authority under desired `PAUSED`,
that is, a `PAUSING` attempt or a `CHECKPOINT_FOR_PAUSE` offer, so that offer
stays pollable (B15-R13). The reaper, a cancel or a disable can fence an offer
the worker never claimed. The reconciliation page then reports it `REVOKED` and
`UNCLAIMED` (`attempts.claimed_at IS NULL`), and the worker tombstones it with a
`NO_CONTAINER` proof (B15-R12).

A runner `FAILED` frame is forwarded unchanged only for these classes:

- `INCOMPATIBLE/CHECKPOINT_RESTORE_UNAVAILABLE`;
- `INVALID_INPUT/INVALID_INPUT`;
- `INTERNAL/INVALID_RESULT`;
- `OOM/CONTAINER_OOM` (B16-R26);
- `INTERNAL/CHUNK_OUTPUT_CONFLICT` (B16-R21);
- `INTERNAL/CHECKPOINT_STORAGE_FAILED` (B14-K5): the runner could not copy
  checkpoint bytes into its bounded `/output` (no space or an I/O error). Like
  every `INTERNAL` class, it is not retried.

An OOM frame is forwarded only with `oom_killed: true`, and `oom_killed: true`
is accepted only on an OOM frame. Any other frame becomes
`INTERNAL/WORKLOAD_EXIT_NONZERO`. The failure observation is `CONTAINER` with
`exit_code` null, because the runner container is still alive. Its
`oom_killed` is true exactly for a forwarded OOM. The API accepts OOM only with
that observation. OOM and INVALID_INPUT are not retried.

A runner that stops itself names the reason, and the agent maps it:

| Runner stop reason | Attempt failure |
|---|---|
| `RUNTIME_LIMIT` | `TIMEOUT/RUNTIME_LIMIT_REACHED` |
| `STARTUP_LIMIT` | `TIMEOUT/STARTUP_TIMEOUT` |
| `LEASE_DEADLINE` | `INFRASTRUCTURE/RUNNER_UNAVAILABLE` (retryable) |
| `FAILURE` after a rejected checkpoint control | `INTERNAL/CHECKPOINT_PROTOCOL_ERROR` |
| any other stop | `INTERNAL/WORKLOAD_EXIT_NONZERO` |

A `LEASE_DEADLINE` stop happens when renewals stopped reaching the API. The
runner enforces it without the agent (B15-R10). A runner whose workload did
not start within its 30-second startup limit stops for `STARTUP_LIMIT`, so the
failure is `STARTUP_TIMEOUT` with `runtime_limit_reached` false. Before the
B1–B16 remediation it reported `RUNTIME_LIMIT`, and the worker reported both as
`RUNTIME_LIMIT_REACHED`. The same mapping applies to the exit status of a
runner whose `STOPPED` frame no worker kept (B15-R14). A control the runner rejects is journaled as
`rejected`, together with `checkpoint_rejected`. Its `STOPPED` frame, still
unread, then names the cause (B14-R10). A checkpoint frame whose control was
rejected is committed without further processing, because the stopping runner
acts on no later checkpoint control. Otherwise that pending frame would keep the
channel closed and `STOPPED` unread (B15-R14).

A stopped runner exits once a worker ACKs its `STOPPED` frame, or after its
3-second linger ([trusted runner](trusted-runner.md#protocol-and-durable-state)).
A renewal or result control connection may therefore be the only one that reads
the frame. Every connection keeps a runner `STOPPED` or `FAILED` frame through
the same journal update as the IPC loop. When an unfinished frame precedes it,
for example a pending checkpoint frame, the terminal frame is journaled apart as
`runner_state.pending_terminal_message` and ACKed `OUT_OF_ORDER`. A second,
different terminal frame is `INVALID`. Other frames stay unacknowledged for the
IPC loop. Once the container exit is proven, a kept terminal frame names the
failure, not the exit code (B15-R16).

## Local Compose contract

`compose.yaml` describes a single-host **development** contract; it is not B21
deployment evidence. Set unique installation and worker UUIDv7 values, a real
fingerprint, a verified `NEXA_CPU_IMAGE_REF` digest, and four absolute secret
file paths through the environment. The database URL secret must contain a
PostgreSQL URL pointing at the Compose `db` service. Run the existing Alembic
migrations explicitly before starting API; the API fails closed on a missing
schema. Set the bootstrap network CIDR to the actual private Compose subnet.
Only `worker` mounts `/var/run/docker.sock`; it does not receive the DB URL.
The API has DB access but no Docker socket. Set `NEXA_WORKER_STATE_ROOT` to
an existing absolute host directory shared with the Docker daemon; Compose
binds it at the identical path in the worker. B11 stores journal, materialized
input and launch spec there so Docker can bind only those exact paths into the
runner. Keep the directory private and durable. An older B10 `worker_state`
named volume is not removed or migrated automatically: preserve its credential,
journal and pending callbacks before switching paths. Database and artifacts
remain separate persistent volumes. No service uses privileged or host networking.
Caddy runs under Docker's init (`init: true`) with `pids_limit: 512`. Its
BusyBox `wget` health check hands the `ssl_client` helper to PID 1 on every
run, and Caddy as PID 1 never reaped it, so each 5-second check leaked one
zombie PID. This leak was the ~31,000-PID `nexa_b10_smoke3` Caddy (ENV-01).
Zombies count in `pids.current` but not in `docker top`/`cgroup.procs`.

With real secret files and environment values, the intended sequence is:

```sh
docker compose up -d db
docker compose run --rm --no-deps api sh -c 'export NEXA_DATABASE_URL="$(cat /run/secrets/nexa_database_url)"; exec alembic upgrade head'
docker compose up -d api worker
```

The Compose syntax was checked with placeholder values and `docker compose
config --quiet`; the B10 image was rebuilt with `uv sync --frozen --no-dev
--no-editable`. Compose smoke project `nexa_b10_smoke3` then started API/Caddy/
PostgreSQL and worker, and a controlled worker SIGKILL/restart moved from PID
7511/incarnation sequence 12 to PID 26765/sequence 13 while preserving the
credential hash and returning the worker to `READY`; heartbeat, reconciliation
and null-poll requests returned HTTP 200. A separate guarded Linux-container
scenario ran the API, PostgreSQL, real B09 runner and worker process together,
then killed and restarted the worker while the runner stayed alive. This
evidence comes from Docker Desktop's Linux VM, not a bare-Linux clean-host or
release deployment.
