import tomllib
from pathlib import Path

from nexa.cli.app import app


def test_product_app_exposes_root_groups():
    names = {group.name for group in app.registered_groups}
    assert {"config", "token", "artifact", "job", "admin"}.issubset(names)
    assert "template" not in names


def test_both_cli_entry_points_are_packaged():
    with Path("pyproject.toml").open("rb") as source:
        scripts = tomllib.load(source)["project"]["scripts"]
    assert scripts["nexa"] == "nexa.cli.app:app"
    assert scripts["nexa-maintenance"] == "nexa.cli.main:app"
