import io
import os

import pytest

from nexa.cli.config import CliConfig, ConfigStore, Profile, resolve_context, resolve_token
from nexa.cli.errors import CliError


def test_context_precedence_is_flag_then_environment_then_profile(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.save(
        CliConfig(
            profiles={"default": Profile(endpoint="https://profile", tenant_id="p")},
            active_profile="default",
        )
    )
    monkeypatch.setenv("NEXA_ENDPOINT", "https://environment")
    monkeypatch.setenv("NEXA_TENANT_ID", "e")
    context = resolve_context(
        store.load(), endpoint="https://flag", profile=None, tenant_id="f", environ=os.environ
    )
    assert (context.endpoint, context.tenant_id) == ("https://flag", "f")


def test_environment_precedes_active_profile(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.save(CliConfig(profiles={"default": Profile(endpoint="https://profile", tenant_id="p")}))
    monkeypatch.setenv("NEXA_ENDPOINT", "https://environment")
    context = resolve_context(
        store.load(), endpoint=None, profile=None, tenant_id=None, environ=os.environ
    )
    assert context.endpoint == "https://environment"
    assert context.tenant_id == "p"


def test_malformed_config_is_a_local_cli_error(tmp_path):
    (tmp_path / "config.toml").write_bytes(b"[profiles\xff")
    with pytest.raises(CliError) as raised:
        ConfigStore(tmp_path).load()
    assert raised.value.exit_code == 2


def test_wrong_config_shape_is_a_local_cli_error(tmp_path):
    (tmp_path / "config.toml").write_text('profiles = "bad"\n', encoding="utf-8")
    with pytest.raises(CliError) as raised:
        ConfigStore(tmp_path).load()
    assert raised.value.exit_code == 2


@pytest.mark.parametrize(
    "body",
    [
        "active_profile = []\n",
        "[profiles.default]\nendpoint = []\n",
        "[profiles.default]\ntenant_id = {}\n",
    ],
)
def test_wrong_config_scalar_type_is_a_local_cli_error(tmp_path, body):
    (tmp_path / "config.toml").write_text(body, encoding="utf-8")
    with pytest.raises(CliError) as raised:
        ConfigStore(tmp_path).load()
    assert raised.value.exit_code == 2


def test_token_sources_prefer_stdin_then_environment_then_storage(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.save_token("default", "stored-token")
    monkeypatch.setenv("NEXA_TOKEN", "environment-token")

    assert (
        resolve_token(store, "default", environ=os.environ, stdin=io.StringIO("stdin-token\n"))
        == "stdin-token"
    )
    assert resolve_token(store, "default", environ=os.environ) == "environment-token"
    monkeypatch.delenv("NEXA_TOKEN")
    assert resolve_token(store, "default", environ=os.environ) == "stored-token"


def test_insecure_existing_files_are_rejected(tmp_path):
    store = ConfigStore(tmp_path)
    store.save(CliConfig(profiles={"default": Profile()}))
    store.save_token("default", "stored-token")

    (tmp_path / "config.toml").chmod(0o644)
    with pytest.raises(CliError) as config_error:
        store.load()
    assert config_error.value.exit_code == 2

    (tmp_path / "config.toml").chmod(0o600)
    (tmp_path / "credentials.json").chmod(0o644)
    with pytest.raises(CliError) as credentials_error:
        store.get_token("default")
    assert credentials_error.value.exit_code == 2


def test_broken_symlinks_are_rejected(tmp_path):
    store = ConfigStore(tmp_path)
    store.config_path.symlink_to(tmp_path / "missing-config.toml")
    with pytest.raises(CliError) as config_error:
        store.load()
    assert config_error.value.exit_code == 2

    store.config_path.unlink()
    store.credentials_path.symlink_to(tmp_path / "missing-credentials.json")
    with pytest.raises(CliError) as credentials_error:
        store.get_token("default")
    assert credentials_error.value.exit_code == 2


def test_insecure_empty_config_directory_is_rejected(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    root.chmod(0o755)
    with pytest.raises(CliError) as raised:
        ConfigStore(root).load()
    assert raised.value.exit_code == 2


def test_unknown_profile_is_a_local_cli_error():
    with pytest.raises(CliError) as raised:
        resolve_context(
            CliConfig(profiles={"default": Profile()}),
            endpoint=None,
            profile="typo",
            tenant_id=None,
            environ={},
        )
    assert raised.value.exit_code == 2
