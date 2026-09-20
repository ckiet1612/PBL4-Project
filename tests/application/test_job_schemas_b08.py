from uuid import UUID

import pytest
from pydantic import ValidationError

from nexa.api.schemas import (
    CpuJobSpec,
    Job,
    JobSubmitRequest,
    LogicalSession,
    ResourceCapacityVector,
    ResourceRequestVector,
)


def _spec() -> dict:
    return {
        "template_id": "cpu-iterative",
        "template_version": 1,
        "input_artifact_id": "018f0d60-7b6a-7a21-9d82-1aa39c4f30b7",
        "resources": {"cpu_millis": 1000, "memory_bytes": 536870912, "gpu_count": 0},
        "priority": 1,
        "runtime_limit_seconds": 120,
        "checkpoint_interval_seconds": 30,
        "parameters": {"iterations": 100000, "seed": 7, "modulus": 1000000007},
    }


def test_job_submit_request_accepts_cpu_discriminator_and_rejects_unknown_fields() -> None:
    request = JobSubmitRequest(spec=_spec())
    assert isinstance(request.spec, CpuJobSpec)
    assert request.spec.template_id == "cpu-iterative"

    with pytest.raises(ValidationError):
        JobSubmitRequest(spec={**_spec(), "unexpected": True})

    missing_required = _spec()
    missing_required.pop("priority")
    with pytest.raises(ValidationError):
        JobSubmitRequest(spec=missing_required)


def test_job_submit_request_rejects_coerced_numeric_json_values() -> None:
    payload = _spec()
    payload["template_version"] = "1"
    payload["resources"] = {**payload["resources"], "cpu_millis": "1000"}

    with pytest.raises(ValidationError):
        JobSubmitRequest(spec=payload)


def test_job_wire_models_require_uuid7_and_initial_state_fields() -> None:
    with pytest.raises(ValidationError):
        ResourceRequestVector(cpu_millis=0, memory_bytes=1, gpu_count=0)

    job = Job.model_validate(
        {
            "job_id": "018f0d60-7b6a-7a21-9d82-1aa39c4f30b7",
            "tenant_id": "018f0d60-7b6a-7a21-9d82-1aa39c4f30b8",
            "user_id": "018f0d60-7b6a-7a21-9d82-1aa39c4f30b9",
            "session_id": "018f0d60-7b6a-7a21-9d82-1aa39c4f30ba",
            "state": "QUEUED",
            "desired_state": "RUNNING",
            "waiting_reason": "waiting_for_worker",
            "spec": _spec(),
            "spec_checksum": "sha256:" + "a" * 64,
            "job_fence": 0,
            "retry_count": 0,
            "max_retries": 2,
            "retry_of_job_id": None,
            "version": 1,
            "event_sequence": 1,
            "created_at": "2026-09-20T00:00:00.000Z",
            "updated_at": "2026-09-20T00:00:00.000Z",
        }
    )
    assert isinstance(job.job_id, UUID)
    session = LogicalSession.model_validate(
        {
            "session_id": str(job.session_id),
            "job_id": str(job.job_id),
            "tenant_id": str(job.tenant_id),
            "derived_state": "QUEUED",
            "created_at": "2026-09-20T00:00:00.000Z",
        }
    )
    assert session.derived_state == "QUEUED"


def test_resource_capacity_keeps_frozen_multi_gpu_bound() -> None:
    capacity = ResourceCapacityVector(cpu_millis=0, memory_bytes=0, gpu_count=64)
    assert capacity.gpu_count == 64

    with pytest.raises(ValidationError):
        ResourceCapacityVector(cpu_millis=0, memory_bytes=0, gpu_count=65)
