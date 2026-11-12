from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.engine import make_url

from content_service import (
    automation_models,  # noqa: F401
    catalog_models,  # noqa: F401
    dameng_alembic,  # noqa: F401
    models,  # noqa: F401
)
from content_service.config import load_settings
from content_service.database_backend import create_database_engine
from content_service.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
database_url = config.attributes.get("database_url") or load_settings().database_url
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
target_metadata = Base.metadata


def ensure_sqlite_parent() -> None:
    url = make_url(config.get_main_option("sqlalchemy.url"))
    if url.drivername == "sqlite" and url.database not in (None, "", ":memory:"):
        Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    ensure_sqlite_parent()
    provided = config.attributes.get("connection")
    if provided is not None:
        context.configure(connection=provided, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    connectable = create_database_engine(database_url)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
