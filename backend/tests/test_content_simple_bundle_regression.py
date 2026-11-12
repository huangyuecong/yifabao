import io
import json
import zipfile
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select


def test_final_bundle_ignores_missing_old_visual_and_keeps_audio_subtitles(
    content_client,
    user_headers,
    other_headers,
):
    task_name = f"旧画面缺失导出-{uuid4().hex[:8]}"
    created = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "video",
            "name": task_name,
            "topic": "只导出最终成品",
            "product": "测试产品",
        },
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]

    from content_service.db import SessionLocal
    from content_service.models import (
        Artifact,
        ContentTask,
        ProjectAsset,
        TaskStage,
        VideoProject,
        VideoProjectRevision,
    )
    from content_service.services import storage

    visual_bytes = b"old-visual-source-that-is-no-longer-needed"
    audio_bytes = b"RIFF\x24\x00\x00\x00WAVEfmt old-final-voice"
    subtitle_bytes = "1\n00:00:00,000 --> 00:00:01,000\n归档字幕\n".encode()
    final_video_bytes = b"\x00\x00\x00\x18ftypmp42-final-render-is-self-contained"
    token = uuid4().hex
    visual_key = f"tests/simple-bundle/{token}/old-shot.png"
    audio_key = f"tests/simple-bundle/{token}/voice.wav"
    subtitle_key = f"tests/simple-bundle/{token}/subtitles.srt"
    final_video_key = f"tests/simple-bundle/{token}/final.mp4"
    visual_path = storage.path_for(visual_key)
    visual_path.write_bytes(visual_bytes)
    storage.path_for(audio_key).write_bytes(audio_bytes)
    storage.path_for(subtitle_key).write_bytes(subtitle_bytes)
    storage.path_for(final_video_key).write_bytes(final_video_bytes)

    project_id = str(uuid4())
    revision_id = str(uuid4())
    with SessionLocal() as session:
        task = session.get(ContentTask, task_id)
        assert task is not None
        project = VideoProject(
            id=project_id,
            task_id=task_id,
            current_revision=1,
            current_revision_id=revision_id,
            created_by="user-a",
        )
        visual = Artifact(
            task_id=task_id,
            kind="image",
            stage_key="image",
            version=1,
            status="confirmed",
            content_json=json.dumps({"media_type": "image/png"}),
            storage_key=visual_key,
            created_by="user-a",
        )
        voice = Artifact(
            task_id=task_id,
            kind="voice",
            stage_key="voice_subtitles",
            version=1,
            status="confirmed",
            content_json=json.dumps({"media_type": "audio/wav"}),
            storage_key=audio_key,
            created_by="user-a",
        )
        subtitle = Artifact(
            task_id=task_id,
            kind="subtitle",
            stage_key="voice_subtitles",
            version=1,
            status="confirmed",
            content_json=json.dumps({"media_type": "application/x-subrip"}),
            storage_key=subtitle_key,
            created_by="user-a",
        )
        final_video = Artifact(
            task_id=task_id,
            kind="video",
            stage_key="video_preview",
            version=3,
            status="confirmed",
            content_json=json.dumps(
                {
                    "project_id": project_id,
                    "revision_id": revision_id,
                    "render_profile": "final-720p",
                    "purpose": "final_project_render",
                    "media_type": "video/mp4",
                }
            ),
            storage_key=final_video_key,
            created_by="user-a",
        )
        session.add_all([project, visual, voice, subtitle, final_video])
        session.flush()

        visual_asset = ProjectAsset(
            project_id=project_id,
            source_type="artifact",
            source_id=visual.id,
            artifact_id=visual.id,
            role="video",
            label="已删除的旧画面",
            created_by="user-a",
        )
        voice_asset = ProjectAsset(
            project_id=project_id,
            source_type="artifact",
            source_id=voice.id,
            artifact_id=voice.id,
            role="voice",
            label="最终配音",
            created_by="user-a",
        )
        subtitle_asset = ProjectAsset(
            project_id=project_id,
            source_type="artifact",
            source_id=subtitle.id,
            artifact_id=subtitle.id,
            role="subtitle",
            label="最终字幕",
            created_by="user-a",
        )
        session.add_all([visual_asset, voice_asset, subtitle_asset])
        session.flush()

        def item(project_asset_id: str, label: str) -> dict:
            return {
                "id": str(uuid4()),
                "project_asset_id": project_asset_id,
                "label": label,
                "enabled": True,
                "start_seconds": 0,
                "duration_seconds": 1,
            }

        timeline = {
            "tracks": [
                {
                    "kind": "video",
                    "name": "主画面",
                    "muted": False,
                    "items": [item(visual_asset.id, "旧镜头")],
                },
                {
                    "kind": "voice",
                    "name": "配音",
                    "muted": False,
                    "items": [item(voice_asset.id, "旁白")],
                },
                {
                    "kind": "subtitle",
                    "name": "字幕",
                    "muted": False,
                    "items": [item(subtitle_asset.id, "对白字幕")],
                },
            ]
        }
        session.add(
            VideoProjectRevision(
                id=revision_id,
                project_id=project_id,
                revision=1,
                source="human",
                timeline_json=json.dumps(timeline, ensure_ascii=False),
                operations_json="[]",
                created_by="user-a",
            )
        )
        video_stage = session.scalar(
            select(TaskStage).where(
                TaskStage.task_id == task_id,
                TaskStage.stage_key == "video_preview",
            )
        )
        assert video_stage is not None
        video_stage.state = "confirmed"
        video_stage.selected_artifact_ids_json = json.dumps({"video": [final_video.id]})
        video_stage.confirmed_artifact_ids_json = json.dumps({"video": [final_video.id]})
        video_stage.confirmed_by = "user-a"
        video_stage.confirmed_at = datetime.now(UTC)
        task.current_stage = "export"
        task.workflow_enforced = True
        session.commit()

    # The immutable render is self-contained; deleting an obsolete visual input
    # must not prevent the user from downloading that already-confirmed result.
    visual_path.unlink()
    assert not visual_path.exists()

    workflow = content_client.get(f"/api/v1/tasks/{task_id}/workflow", headers=user_headers).json()
    export_stage = next(item for item in workflow["stages"] if item["key"] == "export")
    confirmed_export = content_client.post(
        f"/api/v1/tasks/{task_id}/stages/export/confirm",
        headers=user_headers,
        json={"expected_version": export_stage["version"]},
    )
    assert confirmed_export.status_code == 200, confirmed_export.text
    archived = content_client.get(f"/api/v1/tasks/{task_id}", headers=user_headers)
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] is True
    assert (
        content_client.get(f"/api/v1/tasks/{task_id}/export", headers=other_headers).status_code
        == 403
    )

    exported = content_client.get(f"/api/v1/tasks/{task_id}/export", headers=user_headers)
    assert exported.status_code == 200, exported.text
    expected = {
        f"{task_name}-成片-v3.mp4": final_video_bytes,
        f"{task_name}-配音-1.wav": audio_bytes,
        f"{task_name}-字幕-1.srt": subtitle_bytes,
    }
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert set(archive.namelist()) == set(expected)
        assert all("/" not in name and "\\" not in name for name in archive.namelist())
        assert not any(name.lower().endswith(".json") for name in archive.namelist())
        assert not any(
            internal_id in name
            for internal_id in (task_id, project_id, revision_id)
            for name in archive.namelist()
        )
        for name, content in expected.items():
            assert archive.read(name) == content
