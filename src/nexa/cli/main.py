import json
import os

import typer
from sqlalchemy.exc import SQLAlchemyError

from nexa.application.errors import ApplicationError
from nexa.application.identity_service import IdentityService
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
