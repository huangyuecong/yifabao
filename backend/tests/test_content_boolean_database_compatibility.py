import re

import pytest
from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    Table,
    create_engine,
    event,
    false,
    select,
    true,
)
from sqlalchemy.orm import Session

from content_service.advertising_law_rules import (
    ADVERTISING_LAW_RULES,
    SEED_ID,
    seed_advertising_law_rules,
)
from content_service.models import (
    LibraryItem,
    LocalUser,
    OriginalArchive,
    ProjectAsset,
    ResourceEntry,
    RuleSeedState,
    SensitiveRule,
)


@pytest.fixture
def dm_dialect():
    pytest.importorskip("dmSQLAlchemy")
    from dmSQLAlchemy.dmpython import DMDialect_dmPython

    return DMDialect_dmPython()


@pytest.mark.parametrize("already_completed", [False, True])
def test_startup_rule_seed_executes_portable_predicate(dm_dialect, already_completed):
    engine = create_engine("sqlite://")
    RuleSeedState.__table__.create(engine)
    SensitiveRule.__table__.create(engine)
    statements = []

    @event.listens_for(engine, "before_execute")
    def check_dm_sql(connection, clause, multiparams, params, execution_options):
        sql = str(clause.compile(dialect=dm_dialect))
        assert not re.search(r"\bIS(?:\s+NOT)?\s+[01]\b", sql, re.IGNORECASE)
        statements.append(sql)

    try:
        with Session(engine) as session:
            session.add(RuleSeedState(id=SEED_ID, completed=already_completed))
            session.commit()
            added = seed_advertising_law_rules(session)
            session.commit()
            assert added == (0 if already_completed else len(ADVERTISING_LAW_RULES))
            assert session.get(RuleSeedState, SEED_ID).completed is True
            # Repeated startup must preserve operator deletions after initialization.
            if not already_completed:
                session.delete(session.scalar(select(SensitiveRule)))
                session.commit()
            assert seed_advertising_law_rules(session) == 0
            session.commit()
        assert any("rule_seed_state.completed = 0" in sql for sql in statements)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("column", "enabled"),
    [
        (LocalUser.is_project_admin, True),
        (RuleSeedState.completed, False),
        (OriginalArchive.upload_attempted, True),
        (ProjectAsset.active, True),
        (ResourceEntry.active, True),
        (LibraryItem.active, True),
        (SensitiveRule.active, True),
    ],
)
def test_dm_boolean_filters_for_permissions_rules_and_knowledge(dm_dialect, column, enabled):
    assert column.property.columns[0].nullable is False
    literal = true() if enabled else false()
    sql = str(select(column).where(column == literal).compile(dialect=dm_dialect))
    assert re.search(r"\s=\s+[01]\b", sql)
    assert not re.search(r"\bIS(?:\s+NOT)?\s+[01]\b", sql, re.IGNORECASE)


@pytest.mark.parametrize(("enabled", "expected"), [(True, [1]), (False, [2])])
def test_boolean_filter_excludes_unknown_without_granting_access(enabled, expected):
    metadata = MetaData()
    table = Table(
        "nullable_permission",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("allowed", Boolean, nullable=True),
    )
    engine = create_engine("sqlite://")
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                table.insert(),
                [{"id": 1, "allowed": True}, {"id": 2, "allowed": False}, {"id": 3, "allowed": None}],
            )
            literal = true() if enabled else false()
            assert connection.scalars(
                select(table.c.id).where(table.c.allowed == literal)
            ).all() == expected
    finally:
        engine.dispose()
