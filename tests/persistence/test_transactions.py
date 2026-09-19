import pytest
from sqlalchemy.exc import DBAPIError

from nexa.infrastructure.persistence.database import (
    DatabaseConfigurationError,
    create_database_engine,
)
from nexa.infrastructure.persistence.transactions import (
    TransactionRetryExhausted,
    is_retryable_transaction_error,
    run_transaction,
)


class _DatabaseFailure(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _db_error(sqlstate: str) -> DBAPIError:
    return DBAPIError("statement", {}, _DatabaseFailure(sqlstate), False)


class _Session:
    def __init__(self) -> None:
        self.closed = False
        self.rolled_back = False

    def begin(self) -> "_Session":
        return self

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def test_database_engine_requires_psycopg_url_and_redacts_parameters() -> None:
    with pytest.raises(DatabaseConfigurationError, match=r"postgresql\+psycopg"):
        create_database_engine("sqlite:///temporary.db")

    engine = create_database_engine("postgresql+psycopg://user:secret@localhost/nexa")
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.url.render_as_string(hide_password=True).endswith("@localhost/nexa")
        assert "secret" not in repr(engine.url)
        assert engine.hide_parameters is True
    finally:
        engine.dispose()


@pytest.mark.parametrize("sqlstate", ["40001", "40P01"])
def test_retry_classifier_accepts_only_transaction_conflicts(sqlstate: str) -> None:
    assert is_retryable_transaction_error(_db_error(sqlstate))


@pytest.mark.parametrize("sqlstate", ["23505", "23503", "08006", "57P01"])
def test_retry_classifier_rejects_data_and_unknown_commit_errors(sqlstate: str) -> None:
    assert not is_retryable_transaction_error(_db_error(sqlstate))


def test_run_transaction_discards_failed_sessions_and_uses_fresh_jitter() -> None:
    sessions: list[_Session] = []
    jitters: list[float] = []
    sleeps: list[float] = []
    attempts = 0

    def factory() -> _Session:
        session = _Session()
        sessions.append(session)
        return session

    def operation(_session: _Session) -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _db_error("40001")
        return "committed"

    def jitter() -> float:
        value = 0.01 + len(jitters) * 0.01
        jitters.append(value)
        return value

    result = run_transaction(
        factory, operation, jitter=jitter, sleeper=sleeps.append, max_attempts=3
    )

    assert result == "committed"
    assert attempts == 3
    assert len(sessions) == 3
    assert all(session.closed for session in sessions)
    assert [session.rolled_back for session in sessions] == [True, True, False]
    assert sleeps == [0.01, 0.02]


def test_run_transaction_stops_after_three_retryable_failures() -> None:
    sessions: list[_Session] = []

    def factory() -> _Session:
        session = _Session()
        sessions.append(session)
        return session

    def operation(_session: _Session) -> None:
        raise _db_error("40P01")

    with pytest.raises(TransactionRetryExhausted):
        run_transaction(factory, operation, jitter=lambda: 0.01, sleeper=lambda _: None)

    assert len(sessions) == 3
    assert all(session.closed and session.rolled_back for session in sessions)


def test_run_transaction_does_not_retry_business_conflict() -> None:
    sessions: list[_Session] = []

    def factory() -> _Session:
        session = _Session()
        sessions.append(session)
        return session

    with pytest.raises(DBAPIError):
        run_transaction(factory, lambda _: (_ for _ in ()).throw(_db_error("23505")))

    assert len(sessions) == 1
    assert sessions[0].closed and sessions[0].rolled_back
