from datetime import datetime

import pytest
from alembic import command
from alembic.ddl.base import ColumnNullable
from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    select,
)
from sqlalchemy.schema import CreateTable

from content_service.database_backend import (
    auto_migrate_enabled,
    create_database_engine,
    database_options,
)
from content_service.database_tools import (
    DatabaseCheckError,
    _source_engine,
    copy_database,
    main,
    migration_config,
)


def test_dm_requires_explicit_enable_and_safe_options(tmp_path):
    url = "dm+dmPython://user:secret@db:5236/APP"
    with pytest.raises(ValueError, match="explicitly"):
        database_options(url, {})
    options = database_options(url, {"CONTENT_DATABASE_BACKEND": "dm"})
    assert options["connect_args"]["autoCommit"] is False
    assert options["hide_parameters"] is True
    assert options["pool_pre_ping"] is True
    assert not auto_migrate_enabled(url, {})
    assert auto_migrate_enabled("sqlite://", {})
    with pytest.raises(ValueError, match="query"):
        database_options(url + "?autoCommit=true", {"CONTENT_DATABASE_BACKEND": "dm"})
    (tmp_path / "dm_svc.conf").write_text("CONTENT_DM=(localhost:5236)")
    options = database_options(
        "dm+dmPython://user:secret@CONTENT_DM/APP",
        {
            "CONTENT_DATABASE_BACKEND": "dm",
            "CONTENT_DM_SERVICE_NAME": "CONTENT_DM",
            "CONTENT_DM_SVC_PATH": str(tmp_path),
        },
    )
    assert "dsn" not in options["connect_args"]
    assert options["connect_args"]["dmsvc_path"] == str(tmp_path)


def test_dm_vendor_dialect_compiles_project_schema_and_alter():
    pytest.importorskip("dmSQLAlchemy")
    from dmSQLAlchemy.dmpython import DMDialect_dmPython

    from content_service import models
    from content_service.db import Base

    dialect = DMDialect_dmPython()
    for table in Base.metadata.sorted_tables:
        assert str(CreateTable(table).compile(dialect=dialect)).startswith("\nCREATE TABLE")
    assert "MODIFY available_at NOT NULL" in str(
        ColumnNullable("jobs", "available_at", False).compile(dialect=dialect)
    )
    # A real production query includes a scalar candidate subquery and limit.
    from content_service.job_leases import _claimable

    candidate = select(models.Job.id).where(_claimable(datetime.now())).limit(1).scalar_subquery()
    sql = str(
        models.Job.__table__.update()
        .where(models.Job.id == candidate)
        .values(status="running")
        .compile(dialect=dialect)
    )
    assert "UPDATE jobs SET" in sql


def test_dm_service_alias_reaches_driver_as_host_without_synthesized_dsn(tmp_path):
    pytest.importorskip("dmSQLAlchemy")
    (tmp_path / "dm_svc.conf").write_text("CONTENT_DM=(localhost:5236)")
    engine = create_database_engine(
        "dm+dmPython://user:secret@CONTENT_DM/APP",
        {
            "CONTENT_DATABASE_BACKEND": "dm",
            "CONTENT_DM_SERVICE_NAME": "CONTENT_DM",
            "CONTENT_DM_SVC_PATH": str(tmp_path),
        },
    )
    args, params = engine.dialect.create_connect_args(engine.url)
    assert params["dsn"].endswith(":5236")
    for listener in engine.dialect.dispatch.do_connect:
        listener(engine.dialect, None, args, params)
    assert params["host"] == "CONTENT_DM"
    assert "dsn" not in params
    engine.dispose()


@pytest.fixture
def migrated_pair(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_DATABASE_BACKEND", "sqlite")
    engines = []
    for name in ("source", "target"):
        url = f"sqlite:///{tmp_path / (name + '.db')}"
        monkeypatch.setenv("CONTENT_DATABASE_URL", url)
        command.upgrade(migration_config(url), "head")
        engine = create_engine(url)
        engines.append(engine)
    yield engines
    for engine in engines:
        engine.dispose()


def test_copy_dry_run_preserves_target_then_copies_and_verifies(migrated_pair):
    source, target = migrated_pair
    meta = MetaData()
    meta.reflect(bind=source)
    users = meta.tables["local_users"]
    now = datetime(2026, 10, 5, 1, 2, 3, 123456)
    with source.begin() as conn:
        conn.execute(
            users.insert(),
            {
                "id": "user-fixed-id",
                "subject": "zhang",
                "display_name": "张三",
                "department": "",
                "is_project_admin": True,
                "permissions_json": "[]",
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "last_seen_at": now,
            },
        )
    report = copy_database(source, target)
    assert report["mode"] == "dry-run"
    with target.connect() as conn:
        assert conn.execute(select(users)).first() is None
    report = copy_database(source, target, apply=True)
    assert report["tables"]["local_users"]["rows"] == 1
    with target.connect() as conn:
        row = conn.execute(select(users)).mappings().one()
        assert row["id"] == "user-fixed-id"
        assert row["display_name"] == "张三"
        assert row["created_at"] == now
    with pytest.raises(DatabaseCheckError, match="empty"):
        copy_database(source, target, apply=True)


def test_copy_self_reference_restored(migrated_pair):
    source, target = migrated_pair
    meta = MetaData()
    nodes = Table(
        "test_nodes",
        meta,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey("test_nodes.id")),
        Column("label", String(3)),
    )
    meta.create_all(source)
    meta.create_all(target)
    with source.begin() as conn:
        conn.execute(
            nodes.insert(),
            [
                {"id": 1, "parent_id": 2, "label": "one"},
                {"id": 2, "parent_id": None, "label": "two"},
            ],
        )
    copy_database(source, target, apply=True)
    with target.connect() as conn:
        assert conn.scalar(select(nodes.c.parent_id).where(nodes.c.id == 1)) == 2


def test_copy_failure_rolls_back_already_inserted_rows(migrated_pair):
    source, target = migrated_pair
    meta = MetaData()
    rows = Table("test_rows", meta, Column("id", Integer, primary_key=True))
    meta.create_all(source)
    meta.create_all(target)
    with source.begin() as connection:
        connection.execute(rows.insert(), [{"id": 1}, {"id": 2}])

    @event.listens_for(target, "before_cursor_execute")
    def fail_second_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO test_rows") and parameters == (2,):
            raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError, match="injected failure"):
        copy_database(source, target, apply=True)
    with target.connect() as connection:
        assert connection.execute(select(rows)).first() is None


def test_rule_seed_completed_state_survives_copy(migrated_pair):
    source, target = migrated_pair
    meta = MetaData()
    meta.reflect(bind=source)
    state = meta.tables["rule_seed_state"]
    with source.begin() as connection:
        connection.execute(state.update().values(completed=True))
    copy_database(source, target, apply=True)
    with target.connect() as connection:
        assert connection.scalar(select(state.c.completed)) is True


def test_copy_rejects_broken_foreign_keys_without_writes(migrated_pair):
    source, target = migrated_pair
    meta = MetaData()
    nodes = Table(
        "test_nodes",
        meta,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey("test_nodes.id")),
    )
    meta.create_all(source)
    meta.create_all(target)
    with source.begin() as conn:
        conn.execute(nodes.insert(), {"id": 1, "parent_id": 99})
    with pytest.raises(DatabaseCheckError, match="foreign-key"):
        copy_database(source, target, apply=True)
    with target.connect() as conn:
        assert conn.execute(select(nodes)).first() is None


def test_source_readonly_and_errors_do_not_expose_password(tmp_path, monkeypatch, capsys):
    path = tmp_path / "snapshot.db"
    create_engine(f"sqlite:///{path}").connect().close()
    engine = _source_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn, pytest.raises(Exception, match="readonly"):
            conn.exec_driver_sql("CREATE TABLE forbidden (id integer)")
    finally:
        engine.dispose()
    monkeypatch.setenv(
        "CONTENT_DATABASE_URL", "dm+dmPython://user:never-log-this@db/SCHEMA?bad=true"
    )
    monkeypatch.setenv("CONTENT_DATABASE_BACKEND", "dm")
    assert main(["preflight"]) == 1
    assert "never-log-this" not in capsys.readouterr().out


def test_cli_upgrade_requires_backup_and_maintenance(monkeypatch, capsys):
    monkeypatch.setenv("CONTENT_DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("CONTENT_DATABASE_URL", "sqlite://")
    assert main(["upgrade", "--apply"]) == 2
    assert "writers-stopped" in capsys.readouterr().out
