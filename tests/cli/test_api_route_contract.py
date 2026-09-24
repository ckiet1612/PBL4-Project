from types import SimpleNamespace

from nexa.api.app import create_app

SUPPORTED_CLI_ROUTES = {
    ("GET", "/v1/tokens"),
    ("POST", "/v1/tokens"),
    ("DELETE", "/v1/tokens/{token_id}"),
    ("GET", "/v1/artifacts"),
    ("POST", "/v1/artifacts"),
    ("GET", "/v1/artifacts/{artifact_id}"),
    ("GET", "/v1/artifacts/{artifact_id}/content"),
    ("GET", "/v1/jobs"),
    ("POST", "/v1/jobs"),
    ("GET", "/v1/jobs/{job_id}"),
    ("GET", "/v1/sessions/{session_id}"),
    ("GET", "/v1/jobs/{job_id}/events"),
    ("GET", "/v1/jobs/{job_id}/result"),
    ("GET", "/v1/admin/tenants"),
    ("POST", "/v1/admin/tenants"),
    ("GET", "/v1/admin/tenants/{tenant_id}"),
    ("PATCH", "/v1/admin/tenants/{tenant_id}"),
    ("GET", "/v1/admin/users"),
    ("POST", "/v1/admin/users"),
    ("GET", "/v1/admin/users/{user_id}"),
    ("PATCH", "/v1/admin/users/{user_id}"),
    ("GET", "/v1/admin/tenants/{tenant_id}/memberships"),
    ("POST", "/v1/admin/tenants/{tenant_id}/memberships"),
    ("DELETE", "/v1/admin/tenants/{tenant_id}/memberships/{user_id}"),
    ("GET", "/v1/admin/policy"),
    ("PATCH", "/v1/admin/policy"),
    ("GET", "/v1/admin/tenants/{tenant_id}/policy"),
    ("PATCH", "/v1/admin/tenants/{tenant_id}/policy"),
    ("GET", "/v1/admin/audit"),
}


EXCLUDED_CLI_ROUTES = {
    "/v1/templates",
    "/v1/jobs/{job_id}/attempts",
    "/v1/jobs/{job_id}/checkpoints",
    "/v1/jobs/{job_id}/logs",
    "/v1/jobs/{job_id}/progress",
    "/v1/admin/jobs",
    "/v1/admin/workers",
    "/v1/admin/allocations",
    "/v1/admin/fairness",
    "/v1/admin/recovery-events",
}


def test_product_cli_routes_are_registered_in_the_fastapi_app() -> None:
    schema = create_app(SimpleNamespace()).openapi()
    registered = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
        if method.lower() in {"get", "post", "patch", "put", "delete"}
    }

    assert registered >= SUPPORTED_CLI_ROUTES


def test_unwired_backend_surfaces_are_excluded_from_product_cli_contract() -> None:
    from nexa.cli.app import app

    schema = create_app(SimpleNamespace()).openapi()
    registered_paths = set(schema["paths"])
    assert EXCLUDED_CLI_ROUTES.isdisjoint(registered_paths)

    root_help = app.registered_groups
    group_names = {group.name for group in root_help}
    assert group_names == {"config", "token", "artifact", "job", "admin"}
