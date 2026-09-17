from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class ConfigError(ValueError):
    """Raised when declared Nexa configuration is missing or invalid."""


@dataclass(frozen=True, slots=True, repr=False)
class Settings:
    environment: Environment
    database_url: str
    artifact_root: Path
    api_json_max_bytes: int
    log_level: LogLevel

    def __repr__(self) -> str:
        return (
            "Settings("
            f"environment={self.environment!r}, "
            "database_url=<redacted>, artifact_root=<redacted>, "
            f"api_json_max_bytes={self.api_json_max_bytes!r}, "
            f"log_level={self.log_level!r})"
        )


_ALLOWED_KEYS = frozenset(
    {
        "NEXA_ENVIRONMENT",
        "NEXA_DATABASE_URL",
        "NEXA_ARTIFACT_ROOT",
        "NEXA_API_JSON_MAX_BYTES",
        "NEXA_LOG_LEVEL",
    }
)
_ENVIRONMENTS = frozenset({"development", "test", "production"})
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def load_settings(environ: Mapping[str, str]) -> Settings:
    unknown = sorted(key for key in environ if key.startswith("NEXA_") and key not in _ALLOWED_KEYS)
    if unknown:
        raise ConfigError(f"Unknown Nexa configuration variable(s): {', '.join(unknown)}")

    database_url = environ.get("NEXA_DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("Missing required configuration variable: NEXA_DATABASE_URL")
    try:
        parsed_database_url = urlsplit(database_url)
        database_port = parsed_database_url.port
    except ValueError:
        raise ConfigError(
            "NEXA_DATABASE_URL must use postgresql+psycopg and include a database name"
        ) from None
    if (
        not database_url.startswith("postgresql+psycopg://")
        or parsed_database_url.scheme != "postgresql+psycopg"
        or not parsed_database_url.path.strip("/")
        or parsed_database_url.fragment
        or (database_port is not None and parsed_database_url.hostname is None)
    ):
        raise ConfigError(
            "NEXA_DATABASE_URL must use postgresql+psycopg and include a database name"
        )

    artifact_root_text = environ.get("NEXA_ARTIFACT_ROOT", "").strip()
    if not artifact_root_text:
        raise ConfigError("Missing required configuration variable: NEXA_ARTIFACT_ROOT")
    artifact_root = Path(artifact_root_text)
    if not artifact_root.is_absolute():
        raise ConfigError("NEXA_ARTIFACT_ROOT must be an absolute path")

    environment_text = environ.get("NEXA_ENVIRONMENT", "development").strip().lower()
    if environment_text not in _ENVIRONMENTS:
        raise ConfigError("NEXA_ENVIRONMENT must be development, test, or production")
    environment = cast(Environment, environment_text)

    raw_json_limit = environ.get("NEXA_API_JSON_MAX_BYTES", "1048576").strip()
    try:
        api_json_max_bytes = int(raw_json_limit)
    except ValueError:
        raise ConfigError("NEXA_API_JSON_MAX_BYTES must be an integer") from None
    if not 65_536 <= api_json_max_bytes <= 16_777_216:
        raise ConfigError("NEXA_API_JSON_MAX_BYTES must be between 65536 and 16777216")

    log_level_text = environ.get("NEXA_LOG_LEVEL", "INFO").strip().upper()
    if log_level_text not in _LOG_LEVELS:
        raise ConfigError("NEXA_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    log_level = cast(LogLevel, log_level_text)

    return Settings(
        environment=environment,
        database_url=database_url,
        artifact_root=artifact_root,
        api_json_max_bytes=api_json_max_bytes,
        log_level=log_level,
    )
