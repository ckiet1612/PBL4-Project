from nexa.cli.config import CliConfig, ConfigStore, Profile


def test_store_uses_private_modes_and_never_puts_token_in_config(tmp_path):
    store = ConfigStore(tmp_path)
    store.save_token("default", "opaque-secret")
    store.save(CliConfig(profiles={"default": Profile(endpoint="https://nexa", tenant_id="t")}))
    assert oct((tmp_path / "credentials.json").stat().st_mode & 0o777) == "0o600"
    assert "opaque-secret" not in (tmp_path / "config.toml").read_text()
    assert oct(tmp_path.stat().st_mode & 0o777) == "0o700"
    assert store.get_token("default") == "opaque-secret"
