import json

from typer.testing import CliRunner

from nexa.cli.app import app
from nexa.cli.config import ConfigStore


def test_config_commands_persist_non_secret_values(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    runner = CliRunner()
    assert runner.invoke(app, ["config", "set-endpoint", "https://nexa.test"]).exit_code == 0
    assert runner.invoke(app, ["config", "set-tenant", "tenant-1"]).exit_code == 0
    assert runner.invoke(app, ["config", "use-profile", "work"]).exit_code == 0
    store = ConfigStore(tmp_path / "nexa")
    loaded = store.load()
    assert loaded.active_profile == "work"
    assert loaded.profiles["default"].endpoint == "https://nexa.test"
    assert loaded.profiles["default"].tenant_id == "tenant-1"


def test_config_show_redacts_saved_token(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    store = ConfigStore(tmp_path / "nexa")
    store.save_token("default", "opaque-secret")
    result = CliRunner().invoke(app, ["--output", "json", "config", "show"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["token"] == "<redacted>"
    assert "opaque-secret" not in result.stdout


def test_config_write_failure_is_a_local_cli_error(tmp_path, monkeypatch):
    config_home = tmp_path / "not-a-directory"
    config_home.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    result = CliRunner().invoke(app, ["config", "set-endpoint", "https://nexa.test"])

    assert result.exit_code == 2
    assert "configuration could not be written" in result.stderr
    assert "Traceback" not in result.stderr
