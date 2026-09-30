# B09 Trusted Runner

The image entrypoint is `nexa.workloads.trusted_runner`. It supervises the
allowlisted CPU adapter as a less-trusted child and remains responsible for
startup, authority, runtime, log and stop bounds even when the worker control
connection disappears.

## UID, registration and mount boundary

Docker starts PID 1 as `1000:1000` with rootfs read-only, network disabled, all
capabilities dropped, no added capabilities and `no-new-privileges`. No runtime
path starts as root or requires `SETUID`/`SETGID`:

1. The runner validates the immutable launch spec mounted read-only at
   `/run/nexa-input/launch-spec.json`.
2. The worker starts the supervisor only by exact full container ID using
   detached `docker exec --user 1001:1000` and the fixed allowlisted command.
3. The supervisor connects to a one-shot registration socket. The runner checks
   Linux `SO_PEERCRED` for UID 1001/GID 1000, accepts one connection and unlinks
   the registration path. The workload child never inherits this stream.
4. Authority may arrive before registration, but production launch remains
   queued until the validated supervisor stream exists. The runner rechecks
   that the authority deadline is still strictly in the future immediately
   before sending exactly one `START`; the supervisor then creates the CPU
   process in its own session/process group as UID 1001.

The runner records the runtime start instant at the `START` send boundary, but
persists that transition only after the send succeeds so an fsync cannot move
the command beyond the startup deadline. A failed send rolls the transition
back. If a connected supervisor omits `STARTED`, the runner keeps the stream,
sends `TERM 0`, waits for `EXIT` and only then emits `STOPPED`. If the stream
disconnects after `START` and stop cannot be confirmed, the runner stays
`STOPPING`, emits no false `STOPPED`, and terminates PID 1 fail closed. Only an
`EXIT` report confirms the stop: a stream that fails, breaks protocol or is reset
(Linux reports a peer that closed with unread data as a reset, not end of file)
is such a disconnect (REM-R01). A
watchdog stop that cannot be confirmed does the same: before the B1–B16
remediation it left the runner `STOPPING` and serving until reconciliation
removed the container (B15-R14).

`/run/nexa` is an internal tmpfs owned by UID 1000 with mode `0711`; it is not a
host bind mount. The worker control socket and runner state are mode `0600`.
The worker reaches the control socket only through
`docker exec --interactive --user 1000:1000` against the exact full container
ID and `nexa.workloads.control_relay`.

The actual workload does not inherit the supervisor pipe, Docker socket,
worker/database credentials, `NEXA_CONTROL_SOCKET`, `NEXA_RUNNER_STATE` or
`NEXA_LAUNCH_SPEC`. UID 1001 cannot connect to the control socket or signal UID
1000. `/output` is a bounded tmpfs writable by UID 1001 and readable/writable by
the runner group so the runner, not the workload, constructs the final
manifest.

## Protocol and durable state

The worker channel uses length-prefixed UTF-8 JSON with a 64 KiB frame maximum.
Envelope and every payload branch are closed and checked for schema version,
UUIDv7, int64/batch bounds, enum values and finite numbers before any sequence
or effect is committed. Unknown fields/types, duplicate JSON fields and
`NaN`/`Infinity` are invalid.

`STOPPED.reason` also accepts `STARTUP_LIMIT` since the B1–B16 remediation; the
addition stays within frame `schema_version` 1. `REQUEST_STOP` does not accept
it: only the runner stops for its startup limit. Frames from earlier runners
still parse. An earlier worker rejects the new reason as a protocol error and
fails the attempt closed, so the worker image is upgraded no later than a runner
image that sends it; both are built from the same source.

Runner messages use `message_sequence`; worker controls use
`control_sequence`; progress snapshots use an independent
`progress_sequence`. Sequence hashing covers canonical `{type,payload}`. The
same sequence and hash is `DUPLICATE`; a conflicting hash is `INVALID` and
stops the attempt; lower unseen or skipped sequences are `OUT_OF_ORDER`. Exact
replay is classified before the terminal-state guard, so a committed
`REQUEST_STOP` replay remains `DUPLICATE` without another stop effect.

Control sequence/hash state, pending runner frames, next message/progress
sequence, deadline state, stop reason, completion token, reserved result
identity, descriptors, bindings and manifest state are atomically persisted.
Malformed pending state fails closed on reload. A disconnected peer may connect
again and receives the same unacknowledged message bytes/sequence without a
second effect or new reservation.

Since B15 a runner that reaches `STOPPED` keeps serving for at most
`STOP_FRAME_LINGER_SECONDS` (3 s). It exits earlier once any worker ACK names the
`STOPPED` sequence with a code other than `INVALID`. The worker ACKs a terminal
frame it keeps behind an unfinished one with `OUT_OF_ORDER`. Sending the frame
is not delivery: a renewal or result control connection reads only its own ACK.
A peer that never ACKs cannot hold the container open past the linger, so the
B09 bounds hold: the container stops within 7 s of a controller disconnect and
within 8 s of an unacknowledged `STOPPED`. Before B15 the runner exited as soon
as it stopped. A stop with no worker connected then ended the container with
exit 0 and no stop reason, which the worker reports as `RUNNER_PROTOCOL_ERROR`
(B15-R14, B15-R16).

Since the B1–B16 remediation the container exit status still names the stop
when no worker kept the `STOPPED` frame. PID 1 (`python -m
nexa.workloads.trusted_runner`) exits as follows:

| Runner end | Exit status |
|---|---|
| a worker ACKed the `STOPPED` frame | 0 |
| stopped, no worker ACK, or failed closed after a stop was requested | `STOP_EXIT_CODES[reason]`: `PAUSE` 90, `CANCEL` 91, `LEASE_DEADLINE` 92, `FAILURE` 93, `RUNTIME_LIMIT` 94, `SHUTDOWN` 95, `STARTUP_LIMIT` 96 |
| unreadable launch spec | 78 |
| failed closed with no stop reason | 124 |

The worker maps a stop status exactly as it maps the `STOPPED` frame with that
reason ([worker agent](worker-agent.md#pause-cancel-and-runner-stop-reasons-b15)).
Exit 0 without a frame the worker saw remains `RUNNER_PROTOCOL_ERROR`; 124 and
Docker statuses such as 137 remain `RUNNER_UNAVAILABLE`.

## Authority and watchdog

The runner begins in `WAITING_AUTHORITY`; Docker start alone never authorizes
compute. The worker-side deadline primitive binds a candidate to one callback:

```text
candidate = callback_first_send_monotonic + lease_duration - safety_margin
```

Only an API acknowledgment received strictly before that candidate permits the
same candidate to be sent as `SET_AUTHORITY_DEADLINE`. Failed, absent, equal-to-
candidate, late or replayed responses do not extend authority. Retry preserves
the original first-send time, deadlines never move backward, and expiry cannot
revive. Renewal does not reset the attempt runtime budget.

An independent watchdog enforces the 30-second startup limit, a maximum
300-second/spec-shorter runtime, authority deadline and runner log bound. It
does not depend on the socket receive loop. Accepting authority does not end
the startup budget: while the UID-1001 supervisor has not started the workload,
the watchdog still uses the persisted runner creation timestamp, and the
deferred registration path rechecks the same strict deadline immediately
before `START`. Registration at or after 30 seconds stops for `STARTUP_LIMIT`
without starting compute. A runner that never receives authority stops for
`STARTUP_LIMIT` at the same limit. Before the B1–B16 remediation both reported
`RUNTIME_LIMIT`, so a startup timeout looked like a runtime-limit observation
(B15-R10). Stop first requests a graceful process-group
termination for the remaining requested grace, capped at five seconds. The
UID-1001 supervisor owns that timer, sends `SIGKILL` immediately when grace is
zero or after the shorter grace expires, and reports one final exit status; the
runner does not queue an unread second kill command. The supervisor verifies
group absence before `STOPPED`, handles descendants even if the direct workload
parent exits first, and kills remaining compute when the runner/supervisor pipe
closes. A workload that already exited, for example one that finished while no
worker was connected, leaves a supervisor that has reported its exit and closed
its socket; a later deadline stop then sends no `TERM` and still emits `STOPPED`
with the reported exit status. Before B15 that `TERM` failed and left the
runner `STOPPING` with no stop frame (B15-R15).

## CPU result boundary

`cpu.iterative` strictly validates its input and produces canonical result bytes
with exactly `iterations`, `final_accumulator`, `input_checksum` and
`spec_checksum`. Progress sequence is separate from the runner envelope.

The implemented result path is:

```text
RESULT_PREPARE -> PREPARE_RESULT -> RESULT_FILE_BATCH
-> BIND_ARTIFACT_BATCH -> FINALIZE_RESULT_MANIFEST -> RESULT_READY
```

The completion token is generated once and replayed. The runner validates
reserved IDs, descriptor/binding identity, missing/duplicate/conflicting
bindings, batch bounds, file checksum, descriptor checksum, binding-set
checksum, embedded `manifest_checksum` and the checksum of complete manifest
bytes. It copies server-returned artifact IDs into the manifest and never
invents a committed identity.

B09 does not call reservation/upload/publish APIs and does not claim that a
local result is a succeeded Job. B11 owns those fenced transactions.

## CPU checkpoint boundary (B14)

Checkpoint controls are accepted only when the launch spec carries a checkpoint
block. The worker adds it for a `checkpointable` template on an image labelled
`io.nexa.runner.checkpoint=cpu-state-v1`; otherwise the controls stay a protocol
error, as in B09. With the block present, the entrypoint writes a closed
canonical snapshot `{schema_version, step, accumulator, input_checksum,
spec_checksum}` to `/output/state.json`. It writes at start, at most once per
second on the 65,536-step observer stride, and at the final step, always by
temp file, fsync and atomic replace. The workload never sees the checkpoint
reservation or manifest.

```text
REQUEST_CHECKPOINT -> CHECKPOINT_FILES_READY -> BIND_ARTIFACT_BATCH(CHECKPOINT)
-> FINALIZE_CHECKPOINT_MANIFEST -> CHECKPOINT_READY
```

`REQUEST_CHECKPOINT` is refused before the workload starts, after the result
is reserved, past its frozen monotonic deadline, while a
previous cycle is incomplete, or when the sequence does not advance. The runner
reads the live snapshot as a regular non-symlink file of at most 4 KiB and
validates it against the spec and input checksums. It rejects a cursor below
the one already committed. It then copies the bytes to a read-only (0440)
`checkpoint-<sequence>-state.json` (logical name `state.json`, kind
`CHECKPOINT_FILE`), so later snapshot rewrites cannot change them. After the
bindings match that descriptor, the runner writes the canonical manifest
(provenance, compatibility, cursor, files and embedded `manifest_checksum`) as
0440 and emits `CHECKPOINT_READY`. If the workload finishes while a cycle is
open, `RESULT_PREPARE` is deferred until `CHECKPOINT_READY`, because the server
refuses a result reservation for a checkpointing attempt. The pending cycle,
descriptor and bindings are part of the persisted runner state and replay
byte-identically after a controller reconnect.

**Checkpoint storage failure (B14-K5).** Staging copies share the attempt's
bounded `/output` tmpfs with the workload. Suppose the runner gets `ENOSPC`,
`EDQUOT` or `EIO` while it reads the live snapshot, copies checkpoint files,
writes the chunk-output manifest of an open checkpoint, or writes the
checkpoint manifest. It then emits `FAILED` `INTERNAL/CHECKPOINT_STORAGE_FAILED`
and rejects the control. It stops the workload with `FAILURE`, and never
announces a file it did not copy completely. The same holds for a waiting
request staged by the deadline watchdog. The class is not retried: a new
attempt would fill the same bounds again. Before the fix, the `OSError` ended
the runner with status 124, which the worker reported as retryable
`INFRASTRUCTURE/RUNNER_UNAVAILABLE`. Other `OSError`s keep that fail-closed
exit. Storage errors on the result path are unchanged.

Since B15 the runner accepts `REQUEST_CHECKPOINT{reason: PAUSE}` through the same
cycle; it does not stop the workload by itself. A request that arrives before
the workload's first `state.json` write (a pause right after launch) waits for
that write until the request's frozen deadline and is then staged as usual; an
invalid snapshot still fails closed, a second request while one waits is
rejected, and a result staged meanwhile is deferred until `CHECKPOINT_READY`
(B15-R20). After the server commits that
checkpoint the worker sends `REQUEST_STOP{reason: PAUSE}`, and the runner stops
the workload within the requested grace and reports `STOPPED{PAUSE}`. The runner
also stops itself with `STARTUP_LIMIT` (its workload did not start within 30
seconds), `RUNTIME_LIMIT`, `LEASE_DEADLINE` (its monotonic authority deadline
passed before a renewal extended it) or `FAILURE` (for example after a rejected
checkpoint control). The worker maps each reason to an Attempt failure
as listed in [worker agent](worker-agent.md#pause-cancel-and-runner-stop-reasons-b15).
Cancel does not reach the runner as a control: the worker stops the exact
container after the server revokes the authority.

On restore, the worker mounts the verified state read-only at
`/input/restore-state.json`. The runner re-checks its checksum, step and
accumulator against the launch spec before `START`, and the entrypoint decodes it
again against the input it reads. The adapter resumes from step `k` with
`a(k+1) = (a(k) * 1664525 + 1013904223 + k) mod m`, so the resumed result bytes
equal an uninterrupted run. Result format, UIDs, mounts and all B09 hardening
are unchanged.

## PyTorch adapters and chunk frames (B16; đã triển khai, chờ Task Review)

The runner stays stdlib-only. It now also launches the two PyTorch adapters
listed in `nexa.domain.workload_adapters`, `pytorch.cifar10` and
`batch.inference`, each from its own image built from `deploy/pytorch-cpu/`
(B16-R03). For these adapters the worker writes launch spec **v3**
(`nexa.workloads.adapter_launch`). It is a closed document with adapter
identity, validated template parameters, `threads = max(1, cpu_millis // 1000)`,
fixed input paths, provenance, the checkpoint block and the restore block. CPU
iterative keeps launch spec v1/v2 and its bytes do not change. The runner turns
v3 into a fixed argv:

- `python -m nexa.workloads.pytorch_cifar10`;
- `python -m nexa.workloads.batch_inference`.

No value in the argv comes from anything other than the validated spec.

Inputs are fixed read-only mounts:

- `/input/dataset.arrow`;
- `/input/model.safetensors` (inference only);
- `/input/restore/` (the restore files).

Training:

- **Workload output.** The workload atomically replaces one
  `/output/state.safetensors` at batch boundaries, at most once per second.
- **Checkpoint cycle.** On `REQUEST_CHECKPOINT` the runner reads that file
  through one fd, bounded. It checks the header, keys, shapes and dtypes
  against an allowlist, and checks that the step is monotonic. It then splits
  the file into `model.safetensors`, `optimizer.safetensors`, `rng.safetensors`
  and `training-state.json`, the four files of the adapter rule, in that
  order.
- **Restore.** The runner re-validates the four mounted files against the spec
  before `START`.
- **Result.** `model.safetensors` and `metrics.json`. The runner validates the
  full closed metrics schema.
- **Code bounds.** Neither the runner nor the adapters use `torch.save/load`,
  pickle, marshal or eval/exec (AST test).

Inference is a chunked cycle:

```text
CHUNK_FILE_BATCH (<=64 chunks/frame) -> BIND_ARTIFACT_BATCH(CHUNK_OUTPUT)
-> CHECKPOINT_FILES_READY [inference-state.json] | RESULT_FILE_BATCH [summary.json]
-> BIND_ARTIFACT_BATCH -> AUXILIARY_MANIFEST_READY (CHUNK_OUTPUT_MANIFEST)
-> BIND_ARTIFACT_BATCH(CHUNK_OUTPUT) -> FINALIZE_* -> CHECKPOINT_READY | RESULT_READY
```

- **Chunk files.** Chunks are named `chunk-%08d.jsonl` or `.parquet`. The runner
  reads only their name, size and checksum, never their content. It unlinks
  each chunk file once the worker's binding is accepted.
- **Window.** With checkpointing on, the workload holds at most
  `INFERENCE_WINDOW = 8` unbound chunk files in the 16 MiB `/output` tmpfs
  (B16-R13).
- **Chunk-output manifest.** The runner builds it only from committed chunk
  bindings: the restored prefix first, then the recognized chunks carried
  forward, then the chunks of this attempt.
- **Recognized chunks (B16-R21).** The launch spec key `recognized_chunks`
  lists the chunks the server recognized beyond the restore cursor (from chunk
  0 after a fallback), with their original source attempt and fence. They are
  never recomputed (RV03). Their files are mounted read-only under
  `/input/recognized/`. Before starting the workload the runner reads each one
  bounded and compares size and checksum with the spec; a mismatch or missing
  file emits `FAILED` `INTERNAL/CHUNK_OUTPUT_UNAVAILABLE`. The workload gets
  `--recognized-dir /input/recognized --recognized-count N`: it validates each
  file's chunk header, indexes and records checksum, adds its predictions to
  the carried totals, and writes its first state past the carried chunks. The
  runner republishes each original entry unchanged and never uploads it. A
  chunk file the workload still writes for a recognized index stops the
  workload and emits `FAILED` `INTERNAL/CHUNK_OUTPUT_CONFLICT`.
- **Item count.** The runner checks that `item_count` in the Arrow metadata
  equals the row count; otherwise `INVALID_INPUT`.
- **Restore.** A restore mounts `inference-state.json` and the carried
  chunk-output manifest (`/input/restore/chunk-output-manifest.json`). Chunk
  bytes are not mounted (B16-R18).

**Workload exit.** A non-zero workload exit becomes one `FAILED` frame
(B16-R24/R26):

- For the two adapters, exit 65 means that the workload rejected its input or
  parameters. It becomes `INVALID_INPUT/INVALID_INPUT`. The parsers map every
  JSON, integer-limit, nesting and type error to that path. Parameters out of
  range, malformed Arrow metadata or safetensors header, and a chunk plan above
  `MAX_CHUNKS` (B16-R17/R24) are rejected before any output. A chunk file above
  the upload bound is found only when that chunk is produced. For chunk k > 0,
  chunks 0..k-1 and the inference state are already written by then, and may
  already be bound or recognized by a checkpoint (B16-DOC-01;
  `test_oversized_later_chunk_is_invalid_input_after_the_earlier_chunks`).
- Otherwise the runner compares the `oom_kill` counter of the container's
  cgroup v2 `memory.events` with the value it read right before `START`. The
  baseline is persisted in the runner state. A rise means
  `OOM/CONTAINER_OOM` with `oom_killed: true`. This applies to every adapter,
  CPU iterative included.
- An unreadable counter at either point is no evidence. Any other exit then
  stays `INTERNAL/WORKLOAD_EXIT_NONZERO`.

A signal exit is reported as 128 + signal.

All B09 hardening stays unchanged: UIDs 1000/1001, read-only rootfs and input,
`network none`, `cap_drop ALL`, seccomp and no-new-privileges, and the PID,
memory and CPU bounds. The B16 hardening oracle on VPS1 reads every process in
the container cgroup (B16-R22).

## Verified boundary

The production executor/entrypoint path, distinct UIDs, private control relay,
result handshake, controller disconnect, authority expiry under partial IPC,
log overflow, CPU throttling, PID cap, scratch exhaustion and cgroup OOM were
run on Docker Desktop's Linux `aarch64` VM. This is not bare Linux deployment,
two-host, GPU, reboot/reconciliation or release evidence.
