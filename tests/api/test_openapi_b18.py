"""B18 adminListJobs/adminGetJob/adminQueryFairness are registered with the contract shape."""

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from nexa.api.routes_admin import router as admin_router
from nexa.api.schemas import FairnessReport

_READ_SECURITY = [{"browserCookie": []}, {"cliBearer": []}]
_TENANT = "01890a5d-ac96-7000-8000-000000000001"


def _document():
    app = FastAPI()
    app.include_router(admin_router)
    return app.openapi()


def test_admin_job_operations_shape():
    document = _document()
    listing = document["paths"]["/v1/admin/jobs"]["get"]
    assert listing["operationId"] == "adminListJobs"
    assert listing["security"] == _READ_SECURITY
    parameters = {item["name"]: item for item in listing["parameters"]}
    assert set(parameters) == {
        "cursor",
        "page_size",
        "tenant_id",
        "user_id",
        "state",
        "waiting_reason",
        "created_after",
    }
    assert all(item["in"] == "query" for item in parameters.values())
    assert parameters["page_size"]["schema"]["maximum"] == 100
    assert parameters["page_size"]["schema"]["default"] == 50
    assert {"200", "400", "401", "403", "500", "503"} <= set(listing["responses"])
    assert listing["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/JobPage"
    }

    single = document["paths"]["/v1/admin/jobs/{job_id}"]["get"]
    assert single["operationId"] == "adminGetJob"
    assert single["security"] == _READ_SECURITY
    assert [item["name"] for item in single["parameters"]] == ["job_id"]
    assert {"200", "400", "401", "403", "404", "500", "503"} <= set(single["responses"])
    assert single["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Job"
    }


def test_admin_fairness_operation_shape_has_no_422():
    document = _document()
    operation = document["paths"]["/v1/admin/fairness"]["get"]
    assert operation["operationId"] == "adminQueryFairness"
    assert operation["security"] == _READ_SECURITY
    parameters = {item["name"]: item for item in operation["parameters"]}
    assert set(parameters) == {"from", "to", "bucket_seconds", "tenant_id"}
    assert all(parameters[name]["required"] for name in ("from", "to", "bucket_seconds"))
    assert parameters["tenant_id"].get("required", False) is False
    assert parameters["bucket_seconds"]["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 86400,
    }
    # Every parameter violation is a 400 (B18-R07): the operation documents no 422.
    assert set(operation["responses"]) == {"200", "400", "401", "403", "500", "503"}
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/FairnessReport"
    }
    schemas = document["components"]["schemas"]
    bucket = schemas["FairnessBucket"]
    assert bucket["additionalProperties"] is False
    assert set(bucket["required"]) == {
        "tenant_id",
        "start_at",
        "end_at",
        "weight",
        "dominant_resource_time_seconds",
        "normalized_service",
        "allocation_occupancy_seconds",
    }
    assert schemas["FairnessReport"]["properties"]["buckets"]["maxItems"] == 1000


def _bucket(**overrides):
    body = {
        "tenant_id": _TENANT,
        "start_at": "2026-09-01T00:00:00.000Z",
        "end_at": "2026-09-01T00:01:00.000Z",
        "weight": 1.0,
        "dominant_resource_time_seconds": 0.5,
        "normalized_service": 0.5,
        "allocation_occupancy_seconds": 1.0,
    }
    body.update(overrides)
    return body


def test_fairness_report_model_is_closed_and_bounded():
    report = {
        "from": "2026-09-01T00:00:00.000Z",
        "to": "2026-09-01T00:01:00.000Z",
        "bucket_seconds": 60,
        "buckets": [_bucket()],
    }
    assert FairnessReport.model_validate(report).bucket_seconds == 60
    for invalid in (
        {**report, "extra": 1},
        {**report, "buckets": [_bucket(weight=0)]},
        {**report, "buckets": [_bucket(normalized_service=-1)]},
        {**report, "buckets": [_bucket()] * 1001},
    ):
        with pytest.raises(ValidationError):
            FairnessReport.model_validate(invalid)
