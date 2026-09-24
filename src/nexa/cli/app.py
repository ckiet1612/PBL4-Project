"""REST-only product CLI entry point."""

import os
from typing import Annotated

import typer
from typer import Context

from .commands.admin import register as register_admin
from .commands.artifact import register as register_artifact
from .commands.config import register as register_config
from .commands.job import register as register_job
from .commands.token import register as register_token
from .errors import OutputMode

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _group(name: str) -> typer.Typer:
    group = typer.Typer(no_args_is_help=True, add_completion=False)

    @group.callback()
    def callback() -> None:
        """Product CLI command group."""

    app.add_typer(group, name=name)
    return group


config = _group("config")
token = _group("token")
artifact = _group("artifact")
job = _group("job")
admin = _group("admin")


@app.callback()
def root(
    ctx: Context,
    output: Annotated[
        OutputMode, typer.Option("--output", case_sensitive=False)
    ] = OutputMode.HUMAN,
    endpoint: Annotated[str | None, typer.Option("--endpoint")] = None,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    tenant_id: Annotated[str | None, typer.Option("--tenant", "--tenant-id")] = None,
    token_stdin: Annotated[
        bool, typer.Option("--token-stdin", help="Read the bearer token from stdin.")
    ] = False,
) -> None:
    """Interact with the Nexa REST API."""
    ctx.ensure_object(dict)
    ctx.obj.update(
        output=output,
        endpoint=endpoint,
        profile=profile,
        tenant_id=tenant_id,
        token_stdin=token_stdin,
        environ=os.environ,
    )


register_config(config)
register_token(token)
register_artifact(artifact)
register_job(job)
register_admin(admin)


if __name__ == "__main__":
    app()
