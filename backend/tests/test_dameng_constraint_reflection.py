"""Exercise the installed DM dialect against two schemas in a catalog fixture."""

import re

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import ObjectKind, ObjectScope

from content_service.database_backend import create_database_engine


class CatalogConnection:
    """Execute vendor reflection SQL; translate DM's Oracle outer-join syntax."""

    def __init__(self, connection):
        self.connection = connection
        self.statements = []

    def execute(self, statement, parameters=None, **kwargs):
        parameters = dict(statement.compile().params) | (parameters or {})
        query = str(statement)
        self.statements.append(query)
        # SQLite provides the same catalog rows but accepts ANSI joins only.
        query = re.sub(
            r"FROM (user|all)_constraints ac,\s*(?:user|all)_cons_columns loc,"
            r"\s*(?:user|all)_cons_columns rem WHERE",
            lambda match: (
                f"FROM {match[1]}_constraints ac "
                f"JOIN {match[1]}_cons_columns loc "
                "ON ac.owner = loc.owner AND ac.constraint_name = loc.constraint_name "
                f"LEFT JOIN {match[1]}_cons_columns rem "
                "ON ac.r_owner = rem.owner AND ac.r_constraint_name = rem.constraint_name "
                "WHERE"
            ),
            query,
            flags=re.IGNORECASE,
        )
        query = query.replace("AND ac.r_owner = rem.owner(+)", "")
        query = query.replace("AND ac.r_constraint_name = rem.constraint_name(+)", "")
        return self.connection.execute(text(query), parameters)


@pytest.fixture
def owned_schemas():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE all_constraints (owner TEXT, table_name TEXT, constraint_name TEXT, "
            "constraint_type TEXT, r_owner TEXT, r_constraint_name TEXT, delete_rule TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE all_cons_columns (owner TEXT, table_name TEXT, constraint_name TEXT, "
            "column_name TEXT, position INTEGER)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE all_tables (owner TEXT, table_name TEXT, duration TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE all_objects (owner TEXT, object_name TEXT, object_type TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE all_ind_columns (table_name TEXT, index_name TEXT, table_owner TEXT, "
            "column_name TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE user_indexes (table_name TEXT, index_name TEXT, table_owner TEXT, "
            "index_type TEXT, uniqueness TEXT, compression TEXT, prefix_length INTEGER, "
            "generated TEXT)"
        )
        for view in ("constraints", "cons_columns"):
            connection.exec_driver_sql(f"CREATE VIEW user_{view} AS SELECT * FROM all_{view}")
        for owner, parent, suffix in (
            ("APP_CONTENT", "CONTENT_TASKS", "B8"),
            ("XJ_AIAGENT_PORD", "SESSIONS", "OTHER"),
        ):
            for table_name in ("ARTIFACTS", parent):
                connection.exec_driver_sql(
                    "INSERT INTO all_tables VALUES (?, ?, NULL)", (owner, table_name)
                )
                connection.exec_driver_sql(
                    "INSERT INTO all_objects VALUES (?, ?, 'TABLE')", (owner, table_name)
                )
            for table, name, kind, column, remote in (
                ("ARTIFACTS", f"PK_ARTIFACTS_{suffix}", "P", "ID", None),
                (parent, f"PK_PARENT_{suffix}", "P", "ID", None),
                ("ARTIFACTS", f"FK_PARENT_{suffix}", "R", "TASK_ID", f"PK_PARENT_{suffix}"),
                ("ARTIFACTS", f"UQ_TOKEN_{suffix}", "U", "TOKEN", None),
            ):
                connection.exec_driver_sql(
                    "INSERT INTO all_constraints VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (owner, table, name, kind, owner if remote else None, remote),
                )
                connection.exec_driver_sql(
                    "INSERT INTO all_cons_columns VALUES (?, ?, ?, ?, 1)",
                    (owner, table, name, column),
                )
    try:
        with engine.connect() as connection:
            yield CatalogConnection(connection)
    finally:
        engine.dispose()


def test_real_cross_schema_foreign_key_is_preserved_not_hidden(owned_schemas):
    pytest.importorskip("dmSQLAlchemy")
    owned_schemas.connection.exec_driver_sql(
        "UPDATE all_constraints SET r_owner = 'XJ_AIAGENT_PORD', "
        "r_constraint_name = 'PK_PARENT_OTHER' "
        "WHERE owner = 'APP_CONTENT' AND constraint_type = 'R'"
    )
    engine = create_database_engine(
        "dm+dmPython://user:secret@db:5239/APP_CONTENT",
        {"CONTENT_DATABASE_BACKEND": "dm", "CONTENT_DM_SCHEMA": "APP_CONTENT"},
        exclude_tablespaces=(),
    )
    try:
        engine.dialect.default_schema_name = "xj_content_b8"
        foreign = reflect_constraints(engine.dialect, owned_schemas, "get_multi_foreign_keys")
        assert len(foreign) == 1
        assert foreign[0]["referred_table"] == "sessions"
        assert foreign[0]["referred_schema"] == "xj_aiagent_pord"
    finally:
        engine.dispose()


def reflect_constraints(dialect, catalog, method, schema=None):
    return dict(
        getattr(dialect, method)(
            catalog,
            schema=schema,
            filter_names=["artifacts"],
            scope=ObjectScope.DEFAULT,
            kind=ObjectKind.TABLE,
            info_cache={},
        )
    )[(schema, "artifacts")]


def test_vendor_bulk_constraints_mix_owned_schemas(owned_schemas):
    pytest.importorskip("dmSQLAlchemy")
    from dmSQLAlchemy.dmpython import DMDialect_dmPython

    dialect = DMDialect_dmPython(exclude_tablespaces=())
    dialect.default_schema_name = "xj_content_b8"
    dialect.enable_cache = False
    foreign = reflect_constraints(dialect, owned_schemas, "get_multi_foreign_keys")
    assert {row["referred_table"] for row in foreign} == {"content_tasks", "sessions"}
    primary = reflect_constraints(dialect, owned_schemas, "get_multi_pk_constraint")
    assert primary["constrained_columns"] == ["id", "id"]
    unique = reflect_constraints(dialect, owned_schemas, "get_multi_unique_constraints")
    assert {row["name"] for row in unique} == {"uq_token_b8", "uq_token_other"}


@pytest.mark.parametrize("schema", [None, "APP_CONTENT"])
def test_application_engine_scopes_constraints_to_dedicated_schema(owned_schemas, schema):
    pytest.importorskip("dmSQLAlchemy")
    from dmSQLAlchemy.dmpython import DMDialect_dmPython

    engine = create_database_engine(
        "dm+dmPython://user:secret@db:5239/APP_CONTENT",
        {"CONTENT_DATABASE_BACKEND": "dm", "CONTENT_DM_SCHEMA": "APP_CONTENT"},
        exclude_tablespaces=(),
    )
    try:
        dialect = engine.dialect
        dialect.default_schema_name = "xj_content_b8"
        foreign = reflect_constraints(dialect, owned_schemas, "get_multi_foreign_keys", schema)
        assert len(foreign) == 1
        assert foreign[0]["referred_table"] == "content_tasks"
        assert foreign[0]["referred_schema"] == ("xj_content_b8" if schema else None)
        assert foreign[0]["constrained_columns"] == ["task_id"]
        primary = reflect_constraints(dialect, owned_schemas, "get_multi_pk_constraint", schema)
        assert primary == {"constrained_columns": ["id"], "name": "pk_artifacts_b8"}
        unique = reflect_constraints(dialect, owned_schemas, "get_multi_unique_constraints", schema)
        assert unique == [{"name": "uq_token_b8", "column_names": ["token"]}]
        # No global monkey patch: unrelated applications keep their own dialect.
        unpatched = DMDialect_dmPython()
        assert "get_multi_foreign_keys" not in unpatched.__dict__
        assert "get_multi_columns" not in dialect.__dict__
        assert "get_multi_indexes" not in dialect.__dict__
    finally:
        engine.dispose()
