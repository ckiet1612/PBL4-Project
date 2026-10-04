"""B19 configuration keys: ops listener, log format, GC/metrics cadence, resource limits."""

import pytest

from nexa.config import (
    ConfigError,
    ResourceLimits,
    coordinator_artifact_quota_bytes,
    load_settings,
    log_settings,
    ops_bind_setting,
    resource_limits,
)
from tests.test_config import SAFE_ENV

MIB = 1024**2
GIB = 1024**3


def _settings(**extra):
    return load_settings({**SAFE_ENV, **extra})


def test_b19_api_defaults_keep_ops_listener_off():
    settings = _settings()
    assert settings.ops_bind is None
    assert settings.log_format == "json"
    assert settings.gc_interval_seconds == 60
    assert settings.metrics_cache_seconds == 15
    assert settings.metrics_statement_timeout_ms == 2000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("127.0.0.1:9100", ("127.0.0.1", 9100)),
        ("0.0.0.0:1", ("0.0.0.0", 1)),
        ("[::1]:65535", ("::1", 65535)),
        ("localhost:9200", ("localhost", 9200)),
        ("  ", None),
    ],
)
def test_ops_bind_accepts_host_port(raw, expected):
    assert ops_bind_setting({"NEXA_OPS_BIND": raw}) == expected
    assert _settings(NEXA_OPS_BIND=raw).ops_bind == expected


@pytest.mark.parametrize(
    "raw",
    ["9100", ":9100", "127.0.0.1:", "127.0.0.1:0", "127.0.0.1:65536", "h:x", "a b:1", "[::1:9"],
)
def test_ops_bind_rejects_invalid_values_without_echo(raw):
    with pytest.raises(ConfigError) as caught:
        ops_bind_setting({"NEXA_OPS_BIND": raw})
    assert "NEXA_OPS_BIND" in str(caught.value)
    assert raw.strip() not in str(caught.value) or raw.strip() == ""
    with pytest.raises(ConfigError):
        _settings(NEXA_OPS_BIND=raw)


def test_log_settings_defaults_and_validation():
    assert log_settings({}) == ("INFO", "json")
    assert log_settings({"NEXA_LOG_LEVEL": "debug", "NEXA_LOG_FORMAT": "TEXT"}) == (
        "DEBUG",
        "text",
    )
    with pytest.raises(ConfigError, match="NEXA_LOG_FORMAT"):
        log_settings({"NEXA_LOG_FORMAT": "xml"})
    with pytest.raises(ConfigError, match="NEXA_LOG_LEVEL"):
        log_settings({"NEXA_LOG_LEVEL": "loud"})
    assert _settings(NEXA_LOG_FORMAT="text").log_format == "text"


@pytest.mark.parametrize(
    ("key", "low", "high"),
    [
        ("NEXA_GC_INTERVAL_SECONDS", 5, 3600),
        ("NEXA_METRICS_CACHE_SECONDS", 1, 300),
        ("NEXA_METRICS_STATEMENT_TIMEOUT_MS", 1, 10000),
    ],
)
def test_api_cadence_boundaries(key, low, high):
    _settings(**{key: str(low)})
    _settings(**{key: str(high)})
    for bad in (low - 1, high + 1):
        with pytest.raises(ConfigError, match=key):
            _settings(**{key: str(bad)})


def test_resource_limit_defaults_follow_contract():
    assert resource_limits({}) == ResourceLimits(
        container_pid_limit=512, scratch_max_bytes=2 * GIB, attempt_log_max_bytes=100 * MIB
    )


@pytest.mark.parametrize(
    ("key", "accepted", "rejected"),
    [
        ("NEXA_CONTAINER_PID_LIMIT", (32, 32768), (31, 32769)),
        ("NEXA_SCRATCH_MAX_BYTES", (64 * MIB,), (64 * MIB - 1,)),
        ("NEXA_ATTEMPT_LOG_MAX_BYTES", (MIB, 10 * GIB), (MIB - 1, 10 * GIB + 1)),
    ],
)
def test_resource_limit_boundaries(key, accepted, rejected):
    for value in accepted:
        resource_limits({key: str(value)})
    for value in rejected:
        with pytest.raises(ConfigError, match=key) as caught:
            resource_limits({key: str(value)})
        assert str(value) not in str(caught.value).split(" and ")[0]
    with pytest.raises(ConfigError, match=key):
        resource_limits({key: "lots"})


def test_resource_limits_are_not_api_keys():
    # The API keeps rejecting worker-only keys; compose passes env per service.
    with pytest.raises(ConfigError, match="Unknown Nexa configuration"):
        _settings(NEXA_CONTAINER_PID_LIMIT="512")


def test_coordinator_quota_matches_api_default_and_bounds():
    assert coordinator_artifact_quota_bytes({}) == _settings().tenant_artifact_quota_bytes
    assert coordinator_artifact_quota_bytes({"NEXA_TENANT_ARTIFACT_QUOTA_BYTES": "1"}) == 1
    with pytest.raises(ConfigError, match="NEXA_TENANT_ARTIFACT_QUOTA_BYTES"):
        coordinator_artifact_quota_bytes({"NEXA_TENANT_ARTIFACT_QUOTA_BYTES": "0"})
