from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from typer import Context

from ..errors import CliError, OutputMode
from ..idempotency import new_idempotency_key
from ..output import emit_success
from .job import (
    _IDEMPOTENCY_KEY_PATTERN,
    _client,
    _error,
    _load_json,
    _read,
    _state,
    _strict_page,
    _with_headers,
)


def register(group: typer.Typer) -> None:
    @group.command("submit")
    def submit_sweep(
        ctx: Context,
        file: Annotated[Path, typer.Option("--file", exists=True, readable=True)],
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
        idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    ) -> None:
        """Submit a finite sweep of at most 100 child jobs; reuse the key to resume."""
        client = None
        try:
            request = _load_json(file, label="sweep request")
            if not isinstance(request, dict):
                raise CliError("sweep request JSON must be an object", exit_code=2)
            if idempotency_key is not None and not _IDEMPOTENCY_KEY_PATTERN.fullmatch(
                idempotency_key
            ):
                raise CliError(
                    "idempotency key must be 16 to 128 visible ASCII characters", exit_code=2
                )
            client = _client(ctx, tenant)
            response = client.request_json(
                "POST",
                "/v1/sweeps",
                json_body=request,
                mutation=True,
                idempotency_key=idempotency_key or new_idempotency_key(),
            )
            emit_success(_with_headers(response), mode=_state(ctx).get("output", OutputMode.HUMAN))
        except CliError as exc:
            _error(ctx, exc)
        finally:
            if client is not None:
                client.close()

    @group.command("show")
    def show_sweep(
        ctx: Context,
        sweep_id: Annotated[str, typer.Argument()],
        cursor: Annotated[str | None, typer.Option("--cursor")] = None,
        page_size: Annotated[int, typer.Option("--page-size")] = 50,
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    ) -> None:
        """Read a sweep and one page of child outcomes ordered by child index."""
        query = _strict_page(ctx, cursor, page_size)
        _read(ctx, "GET", f"/v1/sweeps/{sweep_id}", query=query, tenant=tenant)
