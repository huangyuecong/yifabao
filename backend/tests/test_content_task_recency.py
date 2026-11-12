import io
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from PIL import Image
from sqlalchemy import update


def _create_task(client, headers, *, mode: str = "copy") -> dict:
    response = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"mode": mode, "name": f"最近更新回归-{uuid4().hex[:8]}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() == timedelta(0)
    return parsed


def _archive_task(client, headers, task_id: str) -> None:
    current = client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    if current.status_code != 200:
        return
    response = client.delete(
        f"/api/v1/tasks/{task_id}",
        headers=headers,
        params={"expected_version": current.json()["version"]},
    )
    assert response.status_code == 200, response.text


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "#17806f").save(output, "PNG")
    return output.getvalue()


def test_content_api_serializes_legacy_naive_datetimes_as_utc(
    content_client,
    user_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import ContentTask

    task = _create_task(content_client, user_headers)
    library_id = None
    try:
        legacy_utc = datetime(2099, 9, 3, 8, 23, 25, 836679)
        with SessionLocal.begin() as session:
            session.execute(
                update(ContentTask)
                .where(ContentTask.id == task["id"])
                .values(updated_at=legacy_utc)
            )

        fetched = content_client.get(
            f"/api/v1/tasks/{task['id']}",
            headers=user_headers,
        )
        assert fetched.status_code == 200, fetched.text
        assert _parse_utc(fetched.json()["updated_at"]) == legacy_utc.replace(tzinfo=UTC)

        # This endpoint intentionally has no response_model, so it also verifies
        # the shared JSON response boundary used by raw dictionary responses.
        library = content_client.post(
            "/api/v1/library/items",
            headers=user_headers,
            json={
                "category": "brand",
                "name": f"UTC 序列化回归-{uuid4().hex[:8]}",
                "crawled_at": "2026-09-03T08:23:25.836679",
            },
        )
        assert library.status_code == 200, library.text
        library_id = library.json()["id"]
        assert _parse_utc(library.json()["crawled_at"]) == datetime(
            2026,
            9,
            3,
            8,
            23,
            25,
            836679,
            tzinfo=UTC,
        )
    finally:
        _archive_task(content_client, user_headers, task["id"])
        if library_id:
            content_client.delete(f"/api/v1/library/items/{library_id}", headers=user_headers)


def test_task_list_sorts_by_aggregated_related_content_update(
    content_client,
    user_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import Artifact, ContentTask

    related_task = _create_task(content_client, user_headers)
    newer_task = _create_task(content_client, user_headers)
    related_at = datetime(2099, 1, 3, 12, tzinfo=UTC)
    try:
        with SessionLocal.begin() as session:
            session.execute(
                update(ContentTask)
                .where(ContentTask.id == related_task["id"])
                .values(updated_at=datetime(2099, 1, 1, 12, tzinfo=UTC))
            )
            session.execute(
                update(ContentTask)
                .where(ContentTask.id == newer_task["id"])
                .values(updated_at=datetime(2099, 1, 2, 12, tzinfo=UTC))
            )
            session.add(
                Artifact(
                    task_id=related_task["id"],
                    kind="copy",
                    version=1,
                    status="draft",
                    content_json="{}",
                    created_by="user-a",
                    created_at=related_at,
                )
            )

        response = content_client.get("/api/v1/tasks", headers=user_headers)
        assert response.status_code == 200, response.text
        relevant = [
            item for item in response.json() if item["id"] in {related_task["id"], newer_task["id"]}
        ]
        assert [item["id"] for item in relevant] == [related_task["id"], newer_task["id"]]
        assert _parse_utc(relevant[0]["updated_at"]) == related_at
    finally:
        _archive_task(content_client, user_headers, related_task["id"])
        _archive_task(content_client, user_headers, newer_task["id"])


def test_task_history_can_include_archived_tasks_without_changing_recent_default(
    content_client,
    user_headers,
):
    task = _create_task(content_client, user_headers)
    before_archive = content_client.get("/api/v1/tasks", headers=user_headers)
    assert before_archive.status_code == 200, before_archive.text
    assert task["id"] in {item["id"] for item in before_archive.json()}

    _archive_task(content_client, user_headers, task["id"])

    recent = content_client.get("/api/v1/tasks", headers=user_headers)
    assert recent.status_code == 200, recent.text
    assert task["id"] not in {item["id"] for item in recent.json()}

    history = content_client.get(
        "/api/v1/tasks",
        headers=user_headers,
        params={"include_archived": "true"},
    )
    assert history.status_code == 200, history.text
    archived = next(item for item in history.json() if item["id"] == task["id"])
    assert archived["status"] == "archived"
    assert archived["archived"] is True
    assert archived["workflow_status"] == "draft"


def test_archiving_preserves_completed_workflow_and_restore_returns_it(
    content_client,
    user_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import ContentTask, TaskStage

    task = _create_task(content_client, user_headers)
    try:
        with SessionLocal.begin() as session:
            session.query(ContentTask).filter(ContentTask.id == task["id"]).update(
                {"status": "confirmed", "current_stage": "export"}
            )
            session.query(TaskStage).filter(
                TaskStage.task_id == task["id"], TaskStage.stage_key == "export"
            ).update({"state": "confirmed"})

        current = content_client.get(f"/api/v1/tasks/{task['id']}", headers=user_headers).json()
        archived = content_client.delete(
            f"/api/v1/tasks/{task['id']}",
            headers=user_headers,
            params={"expected_version": current["version"]},
        )
        assert archived.status_code == 200, archived.text

        history = content_client.get(f"/api/v1/tasks/{task['id']}", headers=user_headers)
        assert history.status_code == 200, history.text
        payload = history.json()
        assert payload["status"] == "archived"
        assert payload["archived"] is True
        assert payload["workflow_status"] == "confirmed"

        restored = content_client.post(f"/api/v1/tasks/{task['id']}/restore", headers=user_headers)
        assert restored.status_code == 200, restored.text
        assert restored.json()["status"] == "draft"
        assert restored.json()["archived"] is False
        assert restored.json()["is_edit_draft"] is True
        assert restored.json()["id"] != task["id"]
        assert (
            content_client.get(f"/api/v1/tasks/{task['id']}", headers=user_headers).json()[
                "archived"
            ]
            is True
        )
    finally:
        current = content_client.get(f"/api/v1/tasks/{task['id']}", headers=user_headers)
        if current.status_code == 200 and current.json().get("status") != "archived":
            _archive_task(content_client, user_headers, task["id"])


def test_archiving_freezes_completed_workflow_even_when_export_stage_is_stale(
    content_client,
    user_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import ContentTask, TaskStage

    task = _create_task(content_client, user_headers)
    try:
        with SessionLocal.begin() as session:
            session.query(ContentTask).filter(ContentTask.id == task["id"]).update(
                {"status": "confirmed", "current_stage": "export"}
            )
            session.query(TaskStage).filter(
                TaskStage.task_id == task["id"], TaskStage.stage_key == "export"
            ).update({"state": "stale"})

        current = content_client.get(f"/api/v1/tasks/{task['id']}", headers=user_headers).json()
        archived = content_client.delete(
            f"/api/v1/tasks/{task['id']}",
            headers=user_headers,
            params={"expected_version": current["version"]},
        )
        assert archived.status_code == 200, archived.text
        payload = content_client.get(f"/api/v1/tasks/{task['id']}", headers=user_headers).json()
        assert payload["archived"] is True
        assert payload["workflow_status"] == "confirmed"
    finally:
        current = content_client.get(f"/api/v1/tasks/{task['id']}", headers=user_headers)
        if current.status_code == 200 and current.json().get("status") != "archived":
            _archive_task(content_client, user_headers, task["id"])


def test_task_status_is_not_completed_before_export_confirmation(
    content_client,
    user_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import ContentTask

    task = _create_task(content_client, user_headers)
    try:
        # Reproduce legacy/stale data that marked a task completed while an
        # earlier stage was still active.  The response must be conservative.
        with SessionLocal.begin() as session:
            session.query(ContentTask).filter(ContentTask.id == task["id"]).update(
                {"status": "confirmed", "current_stage": "topic"}
            )
        fetched = content_client.get(f"/api/v1/tasks/{task['id']}", headers=user_headers)
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["status"] != "confirmed"
    finally:
        _archive_task(content_client, user_headers, task["id"])


def test_confirm_delete_and_video_project_advance_recent_update(
    content_client,
    user_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import Artifact

    artifact_task = _create_task(content_client, user_headers)
    video_task = _create_task(content_client, user_headers, mode="video")
    try:
        with SessionLocal.begin() as session:
            confirmed = Artifact(
                task_id=artifact_task["id"],
                kind="music",
                version=1,
                status="draft",
                content_json="{}",
                created_by="user-a",
            )
            discarded = Artifact(
                task_id=artifact_task["id"],
                kind="music",
                version=2,
                status="draft",
                content_json="{}",
                created_by="user-a",
            )
            session.add_all([confirmed, discarded])
            session.flush()
            confirmed_id = confirmed.id
            discarded_id = discarded.id

        before_confirm = _parse_utc(
            content_client.get(
                f"/api/v1/tasks/{artifact_task['id']}",
                headers=user_headers,
            ).json()["updated_at"]
        )
        time.sleep(0.01)
        confirmed_response = content_client.post(
            f"/api/v1/artifacts/{confirmed_id}/confirm",
            headers=user_headers,
        )
        assert confirmed_response.status_code == 200, confirmed_response.text
        after_confirm = _parse_utc(
            content_client.get(
                f"/api/v1/tasks/{artifact_task['id']}",
                headers=user_headers,
            ).json()["updated_at"]
        )
        assert after_confirm > before_confirm

        time.sleep(0.01)
        discarded_response = content_client.delete(
            f"/api/v1/artifacts/{discarded_id}",
            headers=user_headers,
        )
        assert discarded_response.status_code == 200, discarded_response.text
        after_discard = _parse_utc(
            content_client.get(
                f"/api/v1/tasks/{artifact_task['id']}",
                headers=user_headers,
            ).json()["updated_at"]
        )
        assert after_discard > after_confirm

        before_project = _parse_utc(
            content_client.get(
                f"/api/v1/tasks/{video_task['id']}",
                headers=user_headers,
            ).json()["updated_at"]
        )
        time.sleep(0.01)
        project = content_client.post(
            f"/api/v1/tasks/{video_task['id']}/video-project",
            headers=user_headers,
        )
        assert project.status_code == 200, project.text
        after_project = _parse_utc(
            content_client.get(
                f"/api/v1/tasks/{video_task['id']}",
                headers=user_headers,
            ).json()["updated_at"]
        )
        assert after_project > before_project

        time.sleep(0.01)
        uploaded_asset = content_client.post(
            f"/api/v1/video-projects/{project.json()['id']}/assets/upload",
            headers=user_headers,
            data={"role": "video"},
            files={"file": ("recent-update.png", _png_bytes(), "image/png")},
        )
        assert uploaded_asset.status_code == 200, uploaded_asset.text
        after_asset = _parse_utc(
            content_client.get(
                f"/api/v1/tasks/{video_task['id']}",
                headers=user_headers,
            ).json()["updated_at"]
        )
        assert after_asset > after_project
    finally:
        _archive_task(content_client, user_headers, artifact_task["id"])
        _archive_task(content_client, user_headers, video_task["id"])
