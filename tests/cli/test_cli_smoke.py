from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_product_cli_help_keeps_maintenance_separate() -> None:
    product = subprocess.run(
        [sys.executable, "-m", "nexa.cli.app", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    maintenance_command = Path(sys.executable).with_name("nexa-maintenance")
    maintenance_args = (
        [str(maintenance_command), "--help"]
        if maintenance_command.exists()
        else [sys.executable, "-c", "from nexa.cli.main import app; app()", "--help"]
    )
    maintenance = subprocess.run(
        maintenance_args,
        capture_output=True,
        text=True,
        check=False,
    )

    assert product.returncode == 0, product.stderr
    assert "config" in product.stdout
    assert "artifact" in product.stdout
    assert maintenance.returncode == 0, maintenance.stderr
    assert "reopen-worker-bootstrap" in maintenance.stdout
