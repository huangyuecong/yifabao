import io
import json
import zipfile
from uuid import uuid4

from docx import Document
from sqlalchemy import select


def _stage(workflow: dict, key: str) -> dict:
    return next(item for item in workflow["stages"] if item["key"] == key)


def _document_text(content: bytes) -> str:
    document = Document(io.BytesIO(content))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def test_archived_full_export_recovers_only_the_selected_creative_deliverables(
    content_client,
    user_headers,
    other_headers,
):
    task_name = f"新春宣传归档-{uuid4().hex[:8]}"
    created = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "full",
            "name": task_name,
            "topic": "新春团圆",
            "product": "测试产品",
            "audience": "家庭用户",
        },
    )
    assert created.status_code == 200, created.text
    task = created.json()

    generated_titles = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/topic/generate",
        headers=user_headers,
        json={"count": 2},
    )
    assert generated_titles.status_code == 200, generated_titles.text
    title_candidates = generated_titles.json()["artifacts"]
    selected_title = title_candidates[1]
    unselected_title = title_candidates[0]
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    confirmed_title = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/topic/confirm",
        headers=user_headers,
        json={
            "expected_version": _stage(workflow, "topic")["version"],
            "artifact_ids": [selected_title["id"]],
        },
    )
    assert confirmed_title.status_code == 200, confirmed_title.text

    generated_copies = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/copy/generate",
        headers=user_headers,
        json={"count": 2},
    )
    assert generated_copies.status_code == 200, generated_copies.text
    copy_candidates = generated_copies.json()["artifacts"]
    selected_copy = copy_candidates[1]
    unselected_copy = copy_candidates[0]
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    selected_copy_response = content_client.put(
        f"/api/v1/tasks/{task['id']}/stages/copy/selection",
        headers=user_headers,
        json={
            "expected_version": _stage(workflow, "copy")["version"],
            "artifact_ids": [selected_copy["id"]],
        },
    )
    assert selected_copy_response.status_code == 200, selected_copy_response.text
    assert selected_copy_response.json()["state"] == "confirmed"

    compliance = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/compliance/generate",
        headers=user_headers,
        json={},
    )
    assert compliance.status_code == 200, compliance.text
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    confirmed_compliance = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/compliance/confirm",
        headers=user_headers,
        json={"expected_version": _stage(workflow, "compliance")["version"]},
    )
    assert confirmed_compliance.status_code == 200, confirmed_compliance.text

    generated_images = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/image/generate",
        headers=user_headers,
        json={
            "count": 3,
            "parameters": {
                "prompt": "新春家庭团圆产品海报",
                "scene": "新春团圆宴",
                "style": "温暖写实摄影",
                "size": "64x64",
            },
        },
    )
    assert generated_images.status_code == 200, generated_images.text
    image_candidates = generated_images.json()["artifacts"]
    selected_images = [image_candidates[0], image_candidates[2]]
    unselected_image = image_candidates[1]
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    selected_image_response = content_client.put(
        f"/api/v1/tasks/{task['id']}/stages/image/selection",
        headers=user_headers,
        json={
            "expected_version": _stage(workflow, "image")["version"],
            "artifact_ids": [item["id"] for item in selected_images],
        },
    )
    assert selected_image_response.status_code == 200, selected_image_response.text
    confirmed_images = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/image/confirm",
        headers=user_headers,
        json={"expected_version": selected_image_response.json()["version"]},
    )
    assert confirmed_images.status_code == 200, confirmed_images.text

    latest_task = content_client.get(f"/api/v1/tasks/{task['id']}", headers=user_headers).json()
    changed = content_client.patch(
        f"/api/v1/tasks/{task['id']}",
        headers=user_headers,
        json={
            "expected_version": latest_task["version"],
            "audience": "春节返乡家庭用户",
        },
    )
    assert changed.status_code == 200, changed.text
    stale_workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    for key in ("topic", "copy", "compliance", "image"):
        assert _stage(stale_workflow, key)["state"] == "stale"
        assert _stage(stale_workflow, key)["selected_artifact_ids"] == {}
        assert _stage(stale_workflow, key)["confirmed_artifact_ids"] == {}
    assert _stage(stale_workflow, "video_preview")["state"] == "not_started"
    assert _stage(stale_workflow, "export")["state"] == "not_started"

    from content_service.db import SessionLocal
    from content_service.models import Artifact, TaskStage
    from content_service.services import storage

    final_video_bytes = b"\x00\x00\x00\x18ftypmp42-new-year-final-video"
    final_video_key = f"tests/archive-export/{uuid4().hex}/final.mp4"
    storage.path_for(final_video_key).write_bytes(final_video_bytes)
    with SessionLocal() as session:
        final_video = Artifact(
            task_id=task["id"],
            kind="video",
            stage_key="video_preview",
            version=1,
            status="draft",
            content_json=json.dumps(
                {
                    "media_type": "video/mp4",
                    "purpose": "final_project_render",
                    "render_profile": "final-720p",
                }
            ),
            storage_key=final_video_key,
            created_by=user_headers["X-User-Id"],
        )
        session.add(final_video)
        session.commit()
        final_video_id = final_video.id

    confirmed_video = content_client.post(
        f"/api/v1/artifacts/{final_video_id}/confirm",
        headers=user_headers,
    )
    assert confirmed_video.status_code == 200, confirmed_video.text
    before_archive = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    assert before_archive["export_artifact_ids"] == [final_video_id]
    confirmed_export = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/export/confirm",
        headers=user_headers,
        json={"expected_version": _stage(before_archive, "export")["version"]},
    )
    assert confirmed_export.status_code == 200, confirmed_export.text

    expected_ids = {
        selected_title["id"],
        selected_copy["id"],
        *(item["id"] for item in selected_images),
        final_video_id,
    }
    excluded_ids = {
        unselected_title["id"],
        unselected_copy["id"],
        unselected_image["id"],
    }
    archived_workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    assert set(archived_workflow["export_artifact_ids"]) == expected_ids
    assert excluded_ids.isdisjoint(archived_workflow["export_artifact_ids"])
    archived_task = content_client.get(f"/api/v1/tasks/{task['id']}", headers=user_headers)
    assert archived_task.status_code == 200, archived_task.text
    assert archived_task.json()["archived"] is True

    selected_title_text = selected_title["content"]["candidates"][0]["title"]
    title_download = content_client.get(
        f"/api/v1/artifacts/{selected_title['id']}/download",
        headers=user_headers,
        params={"format": "docx"},
    )
    assert title_download.status_code == 200, title_download.text
    assert selected_title_text in _document_text(title_download.content)
    forbidden_title_download = content_client.get(
        f"/api/v1/artifacts/{selected_title['id']}/download",
        headers=other_headers,
        params={"format": "docx"},
    )
    assert forbidden_title_download.status_code == 403

    with SessionLocal() as session:
        frozen_video = session.get(Artifact, final_video_id)
        frozen_content = json.loads(frozen_video.content_json)
        assert set(frozen_content["deliverable_artifact_ids"]) == expected_ids

        late_draft_key = f"tests/archive-export/{uuid4().hex}/late-draft.png"
        late_draft_bytes = b"late-draft-must-not-enter-the-archive"
        storage.path_for(late_draft_key).write_bytes(late_draft_bytes)
        late_draft = Artifact(
            task_id=task["id"],
            kind="image",
            stage_key="image",
            version=999,
            status="draft",
            content_json=json.dumps({"media_type": "image/png"}),
            storage_key=late_draft_key,
            created_by=user_headers["X-User-Id"],
        )
        session.add(late_draft)
        session.commit()
        late_draft_id = late_draft.id

        rows = session.scalars(
            select(TaskStage).where(TaskStage.task_id == task["id"]).order_by(TaskStage.stage_key)
        ).all()
        stage_snapshot = {
            row.stage_key: (
                row.state,
                row.selected_artifact_ids_json,
                row.confirmed_artifact_ids_json,
                row.version,
                row.confirmed_by,
                row.confirmed_at,
            )
            for row in rows
        }

    exported = content_client.get(f"/api/v1/tasks/{task['id']}/export", headers=user_headers)
    assert exported.status_code == 200, exported.text
    expected_names = {
        f"{task_name}-标题-v{selected_title['version']}.docx",
        f"{task_name}-文案-v{selected_copy['version']}.docx",
        *(f"{task_name}-图片-v{item['version']}.png" for item in selected_images),
        f"{task_name}-成片-v1.mp4",
    }
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert set(archive.namelist()) == expected_names
        assert archive.read(f"{task_name}-成片-v1.mp4") == final_video_bytes
        assert selected_title_text in _document_text(
            archive.read(f"{task_name}-标题-v{selected_title['version']}.docx")
        )
        selected_copy_body = selected_copy["content"]["candidates"][0]["body"]
        assert selected_copy_body in _document_text(
            archive.read(f"{task_name}-文案-v{selected_copy['version']}.docx")
        )
        for image in selected_images:
            filename = f"{task_name}-图片-v{image['version']}.png"
            assert archive.read(filename) == storage.path_for(image["storage_key"]).read_bytes()
        assert not any(name.lower().endswith(".json") for name in archive.namelist())

    after_draft = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    assert set(after_draft["export_artifact_ids"]) == expected_ids
    assert late_draft_id not in after_draft["export_artifact_ids"]

    with SessionLocal() as session:
        rows = session.scalars(
            select(TaskStage).where(TaskStage.task_id == task["id"]).order_by(TaskStage.stage_key)
        ).all()
        assert {
            row.stage_key: (
                row.state,
                row.selected_artifact_ids_json,
                row.confirmed_artifact_ids_json,
                row.version,
                row.confirmed_by,
                row.confirmed_at,
            )
            for row in rows
        } == stage_snapshot

    restored = content_client.post(
        f"/api/v1/tasks/{task['id']}/restore",
        headers=user_headers,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived"] is False
    assert restored.json()["is_edit_draft"] is True
    assert restored.json()["id"] != task["id"]
    restored_workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    assert _stage(restored_workflow, "export")["state"] == "confirmed"
    assert set(restored_workflow["export_artifact_ids"]) == expected_ids
    restored_title = content_client.get(
        f"/api/v1/artifacts/{selected_title['id']}/download",
        headers=user_headers,
        params={"format": "docx"},
    )
    assert restored_title.status_code == 200, restored_title.text
    assert selected_title_text in _document_text(restored_title.content)

    with SessionLocal() as session:
        rows = session.scalars(
            select(TaskStage).where(TaskStage.task_id == task["id"]).order_by(TaskStage.stage_key)
        ).all()
        assert {
            row.stage_key: (
                row.state,
                row.selected_artifact_ids_json,
                row.confirmed_artifact_ids_json,
                row.version,
                row.confirmed_by,
                row.confirmed_at,
            )
            for row in rows
        } == stage_snapshot

    # The archive remains downloadable; all subsequent writes target the new
    # private draft and its independently identified artifact copies.
    assert (
        content_client.get(
            f"/api/v1/tasks/{task['id']}/export",
            headers=user_headers,
        ).status_code
        == 200
    )
    task = restored.json()
    draft_artifacts = content_client.get(
        f"/api/v1/tasks/{task['id']}/artifacts",
        headers=user_headers,
    ).json()
    selected_title = next(
        item
        for item in draft_artifacts
        if (item["kind"] == selected_title["kind"] and item["version"] == selected_title["version"])
    )
    draft_workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow",
        headers=user_headers,
    ).json()
    assert _stage(draft_workflow, "export")["state"] == "in_progress"
    assert len(draft_workflow["export_artifact_ids"]) == len(expected_ids)
    assert expected_ids.isdisjoint(draft_workflow["export_artifact_ids"])
    expected_ids = set(draft_workflow["export_artifact_ids"])
    changed_after_restore = content_client.patch(
        f"/api/v1/tasks/{task['id']}",
        headers=user_headers,
        json={
            "expected_version": restored.json()["version"],
            "audience": "恢复后重新编辑的新受众",
        },
    )
    assert changed_after_restore.status_code == 200, changed_after_restore.text
    edited_workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    assert _stage(edited_workflow, "export")["state"] == "stale"
    currently_confirmed_ids = {
        artifact_id
        for stage in edited_workflow["stages"]
        if stage["applicable"] and stage["state"] == "confirmed"
        for ids in stage["confirmed_artifact_ids"].values()
        for artifact_id in ids
    }
    assert set(edited_workflow["export_artifact_ids"]) == currently_confirmed_ids
    assert expected_ids.isdisjoint(edited_workflow["export_artifact_ids"])
    assert (
        content_client.get(
            f"/api/v1/artifacts/{selected_title['id']}/download",
            headers=user_headers,
            params={"format": "docx"},
        ).status_code
        == 409
    )
    assert (
        content_client.get(
            f"/api/v1/tasks/{task['id']}/export",
            headers=user_headers,
        ).status_code
        == 409
    )

    with SessionLocal() as session:
        rows = session.scalars(
            select(TaskStage).where(TaskStage.task_id == task["id"]).order_by(TaskStage.stage_key)
        ).all()
        edited_stage_snapshot = {
            row.stage_key: (
                row.state,
                row.selected_artifact_ids_json,
                row.confirmed_artifact_ids_json,
                row.version,
                row.confirmed_by,
                row.confirmed_at,
            )
            for row in rows
        }

    # Resolver-backed reads must remain observational after the restored task
    # has become an active draft again; they must not repair stale mappings.
    repeated = content_client.get(f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers)
    assert repeated.status_code == 200, repeated.text
    with SessionLocal() as session:
        rows = session.scalars(
            select(TaskStage).where(TaskStage.task_id == task["id"]).order_by(TaskStage.stage_key)
        ).all()
        assert {
            row.stage_key: (
                row.state,
                row.selected_artifact_ids_json,
                row.confirmed_artifact_ids_json,
                row.version,
                row.confirmed_by,
                row.confirmed_at,
            )
            for row in rows
        } == edited_stage_snapshot
