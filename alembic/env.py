from __future__ import with_statement

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.database import Base
from app import models  # noqa: F401 - imports model metadata


config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"check_same_thread": False, "timeout": 30}
        if get_settings().database_url.startswith("sqlite")
        else {},
    )
    with connectable.connect() as connection:
        if get_settings().database_url.startswith("sqlite"):
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("PRAGMA busy_timeout=30000")
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        # SQLite can auto-commit DDL while leaving Alembic's version-table DML
        # pending in the Python sqlite driver. Commit explicitly so a fresh
        # database never ends up with tables but no revision row.
        if connection.in_transaction():
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
