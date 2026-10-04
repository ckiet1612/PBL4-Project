import json
import os
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from nexa.application.errors import ApplicationError
from nexa.application.identity_service import IdentityService
from nexa.application.storage_checks import result_consistency, storage_summary
from nexa.application.template_registry import (
    TemplateDefinitionError,
    load_definition,
    register_template,
    version_values,
)
from nexa.config import ConfigError, load_settings
from nexa.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from nexa.infrastructure.persistence.schema_guard import (
    SchemaCompatibilityError,
    require_current_schema,
)
from nexa.infrastructure.persistence.transactions import TransactionRetryExhausted
from nexa.infrastructure.security import SecurityConfigurationError, read_header_secret_file

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def maintenance() -> None:
    """Run local Nexa maintenance operations."""


@app.command("reopen-worker-bootstrap")
def reopen_worker_bootstrap() -> None:
    """Open a new bounded local-worker bootstrap rotation window."""
    engine = None
    try:
        settings = load_settings(os.environ)
        engine = create_database_engine(settings.database_url)
        require_current_schema(engine)
        identity = IdentityService(create_session_factory(engine), settings)
        identity.initialize()
        result = identity.reopen_worker_bootstrap_window(
            bootstrap_secret=read_header_secret_file(settings.bootstrap_secret_file)
        )
        typer.echo(json.dumps(result, separators=(",", ":"), sort_keys=True))
    except ApplicationError as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(code=1) from None
    except (ConfigError, SecurityConfigurationError, SchemaCompatibilityError):
        typer.echo("error: maintenance configuration is invalid or unavailable", err=True)
        raise typer.Exit(code=1) from None
    except (SQLAlchemyError, TransactionRetryExhausted):
        typer.echo("error: database dependency is unavailable", err=True)
        raise typer.Exit(code=1) from None
    finally:
        if engine is not None:
            engine.dispose()


@app.command("register-template")
def register_template_version(
    file: Annotated[Path, typer.Option("--file", exists=True, dir_okay=False, readable=True)],
    image_digest: Annotated[str, typer.Option("--image-digest")],
) -> None:
    """Register one immutable template version from a definition file and built image digest."""
    engine = None
    try:
        definition = load_definition(file.read_bytes())
        version_values(definition, image_digest)
        settings = load_settings(os.environ)
        engine = create_database_engine(settings.database_url)
        require_current_schema(engine)
        result = register_template(create_session_factory(engine), definition, image_digest)
        typer.echo(json.dumps(result, separators=(",", ":"), sort_keys=True))
    except TemplateDefinitionError as exc:
        typer.echo(f"error: template definition is invalid: {exc}", err=True)
        raise typer.Exit(code=2) from None
    except OSError:
        typer.echo("error: template definition file is unreadable", err=True)
        raise typer.Exit(code=2) from None
    except ApplicationError as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(code=1) from None
    except (ConfigError, SecurityConfigurationError, SchemaCompatibilityError):
        typer.echo("error: maintenance configuration is invalid or unavailable", err=True)
        raise typer.Exit(code=1) from None
    except (SQLAlchemyError, TransactionRetryExhausted):
        typer.echo("error: database dependency is unavailable", err=True)
        raise typer.Exit(code=1) from None
    finally:
        if engine is not None:
            engine.dispose()


def _read_only_check(check) -> dict:
    """Run one read-only check; exit 0 consistent, 1 inconsistent, 2 on any error.

    The checks write nothing and record no audit (they are not admin REST), so they
    also run while the policy is WRITE_FROZEN; the transaction is READ ONLY.
    """
    engine = None
    try:
        settings = load_settings(os.environ)
        engine = create_database_engine(settings.database_url)
        require_current_schema(engine)
        with create_session_factory(engine)() as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            summary = check(session)
            session.rollback()
    except (ConfigError, SecurityConfigurationError, SchemaCompatibilityError):
        typer.echo("error: maintenance configuration is invalid or unavailable", err=True)
        raise typer.Exit(code=2) from None
    except SQLAlchemyError:
        typer.echo("error: database dependency is unavailable", err=True)
        raise typer.Exit(code=2) from None
    finally:
        if engine is not None:
            engine.dispose()
    typer.echo(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    if not summary["consistent"]:
        raise typer.Exit(code=1)
    return summary


@app.command("storage-check")
def storage_check() -> None:
    """Compare tenant byte counters with committed artifacts and ACTIVE uploads."""
    _read_only_check(storage_summary)


@app.command("consistency-check")
def consistency_check(
    accepted_ids: Annotated[Path, typer.Option("--accepted-ids", dir_okay=False)],
) -> None:
    """Check accepted job IDs and the one-final-result rule (one job ID per line)."""
    try:
        lines = accepted_ids.read_text(encoding="utf-8").splitlines()
        accepted = [UUID(line.strip()) for line in lines if line.strip()]
    except (OSError, UnicodeDecodeError):
        typer.echo("error: accepted ID file is unreadable", err=True)
        raise typer.Exit(code=2) from None
    except ValueError:
        typer.echo("error: accepted ID file must hold one job ID per line", err=True)
        raise typer.Exit(code=2) from None
    _read_only_check(lambda session: result_consistency(session, accepted))
