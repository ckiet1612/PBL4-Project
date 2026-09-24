"""B11 routes and response contracts are registered without a database startup."""

from fastapi import FastAPI

from nexa.api.routes_jobs import router as jobs_router
from nexa.api.routes_worker import router as worker_router


def test_b11_operation_matrix_and_closed_poll_offer_schema():
    app = FastAPI()
    app.include_router(jobs_router)
    app.include_router(worker_router)
    document = app.openapi()
    expected = {
        "getJobResult": ("/v1/jobs/{job_id}/result", "get"),
        "workerPollDispatch": ("/v1/workers/{worker_id}/poll", "post"),
        "workerClaimAttempt": ("/v1/attempts/{attempt_id}/claim", "post"),
        "workerStartAttempt": ("/v1/attempts/{attempt_id}/start", "post"),
        "workerReserveResult": ("/v1/attempts/{attempt_id}/result-reservations", "post"),
        "workerUploadAttemptArtifact": ("/v1/attempts/{attempt_id}/artifacts", "post"),
        "workerDownloadExecutionArtifact": (
            "/v1/attempts/{attempt_id}/execution-artifacts/{artifact_id}/content",
            "get",
        ),
        "workerCompleteAttempt": ("/v1/attempts/{attempt_id}/complete", "post"),
        "workerFailAttempt": ("/v1/attempts/{attempt_id}/fail", "post"),
        "workerReportCleanup": ("/v1/attempts/{attempt_id}/cleanup", "post"),
    }
    for operation_id, (path, method) in expected.items():
        assert document["paths"][path][method]["operationId"] == operation_id
    response = document["components"]["schemas"]["PollResponse"]
    assert response["properties"]["offer"] != {"type": "null"}
    assert "DispatchOffer" in document["components"]["schemas"]
    assert "ResultRecord" in document["components"]["schemas"]
