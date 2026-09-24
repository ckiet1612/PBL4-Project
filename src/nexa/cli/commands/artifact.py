from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Annotated

import typer
from typer import Context

from ..client import NexaClient
from ..config import ConfigStore, resolve_context, resolve_token
from ..errors import CliError, OutputMode, render_error
from ..idempotency import new_idempotency_key
from ..output import emit_success

_CHUNK_SIZE = 64 * 1024
_MAX_PAGE_SIZE = 100
_RANGE_PATTERN = re.compile(r"^bytes=[0-9]+-[0-9]*$")


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


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            while chunk := source.read(_CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise CliError("unable to read artifact file", exit_code=2) from exc
    return size, f"sha256:{digest.hexdigest()}"


def register(group: typer.Typer) -> None:
    @group.command("list")
    def list_artifacts(
        ctx: Context,
        cursor: Annotated[str | None, typer.Option("--cursor")] = None,
        page_size: Annotated[int, typer.Option("--page-size")] = 50,
        kind: Annotated[str | None, typer.Option("--kind")] = None,
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    ) -> None:
        """List committed artifacts visible to the tenant."""
        query: dict[str, object] = {"page_size": min(page_size, _MAX_PAGE_SIZE)}
        if cursor is not None:
            query["cursor"] = cursor
        if kind is not None:
            query["kind"] = kind
        client = None
        try:
            if page_size < 1:
                raise CliError("page size must be at least 1", exit_code=2)
            client = _client(ctx, tenant)
            response = client.request_json("GET", "/v1/artifacts", query=query)
            emit_success(response.body, mode=_state(ctx).get("output", OutputMode.HUMAN))
        except CliError as exc:
            _error(ctx, exc)
        finally:
            if client is not None:
                client.close()

    @group.command("get")
    def get_artifact(
        ctx: Context,
        artifact_id: Annotated[str, typer.Argument()],
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    ) -> None:
        """Read metadata for one owned artifact."""
        client = None
        try:
            client = _client(ctx, tenant)
            response = client.request_json("GET", f"/v1/artifacts/{artifact_id}")
            emit_success(response.body, mode=_state(ctx).get("output", OutputMode.HUMAN))
        except CliError as exc:
            _error(ctx, exc)
        finally:
            if client is not None:
                client.close()

    @group.command("upload")
    def upload_artifact(
        ctx: Context,
        source: Annotated[Path, typer.Argument(exists=True, readable=True)],
        kind: Annotated[str, typer.Option("--kind")],
        media_type: Annotated[str, typer.Option("--media-type")],
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
        idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    ) -> None:
        """Upload and commit one immutable artifact."""
        client = None
        try:
            size, checksum = _file_digest(source)
            client = _client(ctx, tenant)
            response = client.stream_upload_file(
                source,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Artifact-Checksum": checksum,
                    "X-Artifact-Size": str(size),
                    "X-Artifact-Kind": kind,
                    "X-Artifact-Media-Type": media_type,
                },
                idempotency_key=idempotency_key or new_idempotency_key(),
            )
            if isinstance(response.body, dict):
                returned_size = response.body.get("size_bytes")
                returned_checksum = response.body.get("checksum")
                if returned_size is not None and returned_size != size:
                    raise CliError("artifact size integrity check failed", exit_code=10)
                if returned_checksum is not None and returned_checksum != checksum:
                    raise CliError("artifact checksum integrity check failed", exit_code=10)
            emit_success(response.body, mode=_state(ctx).get("output", OutputMode.HUMAN))
        except (CliError, OSError) as exc:
            if isinstance(exc, OSError):
                exc = CliError("unable to read artifact file", exit_code=2)
            _error(ctx, exc)
        finally:
            if client is not None:
                client.close()

    @group.command("download")
    def download_artifact(
        ctx: Context,
        artifact_id: Annotated[str, typer.Argument()],
        output_file: Annotated[Path, typer.Option("--output-file")],
        range_header: Annotated[str | None, typer.Option("--range")] = None,
        force: Annotated[bool, typer.Option("--force")] = False,
        checksum: Annotated[str | None, typer.Option("--checksum", "--expected-checksum")] = None,
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    ) -> None:
        """Download artifact content with staged atomic replacement."""
        if range_header is not None and not _RANGE_PATTERN.fullmatch(range_header):
            _error(ctx, CliError("range must match bytes=START-END", exit_code=2))
        headers = {"Range": range_header} if range_header is not None else {}
        client = None
        try:
            client = _client(ctx, tenant)
            result = client.stream_download(
                f"/v1/artifacts/{artifact_id}/content",
                output_file,
                headers=headers,
                force=force,
                expected_checksum=checksum,
            )
            emit_success(
                {
                    "path": str(result.path),
                    "size": result.size,
                    "sha256": f"sha256:{result.sha256}",
                },
                mode=_state(ctx).get("output", OutputMode.HUMAN),
            )
        except CliError as exc:
            _error(ctx, exc)
        finally:
            if client is not None:
                client.close()
