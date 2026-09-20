from pathlib import Path

import pytest

from nexa.config import ConfigError, load_settings

SAFE_ENV = {
    "NEXA_ENVIRONMENT": "test",
    "NEXA_DATABASE_URL": "postgresql+psycopg://nexa@localhost/nexa",
    "NEXA_ARTIFACT_ROOT": "/srv/nexa/artifacts",
    "NEXA_API_JSON_MAX_BYTES": "1048576",
    "NEXA_LOG_LEVEL": "INFO",
    "NEXA_PUBLIC_ORIGIN": "https://nexa.test",
    "NEXA_SERVER_SECRET_FILE": "/run/secrets/nexa_server_secret",
    "NEXA_BOOTSTRAP_SECRET_FILE": "/run/secrets/nexa_bootstrap_secret",
    "NEXA_INSTALLATION_ID": "018f05c4-a922-7d0d-9f55-f9084a72d0f1",
    "NEXA_LOCAL_WORKER_ID": "018f05c4-a922-7d0d-9f55-f9084a72d0f2",
    "NEXA_LOCAL_WORKER_FINGERPRINT": "sha256:" + "a" * 64,
}


def test_load_settings_accepts_safe_mapping() -> None:
    settings = load_settings(SAFE_ENV)

    assert settings.environment == "test"
    assert settings.database_url == SAFE_ENV["NEXA_DATABASE_URL"]
    assert settings.artifact_root == Path("/srv/nexa/artifacts")
    assert settings.api_json_max_bytes == 1_048_576
    assert settings.log_level == "INFO"
    assert settings.public_origin == "https://nexa.test"
    assert settings.server_secret_file == Path("/run/secrets/nexa_server_secret")
    assert settings.bootstrap_secret_file == Path("/run/secrets/nexa_bootstrap_secret")
    assert settings.argon2_memory_kib == 19_456
    assert settings.argon2_time_cost == 2
    assert settings.argon2_parallelism == 1
    assert settings.browser_session_absolute_ttl_seconds == 43_200
    assert settings.browser_session_idle_ttl_seconds == 7_200
    assert settings.login_rate_per_minute == 10
    assert settings.idempotency_terminal_retention_days == 30
    assert settings.artifact_max_file_bytes == 10 * 1024**3
    assert settings.tenant_artifact_quota_bytes == 100 * 1024**3
    assert settings.staging_ttl_seconds == 86_400
    assert settings.orphan_ttl_seconds == 86_400
    assert settings.storage_high_watermark_percent == 85
    assert settings.storage_critical_watermark_percent == 95


def test_optional_values_have_non_sensitive_defaults() -> None:
    settings = load_settings(
        {
            "NEXA_DATABASE_URL": SAFE_ENV["NEXA_DATABASE_URL"],
            "NEXA_ARTIFACT_ROOT": SAFE_ENV["NEXA_ARTIFACT_ROOT"],
        }
    )

    assert settings.environment == "development"
    assert settings.api_json_max_bytes == 1_048_576
    assert settings.log_level == "INFO"
    assert settings.public_origin == "https://localhost"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("NEXA_ARGON2_MEMORY_KIB", "19455"),
        ("NEXA_ARGON2_TIME_COST", "1"),
        ("NEXA_ARGON2_PARALLELISM", "0"),
    ],
)
def test_argon2_configuration_below_owasp_floor_is_rejected(key: str, value: str) -> None:
    with pytest.raises(ConfigError, match=key):
        load_settings({**SAFE_ENV, key: value})


def test_production_requires_https_public_origin() -> None:
    with pytest.raises(ConfigError, match="NEXA_PUBLIC_ORIGIN"):
        load_settings(
            {
                **SAFE_ENV,
                "NEXA_ENVIRONMENT": "production",
                "NEXA_PUBLIC_ORIGIN": "http://nexa.example",
            }
        )


def test_session_idle_ttl_must_be_shorter_than_absolute_ttl() -> None:
    with pytest.raises(ConfigError, match="NEXA_BROWSER_SESSION_IDLE_TTL_SECONDS"):
        load_settings(
            {
                **SAFE_ENV,
                "NEXA_BROWSER_SESSION_ABSOLUTE_TTL_SECONDS": "7200",
                "NEXA_BROWSER_SESSION_IDLE_TTL_SECONDS": "7200",
            }
        )


def test_missing_database_url_names_variable_without_a_value() -> None:
    with pytest.raises(ConfigError, match="NEXA_DATABASE_URL") as exc_info:
        load_settings({"NEXA_ARTIFACT_ROOT": SAFE_ENV["NEXA_ARTIFACT_ROOT"]})

    assert SAFE_ENV["NEXA_ARTIFACT_ROOT"] not in str(exc_info.value)


def test_missing_artifact_root_names_variable_without_a_value() -> None:
    with pytest.raises(ConfigError, match="NEXA_ARTIFACT_ROOT") as exc_info:
        load_settings({"NEXA_DATABASE_URL": SAFE_ENV["NEXA_DATABASE_URL"]})

    assert SAFE_ENV["NEXA_DATABASE_URL"] not in str(exc_info.value)


def test_non_postgresql_database_url_is_rejected_without_echoing_value() -> None:
    invalid_url = "mysql://user:password@private.example/nexa"

    with pytest.raises(ConfigError, match="NEXA_DATABASE_URL") as exc_info:
        load_settings({**SAFE_ENV, "NEXA_DATABASE_URL": invalid_url})

    assert invalid_url not in str(exc_info.value)


@pytest.mark.parametrize(
    "invalid_url",
    [
        "postgresql://nexa@localhost/nexa",
        "postgresql+psycopg://",
        "postgresql+psycopg://localhost",
        "postgresql+psycopg:/nexa",
    ],
)
def test_driver_ambiguous_or_unusable_database_url_is_rejected(invalid_url: str) -> None:
    with pytest.raises(ConfigError, match="NEXA_DATABASE_URL") as exc_info:
        load_settings({**SAFE_ENV, "NEXA_DATABASE_URL": invalid_url})

    assert invalid_url not in str(exc_info.value)


def test_unix_socket_database_url_with_database_target_is_accepted() -> None:
    database_url = "postgresql+psycopg:///nexa?host=/var/run/postgresql"

    settings = load_settings({**SAFE_ENV, "NEXA_DATABASE_URL": database_url})

    assert settings.database_url == database_url


@pytest.mark.parametrize("invalid_port", ["not-a-port", "-1", "65536"])
def test_invalid_database_port_is_rejected_without_echoing_value(invalid_port: str) -> None:
    database_url = f"postgresql+psycopg://nexa@localhost:{invalid_port}/nexa"

    with pytest.raises(ConfigError, match="NEXA_DATABASE_URL") as exc_info:
        load_settings({**SAFE_ENV, "NEXA_DATABASE_URL": database_url})

    assert database_url not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_valid_tcp_database_port_is_accepted() -> None:
    database_url = "postgresql+psycopg://nexa@localhost:5432/nexa"

    settings = load_settings({**SAFE_ENV, "NEXA_DATABASE_URL": database_url})

    assert settings.database_url == database_url


def test_relative_artifact_root_is_rejected_without_echoing_value() -> None:
    invalid_path = "private/artifacts"

    with pytest.raises(ConfigError, match="NEXA_ARTIFACT_ROOT") as exc_info:
        load_settings({**SAFE_ENV, "NEXA_ARTIFACT_ROOT": invalid_path})

    assert invalid_path not in str(exc_info.value)


@pytest.mark.parametrize("value", ["staging", "", "TESTING"])
def test_invalid_environment_is_rejected(value: str) -> None:
    with pytest.raises(ConfigError, match="NEXA_ENVIRONMENT"):
        load_settings({**SAFE_ENV, "NEXA_ENVIRONMENT": value})


@pytest.mark.parametrize("value", ["65536", "16777216"])
def test_api_json_limit_accepts_contract_boundaries(value: str) -> None:
    settings = load_settings({**SAFE_ENV, "NEXA_API_JSON_MAX_BYTES": value})

    assert settings.api_json_max_bytes == int(value)


@pytest.mark.parametrize("value", ["65535", "16777217"])
def test_out_of_range_api_json_limit_is_rejected(value: str) -> None:
    with pytest.raises(ConfigError, match="NEXA_API_JSON_MAX_BYTES") as exc_info:
        load_settings({**SAFE_ENV, "NEXA_API_JSON_MAX_BYTES": value})

    assert value not in str(exc_info.value)


def test_non_integer_api_json_limit_is_rejected_without_value_or_cause() -> None:
    invalid_value = "not-an-integer-secret"

    with pytest.raises(ConfigError, match="NEXA_API_JSON_MAX_BYTES") as exc_info:
        load_settings({**SAFE_ENV, "NEXA_API_JSON_MAX_BYTES": invalid_value})

    assert invalid_value not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_artifact_storage_limits_require_quota_and_watermark_order() -> None:
    with pytest.raises(ConfigError, match="NEXA_TENANT_ARTIFACT_QUOTA_BYTES"):
        load_settings(
            {
                **SAFE_ENV,
                "NEXA_ARTIFACT_MAX_FILE_BYTES": str(2 * 1024**3),
                "NEXA_TENANT_ARTIFACT_QUOTA_BYTES": str(1024**2),
            }
        )
    with pytest.raises(ConfigError, match="NEXA_STORAGE_CRITICAL_WATERMARK_PERCENT"):
        load_settings(
            {
                **SAFE_ENV,
                "NEXA_STORAGE_HIGH_WATERMARK_PERCENT": "90",
                "NEXA_STORAGE_CRITICAL_WATERMARK_PERCENT": "90",
            }
        )


def test_artifact_storage_contract_boundaries_are_accepted() -> None:
    settings = load_settings(
        {
            **SAFE_ENV,
            "NEXA_ARTIFACT_MAX_FILE_BYTES": str(1024**2),
            "NEXA_TENANT_ARTIFACT_QUOTA_BYTES": str(1024**2),
            "NEXA_STAGING_TTL_SECONDS": "3600",
            "NEXA_ORPHAN_TTL_SECONDS": "604800",
            "NEXA_STORAGE_HIGH_WATERMARK_PERCENT": "50",
            "NEXA_STORAGE_CRITICAL_WATERMARK_PERCENT": "99",
        }
    )
    assert settings.artifact_max_file_bytes == 1024**2


def test_idempotency_terminal_retention_accepts_configured_contract_value() -> None:
    settings = load_settings({**SAFE_ENV, "NEXA_IDEMPOTENCY_TERMINAL_RETENTION_DAYS": "45"})

    assert settings.idempotency_terminal_retention_days == 45


@pytest.mark.parametrize("value", ["29", "999999999", "not-an-integer"])
def test_idempotency_terminal_retention_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ConfigError, match="NEXA_IDEMPOTENCY_TERMINAL_RETENTION_DAYS"):
        load_settings({**SAFE_ENV, "NEXA_IDEMPOTENCY_TERMINAL_RETENTION_DAYS": value})


def test_log_level_is_normalized_to_uppercase() -> None:
    settings = load_settings({**SAFE_ENV, "NEXA_LOG_LEVEL": "warning"})

    assert settings.log_level == "WARNING"


def test_invalid_log_level_is_rejected_without_echoing_value() -> None:
    invalid_level = "verbose-secret"

    with pytest.raises(ConfigError, match="NEXA_LOG_LEVEL") as exc_info:
        load_settings({**SAFE_ENV, "NEXA_LOG_LEVEL": invalid_level})

    assert invalid_level not in str(exc_info.value)


def test_unknown_nexa_variable_is_rejected_without_echoing_value() -> None:
    unknown_value = "do-not-echo"

    with pytest.raises(ConfigError, match="NEXA_UNDECLARED") as exc_info:
        load_settings({**SAFE_ENV, "NEXA_UNDECLARED": unknown_value})

    assert unknown_value not in str(exc_info.value)


def test_unrelated_process_variable_is_ignored() -> None:
    settings = load_settings({**SAFE_ENV, "PATH": "/usr/bin"})

    assert settings.environment == "test"


def test_sensitive_values_are_redacted_from_repr() -> None:
    rendered = repr(load_settings(SAFE_ENV))

    assert SAFE_ENV["NEXA_DATABASE_URL"] not in rendered
    assert SAFE_ENV["NEXA_ARTIFACT_ROOT"] not in rendered
    assert "database_url=<redacted>" in rendered
    assert "artifact_root=<redacted>" in rendered
    assert SAFE_ENV["NEXA_SERVER_SECRET_FILE"] not in rendered
    assert SAFE_ENV["NEXA_BOOTSTRAP_SECRET_FILE"] not in rendered


def test_env_example_is_loadable_without_real_credentials() -> None:
    example_path = Path(__file__).resolve().parents[1] / ".env.example"
    values: dict[str, str] = {}

    for raw_line in example_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator == "=", f"Malformed example line for {key}"
        assert key not in values, f"Duplicate example key: {key}"
        values[key] = value

    settings = load_settings(values)

    assert set(values) == {
        "NEXA_ADMIN_BOOTSTRAP_WINDOW_SECONDS",
        "NEXA_API_JSON_MAX_BYTES",
        "NEXA_ARGON2_MEMORY_KIB",
        "NEXA_ARGON2_PARALLELISM",
        "NEXA_ARGON2_TIME_COST",
        "NEXA_ARTIFACT_ROOT",
        "NEXA_ARTIFACT_MAX_FILE_BYTES",
        "NEXA_BOOTSTRAP_SECRET_FILE",
        "NEXA_BROWSER_SESSION_ABSOLUTE_TTL_SECONDS",
        "NEXA_BROWSER_SESSION_IDLE_TTL_SECONDS",
        "NEXA_CLI_TOKEN_DEFAULT_TTL_SECONDS",
        "NEXA_CURSOR_TTL_SECONDS",
        "NEXA_DATABASE_URL",
        "NEXA_ENVIRONMENT",
        "NEXA_IDEMPOTENCY_PENDING_WAIT_MILLISECONDS",
        "NEXA_IDEMPOTENCY_TERMINAL_RETENTION_DAYS",
        "NEXA_ORPHAN_TTL_SECONDS",
        "NEXA_INSTALLATION_ID",
        "NEXA_LOGIN_RATE_PER_MINUTE",
        "NEXA_LOCAL_WORKER_FINGERPRINT",
        "NEXA_LOCAL_WORKER_ID",
        "NEXA_LOG_LEVEL",
        "NEXA_MAINTENANCE_CIDRS",
        "NEXA_PASSWORD_HASH_CONCURRENCY",
        "NEXA_PUBLIC_ORIGIN",
        "NEXA_SERVER_SECRET_FILE",
        "NEXA_STAGING_TTL_SECONDS",
        "NEXA_STORAGE_CRITICAL_WATERMARK_PERCENT",
        "NEXA_STORAGE_HIGH_WATERMARK_PERCENT",
        "NEXA_TENANT_ARTIFACT_QUOTA_BYTES",
        "NEXA_TRUSTED_PROXY_CIDRS",
        "NEXA_WORKER_BOOTSTRAP_WINDOW_SECONDS",
        "NEXA_WORKER_CREDENTIAL_TTL_SECONDS",
    }
    assert settings.environment == "development"
    assert settings.database_url == "postgresql+psycopg://nexa@localhost/nexa"
    assert settings.artifact_root == Path("/srv/nexa/artifacts")
    assert settings.api_json_max_bytes == 1_048_576
    assert settings.log_level == "INFO"
    assert settings.public_origin == "https://localhost"
    assert "password" not in settings.database_url
