"""Alembic operations used by this project's DM migrations.

dmSQLAlchemy supplies SQLAlchemy DDL, but does not register an Alembic impl.
DM supports Oracle-style ADD/MODIFY/RENAME COLUMN. DDL may commit implicitly:
failed schema upgrades require DBA recovery, not transaction rollback.
"""

from alembic.ddl import base, oracle
from alembic.ddl.impl import DefaultImpl
from sqlalchemy.ext.compiler import compiles


class DamengImpl(DefaultImpl):
    __dialect__ = "dm"
    transactional_ddl = False


for operation, renderer in (
    (base.AddColumn, oracle.visit_add_column),
    (base.ColumnNullable, oracle.visit_column_nullable),
    (base.ColumnType, oracle.visit_column_type),
    (base.ColumnName, oracle.visit_column_name),
    (base.ColumnDefault, oracle.visit_column_default),
    (base.RenameTable, oracle.visit_rename_table),
):
    compiles(operation, "dm")(renderer)
