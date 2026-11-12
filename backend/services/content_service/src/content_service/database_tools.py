"""Read-only preflight and explicit schema/data migration commands.

Credentials are accepted only through environment variables; errors deliberately
omit driver messages, SQL parameters and connection URLs.
"""

import argparse
import base64
import hashlib
import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Boolean, DateTime, MetaData, String, func, select, text
from sqlalchemy.engine import make_url

from . import dameng_alembic  # noqa: F401
from .database_backend import create_database_engine


class DatabaseCheckError(RuntimeError):
    pass


def migration_config(url: str) -> Config:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.attributes["database_url"] = url
    return config


def check_database(connection, *, require_current: bool = True) -> dict:
    connection.execute(select(1)).scalar_one()
    current = set(MigrationContext.configure(connection).get_current_heads())
    expected = set(
        ScriptDirectory.from_config(migration_config(str(connection.engine.url))).get_heads()
    )
    if require_current and current != expected:
        raise DatabaseCheckError("Schema revision differs; run a reviewed schema migration first")
    if connection.dialect.name == "dm":
        mode = connection.scalar(text("SELECT MODE$ FROM V$INSTANCE"))
        if str(mode).strip().upper() == "STANDBY":
            raise DatabaseCheckError("The application requires a primary read/write DM connection")
        empty_is_null = connection.scalar(
            text("SELECT CASE WHEN '' IS NULL THEN 1 ELSE 0 END FROM DUAL")
        )
        if empty_is_null:
            raise DatabaseCheckError(
                "This application requires distinct empty strings and NULL; ask DBA to review DM compatibility settings"
            )
    return {
        "backend": connection.dialect.name,
        "schema_current": current == expected,
        "revisions": sorted(current),
        "expected_revisions": sorted(expected),
    }


def _canonical(value, column):
    if value is None:
        return None
    if hasattr(value, "read"):
        value = value.read()
    if isinstance(column.type, Boolean):
        return bool(value)
    if isinstance(column.type, DateTime) and isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(timespec="microseconds")
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    return value


def _digest(connection, table, source_columns) -> str:
    digest = hashlib.sha256()
    statement = select(table).order_by(*table.primary_key.columns)
    for row in connection.execute(statement).mappings():
        values = [_canonical(row[column.name], column) for column in source_columns]
        digest.update(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def copy_database(source_engine, target_engine, *, apply: bool = False) -> dict:
    """Copy an already-migrated, frozen SQLite snapshot into an empty current schema.

    No DDL, deletes, constraint disabling or per-table commits. A data failure
    rolls back the target transaction. The caller must stop all target writers.
    """
    if source_engine.dialect.name != "sqlite":
        raise DatabaseCheckError("The migration source must be a SQLite snapshot")
    if source_engine.url == target_engine.url:
        raise DatabaseCheckError("Source and target must be different databases")
    with source_engine.connect() as source, target_engine.begin() as target:
        source.exec_driver_sql("PRAGMA query_only=ON")
        source.exec_driver_sql("BEGIN")
        if source.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise DatabaseCheckError("SQLite snapshot contains broken foreign-key references")
        check_database(source)
        check_database(target)
        src = MetaData()
        dst = MetaData()
        src.reflect(bind=source)
        dst.reflect(bind=target)
        if set(src.tables) != set(dst.tables):
            raise DatabaseCheckError("Source and target table sets differ")
        report = {"mode": "apply" if apply else "dry-run", "tables": {}}
        tables = [table for table in src.sorted_tables if table.name != "alembic_version"]
        if "jobs" in src.tables:
            jobs = src.tables["jobs"]
            if source.scalar(
                select(func.count()).select_from(jobs).where(jobs.c.status == "running")
            ):
                raise DatabaseCheckError(
                    "Source has running jobs; drain workers before taking the snapshot"
                )
        for table in tables:
            other = dst.tables[table.name]
            if set(table.c.keys()) != set(other.c.keys()) or not list(table.primary_key.columns):
                raise DatabaseCheckError(f"Column/primary-key mismatch: {table.name}")
            count = target.scalar(select(func.count()).select_from(other))
            # Schema migration 0014 seeds precisely this singleton, never user data.
            if table.name == "local_access_lock":
                if target.execute(select(other.c.id)).scalars().all() != [1]:
                    raise DatabaseCheckError("Target lock singleton is invalid")
                if source.execute(select(table.c.id)).scalars().all() != [1]:
                    raise DatabaseCheckError("Source lock singleton is invalid")
            elif table.name == "rule_seed_state":
                seeded = target.execute(select(other)).mappings().all()
                if seeded != [{"id": "advertising-law-v1", "completed": False}]:
                    raise DatabaseCheckError("Target rule seed state is not a fresh migration")
                if source.execute(select(table.c.id)).scalars().all() != ["advertising-law-v1"]:
                    raise DatabaseCheckError("Source rule seed singleton is invalid")
            elif count:
                raise DatabaseCheckError(f"Target must be empty: {table.name}")
            report["tables"][table.name] = {
                "rows": source.scalar(select(func.count()).select_from(table)),
                "sha256": _digest(source, table, list(table.c)),
            }
            # Check stricter target string lengths before any target writes.
            for column in other.c:
                if isinstance(column.type, String) and column.type.length:
                    maximum = source.scalar(select(func.max(func.length(table.c[column.name]))))
                    if maximum and maximum > column.type.length:
                        raise DatabaseCheckError(
                            f"Target string length exceeded: {table.name}.{column.name}"
                        )
        if not apply:
            return report
        for table in tables:
            if table.name == "local_access_lock":
                continue
            other = dst.tables[table.name]
            if table.name == "rule_seed_state":
                completed = source.scalar(select(table.c.completed))
                target.execute(
                    other.update()
                    .where(other.c.id == "advertising-law-v1")
                    .values(completed=completed)
                )
                if _digest(target, other, list(table.c)) != report["tables"][table.name]["sha256"]:
                    raise DatabaseCheckError("Rule seed state verification failed")
                continue
            self_keys = [fk for fk in table.foreign_keys if fk.column.table.name == table.name]
            if any(not fk.parent.nullable for fk in self_keys):
                raise DatabaseCheckError(
                    f"Non-null self reference requires a reviewed ordering: {table.name}"
                )
            delayed = []
            for row in source.execute(select(table)).mappings():
                values = dict(row)
                restore = {
                    fk.parent.name: values[fk.parent.name]
                    for fk in self_keys
                    if values[fk.parent.name] is not None
                }
                if restore:
                    delayed.append(
                        (
                            {column.name: values[column.name] for column in table.primary_key},
                            restore,
                        )
                    )
                    values.update(dict.fromkeys(restore))
                target.execute(other.insert(), values)
            for key, values in delayed:
                statement = other.update()
                for name, value in key.items():
                    statement = statement.where(other.c[name] == value)
                target.execute(statement.values(**values))
            expected = report["tables"][table.name]
            actual_count = target.scalar(select(func.count()).select_from(other))
            if (
                actual_count != expected["rows"]
                or _digest(target, other, list(table.c)) != expected["sha256"]
            ):
                raise DatabaseCheckError(f"Copied data verification failed: {table.name}")
        return report


def _source_engine(url: str):
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.query:
        raise DatabaseCheckError("Source URL must point to an existing SQLite snapshot file")
    path = Path(parsed.database).resolve()
    if not path.is_file():
        raise DatabaseCheckError("SQLite snapshot file does not exist")
    readonly = parsed.set(database=f"file:{path.as_posix()}", query={"uri": "true", "mode": "ro"})
    return create_database_engine(readonly, {"CONTENT_DATABASE_BACKEND": "sqlite"})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["preflight", "upgrade", "copy"])
    parser.add_argument("--apply", action="store_true", help="Explicitly permit database writes")
    parser.add_argument("--allow-uninitialized", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument(
        "--backup-reference", help="DBA backup/change-ticket reference, never credentials"
    )
    args = parser.parse_args(argv)
    engines = []
    try:
        url = os.environ.get("CONTENT_DATABASE_URL", "")
        if not url:
            raise DatabaseCheckError("CONTENT_DATABASE_URL is required")
        target = create_database_engine(url)
        engines.append(target)
        if args.operation == "preflight":
            with target.connect() as connection:
                report = check_database(connection, require_current=not args.allow_uninitialized)
        elif args.operation == "upgrade":
            if not args.apply or not args.writers_stopped or not args.backup_reference:
                raise DatabaseCheckError(
                    "Upgrade requires --apply --writers-stopped --backup-reference"
                )
            with target.connect() as connection:
                check_database(connection, require_current=False)
            command.upgrade(migration_config(url), "head")
            with target.connect() as connection:
                report = check_database(connection)
        else:
            if args.apply and (not args.writers_stopped or not args.backup_reference):
                raise DatabaseCheckError("Copy writes require --writers-stopped --backup-reference")
            source_url = os.environ.get("CONTENT_MIGRATION_SOURCE_URL", "")
            if not source_url:
                raise DatabaseCheckError("CONTENT_MIGRATION_SOURCE_URL is required")
            source = _source_engine(source_url)
            engines.append(source)
            report = copy_database(source, target, apply=args.apply)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except DatabaseCheckError as exc:
        print(f"Database check stopped: {exc}")
        return 2
    except Exception as exc:
        print(
            f"Database operation failed ({type(exc).__name__}); connection values and driver details were not logged"
        )
        return 1
    finally:
        for engine in engines:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
