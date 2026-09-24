# B11 coordinator and CPU execution

The coordinator is a separate process. Run it with `NEXA_DATABASE_URL` after the
current Alembic schema is ready:

```sh
PYTHONPATH=src .venv/bin/python -m nexa.coordinator.main
```

It acquires the singleton PostgreSQL leadership row for 15 seconds and renews
it every 5 seconds. A lost lease stops all dispatch mutations. Each tick charges
held allocations, builds a B04 policy snapshot, and commits an allocation,
server startup nonce, attempt lease, authority grant, job fence and event in one
transaction. The worker sees the committed offer on `workerPollDispatch`; the
coordinator never opens the Docker socket.

The worker claims the offer, receives an immutable execution graph, downloads
only graph artifacts, starts the B09 CPU runner and renews its lease. Result
reservation IDs and artifact IDs are server generated. A runner finalized
manifest must be uploaded and bound before completion can recognize a `Result`.
`SUCCEEDED` and resource release are separate: allocation accounting remains
held until a matching `NoContainerProof` or exact `ContainerStoppedProof` is
committed by cleanup. Duplicate callback IDs replay their durable response.

For development Compose, set `NEXA_WORKER_STATE_ROOT` to an existing durable
absolute host directory visible at the same path inside the worker and to the
Docker daemon. This is required for the runner's exact input/launch-spec bind
mounts; the runner receives only the bound input and control directory, never
the worker credential or Docker socket. Set a verified `NEXA_CPU_IMAGE_REF` of
the form `registry/name@sha256:<digest>`. Then `docker compose up db api
coordinator worker caddy` uses the coordinator image without a Docker socket. API and worker logs must
not be used as correctness evidence; inspect job events, authority, allocations,
ledger segments and result metadata in PostgreSQL.

B11 implements the CPU vertical slice and focused fencing/cleanup tests. GPU,
checkpoint restore, cancellation/reaper automation, fairness scale benchmarks,
bare-Linux portability and independent Task Review remain later gates.
