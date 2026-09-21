import os
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-postgres",
        action="store_true",
        default=False,
        help="run PostgreSQL 17 integration tests",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "postgres: requires a guarded PostgreSQL 17 test database")
    config.addinivalue_line(
        "markers",
        "docker: requires explicit NEXA_RUN_DOCKER=1 and an exact NEXA_B09_IMAGE_REF",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-postgres"):
        return
    skip = pytest.mark.skip(reason="requires --run-postgres and NEXA_TEST_DATABASE_URL")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip)


def _guard_test_database_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise pytest.UsageError("NEXA_TEST_DATABASE_URL is not a valid URL") from exc
    database_name = parsed.path.removeprefix("/")
    if parsed.scheme != "postgresql+psycopg":
        raise pytest.UsageError("NEXA_TEST_DATABASE_URL must use postgresql+psycopg")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1", "postgres"}:
        raise pytest.UsageError("NEXA_TEST_DATABASE_URL must target a loopback or CI postgres host")
    if port is not None and not 1 <= port <= 65535:
        raise pytest.UsageError("NEXA_TEST_DATABASE_URL contains an invalid port")
    if not database_name.startswith("nexa_b05_test_"):
        raise pytest.UsageError("PostgreSQL test database name must start with nexa_b05_test_")
    if url == os.environ.get("NEXA_DATABASE_URL"):
        raise pytest.UsageError("Refusing to use NEXA_DATABASE_URL as the destructive test target")


@pytest.fixture(scope="session")
def postgres_database_url(pytestconfig: pytest.Config) -> str:
    if not pytestconfig.getoption("--run-postgres"):
        pytest.skip("PostgreSQL integration mode is disabled")
    url = os.environ.get("NEXA_TEST_DATABASE_URL", "")
    if not url:
        raise pytest.UsageError("--run-postgres requires NEXA_TEST_DATABASE_URL")
    _guard_test_database_url(url)
    return url


@pytest.fixture
def clean_postgres_database(postgres_database_url: str) -> Iterator[str]:
    engine = create_engine(postgres_database_url, isolation_level="AUTOCOMMIT")
    validated = False
    try:
        with engine.connect() as connection:
            major = connection.execute(text("SHOW server_version_num")).scalar_one()
            if int(major) // 10_000 != 17:
                raise pytest.UsageError("B05 integration tests require PostgreSQL major version 17")
            validated = True
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        yield postgres_database_url
    finally:
        if validated:
            with engine.connect() as connection:
                connection.execute(text("DROP SCHEMA public CASCADE"))
                connection.execute(text("CREATE SCHEMA public"))
        engine.dispose()


@pytest.fixture
def migrated_postgres_engine(clean_postgres_database: str) -> Iterator[Engine]:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", clean_postgres_database.replace("%", "%%"))
    command.upgrade(config, "head")
    engine = create_engine(clean_postgres_database, pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()
