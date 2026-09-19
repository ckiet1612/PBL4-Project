import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.simulator.model import (  # noqa: E402
    JobSpec,
    ResourceVector,
    SimulationConfig,
    TenantSpec,
)
from benchmarks.simulator.trace import FairnessWindow, MaterializedTrace  # noqa: E402


def make_trace(
    jobs: tuple[JobSpec, ...],
    *,
    capacity: ResourceVector | None = None,
    tenants: tuple[TenantSpec, ...] | None = None,
    gpu_uuids: tuple[str, ...] = (),
    fairness_window: FairnessWindow | None = None,
) -> MaterializedTrace:
    configured_capacity = capacity or ResourceVector(2_000, 4_096, 0)
    configured_tenants = tenants or (
        TenantSpec("tenant-a", 1, configured_capacity, 4, 0),
        TenantSpec("tenant-b", 1, configured_capacity, 4, 1),
    )
    return MaterializedTrace(
        version=1,
        trace_id="test-trace",
        seed=1,
        config=SimulationConfig(configured_capacity, gpu_uuids, configured_tenants),
        fairness_window=fairness_window
        or FairnessWindow(
            0,
            max((job.arrival_ms + job.duration_ms for job in jobs), default=1),
            tuple(tenant.tenant_id for tenant in configured_tenants),
        ),
        jobs=jobs,
        trace_checksum="sha256:" + "1" * 64,
        materialized_checksum="sha256:" + "2" * 64,
    )
