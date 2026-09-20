from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from nexa.application.errors import ApplicationError
from nexa.application.job_service import JobService


def _service() -> JobService:
    service = JobService.__new__(JobService)
    service._cursor_key = b"s" * 32
    service.settings = SimpleNamespace(cursor_ttl_seconds=300)
    return service


@pytest.mark.parametrize(
    ("created_at", "row_id"),
    [
        ("2026-09-20T00:00:00", "018f0d60-7b6a-7a21-9d82-1aa39c4f30b7"),
        ("2026-09-20T00:00:00+00:00", "12345678-1234-4234-9234-123456789abc"),
    ],
)
def test_job_cursor_rejects_naive_timestamps_and_non_uuid7_ids(
    created_at: str, row_id: str
) -> None:
    service = _service()
    now = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    binding = {"actor_id": "actor", "tenant_id": "tenant", "operation_id": "listJobs"}
    cursor = service._cursor(now).encode(
        binding=binding,
        position={"created_at": created_at, "id": row_id},
    )

    with pytest.raises(ApplicationError) as raised:
        service._decode_cursor(cursor, binding=binding, now=now)

    assert raised.value.code == "invalid_cursor"


def test_job_cursor_rejects_expired_tokens() -> None:
    service = _service()
    issued_at = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    binding = {"actor_id": "actor", "tenant_id": "tenant", "operation_id": "listJobs"}
    cursor = service._cursor(issued_at).encode(
        binding=binding,
        position={
            "created_at": issued_at.isoformat(),
            "id": "018f0d60-7b6a-7a21-9d82-1aa39c4f30b7",
        },
    )

    with pytest.raises(ApplicationError) as raised:
        service._decode_cursor(cursor, binding=binding, now=issued_at + timedelta(seconds=301))

    assert raised.value.code == "invalid_cursor"


def test_template_parameter_schema_bounds_are_enforced() -> None:
    template = {
        "parameter_schema": [
            {
                "name": "iterations",
                "type": "INTEGER",
                "required": True,
                "minimum": 1,
                "maximum": 10,
                "default": None,
            }
        ]
    }

    with pytest.raises(ApplicationError) as raised:
        JobService._validate_parameter_schema(template, {"iterations": 11})

    assert raised.value.code == "infeasible_request"


def test_inventory_capability_matching_requires_closed_entries() -> None:
    row = {
        "architecture": "linux/amd64",
        "workload_capabilities": {
            "adapters": [{"adapter_id": "cpu.iterative", "adapter_version": "1.0.0"}],
            "images": [
                {
                    "image_digest": "sha256:" + "b" * 64,
                    "architecture": "linux/amd64",
                    "verified": True,
                }
            ],
            "frameworks": [{"framework": "NEXA_CPU", "device": "CPU"}],
        },
    }
    requirements = {
        "architectures": ["linux/amd64"],
        "adapter_id": "cpu.iterative",
        "adapter_version": "1.0.0",
        "image_digest": "sha256:" + "b" * 64,
        "device": "CPU",
    }

    assert JobService._inventory_supports(row, requirements)
    assert not JobService._inventory_supports(
        row, {**requirements, "image_digest": "sha256:" + "c" * 64}
    )


def test_inventory_requirements_reject_conflicting_template_binding() -> None:
    with pytest.raises(ApplicationError) as raised:
        JobService._inventory_requirements(
            {
                "adapter_id": "cpu.iterative",
                "adapter_version": "1.0.0",
                "image_digest": "sha256:" + "b" * 64,
                "capability_requirements": {
                    "adapter_id": "other.adapter",
                    "adapter_version": "1.0.0",
                    "image_digest": "sha256:" + "c" * 64,
                },
            }
        )

    assert raised.value.code == "infeasible_request"


def test_inventory_requirements_reject_incomplete_template_declaration() -> None:
    with pytest.raises(ApplicationError) as raised:
        JobService._inventory_requirements(
            {
                "adapter_id": "cpu.iterative",
                "adapter_version": "1.0.0",
                "image_digest": "sha256:" + "b" * 64,
                "capability_requirements": {},
            }
        )

    assert raised.value.code == "infeasible_request"


def test_inventory_requirements_reject_malformed_architecture_declaration() -> None:
    with pytest.raises(ApplicationError) as raised:
        JobService._inventory_requirements(
            {
                "adapter_id": "cpu.iterative",
                "adapter_version": "1.0.0",
                "image_digest": "sha256:" + "b" * 64,
                "capability_requirements": {
                    "architectures": "linux/amd64",
                    "adapter_id": "cpu.iterative",
                    "adapter_version": "1.0.0",
                    "image_digest": "sha256:" + "b" * 64,
                    "device": "CPU",
                    "framework": "NEXA_CPU",
                    "framework_version": "1.0.0",
                    "cuda_runtime_min": None,
                    "driver_min": None,
                    "compute_capability_min": None,
                },
            }
        )

    assert raised.value.code == "infeasible_request"


def test_job_list_rejects_naive_created_after_timestamp() -> None:
    with pytest.raises(ApplicationError) as raised:
        _service().list_jobs(
            None,
            tenant_id=None,
            page_size=50,
            cursor=None,
            state=None,
            template_id=None,
            created_after=datetime(2026, 9, 20),
        )

    assert raised.value.code == "validation_failed"


def test_ready_worker_requires_a_fresh_heartbeat() -> None:
    now = datetime(2026, 9, 20, tzinfo=UTC)

    assert JobService._worker_is_ready(
        {
            "health": "READY",
            "admin_state": "ENABLED",
            "last_heartbeat_at": now - timedelta(seconds=5),
        },
        now,
    )
    assert not JobService._worker_is_ready(
        {
            "health": "READY",
            "admin_state": "ENABLED",
            "last_heartbeat_at": now - timedelta(seconds=31),
        },
        now,
    )
    assert not JobService._worker_is_ready(
        {
            "health": "READY",
            "admin_state": "DRAINING",
            "last_heartbeat_at": now - timedelta(seconds=5),
        },
        now,
    )
