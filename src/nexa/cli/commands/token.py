from __future__ import annotations

import os
import sys
from typing import Annotated

import typer
from typer import Context

from ..client import NexaClient
from ..config import ConfigStore, resolve_context, resolve_token
from ..errors import ApiError, CliError, OutputMode, render_error
from ..idempotency import new_idempotency_key
from ..output import emit_success

SCOPES = (
    "jobs:read",
    "jobs:write",
    "artifacts:read",
    "artifacts:write",
    "tokens:write",
    "admin:read",
    "admin:write",
)


def _state(ctx: Context) -> dict:
    return ctx.find_root().obj or {}


def _client(ctx: Context) -> tuple[NexaClient, ConfigStore, object]:
    state = _state(ctx)
    store = ConfigStore()
    resolved = resolve_context(
        store.load(),
        endpoint=state.get("endpoint"),
        profile=state.get("profile"),
        tenant_id=state.get("tenant_id"),
        environ=state.get("environ", os.environ),
    )
    client = NexaClient(
        resolved.endpoint,
        token=resolve_token(
            store,
            resolved.profile,
            environ=state.get("environ", os.environ),
            stdin=sys.stdin if state.get("token_stdin") else None,
        ),
        tenant_id=resolved.tenant_id,
    )
    return client, store, resolved


def _error(ctx: Context, exc: CliError) -> None:
    render_error(exc, _state(ctx).get("output", OutputMode.HUMAN))
    raise typer.Exit(code=exc.exit_code)


def register(group: typer.Typer) -> None:
    @group.command("list")
    def list_tokens(
        ctx: Context,
        cursor: Annotated[str | None, typer.Option("--cursor")] = None,
        page_size: Annotated[int, typer.Option("--page-size")] = 50,
    ) -> None:
        if page_size < 1 or page_size > 100:
            _error(ctx, CliError("page size must be between 1 and 100", exit_code=2))
        query: dict[str, object] = {"page_size": page_size}
        if cursor is not None:
            query["cursor"] = cursor
        client = None
        try:
            client, _, _ = _client(ctx)
            response = client.request_json("GET", "/v1/tokens", query=query)
            emit_success(response.body, mode=_state(ctx).get("output", OutputMode.HUMAN))
        except CliError as exc:
            _error(ctx, exc)
        finally:
            if client is not None:
                client.close()

    @group.command("create")
    def create_token(
        ctx: Context,
        scope: Annotated[list[str], typer.Option("--scope")],
        persist: Annotated[bool, typer.Option("--persist")] = False,
        idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
        name: Annotated[str, typer.Option("--name")] = "nexa-cli",
        expires_in_seconds: Annotated[int, typer.Option("--expires-in-seconds")] = 86400,
    ) -> None:
        invalid = [item for item in scope if item not in SCOPES]
        if invalid:
            _error(ctx, CliError(f"invalid token scope: {invalid[0]}", exit_code=2))
        key = idempotency_key or new_idempotency_key()
        client = None
        try:
            client, store, resolved = _client(ctx)
            response = client.request_json(
                "POST",
                "/v1/tokens",
                json_body={"name": name, "scopes": scope, "expires_in_seconds": expires_in_seconds},
                mutation=True,
                idempotency_key=key,
            )
            payload = response.body if isinstance(response.body, dict) else {}
            persist_error: CliError | None = None
            if persist and isinstance(payload.get("token"), str):
                try:
                    store.save_token(resolved.profile, payload["token"])
                except (CliError, OSError) as exc:
                    persist_error = (
                        exc
                        if isinstance(exc, CliError)
                        else CliError("credentials could not be written", exit_code=2)
                    )
            emit_success(payload, mode=_state(ctx).get("output", OutputMode.HUMAN))
            if persist_error is not None:
                typer.echo(
                    "warning: token was created but could not be persisted; save the displayed "
                    "token securely now and revoke it if it was not captured.",
                    err=True,
                )
                raise typer.Exit(code=2)
        except ApiError as exc:
            _error(ctx, exc)
        except CliError as exc:
            _error(ctx, exc)
        finally:
            if client is not None:
                client.close()

    @group.command("revoke")
    def revoke_token(
        ctx: Context,
        token_id: Annotated[str, typer.Argument()],
        idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    ) -> None:
        key = idempotency_key or new_idempotency_key()
        client = None
        try:
            client, _, _ = _client(ctx)
            response = client.request_json(
                "DELETE", f"/v1/tokens/{token_id}", mutation=True, idempotency_key=key
            )
            emit_success(response.body, mode=_state(ctx).get("output", OutputMode.HUMAN))
        except CliError as exc:
            _error(ctx, exc)
        finally:
            if client is not None:
                client.close()
