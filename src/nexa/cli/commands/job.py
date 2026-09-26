from __future__ import annotations

import json
import math
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


def _query_page(ctx: Context, cursor: str | None, page_size: int) -> dict[str, object]:
    if page_size < 1:
        _error(ctx, CliError("page size must be at least 1", exit_code=2))
    query: dict[str, object] = {"page_size": min(page_size, _MAX_PAGE_SIZE)}
    if cursor is not None:
        query["cursor"] = cursor
    return query


def _with_headers(response) -> object:
    payload = response.body
    payload = {"data": payload} if not isinstance(payload, dict) else dict(payload)
    for key, header in (("location", "location"), ("etag", "etag"), ("request_id", "x-request-id")):
        value = response.headers.get(header)
        if value is not None:
            payload[key] = value
    return payload


def _load_json(path: Path) -> object:
    try:
        with path.open(encoding="utf-8") as source:
            return json.load(
                source,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite,
                parse_float=_parse_finite_float,
            )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise CliError("job spec JSON is invalid", exit_code=2) from exc


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


def _explicit_spec(
    *,
    template_id: str | None,
    template_version: int | None,
    input_artifact_id: str | None,
    cpu_millis: int | None,
    memory_bytes: int | None,
    gpu_count: int | None,
    priority: int | None,
    runtime_limit_seconds: int | None,
    checkpoint_interval_seconds: int | None,
    parameters_json: Path | None,
    model_artifact_id: str | None,
) -> dict[str, object]:
    if template_id is None:
        raise CliError("--template-id is required when --spec-file is absent", exit_code=2)
    spec: dict[str, object] = {"template_id": template_id}
    values = {
        "template_version": template_version,
        "input_artifact_id": input_artifact_id,
        "priority": priority,
        "runtime_limit_seconds": runtime_limit_seconds,
        "checkpoint_interval_seconds": checkpoint_interval_seconds,
        "model_artifact_id": model_artifact_id,
    }
    spec.update({key: value for key, value in values.items() if value is not None})
    resources = {
        "cpu_millis": cpu_millis,
        "memory_bytes": memory_bytes,
        "gpu_count": gpu_count,
    }
    if any(value is not None for value in resources.values()):
        spec["resources"] = {key: value for key, value in resources.items() if value is not None}
    if parameters_json is not None:
        parameters = _load_json(parameters_json)
        if not isinstance(parameters, dict):
            raise CliError("parameters JSON must be an object", exit_code=2)
        spec["parameters"] = parameters
    return spec


def register(group: typer.Typer) -> None:
    @group.command("submit")
    def submit_job(
        ctx: Context,
        spec_file: Annotated[
            Path | None, typer.Option("--spec-file", exists=True, readable=True)
        ] = None,
        template_id: Annotated[str | None, typer.Option("--template-id")] = None,
        template_version: Annotated[int | None, typer.Option("--template-version")] = None,
        input_artifact_id: Annotated[str | None, typer.Option("--input-artifact-id")] = None,
        cpu_millis: Annotated[int | None, typer.Option("--cpu-millis")] = None,
        memory_bytes: Annotated[int | None, typer.Option("--memory-bytes")] = None,
        gpu_count: Annotated[int | None, typer.Option("--gpu-count")] = None,
        priority: Annotated[int | None, typer.Option("--priority")] = None,
        runtime_limit_seconds: Annotated[
            int | None, typer.Option("--runtime-limit-seconds")
        ] = None,
        checkpoint_interval_seconds: Annotated[
            int | None, typer.Option("--checkpoint-interval-seconds")
        ] = None,
        parameters_json: Annotated[
            Path | None, typer.Option("--parameters-json", exists=True, readable=True)
        ] = None,
        model_artifact_id: Annotated[str | None, typer.Option("--model-artifact-id")] = None,
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
        idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    ) -> None:
        """Submit one job and logical execution session."""
        client = None
        try:
            if spec_file is not None:
                if any(
                    value is not None
                    for value in (
                        template_id,
                        template_version,
                        input_artifact_id,
                        cpu_millis,
                        memory_bytes,
                        gpu_count,
                        priority,
                        runtime_limit_seconds,
                        checkpoint_interval_seconds,
                        parameters_json,
                        model_artifact_id,
                    )
                ):
                    raise CliError(
                        "--spec-file cannot be combined with explicit spec options", exit_code=2
                    )
                spec = _load_json(spec_file)
                if not isinstance(spec, dict):
                    raise CliError("job spec JSON must be an object", exit_code=2)
            else:
                spec = _explicit_spec(
                    template_id=template_id,
                    template_version=template_version,
                    input_artifact_id=input_artifact_id,
                    cpu_millis=cpu_millis,
                    memory_bytes=memory_bytes,
                    gpu_count=gpu_count,
                    priority=priority,
                    runtime_limit_seconds=runtime_limit_seconds,
                    checkpoint_interval_seconds=checkpoint_interval_seconds,
                    parameters_json=parameters_json,
                    model_artifact_id=model_artifact_id,
                )
            client = _client(ctx, tenant)
            response = client.request_json(
                "POST",
                "/v1/jobs",
                json_body={"spec": spec},
                mutation=True,
                idempotency_key=idempotency_key or new_idempotency_key(),
            )
            emit_success(_with_headers(response), mode=_state(ctx).get("output", OutputMode.HUMAN))
        except CliError as exc:
            _error(ctx, exc)
        finally:
            if client is not None:
                client.close()

    @group.command("list")
    def list_jobs(
        ctx: Context,
        cursor: Annotated[str | None, typer.Option("--cursor")] = None,
        page_size: Annotated[int, typer.Option("--page-size")] = 50,
        state: Annotated[str | None, typer.Option("--state")] = None,
        template_id: Annotated[str | None, typer.Option("--template-id")] = None,
        created_after: Annotated[str | None, typer.Option("--created-after")] = None,
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    ) -> None:
        query = _query_page(ctx, cursor, page_size)
        for key, value in (
            ("state", state),
            ("template_id", template_id),
            ("created_after", created_after),
        ):
            if value is not None:
                query[key] = value
        _read(ctx, "GET", "/v1/jobs", query=query, tenant=tenant)

    @group.command("get")
    def get_job(
        ctx: Context,
        job_id: Annotated[str, typer.Argument()],
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    ) -> None:
        _read(ctx, "GET", f"/v1/jobs/{job_id}", tenant=tenant)

    @group.command("session")
    def get_session(
        ctx: Context,
        session_id: Annotated[str, typer.Argument()],
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    ) -> None:
        _read(ctx, "GET", f"/v1/sessions/{session_id}", tenant=tenant)

    @group.command("events")
    def list_events(
        ctx: Context,
        job_id: Annotated[str, typer.Argument()],
        after_sequence: Annotated[int, typer.Option("--after-sequence")] = 0,
        page_size: Annotated[int, typer.Option("--page-size")] = 50,
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    ) -> None:
        query = _query_page(ctx, None, page_size)
        query["after_sequence"] = after_sequence
        _read(ctx, "GET", f"/v1/jobs/{job_id}/events", query=query, tenant=tenant)

    @group.command("checkpoints")
    def list_checkpoints(
        ctx: Context,
        job_id: Annotated[str, typer.Argument()],
        cursor: Annotated[str | None, typer.Option("--cursor")] = None,
        page_size: Annotated[int, typer.Option("--page-size")] = 50,
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    ) -> None:
        query = _query_page(ctx, cursor, page_size)
        _read(ctx, "GET", f"/v1/jobs/{job_id}/checkpoints", query=query, tenant=tenant)

    @group.command("result")
    def get_result(
        ctx: Context,
        job_id: Annotated[str, typer.Argument()],
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    ) -> None:
        _read(ctx, "GET", f"/v1/jobs/{job_id}/result", tenant=tenant)

    @group.command("result-download")
    def result_download(
        ctx: Context,
        job_id: Annotated[str, typer.Argument()],
        output_file: Annotated[Path, typer.Option("--output-file")],
        range_header: Annotated[str | None, typer.Option("--range")] = None,
        force: Annotated[bool, typer.Option("--force")] = False,
        checksum: Annotated[str | None, typer.Option("--checksum", "--expected-checksum")] = None,
        tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    ) -> None:
        if range_header is not None and not _RANGE_PATTERN.fullmatch(range_header):
            _error(ctx, CliError("range must match bytes=START-END", exit_code=2))
        client = None
        try:
            client = _client(ctx, tenant)
            result = client.request_json("GET", f"/v1/jobs/{job_id}/result")
            payload = result.body if isinstance(result.body, dict) else {}
            artifact_id = payload.get("manifest_artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id:
                raise CliError("result response has no manifest artifact", exit_code=9)
            headers = {"Range": range_header} if range_header is not None else {}
            downloaded = client.stream_download(
                f"/v1/artifacts/{artifact_id}/content",
                output_file,
                headers=headers,
                force=force,
                expected_checksum=checksum,
            )
            emit_success(
                {
                    "path": str(downloaded.path),
                    "size": downloaded.size,
                    "sha256": f"sha256:{downloaded.sha256}",
                },
                mode=_state(ctx).get("output", OutputMode.HUMAN),
            )
        except CliError as exc:
            _error(ctx, exc)
        finally:
            if client is not None:
                client.close()


def _read(
    ctx: Context,
    method: str,
    path: str,
    *,
    query: dict[str, object] | None = None,
    tenant: str | None = None,
) -> None:
    client = None
    try:
        client = _client(ctx, tenant)
        response = client.request_json(method, path, query=query)
        emit_success(response.body, mode=_state(ctx).get("output", OutputMode.HUMAN))
    except CliError as exc:
        _error(ctx, exc)
    finally:
        if client is not None:
            client.close()
