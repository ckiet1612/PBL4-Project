import random
import time
from collections.abc import Callable

from sqlalchemy.exc import DBAPIError

_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})


class TransactionRetryExhausted(RuntimeError):
    """Raised after the bounded retry budget is exhausted."""


def is_retryable_transaction_error(error: BaseException) -> bool:
    if not isinstance(error, DBAPIError):
        return False
    sqlstate = getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)
    return sqlstate in _RETRYABLE_SQLSTATES


def run_transaction[S, T](
    session_factory: Callable[[], S],
    operation: Callable[[S], T],
    *,
    max_attempts: int = 3,
    jitter: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> T:
    if not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts must be between 1 and 3")
    jitter = jitter or (lambda: random.uniform(0.010, 0.050))

    for attempt_number in range(1, max_attempts + 1):
        session = session_factory()
        try:
            with session.begin():  # type: ignore[attr-defined]
                return operation(session)
        except DBAPIError as exc:
            session.rollback()  # type: ignore[attr-defined]
            if not is_retryable_transaction_error(exc):
                raise
            if attempt_number == max_attempts:
                raise TransactionRetryExhausted(
                    f"Transaction conflict persisted for {max_attempts} attempts"
                ) from exc
        finally:
            session.close()  # type: ignore[attr-defined]

        delay = jitter()
        if not 0.010 <= delay <= 0.050:
            raise ValueError("transaction retry jitter must be between 10 and 50 milliseconds")
        sleeper(delay)

    raise AssertionError("unreachable")
