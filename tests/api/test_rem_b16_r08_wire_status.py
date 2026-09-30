"""Remediation B16-R08: malformed wire input answers 400; invalid values stay 422.

The contract status table (contracts.md) maps `400` to wire/cursor errors and `422` to
validation, infeasible and capability errors. Every OpenAPI operation with path, query or
body input declares 400. Before the fix the app-wide `RequestValidationError` handler
answered 422 when a path or query parameter had the wrong type, pattern or format. Such
parameters are validated before the endpoint runs, so no database is needed here. The
JSON body parser already answered 400 for malformed JSON, duplicate members and unknown
fields, and 422 for well-formed invalid values. Header errors are outside this finding's
scope (§5.A names path and query); they keep 422, as the application's own header checks
do.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from nexa.api.app import create_app

TENANT = {"X-Nexa-Tenant-Id": "0198a000-0000-7000-8000-000000000001"}


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(SimpleNamespace()), base_url="https://nexa.test")


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/v1/templates/Bad_Id", None),
        ("/v1/jobs/not-a-uuid", None),
        ("/v1/jobs/0198a000-0000-4000-8000-000000000001", None),
        ("/v1/jobs", {"page_size": "0"}),
        ("/v1/jobs", {"page_size": "101"}),
        ("/v1/jobs", {"page_size": "ten"}),
        ("/v1/jobs", {"cursor": "short"}),
        ("/v1/jobs", {"state": "NOT_A_STATE"}),
    ],
)
def test_a_malformed_path_or_query_parameter_is_400(client, path, params):
    response = client.get(path, headers=TENANT, params=params)
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "validation_failed"
    assert response.headers["X-Request-Id"] == body["request_id"]


def test_a_malformed_header_alone_keeps_422(client):
    response = client.get("/v1/templates/cpu-iterative", headers={"X-Nexa-Tenant-Id": "nope"})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"
