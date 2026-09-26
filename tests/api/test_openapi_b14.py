"""B14 checkpoint routes are registered with the contract shapes without a database."""

from fastapi import FastAPI

from nexa.api.routes_jobs import router as jobs_router
from nexa.api.routes_worker import router as worker_router


def test_b14_checkpoint_operations_and_page_schema():
    app = FastAPI()
    app.include_router(jobs_router)
    app.include_router(worker_router)
    document = app.openapi()
    expected = {
        "listJobCheckpoints": ("/v1/jobs/{job_id}/checkpoints", "get"),
        "workerReserveCheckpoint": ("/v1/attempts/{attempt_id}/checkpoint-reservations", "post"),
        "workerPublishCheckpoint": ("/v1/attempts/{attempt_id}/checkpoints", "post"),
    }
    for operation_id, (path, method) in expected.items():
        assert document["paths"][path][method]["operationId"] == operation_id
    listing = document["paths"]["/v1/jobs/{job_id}/checkpoints"]["get"]
    assert listing["security"] == [{"browserCookie": []}, {"cliBearer": []}]
    parameters = {item["name"]: item for item in listing["parameters"]}
    assert set(parameters) == {"job_id", "X-Nexa-Tenant-Id", "cursor", "page_size"}
    assert parameters["page_size"]["schema"]["maximum"] == 100
    schemas = document["components"]["schemas"]
    record = schemas["CheckpointRecord"]
    assert record["additionalProperties"] is False
    assert set(record["properties"]) == {
        "checkpoint_id",
        "job_id",
        "attempt_id",
        "sequence",
        "manifest_artifact_id",
        "manifest_checksum",
        "state",
        "created_at",
    }
    assert schemas["CheckpointPage"]["properties"]["items"]["maxItems"] == 100
