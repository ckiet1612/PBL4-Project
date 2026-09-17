from pathlib import Path

import pytest

from nexa.config import ConfigError, load_settings

SAFE_ENV = {
    "NEXA_ENVIRONMENT": "test",
    "NEXA_DATABASE_URL": "postgresql+psycopg://nexa@localhost/nexa",
    "NEXA_ARTIFACT_ROOT": "/srv/nexa/artifacts",
    "NEXA_API_JSON_MAX_BYTES": "1048576",
    "NEXA_LOG_LEVEL": "INFO",
}


def test_load_settings_accepts_safe_mapping() -> None:
    settings = load_settings(SAFE_ENV)

    assert settings.environment == "test"
    assert settings.database_url == SAFE_ENV["NEXA_DATABASE_URL"]
    assert settings.artifact_root == Path("/srv/nexa/artifacts")
    assert settings.api_json_max_bytes == 1_048_576
    assert settings.log_level == "INFO"


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
        "NEXA_API_JSON_MAX_BYTES",
        "NEXA_ARTIFACT_ROOT",
        "NEXA_DATABASE_URL",
        "NEXA_ENVIRONMENT",
        "NEXA_LOG_LEVEL",
    }
    assert settings.environment == "development"
    assert settings.database_url == "postgresql+psycopg://nexa@localhost/nexa"
    assert settings.artifact_root == Path("/srv/nexa/artifacts")
    assert settings.api_json_max_bytes == 1_048_576
    assert settings.log_level == "INFO"
    assert "password" not in settings.database_url
