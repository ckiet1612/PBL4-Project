import pytest

import tests.conftest as postgres_fixtures


class _ScalarResult:
    def scalar_one(self) -> str:
        return "160000"


class _Connection:
    def __init__(self, statements: list[str]) -> None:
        self._statements = statements

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: object) -> _ScalarResult:
        self._statements.append(str(statement))
        return _ScalarResult()


class _Engine:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def connect(self) -> _Connection:
        return _Connection(self.statements)

    def dispose(self) -> None:
        return None


def test_rejected_postgres_version_does_not_run_destructive_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine()
    monkeypatch.setattr(
        postgres_fixtures,
        "create_engine",
        lambda *_args, **_kwargs: engine,
    )
    fixture = postgres_fixtures.clean_postgres_database.__wrapped__(
        "postgresql+psycopg://test:test@127.0.0.1/nexa_b05_test_safety"
    )

    with pytest.raises(pytest.UsageError, match="PostgreSQL major version 17"):
        next(fixture)

    assert all("DROP SCHEMA" not in statement for statement in engine.statements)
