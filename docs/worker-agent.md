# B10/B11 worker agent: implementation boundary

This is the B10 worker implementation with scoped verification evidence; it is
not an accepted production deployment. The REST API persists incarnation,
bounded reconciliation, heartbeat/inventory, dispatch poll, exact adoption and
lease renewal in PostgreSQL. The local agent drains nonempty pages, discovers
only its installation-labelled containers, rebinds an exact live journal
authority, applies a first-send runner deadline after ACK, renews adopted
attempts and keeps unresolved B11/B15 work pending. Bare-Linux portability,
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
state and do not release capacity or prove a container stopped. A local Linux
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
timed-out operation is allowed to converge before that loop starts another
one. SIGTERM/SIGINT stops the loops. A failed dependency clears
local reconciliation readiness; the entrypoint returns code 75 only when the
supervised run exits with an unrecovered startup/runtime error. This entrypoint
deliberately has no unsafe fallback for an unknown B09 create/start outcome or
a missing B11/B15 failure/cleanup acknowledgment.

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
without a known container remains unresolved; missing Docker identity alone
never proves safe release.
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
