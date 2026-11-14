"""Several people on one SQLite database: a slow model call must not block other users' saves."""

import sqlite3

from sqlalchemy import text
from test_content_workflow_v2 import create_task

from content_service.database_backend import create_database_engine, database_options


def test_sqlite_waits_for_the_writer_and_uses_wal(tmp_path):
    assert database_options("sqlite:///x.db", {})["connect_args"]["timeout"] == 30
    assert database_options("sqlite:///x.db", {"CONTENT_SQLITE_BUSY_SECONDS": "90"})["connect_args"]["timeout"] == 90
    engine = create_database_engine(f"sqlite:///{tmp_path / 'content.db'}", {})
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
    memory = create_database_engine("sqlite://", {})
    with memory.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "memory"


def test_title_generation_releases_the_write_lock_while_the_model_runs(content_client, user_headers):
    from content_service import main as content_main
    from content_service.db import engine
    from content_service.provider_dependencies import get_text_provider
    from content_service.providers import MockTextProvider

    attempts = []

    class SlowModel(MockTextProvider):
        def topics(self, request, count):
            # Another visitor saves something while this request waits for the model.
            other = sqlite3.connect(engine.url.database, timeout=1)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("ROLLBACK")
                attempts.append("saved")
            except sqlite3.OperationalError as error:
                attempts.append(str(error))
            finally:
                other.close()
            return super().topics(request, count)

    task = create_task(content_client, user_headers, "full")
    content_main.app.dependency_overrides[get_text_provider] = lambda: SlowModel()
    try:
        response = content_client.post(
            f"/api/v1/tasks/{task['id']}/stages/topic/generate", headers=user_headers, json={"count": 1}
        )
    finally:
        content_main.app.dependency_overrides.pop(get_text_provider, None)
    assert response.status_code == 200, response.text
    assert attempts == ["saved"]


def test_an_unexpected_model_error_marks_the_run_failed(content_client, user_headers):
    from content_service import main as content_main
    from content_service.provider_dependencies import get_text_provider
    from content_service.providers import MockTextProvider

    class BrokenModel(MockTextProvider):
        def topics(self, request, count):
            raise RuntimeError("boom")

    task = create_task(content_client, user_headers, "full")
    content_main.app.dependency_overrides[get_text_provider] = lambda: BrokenModel()
    try:
        response = content_client.post(
            f"/api/v1/tasks/{task['id']}/stages/topic/generate", headers=user_headers, json={"count": 1}
        )
    finally:
        content_main.app.dependency_overrides.pop(get_text_provider, None)
    runs = content_client.get(f"/api/v1/tasks/{task['id']}/generation-runs",
                              params={"stage_key": "topic"}, headers=user_headers).json()
    assert response.status_code == 500
    items = runs["items"] if isinstance(runs, dict) else runs
    assert [run["status"] for run in items] == ["failed"]
