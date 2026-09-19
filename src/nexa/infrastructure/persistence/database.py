from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


class DatabaseConfigurationError(ValueError):
    """Raised when persistence configuration is unsafe or unsupported."""


def create_database_engine(database_url: str, **engine_options: object) -> Engine:
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise DatabaseConfigurationError(
            "Database URL must use postgresql+psycopg and include a database name"
        ) from exc
    if url.drivername != "postgresql+psycopg" or not url.database:
        raise DatabaseConfigurationError(
            "Database URL must use postgresql+psycopg and include a database name"
        )
    options: dict[str, object] = {
        "isolation_level": "READ COMMITTED",
        "pool_pre_ping": True,
        "hide_parameters": True,
    }
    options.update(engine_options)
    return create_engine(url, **options)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, autoflush=False, expire_on_commit=False)
