import pytest

from tests.api.test_http_contract import _client

pytestmark = pytest.mark.postgres


def test_b08_operation_ids_and_openapi_contract_are_registered(migrated_postgres_engine, tmp_path):
    with _client(migrated_postgres_engine, tmp_path) as client:
        paths = client.app.openapi()["paths"]
        operation_ids = {
            operation["operationId"]
            for path in paths.values()
            for operation in path.values()
            if isinstance(operation, dict) and "operationId" in operation
        }
        assert {
            "submitJob",
            "listJobs",
            "getJob",
            "getLogicalSession",
            "listJobEvents",
        } <= operation_ids
        submit = paths["/v1/jobs"]["post"]
        assert submit["security"] == [
            {"browserCookie": [], "csrfToken": []},
            {"cliBearer": []},
        ]
        for path, method in (
            ("/v1/jobs", "get"),
            ("/v1/jobs/{job_id}", "get"),
            ("/v1/sessions/{session_id}", "get"),
            ("/v1/jobs/{job_id}/events", "get"),
        ):
            assert paths[path][method]["security"] == [
                {"browserCookie": []},
                {"cliBearer": []},
            ]
        assert submit["responses"]["202"]["headers"] == {
            "Location": {"schema": {"type": "string", "format": "uri-reference"}},
            "ETag": {"$ref": "#/components/headers/ETag"},
        }
        parameter_names = {parameter["name"] for parameter in submit["parameters"]}
        assert {"Idempotency-Key", "X-Nexa-Tenant-Id"} <= parameter_names
        schemes = client.app.openapi()["components"]["securitySchemes"]
        assert schemes["browserCookie"] == {
            "type": "apiKey",
            "in": "cookie",
            "name": "nexa_session",
        }
        assert schemes["csrfToken"] == {
            "type": "apiKey",
            "in": "header",
            "name": "X-CSRF-Token",
        }
        assert schemes["cliBearer"]["type"] == "http"
        assert schemes["cliBearer"]["scheme"] == "bearer"
        assert schemes["cliBearer"]["bearerFormat"] == "opaque-cli-token"
