"""Reactivate smart titles for legacy unstarted copy tasks."""

import sqlalchemy as sa
from alembic import op

revision = "content_0009"
down_revision = "content_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE content_tasks
            SET current_stage = 'topic',
                version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE mode = 'copy'
              AND status = 'draft'
              AND current_stage = 'copy'
              AND EXISTS (
                  SELECT 1
                  FROM task_stages AS topic_stage
                  WHERE topic_stage.task_id = content_tasks.id
                    AND topic_stage.stage_key = 'topic'
                    AND topic_stage.state = 'skipped'
                    AND topic_stage.skipped_reason = '当前入口不适用'
              )
              AND EXISTS (
                  SELECT 1
                  FROM task_stages AS copy_stage
                  WHERE copy_stage.task_id = content_tasks.id
                    AND copy_stage.stage_key = 'copy'
                    AND copy_stage.state = 'not_started'
              )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE task_stages
            SET state = 'not_started',
                skipped_reason = NULL,
                version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE stage_key = 'topic'
              AND state = 'skipped'
              AND skipped_reason = '当前入口不适用'
              AND EXISTS (
                  SELECT 1
                  FROM content_tasks
                  WHERE content_tasks.id = task_stages.task_id
                    AND content_tasks.mode = 'copy'
                    AND content_tasks.status = 'draft'
                    AND content_tasks.current_stage = 'topic'
              )
              AND EXISTS (
                  SELECT 1
                  FROM task_stages AS copy_stage
                  WHERE copy_stage.task_id = task_stages.task_id
                    AND copy_stage.stage_key = 'copy'
                    AND copy_stage.state = 'not_started'
              )
            """
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE task_stages
            SET state = 'skipped',
                skipped_reason = '当前入口不适用',
                version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE stage_key = 'topic'
              AND state = 'not_started'
              AND skipped_reason IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM content_tasks
                  WHERE content_tasks.id = task_stages.task_id
                    AND content_tasks.mode = 'copy'
                    AND content_tasks.status = 'draft'
                    AND content_tasks.current_stage = 'topic'
              )
              AND EXISTS (
                  SELECT 1
                  FROM task_stages AS copy_stage
                  WHERE copy_stage.task_id = task_stages.task_id
                    AND copy_stage.stage_key = 'copy'
                    AND copy_stage.state = 'not_started'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM artifacts
                  WHERE artifacts.task_id = task_stages.task_id
                    AND artifacts.stage_key = 'topic'
                    AND artifacts.status NOT IN ('discarded', 'legacy_mock')
              )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE content_tasks
            SET current_stage = 'copy',
                version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE mode = 'copy'
              AND status = 'draft'
              AND current_stage = 'topic'
              AND EXISTS (
                  SELECT 1
                  FROM task_stages AS topic_stage
                  WHERE topic_stage.task_id = content_tasks.id
                    AND topic_stage.stage_key = 'topic'
                    AND topic_stage.state = 'skipped'
                    AND topic_stage.skipped_reason = '当前入口不适用'
              )
              AND EXISTS (
                  SELECT 1
                  FROM task_stages AS copy_stage
                  WHERE copy_stage.task_id = content_tasks.id
                    AND copy_stage.stage_key = 'copy'
                    AND copy_stage.state = 'not_started'
              )
            """
        )
    )
