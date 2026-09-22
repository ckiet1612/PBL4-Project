from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nexa.api.schemas import (
    AdoptRequest,
    Authority,
    ContainerIdentity,
    HeartbeatRequest,
    PollRequest,
    ProgressSnapshot,
    RenewBody,
    RenewWithoutProgressRequest,
    RenewWithProgressRequest,
    RuntimeCapability,
    WorkerIncarnationCreateRequest,
    WorkerInventory,
)

IDS = {
    "worker": "018f05c4-a922-7d0d-9f55-f9084a72d0f2",
    "incarnation": "018f05c4-a922-7d0d-9f55-f9084a72d0f3",
    "prior_incarnation": "018f05c4-a922-7d0d-9f55-f9084a72d0f4",
    "attempt": "018f05c4-a922-7d0d-9f55-f9084a72d0f5",
    "allocation": "018f05c4-a922-7d0d-9f55-f9084a72d0f6",
    "lease": "018f05c4-a922-7d0d-9f55-f9084a72d0f7",
    "nonce": "018f05c4-a922-7d0d-9f55-f9084a72d0f8",
}


def authority(*, incarnation: str | None = None) -> Authority:
    return Authority(
        worker_id=IDS["worker"],
        worker_incarnation_id=incarnation or IDS["incarnation"],
        attempt_id=IDS["attempt"],
        allocation_id=IDS["allocation"],
        lease_id=IDS["lease"],
        job_fence=3,
    )


def inventory() -> WorkerInventory:
    return WorkerInventory(
        architecture="linux/amd64",
        host_cpu_millis=8_000,
        host_memory_bytes=16 * 1024**3,
        allocatable={"cpu_millis": 6_000, "memory_bytes": 12 * 1024**3, "gpu_count": 0},
        runtime=RuntimeCapability(
            docker_version="28.4.0",
            oci_runtime="runc",
            oci_runtime_version="1.3.0",
            cgroups_version=2,
            kernel_release="6.8.0",
            seccomp_available=True,
        ),
        adapters=[{"adapter_id": "cpu.iterative", "adapter_version": "1.0.0"}],
        images=[
            {
                "image_digest": "sha256:" + "a" * 64,
                "architecture": "linux/amd64",
                "verified": True,
            }
        ],
        frameworks=[
            {
                "framework": "NEXA_CPU",
                "framework_version": "1.0.0",
                "device": "CPU",
                "cuda_runtime_version": None,
            }
        ],
        gpu_devices=[],
        discovered_at=datetime(2026, 9, 21, tzinfo=UTC),
    )


def test_worker_incarnation_and_poll_requests_are_closed_and_bounded() -> None:
    assert str(WorkerIncarnationCreateRequest(process_start_nonce=IDS["nonce"]).process_start_nonce)
    assert PollRequest(worker_incarnation_id=IDS["incarnation"], long_poll_seconds=30)
    with pytest.raises(ValidationError):
        PollRequest(worker_incarnation_id=IDS["incarnation"], long_poll_seconds=31)
    with pytest.raises(ValidationError):
        WorkerIncarnationCreateRequest(
            process_start_nonce=IDS["nonce"],
            client_selected_incarnation_id=IDS["incarnation"],
        )


def test_heartbeat_inventory_is_closed_and_observed_container_list_is_bounded() -> None:
    request = HeartbeatRequest(
        worker_incarnation_id=IDS["incarnation"],
        observed_health="STARTING",
        reconcile_complete=False,
        inventory=inventory(),
        observed_containers=[],
    )
    assert request.inventory.allocatable.cpu_millis == 6_000
    with pytest.raises(ValidationError):
        HeartbeatRequest(
            worker_incarnation_id=IDS["incarnation"],
            observed_health="READY",
            reconcile_complete=True,
            inventory={**inventory().model_dump(), "invented_capability": True},
            observed_containers=[],
        )
    container = {
        "container_id": "b" * 64,
        "runtime_identity_digest": "sha256:" + "c" * 64,
    }
    with pytest.raises(ValidationError):
        HeartbeatRequest(
            worker_incarnation_id=IDS["incarnation"],
            observed_health="READY",
            reconcile_complete=True,
            inventory=inventory(),
            observed_containers=[container] * 1001,
        )


def test_adopt_and_renew_requests_preserve_the_closed_authority_and_progress_union() -> None:
    container = ContainerIdentity(
        container_id="d" * 64,
        runtime_identity_digest="sha256:" + "e" * 64,
    )
    adopt = AdoptRequest(
        prior_authority=authority(incarnation=IDS["prior_incarnation"]),
        current_worker_incarnation_id=IDS["incarnation"],
        container=container,
    )
    assert adopt.prior_authority.attempt_id == authority().attempt_id
    assert RenewWithoutProgressRequest(authority=authority(), progress_sequence=0, progress=None)
    assert RenewWithProgressRequest(
        authority=authority(),
        progress_sequence=1,
        progress=ProgressSnapshot(fraction=0.25, step=1, epoch=None, item_cursor=None),
    )
    with pytest.raises(ValidationError):
        RenewWithoutProgressRequest(
            authority=authority(),
            progress_sequence=0,
            progress={"fraction": 0.0, "step": None, "epoch": None, "item_cursor": None},
        )
    with pytest.raises(ValidationError):
        RenewWithProgressRequest(authority=authority(), progress_sequence=1, progress=None)


def test_renew_wire_rejects_boolean_sequence_and_incomplete_progress_snapshot() -> None:
    with pytest.raises(ValidationError):
        RenewBody.model_validate(
            {
                "authority": authority().model_dump(mode="json"),
                "progress_sequence": False,
                "progress": None,
            }
        )

    with pytest.raises(ValidationError):
        RenewBody.model_validate(
            {
                "authority": authority().model_dump(mode="json"),
                "progress_sequence": 1,
                "progress": {"fraction": 0.5},
            }
        )
