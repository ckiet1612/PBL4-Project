"""B17 getJobProgress is registered with the contract shape without a database."""

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from nexa.api.routes_jobs import router as jobs_router
from nexa.api.schemas import ProgressRecord

_JOB = "01890a5d-ac96-7000-8000-000000000001"
_ATTEMPT = "01890a5d-ac96-7000-8000-000000000002"


def test_get_job_progress_operation_shape():
    app = FastAPI()
    app.include_router(jobs_router)
    document = app.openapi()
    operation = document["paths"]["/v1/jobs/{job_id}/progress"]["get"]
    assert operation["operationId"] == "getJobProgress"
    assert operation["security"] == [{"browserCookie": []}, {"cliBearer": []}]
    parameters = {item["name"]: item for item in operation["parameters"]}
    assert set(parameters) == {"job_id", "X-Nexa-Tenant-Id"}
    assert parameters["X-Nexa-Tenant-Id"]["in"] == "header"
    contract = {"200", "400", "401", "403", "404", "500", "503"}
    assert set(operation["responses"]) >= contract
    # FastAPI documents its own 422 on every parameterised read; parity with getJobResult.
    result = document["paths"]["/v1/jobs/{job_id}/result"]["get"]
    assert set(operation["responses"]) == set(result["responses"])
    schemas = document["components"]["schemas"]
    absent = schemas["ProgressAbsent"]
    present = schemas["ProgressPresent"]
    assert absent["additionalProperties"] is False
    assert set(absent["required"]) == {"job_id", "available"}
    assert present["additionalProperties"] is False
    assert set(present["required"]) == {
        "job_id",
        "available",
        "attempt_id",
        "progress_sequence",
        "snapshot",
        "restore_checkpoint_id",
        "reported_at",
    }


def _present(**overrides):
    body = {
        "job_id": _JOB,
        "available": True,
        "attempt_id": _ATTEMPT,
        "progress_sequence": 1,
        "snapshot": {"fraction": 0.5, "step": 1, "epoch": None, "item_cursor": None},
        "restore_checkpoint_id": None,
        "reported_at": "2026-09-30T00:00:00.000Z",
    }
    body.update(overrides)
    return body


def test_progress_record_is_a_closed_present_absent_union():
    absent = ProgressRecord.model_validate({"job_id": _JOB, "available": False})
    assert absent.root.available is False
    assert ProgressRecord.model_validate(_present()).root.progress_sequence == 1
    invalid = [
        {"job_id": _JOB, "available": False, "attempt_id": _ATTEMPT},
        {"job_id": _JOB, "available": True},
        _present(progress_sequence=0),
        _present(snapshot={"fraction": 1.5, "step": None, "epoch": None, "item_cursor": None}),
        _present(extra=1),
    ]
    for body in invalid:
        with pytest.raises(ValidationError):
            ProgressRecord.model_validate(body)
