import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]


def run_migration(
    service: str,
    database_url: str,
    expected_tables: set[str],
    monkeypatch,
    *,
    expected_columns: dict[str, set[str]] | None = None,
) -> None:
    prefix = service.upper()
    monkeypatch.setenv(f"{prefix}_DATABASE_URL", database_url)
    config = Config(str(ROOT / "services" / f"{service}_service" / "alembic.ini"))

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert expected_tables <= tables
    assert "alembic_version" in tables
    for table_name, columns in (expected_columns or {}).items():
        assert columns <= {column["name"] for column in inspector.get_columns(table_name)}

    command.downgrade(config, "base")
    remaining = set(inspect(engine).get_table_names())
    assert not (expected_tables & remaining)
    engine.dispose()


def test_content_migrations_upgrade_and_downgrade(tmp_path, monkeypatch):
    content_tables = {
        "library_items",
        "sensitive_rules",
        "content_tasks",
        "artifacts",
        "egress_grants",
        "jobs",
        "audit_logs",
    }
    run_migration("content", f"sqlite:///{tmp_path / 'content.db'}", content_tables, monkeypatch)


def test_content_task_knowledge_migration_backfills_product_and_normalizes_length(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'content-v7-task.db'}"
    monkeypatch.setenv("CONTENT_DATABASE_URL", database_url)
    config = Config(str(ROOT / "services" / "content_service" / "alembic.ini"))
    command.upgrade(config, "content_0007")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO resource_entries
                    (id, owner_id, resource_kind, title, category, tags_json,
                     parent_id, data_level, current_version_id, metadata_json,
                     active, created_at, updated_at)
                VALUES
                    ('legacy-product', 'legacy-owner', 'product', '旧产品',
                     'product', '[]', NULL, 'internal', NULL, '{}', 1,
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )
        for task_id, product_id, length in (
            ("legacy-product-task", "legacy-product", "500字"),
            ("legacy-long-task", None, "长篇"),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO content_tasks
                        (id, owner_id, mode, name, topic, product, keywords_json,
                         audience, content_type, style, length, requirements,
                         current_stage, status, allowed_users_json,
                         allowed_departments_json, created_at, updated_at,
                         version, workflow_enforced, product_resource_id)
                    VALUES
                        (:id, 'legacy-owner', 'copy', '旧任务', '旧主题', '旧产品',
                         '[]', '大众', '文案', '正式', :length, '',
                         'configuration', 'draft', '[]', '[]',
                         CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1, 1, :product_id)
                    """
                ),
                {"id": task_id, "product_id": product_id, "length": length},
            )

    command.upgrade(config, "content_0008")
    with engine.begin() as connection:
        rows = {
            row["id"]: row
            for row in connection.execute(
                text(
                    """
                    SELECT id, length, knowledge_resource_ids_json
                    FROM content_tasks
                    WHERE id IN ('legacy-product-task', 'legacy-long-task')
                    """
                )
            ).mappings()
        }

    assert rows["legacy-product-task"]["length"] == "中"
    assert json.loads(rows["legacy-product-task"]["knowledge_resource_ids_json"]) == [
        "legacy-product"
    ]
    assert rows["legacy-long-task"]["length"] == "长"
    assert json.loads(rows["legacy-long-task"]["knowledge_resource_ids_json"]) == []

    command.downgrade(config, "base")
    engine.dispose()


def test_copy_smart_title_migration_only_reactivates_unstarted_legacy_tasks(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'content-v8-smart-title.db'}"
    monkeypatch.setenv("CONTENT_DATABASE_URL", database_url)
    config = Config(str(ROOT / "services" / "content_service" / "alembic.ini"))
    command.upgrade(config, "content_0008")
    engine = create_engine(database_url)

    with engine.begin() as connection:
        for task_id, status, current_stage, copy_state in (
            ("legacy-unstarted-copy", "draft", "copy", "not_started"),
            ("legacy-finished-copy", "confirmed", "export", "confirmed"),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO content_tasks
                        (id, owner_id, mode, name, topic, product, product_resource_id,
                         knowledge_resource_ids_json, keywords_json, audience, content_type,
                         style, length, requirements, current_stage, status, version,
                         workflow_enforced, allowed_users_json, allowed_departments_json,
                         created_at, updated_at)
                    VALUES
                        (:id, 'legacy-owner', 'copy', '旧文案任务', '', '', NULL,
                         '[]', '[]', '', '文案', '正式', '中', '', :current_stage,
                         :status, 1, 1, '[]', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "id": task_id,
                    "status": status,
                    "current_stage": current_stage,
                },
            )
            for stage_key, stage_state, skipped_reason in (
                ("topic", "skipped", "当前入口不适用"),
                ("copy", copy_state, None),
            ):
                connection.execute(
                    text(
                        """
                        INSERT INTO task_stages
                            (id, task_id, stage_key, state, requirements,
                             selected_artifact_ids_json, confirmed_artifact_ids_json,
                             skipped_reason, version, confirmed_by, confirmed_at,
                             created_at, updated_at)
                        VALUES
                            (:id, :task_id, :stage_key, :state, '', '{}', '{}',
                             :skipped_reason, 1, NULL, NULL,
                             CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """
                    ),
                    {
                        "id": f"{task_id}-{stage_key}",
                        "task_id": task_id,
                        "stage_key": stage_key,
                        "state": stage_state,
                        "skipped_reason": skipped_reason,
                    },
                )

    command.upgrade(config, "content_0009")
    with engine.begin() as connection:
        task_rows = {
            row["id"]: row
            for row in connection.execute(
                text(
                    """
                    SELECT id, current_stage, version
                    FROM content_tasks
                    WHERE id LIKE 'legacy-%-copy'
                    """
                )
            ).mappings()
        }
        topic_rows = {
            row["task_id"]: row
            for row in connection.execute(
                text(
                    """
                    SELECT task_id, state, skipped_reason, version
                    FROM task_stages
                    WHERE stage_key = 'topic' AND task_id LIKE 'legacy-%-copy'
                    """
                )
            ).mappings()
        }

    assert task_rows["legacy-unstarted-copy"]["current_stage"] == "topic"
    assert task_rows["legacy-unstarted-copy"]["version"] == 2
    assert topic_rows["legacy-unstarted-copy"]["state"] == "not_started"
    assert topic_rows["legacy-unstarted-copy"]["skipped_reason"] is None
    assert topic_rows["legacy-unstarted-copy"]["version"] == 2
    assert task_rows["legacy-finished-copy"]["current_stage"] == "export"
    assert topic_rows["legacy-finished-copy"]["state"] == "skipped"

    command.downgrade(config, "content_0008")
    with engine.begin() as connection:
        reverted = (
            connection.execute(
                text(
                    """
                SELECT content_tasks.current_stage, task_stages.state,
                       task_stages.skipped_reason
                FROM content_tasks
                JOIN task_stages ON task_stages.task_id = content_tasks.id
                WHERE content_tasks.id = 'legacy-unstarted-copy'
                  AND task_stages.stage_key = 'topic'
                """
                )
            )
            .mappings()
            .one()
        )
    assert reverted["current_stage"] == "copy"
    assert reverted["state"] == "skipped"
    assert reverted["skipped_reason"] == "当前入口不适用"

    command.downgrade(config, "base")
    engine.dispose()
