"""Content ORM through the real DM dialect and an offline simulated DBAPI boundary."""
import re
import sqlite3
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, insert, inspect
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import FlushError
from sqlalchemy.schema import CreateTable

from content_service.database_backend import create_database_engine
from content_service.db import Base
from content_service.models import AuditLog, ContentTask, LocalUser
from content_service.workflow import INITIAL_STAGE, STAGE_KEYS, initialize_workflow


class InsertCursor:
    description = None
    rowcount = 1
    arraysize = 1
    output_stream = 0
    # The deployed DM driver returns an opaque ROWID. String primary keys
    # supplied by the application must not be recovered by querying this value.
    lastrowid = "AAAAAAAAAAAAAAAAAB"

    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, statement, parameters=None):
        self.connection.statements.append(statement)
        upper = statement.upper()
        assert "ROWID" not in upper and "SCOPE_IDENTITY" not in upper
        assert self.connection.incomplete_returning or " RETURNING " not in upper
        self.description = None
        if "MODE$" in upper:
            self.rows = [("PRIMARY",)]
        elif "CASE WHEN '' IS NULL" in upper:
            self.rows = [(0,)]
        elif "SET_SESSION_IDENTITY_CHECK" in upper or upper.startswith(("SET SCHEMA", "SELECT 1")):
            self.rows = [(1,)]
        elif upper.startswith("SELECT"):
            # SQLite stores the simulated DBAPI data. The only DM-only syntax
            # in seeding reads is the result limit; preserve its bound.
            sql = re.sub(r"WHERE ROWNUM <= (\d+|\?)", r"LIMIT \1", statement)
            sql = re.sub(r"FETCH FIRST (\d+|\?) ROWS ONLY", r"LIMIT \1", sql)
            cursor = self.connection.data.execute(sql, parameters or ())
            self.description = cursor.description
            datetime_names = {"created_at", "updated_at", "confirmed_at", "completed_at",
                              "archived_at", "resolved_at", "last_seen_at"}
            self.rows = [
                tuple(
                    datetime.fromisoformat(value)
                    if isinstance(value, str)
                    and any(
                        item[0] == name or item[0].endswith("_" + name) for name in datetime_names
                    )
                    else value
                    for item, value in zip(cursor.description, row, strict=True)
                )
                for row in cursor.fetchall()
            ]
        else:
            match = re.fullmatch(
                r"INSERT INTO (\w+) \(([^)]+)\) VALUES \(([^)]+)\)(?: RETURNING \1.id INTO \?)?",
                statement,
            )
            assert match, f"Unexpected SQL: {statement}"
            table, columns, _ = match.groups()
            if table == self.connection.fail_table or (
                self.connection.fail_insert_at is not None
                and len(self.connection.inserts) + 1 == self.connection.fail_insert_at
            ):
                raise RuntimeError("synthetic child insert failure")
            if " RETURNING " in statement:
                assert parameters[-1] is None
                parameters = parameters[:-1]
            values = dict(
                zip((column.strip() for column in columns.split(",")), parameters, strict=True)
            )
            self.connection.pending.append((table, values))
            self.connection.inserts.append((table, values))
            sql = statement.split(" RETURNING ")[0]
            self.connection.data.execute(
                sql,
                [
                    value.isoformat(" ") if isinstance(value, datetime) else value
                    for value in parameters
                ],
            )
            self.rowcount = (
                self.connection.insert_rowcounts.pop(0) if self.connection.insert_rowcounts else 1
            )
            self.rows = []
        return self

    def executemany(self, statement, parameters):
        self.connection.executemany_calls += 1
        returned_ids = []
        for values in parameters:
            self.execute(statement, values)
            returned_ids.append(self.connection.inserts[-1][1]["id"])
        self.rowcount = len(parameters)
        # Reproduce the missing primary-key result rows reported by SQLAlchemy
        # on the target server, rather than inventing a successful return array.
        result = [list(values) for values in zip(*parameters, strict=True)]
        if " RETURNING " in statement:
            result[-1] = returned_ids[:-1]
        return result

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def setinputsizes(self, *args, **kwargs):
        pass

    def close(self):
        pass


def make_engine(*, original=False):
    driver = pytest.importorskip("dmPython")
    pytest.importorskip("dmSQLAlchemy.dmpython")
    connection = InsertConnection()
    connection.incomplete_returning = original
    module = SimpleNamespace(**{name: getattr(driver, name) for name in dir(driver)})
    module.connect = lambda *args, **kwargs: connection
    url = "dm+dmPython://test:synthetic@unused:5239/APP_CONTENT"
    engine = (create_engine(url, module=module) if original else create_database_engine(
        url, {"CONTENT_DATABASE_BACKEND": "dm", "CONTENT_DM_SCHEMA": "APP_CONTENT"},
        module=module))
    engine.dialect.initialize = lambda _c: None
    return engine, connection


@pytest.fixture
def dm_engine():
    engine, connection = make_engine()
    yield engine, connection
    engine.dispose()
    connection.data.close()


def task_with_workflow(session, mode):
    task = ContentTask(owner_id="test-owner", name="test-task", mode=mode,
                       current_stage=INITIAL_STAGE[mode])
    session.add(task)
    session.flush()
    return task, initialize_workflow(session, task, "test-owner")


def test_original_driver_reproduces_confirmed_production_workflow_flush_error():
    engine, connection = make_engine(original=True)
    try:
        with Session(engine) as session, pytest.raises(FlushError, match="correct number of INSERTed rows"):
            task_with_workflow(session, "image")
        assert connection.executemany_calls == 1
        assert sum(table == "task_stages" for table, _ in connection.inserts) == len(STAGE_KEYS)
        assert connection.commits == 0
        assert connection.data.execute("SELECT COUNT(*) FROM content_tasks").fetchone() == (0,)
    finally:
        engine.dispose()
        connection.data.close()


@pytest.mark.parametrize("mode", ["image", "video"])
def test_real_task_workflow_and_audit_batch_commit_all_application_uuids(dm_engine, mode):
    engine, connection = dm_engine
    with Session(engine, expire_on_commit=False) as session:
        task, stages = task_with_workflow(session, mode)
        assert len(stages) == len(STAGE_KEYS)
        for row in stages:
            assert str(UUID(row.id)) == row.id and inspect(row).identity == (row.id,)
        logs = [AuditLog(owner_id="test-owner", action="task.created", resource_type="content_task",
                         resource_id=task.id, details_json="{}") for _ in range(3)]
        session.add_all(logs)
        session.commit()
        assert len({log.id for log in logs}) == 3
    assert connection.executemany_calls == 0 and connection.commits == 1
    assert not any(" RETURNING " in sql for sql in connection.statements)
    assert connection.data.execute("SELECT COUNT(*) FROM task_stages").fetchone() == (len(STAGE_KEYS),)
    assert connection.data.execute("SELECT COUNT(*) FROM audit_logs").fetchone() == (3,)


@pytest.mark.parametrize("explicit", [False, True])
def test_core_insert_preserves_parameters_primary_keys_and_counts(dm_engine, explicit):
    engine, connection = dm_engine
    rows = [{"subject": f"test-{i}", "display_name": "user"} for i in range(3)]
    if explicit:
        for row in rows:
            row["id"] = str(uuid4())
    before = deepcopy(rows)
    with engine.begin() as transaction:
        result = transaction.execute(insert(LocalUser), rows)
        assert result.rowcount == 3
        ids = [row[0] for row in result.inserted_primary_key_rows]
        assert ids == [values["id"] for _, values in connection.inserts]
        assert len(set(ids)) == 3
    assert rows == before and connection.executemany_calls == 0 and connection.commits == 1


@pytest.mark.parametrize("counts,expected", [([1, 0, 1], 2), ([1, -1, 1], -1)])
def test_batch_rowcount_uses_actual_driver_counts(dm_engine, counts, expected):
    engine, connection = dm_engine
    connection.insert_rowcounts = counts
    with engine.begin() as transaction:
        result = transaction.execute(insert(LocalUser), [
            {"subject": f"test-{i}", "display_name": "user"} for i in range(3)])
        assert result.rowcount == expected


def test_batch_failure_rolls_back_every_row_without_replaying(dm_engine):
    engine, connection = dm_engine
    connection.fail_insert_at = 3
    with pytest.raises(RuntimeError, match="synthetic"), engine.begin() as transaction:
        transaction.execute(insert(LocalUser), [
            {"subject": f"test-{i}", "display_name": "user"} for i in range(5)])
    assert len(connection.inserts) == 2 and connection.commits == 0
    assert sum(s.startswith("INSERT") for s in connection.statements) == 3
    assert connection.data.execute("SELECT COUNT(*) FROM local_users").fetchone() == (0,)


def test_explicit_uuid_orm_batch_does_not_enter_vendor_returning(dm_engine):
    engine, connection = dm_engine
    users = [LocalUser(id=str(uuid4()), subject=f"test-{i}", display_name="user") for i in range(3)]
    with Session(engine, expire_on_commit=False) as session:
        session.add_all(users)
        session.commit()
        assert all(inspect(row).identity == (row.id,) for row in users)
    assert connection.executemany_calls == 0 and len(connection.committed) == 3


def test_explicit_bulk_returning_refused_before_writes(dm_engine):
    engine, connection = dm_engine
    with engine.begin() as transaction, pytest.raises(StatementError, match="RETURNING"):
        transaction.execute(insert(LocalUser).returning(LocalUser.id), [
            {"subject": f"test-{i}", "display_name": "user"} for i in range(2)])
    assert not connection.inserts


def test_policy_is_engine_specific_and_preserves_sqlite(dm_engine):
    configured, _ = dm_engine
    original, connection = make_engine(original=True)
    sqlite = create_database_engine("sqlite://", {"CONTENT_DATABASE_BACKEND": "sqlite"})
    try:
        assert configured.dialect.insert_executemany_returning is False
        assert original.dialect.insert_executemany_returning is True
        assert sqlite.dialect.use_insertmanyvalues is True
        assert configured.dialect.insert_returning == original.dialect.insert_returning
    finally:
        original.dispose()
        connection.data.close()
        sqlite.dispose()


@pytest.mark.parametrize("context", [None, SimpleNamespace(isinsert=False),
    SimpleNamespace(isinsert=True, compiled=None),
    SimpleNamespace(isinsert=True, compiled=SimpleNamespace(effective_returning=[1])),
    SimpleNamespace(isinsert=True, compiled=SimpleNamespace(effective_returning=[], has_out_parameters=True)),
    SimpleNamespace(isinsert=True, compiled=SimpleNamespace(effective_returning=[], has_out_parameters=False),
                    out_parameters={"explicit": 1})])
def test_execution_hook_leaves_other_statement_paths_unchanged(dm_engine, context):
    engine, _ = dm_engine
    hook, = engine.dialect.dispatch.do_executemany
    assert hook(None, "synthetic", [], context) is None


@pytest.mark.parametrize("mode", ["image", "video"])
def test_workflow_serialization_after_creation_and_video_project_creation(dm_engine, mode):
    from content_service.services import create_or_get_video_project
    from content_service.workflow import workflow_json
    engine, connection = dm_engine
    with Session(engine) as session:
        task, stages = task_with_workflow(session, mode)
        result = workflow_json(session, task)
        assert len(result["stages"]) == len(stages)
        assert result["task_id"] == task.id
        if mode == "video":
            project, created = create_or_get_video_project(session, task, "test-owner")
            assert created and project.id and project.current_revision_id
        session.rollback()
    assert connection.commits == 0

class InsertConnection:
    local_code = 1
    str_case_sensitive = True

    def __init__(self):
        self.statements = []
        self.inserts = []
        self.pending = []
        self.committed = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_table = None
        self.fail_insert_at = None
        self.insert_rowcounts = []
        self.executemany_calls = 0
        self.incomplete_returning = False
        self.data = sqlite3.connect(":memory:")
        for table in Base.metadata.sorted_tables:
            self.data.execute(str(CreateTable(table).compile(dialect=sqlite_dialect())))
        self.data.commit()

    def cursor(self):
        return InsertCursor(self)

    def commit(self):
        self.data.commit()
        self.commits += 1
        self.committed.extend(self.pending)
        self.pending.clear()

    def rollback(self):
        self.data.rollback()
        self.rollbacks += 1
        self.pending.clear()

    def close(self):
        pass
