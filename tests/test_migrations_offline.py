import os
import subprocess
import sys
from pathlib import Path


def test_b06_migration_generates_offline_sql(tmp_path: Path) -> None:
    output_path = tmp_path / "b06.sql"
    environment = os.environ.copy()
    environment["NEXA_DATABASE_URL"] = (
        "postgresql+psycopg://nexa_ci:nexa_ci_test@127.0.0.1:55432/nexa_b06_test_ci"
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "upgrade",
            "20260920_0002",
            "--sql",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE auth_control" in result.stdout
    assert "UPDATE users" in result.stdout
    assert "INSERT INTO auth_control" in result.stdout
    output_path.write_text(result.stdout, encoding="utf-8")
