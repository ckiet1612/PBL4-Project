from __future__ import annotations

import os
import sys
from typing import Annotated

import typer
from typer import Context

from ..client import NexaClient
from ..config import ConfigStore, resolve_context, resolve_token
from ..errors import CliError, OutputMode, render_error
from ..output import emit_success


def _state(ctx: Context) -> dict:
    return ctx.find_root().obj or {}


def _client(ctx: Context, tenant_id: str | None = None) -> NexaClient:
    state = _state(ctx)
    store = ConfigStore()
    resolved = resolve_context(
        store.load(),
        endpoint=state.get("endpoint"),
        profile=state.get("profile"),
        tenant_id=tenant_id if tenant_id is not None else state.get("tenant_id"),
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


def register(group: typer.Typer) -> None:
    @group.command("list")
    def list_templates(
        ctx: Context,
        enabled: Annotated[str | None, typer.Option("--enabled")] = None,
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    ) -> None:
        """List visible template versions."""
        query: dict[str, object] = {}
        if enabled is not None:
            if enabled.lower() not in {"true", "false"}:
                _error(ctx, CliError("enabled must be true or false", exit_code=2))
            query["enabled"] = enabled.lower() == "true"
        client = None
        try:
            client = _client(ctx, tenant)
            response = client.request_json("GET", "/v1/templates", query=query)
            emit_success(response.body, mode=_state(ctx).get("output", OutputMode.HUMAN))
        except CliError as exc:
            _error(ctx, exc)
        finally:
            if client is not None:
                client.close()

    @group.command("get")
    def get_template(
        ctx: Context,
        template_id: Annotated[str, typer.Argument()],
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    ) -> None:
        """Read one visible template version."""
        client = None
        try:
            client = _client(ctx, tenant)
            response = client.request_json("GET", f"/v1/templates/{template_id}")
            emit_success(response.body, mode=_state(ctx).get("output", OutputMode.HUMAN))
        except CliError as exc:
            _error(ctx, exc)
        finally:
            if client is not None:
                client.close()
