import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from nexa.infrastructure.persistence.schema import metadata

config = context.config
_PLACEHOLDER_DATABASE_URL = "postgresql+psycopg://invalid/explicit-url-required"
runtime_database_url = os.environ.get("NEXA_DATABASE_URL")
configured_database_url = config.get_main_option("sqlalchemy.url")
if configured_database_url == _PLACEHOLDER_DATABASE_URL and runtime_database_url:
    config.set_main_option("sqlalchemy.url", runtime_database_url.replace("%", "%%"))
elif configured_database_url == _PLACEHOLDER_DATABASE_URL:
    raise RuntimeError("Alembic requires an explicit database URL or NEXA_DATABASE_URL")
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata
_MIGRATION_LOCK_KEY = 7_149_208_505_001


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        hide_parameters=True,
    )
    with connectable.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
        connection.commit()
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK_KEY}
            )
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
