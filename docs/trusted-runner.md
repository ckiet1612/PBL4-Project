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
`STOPPING`, emits no false `STOPPED`, and terminates PID 1 fail closed.

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
before `START`. Registration at or after 30 seconds stops for `RUNTIME_LIMIT`
without starting compute. Stop first requests a graceful process-group
termination for the remaining requested grace, capped at five seconds. The
UID-1001 supervisor owns that timer, sends `SIGKILL` immediately when grace is
zero or after the shorter grace expires, and reports one final exit status; the
runner does not queue an unread second kill command. The supervisor verifies
group absence before `STOPPED`, handles descendants even if the direct workload
parent exits first, and kills remaining compute when the runner/supervisor pipe
closes.

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
local result is a succeeded Job. B11 owns those fenced transactions. Checkpoint
controls remain an explicit unsupported boundary until B14 implements
checkpoint persistence, restore and fallback.

## Verified boundary

The production executor/entrypoint path, distinct UIDs, private control relay,
result handshake, controller disconnect, authority expiry under partial IPC,
log overflow, CPU throttling, PID cap, scratch exhaustion and cgroup OOM were
run on Docker Desktop's Linux `aarch64` VM. This is not bare Linux deployment,
two-host, GPU, reboot/reconciliation or release evidence.
