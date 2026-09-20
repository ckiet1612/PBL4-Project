import re
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

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
    public_origin: str
    server_secret_file: Path
    bootstrap_secret_file: Path
    installation_id: UUID
    local_worker_id: UUID
    local_worker_fingerprint: str
    maintenance_networks: tuple[IPv4Network | IPv6Network, ...]
    trusted_proxy_networks: tuple[IPv4Network | IPv6Network, ...]
    argon2_memory_kib: int
    argon2_time_cost: int
    argon2_parallelism: int
    password_hash_concurrency: int
    browser_session_absolute_ttl_seconds: int
    browser_session_idle_ttl_seconds: int
    cli_token_default_ttl_seconds: int
    worker_credential_ttl_seconds: int
    admin_bootstrap_window_seconds: int
    worker_bootstrap_window_seconds: int
    login_rate_per_minute: int
    cursor_ttl_seconds: int
    idempotency_pending_wait_milliseconds: int
    artifact_max_file_bytes: int
    tenant_artifact_quota_bytes: int
    staging_ttl_seconds: int
    orphan_ttl_seconds: int
    storage_high_watermark_percent: int
    storage_critical_watermark_percent: int

    def __repr__(self) -> str:
        return (
            "Settings("
            f"environment={self.environment!r}, "
            "database_url=<redacted>, artifact_root=<redacted>, "
            "server_secret_file=<redacted>, bootstrap_secret_file=<redacted>, "
            f"api_json_max_bytes={self.api_json_max_bytes!r}, "
            f"log_level={self.log_level!r}, public_origin={self.public_origin!r})"
        )


_ALLOWED_KEYS = frozenset(
    {
        "NEXA_ENVIRONMENT",
        "NEXA_DATABASE_URL",
        "NEXA_ARTIFACT_ROOT",
        "NEXA_API_JSON_MAX_BYTES",
        "NEXA_LOG_LEVEL",
        "NEXA_PUBLIC_ORIGIN",
        "NEXA_SERVER_SECRET_FILE",
        "NEXA_BOOTSTRAP_SECRET_FILE",
        "NEXA_INSTALLATION_ID",
        "NEXA_LOCAL_WORKER_ID",
        "NEXA_LOCAL_WORKER_FINGERPRINT",
        "NEXA_MAINTENANCE_CIDRS",
        "NEXA_TRUSTED_PROXY_CIDRS",
        "NEXA_ARGON2_MEMORY_KIB",
        "NEXA_ARGON2_TIME_COST",
        "NEXA_ARGON2_PARALLELISM",
        "NEXA_PASSWORD_HASH_CONCURRENCY",
        "NEXA_BROWSER_SESSION_ABSOLUTE_TTL_SECONDS",
        "NEXA_BROWSER_SESSION_IDLE_TTL_SECONDS",
        "NEXA_CLI_TOKEN_DEFAULT_TTL_SECONDS",
        "NEXA_WORKER_CREDENTIAL_TTL_SECONDS",
        "NEXA_ADMIN_BOOTSTRAP_WINDOW_SECONDS",
        "NEXA_WORKER_BOOTSTRAP_WINDOW_SECONDS",
        "NEXA_LOGIN_RATE_PER_MINUTE",
        "NEXA_CURSOR_TTL_SECONDS",
        "NEXA_IDEMPOTENCY_PENDING_WAIT_MILLISECONDS",
        "NEXA_ARTIFACT_MAX_FILE_BYTES",
        "NEXA_TENANT_ARTIFACT_QUOTA_BYTES",
        "NEXA_STAGING_TTL_SECONDS",
        "NEXA_ORPHAN_TTL_SECONDS",
        "NEXA_STORAGE_HIGH_WATERMARK_PERCENT",
        "NEXA_STORAGE_CRITICAL_WATERMARK_PERCENT",
    }
)
_ENVIRONMENTS = frozenset({"development", "test", "production"})
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_DEFAULT_INSTALLATION_ID = "018f05c4-a922-7d0d-9f55-f9084a72d0f1"
_DEFAULT_WORKER_ID = "018f05c4-a922-7d0d-9f55-f9084a72d0f2"
_DEFAULT_WORKER_FINGERPRINT = "sha256:" + "0" * 64


def _integer_setting(
    environ: Mapping[str, str], key: str, default: int, *, minimum: int, maximum: int
) -> int:
    raw = environ.get(key, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    return value


def _absolute_path_setting(environ: Mapping[str, str], key: str, default: str) -> Path:
    path = Path(environ.get(key, default).strip())
    if not path.is_absolute():
        raise ConfigError(f"{key} must be an absolute path")
    return path


def _uuid7_setting(environ: Mapping[str, str], key: str, default: str) -> UUID:
    try:
        value = UUID(environ.get(key, default).strip())
    except ValueError:
        raise ConfigError(f"{key} must be a UUIDv7") from None
    if value.version != 7:
        raise ConfigError(f"{key} must be a UUIDv7")
    return value


def _network_setting(
    environ: Mapping[str, str], key: str, default: str
) -> tuple[IPv4Network | IPv6Network, ...]:
    raw_values = [item.strip() for item in environ.get(key, default).split(",") if item.strip()]
    try:
        return tuple(ip_network(item, strict=True) for item in raw_values)
    except ValueError:
        raise ConfigError(f"{key} must contain comma-separated CIDR networks") from None


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

    api_json_max_bytes = _integer_setting(
        environ, "NEXA_API_JSON_MAX_BYTES", 1_048_576, minimum=65_536, maximum=16_777_216
    )

    log_level_text = environ.get("NEXA_LOG_LEVEL", "INFO").strip().upper()
    if log_level_text not in _LOG_LEVELS:
        raise ConfigError("NEXA_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    log_level = cast(LogLevel, log_level_text)

    public_origin = environ.get("NEXA_PUBLIC_ORIGIN", "https://localhost").strip().rstrip("/")
    parsed_origin = urlsplit(public_origin)
    if (
        parsed_origin.scheme != "https"
        or not parsed_origin.hostname
        or parsed_origin.username is not None
        or parsed_origin.password is not None
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        raise ConfigError("NEXA_PUBLIC_ORIGIN must be an HTTPS origin without path or credentials")

    server_secret_file = _absolute_path_setting(
        environ, "NEXA_SERVER_SECRET_FILE", "/run/secrets/nexa_server_secret"
    )
    bootstrap_secret_file = _absolute_path_setting(
        environ, "NEXA_BOOTSTRAP_SECRET_FILE", "/run/secrets/nexa_bootstrap_secret"
    )
    installation_id = _uuid7_setting(environ, "NEXA_INSTALLATION_ID", _DEFAULT_INSTALLATION_ID)
    local_worker_id = _uuid7_setting(environ, "NEXA_LOCAL_WORKER_ID", _DEFAULT_WORKER_ID)
    local_worker_fingerprint = environ.get(
        "NEXA_LOCAL_WORKER_FINGERPRINT", _DEFAULT_WORKER_FINGERPRINT
    ).strip()
    if not _FINGERPRINT_PATTERN.fullmatch(local_worker_fingerprint):
        raise ConfigError("NEXA_LOCAL_WORKER_FINGERPRINT must be a sha256 fingerprint")

    argon2_memory_kib = _integer_setting(
        environ, "NEXA_ARGON2_MEMORY_KIB", 19_456, minimum=19_456, maximum=1_048_576
    )
    argon2_time_cost = _integer_setting(environ, "NEXA_ARGON2_TIME_COST", 2, minimum=2, maximum=20)
    argon2_parallelism = _integer_setting(
        environ, "NEXA_ARGON2_PARALLELISM", 1, minimum=1, maximum=16
    )
    password_hash_concurrency = _integer_setting(
        environ, "NEXA_PASSWORD_HASH_CONCURRENCY", 4, minimum=1, maximum=32
    )
    browser_session_absolute_ttl_seconds = _integer_setting(
        environ,
        "NEXA_BROWSER_SESSION_ABSOLUTE_TTL_SECONDS",
        43_200,
        minimum=300,
        maximum=604_800,
    )
    browser_session_idle_ttl_seconds = _integer_setting(
        environ,
        "NEXA_BROWSER_SESSION_IDLE_TTL_SECONDS",
        7_200,
        minimum=60,
        maximum=86_400,
    )
    if browser_session_idle_ttl_seconds >= browser_session_absolute_ttl_seconds:
        raise ConfigError(
            "NEXA_BROWSER_SESSION_IDLE_TTL_SECONDS must be shorter than the absolute TTL"
        )
    cli_token_default_ttl_seconds = _integer_setting(
        environ,
        "NEXA_CLI_TOKEN_DEFAULT_TTL_SECONDS",
        86_400,
        minimum=300,
        maximum=2_592_000,
    )
    worker_credential_ttl_seconds = _integer_setting(
        environ,
        "NEXA_WORKER_CREDENTIAL_TTL_SECONDS",
        86_400,
        minimum=300,
        maximum=2_592_000,
    )
    admin_bootstrap_window_seconds = _integer_setting(
        environ, "NEXA_ADMIN_BOOTSTRAP_WINDOW_SECONDS", 900, minimum=60, maximum=86_400
    )
    worker_bootstrap_window_seconds = _integer_setting(
        environ, "NEXA_WORKER_BOOTSTRAP_WINDOW_SECONDS", 900, minimum=60, maximum=86_400
    )
    login_rate_per_minute = _integer_setting(
        environ, "NEXA_LOGIN_RATE_PER_MINUTE", 10, minimum=1, maximum=120
    )
    cursor_ttl_seconds = _integer_setting(
        environ, "NEXA_CURSOR_TTL_SECONDS", 86_400, minimum=60, maximum=604_800
    )
    idempotency_pending_wait_milliseconds = _integer_setting(
        environ,
        "NEXA_IDEMPOTENCY_PENDING_WAIT_MILLISECONDS",
        5_000,
        minimum=0,
        maximum=30_000,
    )
    artifact_max_file_bytes = _integer_setting(
        environ,
        "NEXA_ARTIFACT_MAX_FILE_BYTES",
        10 * 1024**3,
        minimum=1 * 1024**2,
        maximum=1 * 1024**4,
    )
    tenant_artifact_quota_bytes = _integer_setting(
        environ,
        "NEXA_TENANT_ARTIFACT_QUOTA_BYTES",
        100 * 1024**3,
        minimum=artifact_max_file_bytes,
        maximum=1 * 1024**4,
    )
    staging_ttl_seconds = _integer_setting(
        environ, "NEXA_STAGING_TTL_SECONDS", 86_400, minimum=3_600, maximum=604_800
    )
    orphan_ttl_seconds = _integer_setting(
        environ, "NEXA_ORPHAN_TTL_SECONDS", 86_400, minimum=3_600, maximum=604_800
    )
    storage_high_watermark_percent = _integer_setting(
        environ, "NEXA_STORAGE_HIGH_WATERMARK_PERCENT", 85, minimum=50, maximum=95
    )
    storage_critical_watermark_percent = _integer_setting(
        environ, "NEXA_STORAGE_CRITICAL_WATERMARK_PERCENT", 95, minimum=51, maximum=99
    )
    if orphan_ttl_seconds < staging_ttl_seconds:
        raise ConfigError("NEXA_ORPHAN_TTL_SECONDS must be at least the staging TTL")
    if storage_critical_watermark_percent <= storage_high_watermark_percent:
        raise ConfigError("NEXA_STORAGE_CRITICAL_WATERMARK_PERCENT must exceed the high watermark")

    return Settings(
        environment=environment,
        database_url=database_url,
        artifact_root=artifact_root,
        api_json_max_bytes=api_json_max_bytes,
        log_level=log_level,
        public_origin=public_origin,
        server_secret_file=server_secret_file,
        bootstrap_secret_file=bootstrap_secret_file,
        installation_id=installation_id,
        local_worker_id=local_worker_id,
        local_worker_fingerprint=local_worker_fingerprint,
        maintenance_networks=_network_setting(
            environ, "NEXA_MAINTENANCE_CIDRS", "127.0.0.0/8,::1/128"
        ),
        trusted_proxy_networks=_network_setting(environ, "NEXA_TRUSTED_PROXY_CIDRS", ""),
        argon2_memory_kib=argon2_memory_kib,
        argon2_time_cost=argon2_time_cost,
        argon2_parallelism=argon2_parallelism,
        password_hash_concurrency=password_hash_concurrency,
        browser_session_absolute_ttl_seconds=browser_session_absolute_ttl_seconds,
        browser_session_idle_ttl_seconds=browser_session_idle_ttl_seconds,
        cli_token_default_ttl_seconds=cli_token_default_ttl_seconds,
        worker_credential_ttl_seconds=worker_credential_ttl_seconds,
        admin_bootstrap_window_seconds=admin_bootstrap_window_seconds,
        worker_bootstrap_window_seconds=worker_bootstrap_window_seconds,
        login_rate_per_minute=login_rate_per_minute,
        cursor_ttl_seconds=cursor_ttl_seconds,
        idempotency_pending_wait_milliseconds=idempotency_pending_wait_milliseconds,
        artifact_max_file_bytes=artifact_max_file_bytes,
        tenant_artifact_quota_bytes=tenant_artifact_quota_bytes,
        staging_ttl_seconds=staging_ttl_seconds,
        orphan_ttl_seconds=orphan_ttl_seconds,
        storage_high_watermark_percent=storage_high_watermark_percent,
        storage_critical_watermark_percent=storage_critical_watermark_percent,
    )
