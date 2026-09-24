"""REST-only system administrator commands.

The command layer deliberately contains no authorization or state logic.  The API
enforces ``admin:read``/``admin:write`` and the SYSTEM_ADMIN grant for every
operation below.
"""

from __future__ import annotations

import json
import math
import os
import sys
import warnings
from dataclasses import dataclass
from getpass import GetPassWarning, getpass
from pathlib import Path
from typing import Annotated

import typer
from typer import Context

from ..client import NexaClient
from ..config import ConfigStore, resolve_context, resolve_token
from ..errors import ApiError, CliError, OutputMode, render_error
from ..idempotency import new_idempotency_key
from ..output import emit_success

MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    query: tuple[str, ...] = ()
    body_fields: tuple[str, ...] = ()
    requires_if_match: bool = False


OPS = {
    "job_list": Operation(
        "GET",
        "/v1/admin/jobs",
        ("cursor", "page_size", "tenant_id", "user_id", "state", "waiting_reason", "created_after"),
    ),
    "job_get": Operation("GET", "/v1/admin/jobs/{job_id}"),
    "global_policy_get": Operation("GET", "/v1/admin/policy"),
    "global_policy_update": Operation(
        "PATCH",
        "/v1/admin/policy",
        body_fields=("global_outstanding_limit", "operational_mode"),
        requires_if_match=True,
    ),
    "tenant_list": Operation("GET", "/v1/admin/tenants", ("cursor", "page_size")),
    "tenant_create": Operation("POST", "/v1/admin/tenants", body_fields=("slug", "display_name")),
    "tenant_get": Operation("GET", "/v1/admin/tenants/{tenant_id}"),
    "tenant_update": Operation(
        "PATCH",
        "/v1/admin/tenants/{tenant_id}",
        body_fields=("display_name", "enabled"),
        requires_if_match=True,
    ),
    "user_list": Operation("GET", "/v1/admin/users", ("cursor", "page_size")),
    "user_create": Operation(
        "POST",
        "/v1/admin/users",
        body_fields=("username", "display_name", "password", "system_roles"),
    ),
    "user_get": Operation("GET", "/v1/admin/users/{user_id}"),
    "user_update": Operation(
        "PATCH",
        "/v1/admin/users/{user_id}",
        body_fields=("display_name", "password", "enabled", "system_roles"),
        requires_if_match=True,
    ),
    "membership_list": Operation(
        "GET", "/v1/admin/tenants/{tenant_id}/memberships", ("cursor", "page_size")
    ),
    "membership_upsert": Operation(
        "POST",
        "/v1/admin/tenants/{tenant_id}/memberships",
        body_fields=("user_id", "role"),
        requires_if_match=True,
    ),
    "membership_delete": Operation(
        "DELETE", "/v1/admin/tenants/{tenant_id}/memberships/{user_id}", requires_if_match=True
    ),
    "tenant_policy_get": Operation("GET", "/v1/admin/tenants/{tenant_id}/policy"),
    "tenant_policy_update": Operation(
        "PATCH",
        "/v1/admin/tenants/{tenant_id}/policy",
        body_fields=(
            "weight",
            "outstanding_limit",
            "user_outstanding_limit",
            "concurrent_attempt_limit",
            "user_concurrent_attempt_limit",
            "resource_limit",
            "submit_rate_per_second",
            "submit_burst",
            "user_submit_rate_per_second",
            "user_submit_burst",
        ),
        requires_if_match=True,
    ),
    "worker_list": Operation("GET", "/v1/admin/workers", ("cursor", "page_size")),
    "worker_get": Operation("GET", "/v1/admin/workers/{worker_id}"),
    "worker_drain": Operation(
        "POST",
        "/v1/admin/workers/{worker_id}/drain",
        body_fields=("reason",),
        requires_if_match=True,
    ),
    "worker_disable": Operation(
        "POST",
        "/v1/admin/workers/{worker_id}/disable",
        body_fields=("reason",),
        requires_if_match=True,
    ),
    "worker_enable": Operation(
        "POST",
        "/v1/admin/workers/{worker_id}/enable",
        body_fields=("reason",),
        requires_if_match=True,
    ),
    "allocation_list": Operation("GET", "/v1/admin/allocations", ("cursor", "page_size", "state")),
    "fairness_query": Operation(
        "GET", "/v1/admin/fairness", ("from", "to", "bucket_seconds", "tenant_id")
    ),
    "recovery_list": Operation(
        "GET", "/v1/admin/recovery-events", ("cursor", "page_size", "from", "to")
    ),
    "audit_list": Operation(
        "GET", "/v1/admin/audit", ("cursor", "page_size", "from", "to", "action")
    ),
}


def _state(ctx: Context) -> dict:
    return ctx.find_root().obj or {}


def _client(ctx: Context) -> NexaClient:
    state = _state(ctx)
    store = ConfigStore()
    resolved = resolve_context(
        store.load(),
        endpoint=state.get("endpoint"),
        profile=state.get("profile"),
        tenant_id=state.get("tenant_id"),
        environ=state.get("environ", os.environ),
    )
    return NexaClient(
        resolved.endpoint,
        token=resolve_token(
            store,
            resolved.profile,
            environ=state.get("environ", os.environ),
            stdin=sys.stdin if state.get("token_stdin") else None,
        ),
        tenant_id=resolved.tenant_id,
    )


def _error(ctx: Context, exc: CliError) -> None:
    render_error(exc, _state(ctx).get("output", OutputMode.HUMAN))
    raise typer.Exit(code=exc.exit_code)


def _page(ctx: Context, cursor: str | None, page_size: int) -> dict[str, object]:
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        _error(ctx, CliError("page size must be between 1 and 100", exit_code=2))
    query: dict[str, object] = {"page_size": page_size}
    if cursor is not None:
        query["cursor"] = cursor
    return query


def _body_file(path: Path, allowed: set[str]) -> dict[str, object]:
    try:
        value = _parse_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CliError("request body JSON is invalid", exit_code=2) from exc
    if not isinstance(value, dict):
        raise CliError("request body JSON must be an object", exit_code=2)
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise CliError(f"unknown field: {unknown[0]}", exit_code=2)
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number: {value}")
    return parsed


def _parse_json(content: str) -> object:
    return json.loads(
        content,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_finite,
        parse_float=_parse_finite_float,
    )


def _body(
    *,
    body_file: Path | None,
    allowed: set[str],
    values: dict[str, object],
    required: set[str] | None = None,
) -> dict[str, object]:
    required = required or set()
    if body_file is not None:
        if any(value is not None for value in values.values()):
            raise CliError("body-file cannot be combined with body options", exit_code=2)
        result = _body_file(body_file, allowed)
    else:
        result = {key: value for key, value in values.items() if value is not None}
    missing = sorted(required - set(result))
    if missing:
        raise CliError(f"missing required field: {missing[0]}", exit_code=2)
    return result


def _read_password(*, from_stdin: bool) -> str:
    if from_stdin:
        try:
            password = sys.stdin.readline().rstrip("\r\n")
        except OSError as exc:
            raise CliError("unable to read password from stdin", exit_code=2) from exc
    else:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                password = getpass("Password: ")
                confirmation = getpass("Confirm password: ")
        except (EOFError, GetPassWarning, KeyboardInterrupt, OSError) as exc:
            raise CliError("unable to read password securely", exit_code=2) from exc
        if password != confirmation:
            raise CliError("passwords do not match", exit_code=2)
    if not password:
        raise CliError("password cannot be empty", exit_code=2)
    return password


def _payload(response) -> object:
    if not isinstance(response.body, dict):
        payload = {} if response.body is None else {"data": response.body}
    else:
        payload = dict(response.body)
    for key, header in (("etag", "etag"), ("location", "location"), ("request_id", "x-request-id")):
        value = response.headers.get(header)
        if value is not None:
            payload[key] = value
    return payload


def _request(
    ctx: Context,
    operation: str,
    *,
    path_args: dict[str, str] | None = None,
    query: dict[str, object] | None = None,
    body: dict[str, object] | None = None,
    idempotency_key: str | None = None,
    if_match: str | None = None,
) -> None:
    op = OPS[operation]
    path = op.path.format(**(path_args or {}))
    if op.requires_if_match and if_match is None:
        _error(ctx, CliError("--if-match is required for this operation", exit_code=2))
    headers = {"If-Match": if_match} if if_match is not None else None
    mutation = op.method in {"POST", "PATCH", "DELETE"}
    client = None
    try:
        client = _client(ctx)
        response = client.request_json(
            op.method,
            path,
            query=query,
            json_body=body,
            headers=headers,
            mutation=mutation,
            idempotency_key=(idempotency_key or new_idempotency_key()) if mutation else None,
        )
        emit_success(_payload(response), mode=_state(ctx).get("output", OutputMode.HUMAN))
    except ApiError as exc:
        _error(ctx, exc)
    except CliError as exc:
        _error(ctx, exc)
    finally:
        if client is not None:
            client.close()


def _register_entity_groups(group: typer.Typer) -> None:
    tenant = typer.Typer(no_args_is_help=True)
    user = typer.Typer(no_args_is_help=True)
    job = typer.Typer(no_args_is_help=True)
    membership = typer.Typer(no_args_is_help=True)
    policy = typer.Typer(no_args_is_help=True)
    tenant_policy = typer.Typer(no_args_is_help=True)
    worker = typer.Typer(no_args_is_help=True)
    allocation = typer.Typer(no_args_is_help=True)
    fairness = typer.Typer(no_args_is_help=True)
    recovery = typer.Typer(no_args_is_help=True)
    audit = typer.Typer(no_args_is_help=True)
    # Contract operations without a FastAPI route stay unregistered until their
    # backend service and API integration evidence exist.
    for sub, name in (
        (tenant, "tenant"),
        (user, "user"),
        (membership, "membership"),
        (policy, "policy"),
        (tenant_policy, "tenant-policy"),
        (audit, "audit"),
    ):
        group.add_typer(sub, name=name)

    @job.command("list")
    def list_jobs(
        ctx: Context,
        cursor: Annotated[str | None, typer.Option("--cursor")] = None,
        page_size: Annotated[int, typer.Option("--page-size")] = 50,
        tenant_id: Annotated[str | None, typer.Option("--tenant-id")] = None,
        user_id: Annotated[str | None, typer.Option("--user-id")] = None,
        state: Annotated[str | None, typer.Option("--state")] = None,
        waiting_reason: Annotated[str | None, typer.Option("--waiting-reason")] = None,
        created_after: Annotated[str | None, typer.Option("--created-after")] = None,
    ) -> None:
        query = _page(ctx, cursor, page_size)
        query.update(
            {
                k: v
                for k, v in {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "state": state,
                    "waiting_reason": waiting_reason,
                    "created_after": created_after,
                }.items()
                if v is not None
            }
        )
        _request(ctx, "job_list", query=query)

    @job.command("get")
    def get_job(ctx: Context, job_id: Annotated[str, typer.Argument()]) -> None:
        _request(ctx, "job_get", path_args={"job_id": job_id})

    @tenant.command("list")
    def list_tenants(
        ctx: Context,
        cursor: Annotated[str | None, typer.Option("--cursor")] = None,
        page_size: Annotated[int, typer.Option("--page-size")] = 50,
    ) -> None:
        _request(ctx, "tenant_list", query=_page(ctx, cursor, page_size))

    @tenant.command("create")
    def create_tenant(
        ctx: Context,
        slug: Annotated[str | None, typer.Option("--slug")] = None,
        display_name: Annotated[str | None, typer.Option("--display-name")] = None,
        body_file: Annotated[
            Path | None, typer.Option("--body-file", exists=True, readable=True)
        ] = None,
        idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    ) -> None:
        try:
            body = _body(
                body_file=body_file,
                allowed={"slug", "display_name"},
                values={"slug": slug, "display_name": display_name},
                required={"slug", "display_name"},
            )
        except CliError as exc:
            _error(ctx, exc)
        _request(ctx, "tenant_create", body=body, idempotency_key=idempotency_key)

    @tenant.command("get")
    def get_tenant(ctx: Context, tenant_id: Annotated[str, typer.Argument()]) -> None:
        _request(ctx, "tenant_get", path_args={"tenant_id": tenant_id})

    @tenant.command("update")
    def update_tenant(
        ctx: Context,
        tenant_id: Annotated[str, typer.Argument()],
        display_name: Annotated[str | None, typer.Option("--display-name")] = None,
        enabled: Annotated[bool | None, typer.Option("--enabled/--disabled")] = None,
        body_file: Annotated[
            Path | None, typer.Option("--body-file", exists=True, readable=True)
        ] = None,
        if_match: Annotated[str | None, typer.Option("--if-match")] = None,
        idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    ) -> None:
        try:
            body = _body(
                body_file=body_file,
                allowed={"display_name", "enabled"},
                values={"display_name": display_name, "enabled": enabled},
            )
        except CliError as exc:
            _error(ctx, exc)
        _request(
            ctx,
            "tenant_update",
            path_args={"tenant_id": tenant_id},
            body=body,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @user.command("list")
    def list_users(
        ctx: Context,
        cursor: Annotated[str | None, typer.Option("--cursor")] = None,
        page_size: Annotated[int, typer.Option("--page-size")] = 50,
    ) -> None:
        _request(ctx, "user_list", query=_page(ctx, cursor, page_size))

    @user.command("create")
    def create_user(
        ctx: Context,
        username: Annotated[str | None, typer.Option("--username")] = None,
        display_name: Annotated[str | None, typer.Option("--display-name")] = None,
        password_stdin: Annotated[
            bool, typer.Option("--password-stdin", help="Read the password from stdin.")
        ] = False,
        system_roles: Annotated[list[str] | None, typer.Option("--system-role")] = None,
        body_file: Annotated[
            Path | None, typer.Option("--body-file", exists=True, readable=True)
        ] = None,
        idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    ) -> None:
        try:
            if body_file is not None and password_stdin:
                raise CliError("body-file cannot be combined with --password-stdin", exit_code=2)
            password = None if body_file is not None else _read_password(from_stdin=password_stdin)
            if body_file is None and system_roles is None:
                system_roles = []
            body = _body(
                body_file=body_file,
                allowed={"username", "display_name", "password", "system_roles"},
                values={
                    "username": username,
                    "display_name": display_name,
                    "password": password,
                    "system_roles": system_roles,
                },
                required={"username", "display_name", "password", "system_roles"},
            )
        except CliError as exc:
            _error(ctx, exc)
        _request(ctx, "user_create", body=body, idempotency_key=idempotency_key)

    @user.command("get")
    def get_user(ctx: Context, user_id: Annotated[str, typer.Argument()]) -> None:
        _request(ctx, "user_get", path_args={"user_id": user_id})

    @user.command("update")
    def update_user(
        ctx: Context,
        user_id: Annotated[str, typer.Argument()],
        display_name: Annotated[str | None, typer.Option("--display-name")] = None,
        password_stdin: Annotated[
            bool, typer.Option("--password-stdin", help="Read the password from stdin.")
        ] = False,
        enabled: Annotated[bool | None, typer.Option("--enabled/--disabled")] = None,
        system_roles: Annotated[list[str] | None, typer.Option("--system-role")] = None,
        body_file: Annotated[
            Path | None, typer.Option("--body-file", exists=True, readable=True)
        ] = None,
        if_match: Annotated[str | None, typer.Option("--if-match")] = None,
        idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    ) -> None:
        try:
            if body_file is not None and password_stdin:
                raise CliError("body-file cannot be combined with --password-stdin", exit_code=2)
            password = _read_password(from_stdin=True) if password_stdin else None
            body = _body(
                body_file=body_file,
                allowed={"display_name", "password", "enabled", "system_roles"},
                values={
                    "display_name": display_name,
                    "password": password,
                    "enabled": enabled,
                    "system_roles": system_roles,
                },
            )
        except CliError as exc:
            _error(ctx, exc)
        _request(
            ctx,
            "user_update",
            path_args={"user_id": user_id},
            body=body,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @membership.command("list")
    def list_memberships(
        ctx: Context,
        tenant_id: Annotated[str, typer.Argument()],
        cursor: Annotated[str | None, typer.Option("--cursor")] = None,
        page_size: Annotated[int, typer.Option("--page-size")] = 50,
    ) -> None:
        _request(
            ctx,
            "membership_list",
            path_args={"tenant_id": tenant_id},
            query=_page(ctx, cursor, page_size),
        )

    @membership.command("upsert")
    def upsert_membership(
        ctx: Context,
        tenant_id: Annotated[str, typer.Argument()],
        user_id: Annotated[str | None, typer.Option("--user-id")] = None,
        role: Annotated[str | None, typer.Option("--role")] = None,
        body_file: Annotated[
            Path | None, typer.Option("--body-file", exists=True, readable=True)
        ] = None,
        if_match: Annotated[str | None, typer.Option("--if-match")] = None,
        idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    ) -> None:
        try:
            body = _body(
                body_file=body_file,
                allowed={"user_id", "role"},
                values={"user_id": user_id, "role": role},
                required={"user_id", "role"},
            )
        except CliError as exc:
            _error(ctx, exc)
        _request(
            ctx,
            "membership_upsert",
            path_args={"tenant_id": tenant_id},
            body=body,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @membership.command("delete")
    def delete_membership(
        ctx: Context,
        tenant_id: Annotated[str, typer.Argument()],
        user_id: Annotated[str, typer.Argument()],
        if_match: Annotated[str | None, typer.Option("--if-match")] = None,
        idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    ) -> None:
        _request(
            ctx,
            "membership_delete",
            path_args={"tenant_id": tenant_id, "user_id": user_id},
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @policy.command("get")
    def get_policy(ctx: Context) -> None:
        _request(ctx, "global_policy_get")

    @policy.command("update")
    def update_policy(
        ctx: Context,
        global_outstanding_limit: Annotated[
            int | None, typer.Option("--global-outstanding-limit")
        ] = None,
        operational_mode: Annotated[str | None, typer.Option("--operational-mode")] = None,
        body_file: Annotated[
            Path | None, typer.Option("--body-file", exists=True, readable=True)
        ] = None,
        if_match: Annotated[str | None, typer.Option("--if-match")] = None,
        idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    ) -> None:
        try:
            body = _body(
                body_file=body_file,
                allowed={"global_outstanding_limit", "operational_mode"},
                values={
                    "global_outstanding_limit": global_outstanding_limit,
                    "operational_mode": operational_mode,
                },
            )
        except CliError as exc:
            _error(ctx, exc)
        _request(
            ctx,
            "global_policy_update",
            body=body,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @tenant_policy.command("get")
    def get_tenant_policy(ctx: Context, tenant_id: Annotated[str, typer.Argument()]) -> None:
        _request(ctx, "tenant_policy_get", path_args={"tenant_id": tenant_id})

    @tenant_policy.command("update")
    def update_tenant_policy(
        ctx: Context,
        tenant_id: Annotated[str, typer.Argument()],
        weight: Annotated[float | None, typer.Option("--weight")] = None,
        outstanding_limit: Annotated[int | None, typer.Option("--outstanding-limit")] = None,
        user_outstanding_limit: Annotated[
            int | None, typer.Option("--user-outstanding-limit")
        ] = None,
        concurrent_attempt_limit: Annotated[
            int | None, typer.Option("--concurrent-attempt-limit")
        ] = None,
        user_concurrent_attempt_limit: Annotated[
            int | None, typer.Option("--user-concurrent-attempt-limit")
        ] = None,
        resource_limit: Annotated[str | None, typer.Option("--resource-limit")] = None,
        submit_rate_per_second: Annotated[
            float | None, typer.Option("--submit-rate-per-second")
        ] = None,
        submit_burst: Annotated[int | None, typer.Option("--submit-burst")] = None,
        user_submit_rate_per_second: Annotated[
            float | None, typer.Option("--user-submit-rate-per-second")
        ] = None,
        user_submit_burst: Annotated[int | None, typer.Option("--user-submit-burst")] = None,
        body_file: Annotated[
            Path | None, typer.Option("--body-file", exists=True, readable=True)
        ] = None,
        if_match: Annotated[str | None, typer.Option("--if-match")] = None,
        idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    ) -> None:
        try:
            resource = None
            if resource_limit is not None:
                try:
                    resource = _parse_json(resource_limit)
                except ValueError as exc:
                    raise CliError("resource-limit JSON is invalid", exit_code=2) from exc
                if not isinstance(resource, dict):
                    raise CliError("resource-limit JSON must be an object", exit_code=2)
            body = _body(
                body_file=body_file,
                allowed=set(OPS["tenant_policy_update"].body_fields),
                values={
                    "weight": weight,
                    "outstanding_limit": outstanding_limit,
                    "user_outstanding_limit": user_outstanding_limit,
                    "concurrent_attempt_limit": concurrent_attempt_limit,
                    "user_concurrent_attempt_limit": user_concurrent_attempt_limit,
                    "resource_limit": resource,
                    "submit_rate_per_second": submit_rate_per_second,
                    "submit_burst": submit_burst,
                    "user_submit_rate_per_second": user_submit_rate_per_second,
                    "user_submit_burst": user_submit_burst,
                },
            )
        except CliError as exc:
            _error(ctx, exc)
        _request(
            ctx,
            "tenant_policy_update",
            path_args={"tenant_id": tenant_id},
            body=body,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @worker.command("list")
    def list_workers(
        ctx: Context,
        cursor: Annotated[str | None, typer.Option("--cursor")] = None,
        page_size: Annotated[int, typer.Option("--page-size")] = 50,
    ) -> None:
        _request(ctx, "worker_list", query=_page(ctx, cursor, page_size))

    @worker.command("get")
    def get_worker(ctx: Context, worker_id: Annotated[str, typer.Argument()]) -> None:
        _request(ctx, "worker_get", path_args={"worker_id": worker_id})

    def worker_action(command: str, operation: str):
        @worker.command(command)
        def action(
            ctx: Context,
            worker_id: Annotated[str, typer.Argument()],
            reason: Annotated[str | None, typer.Option("--reason")] = None,
            body_file: Annotated[
                Path | None, typer.Option("--body-file", exists=True, readable=True)
            ] = None,
            if_match: Annotated[str | None, typer.Option("--if-match")] = None,
            idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
        ) -> None:
            try:
                body = _body(
                    body_file=body_file,
                    allowed={"reason"},
                    values={"reason": reason},
                    required={"reason"},
                )
            except CliError as exc:
                _error(ctx, exc)
            _request(
                ctx,
                operation,
                path_args={"worker_id": worker_id},
                body=body,
                if_match=if_match,
                idempotency_key=idempotency_key,
            )

    worker_action("drain", "worker_drain")
    worker_action("disable", "worker_disable")
    worker_action("enable", "worker_enable")

    @allocation.command("list")
    def list_allocations(
        ctx: Context,
        cursor: Annotated[str | None, typer.Option("--cursor")] = None,
        page_size: Annotated[int, typer.Option("--page-size")] = 50,
        state: Annotated[str | None, typer.Option("--state")] = None,
    ) -> None:
        query = _page(ctx, cursor, page_size)
        if state is not None:
            query["state"] = state
        _request(ctx, "allocation_list", query=query)

    @fairness.command("query")
    def query_fairness(
        ctx: Context,
        from_: Annotated[str, typer.Option("--from")],
        to: Annotated[str, typer.Option("--to")],
        bucket_seconds: Annotated[int, typer.Option("--bucket-seconds")],
        tenant_id: Annotated[str | None, typer.Option("--tenant-id")] = None,
    ) -> None:
        query = {"from": from_, "to": to, "bucket_seconds": bucket_seconds, "tenant_id": tenant_id}
        _request(ctx, "fairness_query", query={k: v for k, v in query.items() if v is not None})

    @recovery.command("list")
    def list_recovery_events(
        ctx: Context,
        from_: Annotated[str, typer.Option("--from")],
        to: Annotated[str, typer.Option("--to")],
        cursor: Annotated[str | None, typer.Option("--cursor")] = None,
        page_size: Annotated[int, typer.Option("--page-size")] = 50,
    ) -> None:
        query = _page(ctx, cursor, page_size)
        query.update({k: v for k, v in {"from": from_, "to": to}.items() if v is not None})
        _request(ctx, "recovery_list", query=query)

    @audit.command("list")
    def list_audit(
        ctx: Context,
        from_: Annotated[str, typer.Option("--from")],
        to: Annotated[str, typer.Option("--to")],
        cursor: Annotated[str | None, typer.Option("--cursor")] = None,
        page_size: Annotated[int, typer.Option("--page-size")] = 50,
        action: Annotated[str | None, typer.Option("--action")] = None,
    ) -> None:
        query = _page(ctx, cursor, page_size)
        query.update(
            {k: v for k, v in {"from": from_, "to": to, "action": action}.items() if v is not None}
        )
        _request(ctx, "audit_list", query=query)


def register(group: typer.Typer) -> None:
    _register_entity_groups(group)
