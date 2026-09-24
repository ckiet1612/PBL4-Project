from typer.testing import CliRunner

from nexa.cli.app import app


def test_unwired_template_group_is_not_exposed():
    result = CliRunner().invoke(app, ["template", "list"])
    assert result.exit_code == 2
    assert "No such command" in result.stderr
