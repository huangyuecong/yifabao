from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import load_settings
from .database_backend import auto_migrate_enabled, create_database_engine
from .time_utils import UTCDateTime


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[type, UTCDateTime]] = {datetime: UTCDateTime()}


settings = load_settings()
engine = create_database_engine(settings.database_url)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def init_database() -> None:
    service_root = Path(__file__).resolve().parents[2]
    config = Config(str(service_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    if not auto_migrate_enabled(settings.database_url):
        from .database_tools import check_database

        with engine.connect() as connection:
            check_database(connection)
        return
    tables = set(inspect(engine).get_table_names())
    if (
        engine.dialect.name == "sqlite"
        and "content_tasks" in tables
        and "alembic_version" not in tables
    ):
        command.stamp(config, "content_0001")
    command.upgrade(config, "head")
