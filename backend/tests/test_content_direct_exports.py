import io
import zipfile
from urllib.parse import unquote
from uuid import uuid4

from docx import Document
from PIL import Image


def create_task(client, headers, mode: str):
    response = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "mode": mode,
            "name": f"直接导出-{mode}-{uuid4().hex[:8]}",
            "topic": "臻品文化",
            "product": "臻品雅酿",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def confirm_artifact(client, headers, artifact_id: str):
    response = client.post(
        f"/api/v1/artifacts/{artifact_id}/confirm",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_topic_exports_only_the_current_confirmed_selected_title(
    content_client,
    user_headers,
):
    task = create_task(content_client, user_headers, "copy")
    generated = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/topic/generate",
        headers=user_headers,
        json={"count": 1},
    )
    assert generated.status_code == 200, generated.text
    topic = generated.json()["artifacts"][0]
    title = topic["content"]["candidates"][0]["title"]
    confirm_artifact(content_client, user_headers, topic["id"])

    text_export = content_client.get(
        f"/api/v1/artifacts/{topic['id']}/download?format=txt",
        headers=user_headers,
    )
    assert text_export.status_code == 200, text_export.text
    assert text_export.content.decode("utf-8").strip() == title

    markdown_export = content_client.get(
        f"/api/v1/artifacts/{topic['id']}/download?format=md",
        headers=user_headers,
    )
    assert markdown_export.status_code == 200, markdown_export.text
    assert markdown_export.content.decode("utf-8").strip() == f"# {title}"

    word_export = content_client.get(
        f"/api/v1/artifacts/{topic['id']}/download?format=docx",
        headers=user_headers,
    )
    assert word_export.status_code == 200, word_export.text
    document = Document(io.BytesIO(word_export.content))
    assert [item.text for item in document.paragraphs if item.text.strip()] == [title]

    replacement = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/topic/generate",
        headers=user_headers,
        json={"count": 1, "instruction": "生成一个新的标题"},
    )
    assert replacement.status_code == 200, replacement.text
    replacement_topic = replacement.json()["artifacts"][0]
    confirm_artifact(content_client, user_headers, replacement_topic["id"])
    stale_export = content_client.get(
        f"/api/v1/artifacts/{topic['id']}/download?format=txt",
        headers=user_headers,
    )
    assert stale_export.status_code == 409, stale_export.text
    assert "当前已选择并确认" in stale_export.json()["detail"]


def test_copy_exports_are_clean_single_documents_and_bundle_omits_compliance(
    content_client,
    user_headers,
):
    task = create_task(content_client, user_headers, "copy")
    generated = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-copy",
        headers=user_headers,
        json={"count": 1},
    )
    assert generated.status_code == 200, generated.text
    copy = confirm_artifact(content_client, user_headers, generated.json()["id"])

    text_export = content_client.get(
        f"/api/v1/artifacts/{copy['id']}/download?format=txt",
        headers=user_headers,
    )
    assert text_export.status_code == 200, text_export.text
    assert text_export.headers["content-type"].startswith("text/plain")
    exported_text = text_export.content.decode("utf-8")
    assert exported_text.strip()
    assert "内容类型：" not in exported_text
    assert "内容 ID" not in exported_text
    assert '"passed"' not in exported_text

    markdown_export = content_client.get(
        f"/api/v1/artifacts/{copy['id']}/download?format=md",
        headers=user_headers,
    )
    assert markdown_export.status_code == 200, markdown_export.text
    assert markdown_export.headers["content-type"].startswith("text/markdown")
    assert markdown_export.content.decode("utf-8").lstrip().startswith("# ")

    word_export = content_client.get(
        f"/api/v1/artifacts/{copy['id']}/download?format=docx",
        headers=user_headers,
    )
    assert word_export.status_code == 200, word_export.text
    assert word_export.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    document = Document(io.BytesIO(word_export.content))
    assert any(paragraph.text.strip() for paragraph in document.paragraphs)

    compliance = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/compliance/generate",
        headers=user_headers,
        json={"count": 1, "source_artifact_id": copy["id"]},
    )
    assert compliance.status_code == 200, compliance.text
    compliance_artifact = compliance.json()["artifacts"][0]
    confirm_artifact(content_client, user_headers, compliance_artifact["id"])

    bundle = content_client.get(
        f"/api/v1/tasks/{task['id']}/export",
        headers=user_headers,
    )
    assert bundle.status_code == 200, bundle.text
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        names = archive.namelist()
        assert names == [f"{task['name']}-文案-v{copy['version']}.docx"]
        bundled_document = Document(io.BytesIO(archive.read(names[0])))
        assert [item.text for item in bundled_document.paragraphs] == [
            item.text for item in document.paragraphs
        ]
    assert "完整文案素材包" in unquote(bundle.headers["content-disposition"])


def test_image_exports_support_png_and_jpg_without_zip(
    content_client,
    user_headers,
):
    task = create_task(content_client, user_headers, "image")
    generated = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-images",
        headers=user_headers,
        json={
            "count": 1,
            "parameters": {"size": "1600x1200", "ratio": "4:3"},
        },
    )
    assert generated.status_code == 200, generated.text
    image_artifact = confirm_artifact(
        content_client,
        user_headers,
        generated.json()[0]["id"],
    )

    for file_format, expected_media_type, expected_pillow_format in (
        ("png", "image/png", "PNG"),
        ("jpg", "image/jpeg", "JPEG"),
    ):
        exported = content_client.get(
            f"/api/v1/artifacts/{image_artifact['id']}/download?format={file_format}",
            headers=user_headers,
        )
        assert exported.status_code == 200, exported.text
        assert exported.headers["content-type"].startswith(expected_media_type)
        assert exported.content[:2] != b"PK"
        with Image.open(io.BytesIO(exported.content)) as image:
            assert image.format == expected_pillow_format
            assert image.size == (1600, 1200)

    unsupported = content_client.get(
        f"/api/v1/artifacts/{image_artifact['id']}/download?format=bmp",
        headers=user_headers,
    )
    assert unsupported.status_code == 422
    assert "PNG、JPG 或 WebP" in unsupported.json()["detail"]


def test_full_bundle_uses_only_current_applicable_confirmed_artifacts(
    content_client,
    user_headers,
):
    from sqlalchemy import select

    from content_service.db import SessionLocal
    from content_service.models import ContentTask, TaskStage
    from content_service.services import create_artifact, dump, storage

    task = create_task(content_client, user_headers, "full")
    with SessionLocal() as session:
        stored_task = session.get(ContentTask, task["id"])

        def add(kind: str, content: dict, *, storage_key: str | None = None):
            artifact = create_artifact(
                session,
                stored_task,
                kind,
                content,
                "user-a",
                storage_key,
            )
            artifact.stage_key = {
                "topic": "topic",
                "copy": "copy",
                "image": "image",
                "voice": "voice_subtitles",
                "video": "video_preview",
            }[kind]
            artifact.status = "confirmed"
            return artifact

        old_image_key = f"tasks/{task['id']}/images/old.png"
        current_image_key = f"tasks/{task['id']}/images/current.png"
        old_video_key = f"tasks/{task['id']}/videos/old.mp4"
        current_video_key = f"tasks/{task['id']}/videos/current.mp4"
        storage.path_for(old_image_key).write_bytes(b"old-history-image")
        storage.path_for(current_image_key).write_bytes(b"current-confirmed-image")
        storage.path_for(old_video_key).write_bytes(b"old-history-video")
        storage.path_for(current_video_key).write_bytes(b"current-confirmed-video")

        add("topic", {"candidates": [{"title": "旧标题"}]})
        current_title = add("topic", {"candidates": [{"title": "当前标题"}]})
        add(
            "copy",
            {"candidates": [{"title": "旧标题", "body": "旧文案"}]},
        )
        current_copy = add(
            "copy",
            {
                "candidates": [{"title": "当前标题", "body": "当前文案"}],
                "source_title_artifact_id": current_title.id,
            },
        )
        add("image", {"media_type": "image/png"}, storage_key=old_image_key)
        current_image = add(
            "image",
            {"media_type": "image/png"},
            storage_key=current_image_key,
        )
        add(
            "video",
            {"purpose": "final_project_render", "media_type": "video/mp4"},
            storage_key=old_video_key,
        )
        current_video = add(
            "video",
            {"purpose": "final_project_render", "media_type": "video/mp4"},
            storage_key=current_video_key,
        )
        hidden_voice = add(
            "voice",
            {"media_type": "audio/wav"},
        )
        current_by_stage = {
            "topic": {"topic": [current_title.id]},
            "copy": {"copy": [current_copy.id]},
            "image": {"image": [current_image.id]},
            "video_preview": {"video": [current_video.id]},
            # This stage is legacy/hidden for full tasks and must not leak into
            # the bundle even if an old database row remains confirmed.
            "voice_subtitles": {"voice": [hidden_voice.id]},
        }
        stages = session.scalars(select(TaskStage).where(TaskStage.task_id == task["id"])).all()
        for task_stage in stages:
            selection = current_by_stage.get(task_stage.stage_key)
            if selection is not None:
                task_stage.state = "confirmed"
                task_stage.selected_artifact_ids_json = dump(selection)
                task_stage.confirmed_artifact_ids_json = dump(selection)
            elif task_stage.stage_key == "export":
                task_stage.state = "confirmed"
        stored_task.workflow_enforced = True
        expected_names = {
            f"{task['name']}-标题-v{current_title.version}.docx",
            f"{task['name']}-文案-v{current_copy.version}.docx",
            f"{task['name']}-图片-v{current_image.version}.png",
            f"{task['name']}-成片-v{current_video.version}.mp4",
        }
        session.commit()

    exported = content_client.get(
        f"/api/v1/tasks/{task['id']}/export",
        headers=user_headers,
    )
    assert exported.status_code == 200, exported.text
    assert "完整创作素材包" in unquote(exported.headers["content-disposition"])
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert set(archive.namelist()) == expected_names
        text_by_kind = {}
        for name in archive.namelist():
            if name.endswith(".docx"):
                document = Document(io.BytesIO(archive.read(name)))
                text_by_kind["文案" if "-文案-" in name else "标题"] = "\n".join(
                    paragraph.text for paragraph in document.paragraphs
                )
        assert text_by_kind["标题"] == "当前标题"
        assert "当前文案" in text_by_kind["文案"]
        assert "旧文案" not in text_by_kind["文案"]
        archived_payloads = [archive.read(name) for name in archive.namelist()]
        assert b"current-confirmed-image" in archived_payloads
        assert b"current-confirmed-video" in archived_payloads
        assert b"old-history-image" not in archived_payloads
        assert b"old-history-video" not in archived_payloads


def test_quick_video_requires_export_confirmation_but_allows_final_single_download(
    content_client,
    user_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import ContentTask
    from content_service.services import create_artifact, storage
    from content_service.workflow import adopt_revision

    task = create_task(content_client, user_headers, "video")
    with SessionLocal() as session:
        stored_task = session.get(ContentTask, task["id"])
        output_key = f"tasks/{task['id']}/videos/quick-final.mp4"
        storage.path_for(output_key).write_bytes(b"quick-final-video")
        video = create_artifact(
            session,
            stored_task,
            "video",
            {"purpose": "final_project_render", "media_type": "video/mp4"},
            "user-a",
            output_key,
        )
        adopt_revision(session, stored_task, video, replace_all=True)
        video_id = video.id
        session.commit()
    assert confirm_artifact(content_client, user_headers, video_id)["status"] == "confirmed"

    single = content_client.get(
        f"/api/v1/artifacts/{video_id}/download",
        headers=user_headers,
    )
    assert single.status_code == 200, single.text
    assert single.content == b"quick-final-video"
    blocked = content_client.get(
        f"/api/v1/tasks/{task['id']}/export",
        headers=user_headers,
    )
    assert blocked.status_code == 409, blocked.text
    assert "确认导出" in blocked.json()["detail"]

    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow",
        headers=user_headers,
    ).json()
    export_stage = next(item for item in workflow["stages"] if item["key"] == "export")
    confirmed_export = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/export/confirm",
        headers=user_headers,
        json={"expected_version": export_stage["version"]},
    )
    assert confirmed_export.status_code == 200, confirmed_export.text
    bundle = content_client.get(
        f"/api/v1/tasks/{task['id']}/export",
        headers=user_headers,
    )
    assert bundle.status_code == 200, bundle.text
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        assert archive.namelist() == [f"{task['name']}-成片-v1.mp4"]
        assert archive.read(archive.namelist()[0]) == single.content


def test_full_bundle_exports_pinned_video_revision_and_its_archived_dependencies(
    content_client,
    user_headers,
):
    """The final package follows the rendered revision, not the mutable project head."""

    from content_service.db import SessionLocal
    from content_service.models import ContentTask, VideoProject
    from content_service.services import (
        create_artifact,
        ensure_project_asset,
        storage,
    )
    from content_service.workflow import adopt_revision

    task = create_task(content_client, user_headers, "full")
    generated = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-images",
        headers=user_headers,
        json={"count": 1},
    )
    assert generated.status_code == 200, generated.text
    image = generated.json()[0]
    assert confirm_artifact(content_client, user_headers, image["id"])["status"] == "confirmed"

    created_project = content_client.post(
        f"/api/v1/tasks/{task['id']}/video-project",
        headers=user_headers,
    )
    assert created_project.status_code == 200, created_project.text
    project = created_project.json()
    workflow_asset = next(item for item in project["assets"] if item["artifact_id"] == image["id"])
    video_track = next(
        item for item in project["revision"]["timeline"]["tracks"] if item["kind"] == "video"
    )
    subtitle_track = next(
        item for item in project["revision"]["timeline"]["tracks"] if item["kind"] == "subtitle"
    )
    with SessionLocal() as session:
        stored_task = session.get(ContentTask, task["id"])
        stored_project = session.get(VideoProject, project["id"])
        subtitle_key = f"tasks/{task['id']}/subtitles/existing.srt"
        storage.path_for(subtitle_key).write_bytes(b"existing-project-subtitle")
        subtitle_artifact = create_artifact(
            session,
            stored_task,
            "subtitle",
            {"media_type": "application/x-subrip"},
            "user-a",
            subtitle_key,
        )
        subtitle_asset = ensure_project_asset(
            session,
            stored_project,
            source_type="artifact",
            source_id=subtitle_artifact.id,
            role="subtitle",
            label="已有字幕文件",
            created_by="user-a",
            artifact_id=subtitle_artifact.id,
        )
        subtitle_asset_id = subtitle_asset.id
        session.commit()
    added = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": project["current_revision"],
            "operations": [
                {
                    "op_id": "add-confirmed-image",
                    "type": "add",
                    "track_id": video_track["id"],
                    "item": {
                        "id": "confirmed-image-clip",
                        "project_asset_id": workflow_asset["id"],
                        "label": "已确认主画面",
                        "start_seconds": 0,
                        "duration_seconds": 5,
                    },
                },
                {
                    "op_id": "add-existing-subtitle-asset",
                    "type": "add",
                    "track_id": subtitle_track["id"],
                    "item": {
                        "id": "existing-subtitle-clip",
                        "project_asset_id": subtitle_asset_id,
                        "label": "已有字幕文件",
                        "start_seconds": 0,
                        "duration_seconds": 5,
                    },
                },
                {
                    "op_id": "add-late-inline-subtitle",
                    "type": "add",
                    "track_id": subtitle_track["id"],
                    "item": {
                        "id": "late-inline-subtitle",
                        "content": "最终版本第二句",
                        "start_seconds": 2.5,
                        "duration_seconds": 1,
                    },
                },
                {
                    "op_id": "add-early-inline-subtitle",
                    "type": "add",
                    "track_id": subtitle_track["id"],
                    "item": {
                        "id": "early-inline-subtitle",
                        "content": "最终版本第一句",
                        "start_seconds": 0.25,
                        "duration_seconds": 1.5,
                    },
                },
                {
                    "op_id": "add-disabled-inline-subtitle",
                    "type": "add",
                    "track_id": subtitle_track["id"],
                    "item": {
                        "id": "disabled-inline-subtitle",
                        "content": "禁用字幕不应导出",
                        "start_seconds": 1,
                        "duration_seconds": 1,
                        "enabled": False,
                    },
                },
            ],
        },
    )
    assert added.status_code == 200, added.text
    rendered_revision = added.json()["revision"]

    with SessionLocal() as session:
        stored_task = session.get(ContentTask, task["id"])
        output_key = f"tasks/{task['id']}/video-projects/{project['id']}/final-test.mp4"
        storage.path_for(output_key).write_bytes(b"immutable-final-video")
        video = create_artifact(
            session,
            stored_task,
            "video",
            {
                "format": "mp4",
                "media_type": "video/mp4",
                "purpose": "final_project_render",
                "render_profile": "final-720p",
                "project_id": project["id"],
                "revision_id": rendered_revision["id"],
                "revision": rendered_revision["revision"],
            },
            "user-a",
            output_key,
        )
        adopt_revision(session, stored_task, video, replace_all=True)
        final_video_id = video.id
        session.commit()
    assert confirm_artifact(content_client, user_headers, final_video_id)["status"] == "confirmed"

    # Move the project head after the render is pinned, then archive the unused
    # asset. Export confirmation archives the task, so these edits must happen first.
    removed = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": rendered_revision["revision"],
            "operations": [
                {
                    "op_id": "remove-after-render",
                    "type": "remove",
                    "item_id": "confirmed-image-clip",
                },
                {
                    "op_id": "change-subtitle-after-render",
                    "type": "update",
                    "item_id": "early-inline-subtitle",
                    "changes": {"content": "工程头新字幕不应导出"},
                },
            ],
        },
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["current_revision_id"] != rendered_revision["id"]
    archived = content_client.delete(
        f"/api/v1/video-projects/{project['id']}/assets/{workflow_asset['id']}",
        headers=user_headers,
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["active"] is False

    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow",
        headers=user_headers,
    ).json()
    export_stage = next(item for item in workflow["stages"] if item["key"] == "export")
    confirmed_export = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/export/confirm",
        headers=user_headers,
        json={"expected_version": export_stage["version"]},
    )
    assert confirmed_export.status_code == 200, confirmed_export.text

    exported = content_client.get(
        f"/api/v1/tasks/{task['id']}/export",
        headers=user_headers,
    )
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        names = archive.namelist()
        assert set(names) == {
            f"{task['name']}-图片-v{image['version']}.png",
            f"{task['name']}-成片-v1.mp4",
            f"{task['name']}-字幕-1.srt",
            f"{task['name']}-成片字幕-v1.srt",
        }
        assert archive.read(f"{task['name']}-成片-v1.mp4") == b"immutable-final-video"
        assert archive.read(f"{task['name']}-字幕-1.srt") == b"existing-project-subtitle"
        exported_subtitles = archive.read(f"{task['name']}-成片字幕-v1.srt").decode("utf-8")
    assert exported_subtitles == (
        "1\n00:00:00,250 --> 00:00:01,750\n最终版本第一句\n\n"
        "2\n00:00:02,500 --> 00:00:03,500\n最终版本第二句\n"
    )
    assert "禁用字幕不应导出" not in exported_subtitles
    assert "工程头新字幕不应导出" not in exported_subtitles


def test_bundle_keeps_created_audio_subtitles_and_deduplicates_render_sidecars(
    content_client,
    tmp_path,
    monkeypatch,
):
    from types import SimpleNamespace

    from agent_core import LocalObjectStorage
    from content_service import services
    from content_service.models import Artifact, ContentTask

    monkeypatch.setattr(services, "settings", SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(services, "storage", LocalObjectStorage(tmp_path / "objects"))
    task = ContentTask(id=str(uuid4()), mode="full", name="春节宣传", version=1)

    def artifact(kind: str, suffix: str, payload: bytes) -> Artifact:
        key = f"created/{kind}{suffix}"
        services.storage.path_for(key).write_bytes(payload)
        return Artifact(
            id=str(uuid4()),
            task_id=task.id,
            kind=kind,
            version=1,
            status="confirmed",
            content_json="{}",
            storage_key=key,
        )

    video = artifact("video", ".mp4", b"final-video")
    voice = artifact("voice", ".mp3", b"created-voice")
    subtitle = artifact("subtitle", ".srt", b"created-subtitle")
    music = tmp_path / "music.wav"
    music.write_bytes(b"rendered-background-music")
    dependencies = [
        {
            "path": services.storage.path_for(voice.storage_key),
            "media_type": "audio/mpeg",
            "roles": ["主配音"],
        },
        {
            "path": services.storage.path_for(subtitle.storage_key),
            "media_type": "application/x-subrip",
            "role": "subtitle",
        },
        {"path": music, "media_type": "audio/wav", "roles": ["背景音乐"]},
        {"path": music, "media_type": "audio/wav", "roles": ["背景音乐"]},
        {"path": tmp_path / "removed-reference.png", "media_type": "image/png"},
        {"path": tmp_path / "removed-source.mp4", "media_type": "video/mp4"},
        {"path": tmp_path / "private-project.json", "media_type": "application/json"},
    ]
    bundle = services.export_task_bundle(task, [video, voice, subtitle], dependencies)
    with zipfile.ZipFile(bundle) as archive:
        assert set(archive.namelist()) == {
            "春节宣传-成片-v1.mp4",
            "春节宣传-配音-v1.mp3",
            "春节宣传-字幕-v1.srt",
            "春节宣传-背景音乐-1.wav",
        }
        assert archive.read("春节宣传-配音-v1.mp3") == b"created-voice"
        assert archive.read("春节宣传-字幕-v1.srt") == b"created-subtitle"
        assert archive.read("春节宣传-背景音乐-1.wav") == b"rendered-background-music"


def test_bundle_preserves_distinct_linked_documents_with_matching_version_numbers(
    content_client,
    tmp_path,
    monkeypatch,
):
    from types import SimpleNamespace

    from content_service import services
    from content_service.models import Artifact, ContentTask

    monkeypatch.setattr(services, "settings", SimpleNamespace(data_dir=tmp_path))
    task = ContentTask(id=str(uuid4()), mode="copy", name="中秋宣传", version=1)
    artifacts = [
        Artifact(
            id=str(uuid4()),
            task_id=str(uuid4()),
            kind="copy",
            version=1,
            status="confirmed",
            content_json=services.dump({"candidate": {"title": title, "body": body}}),
        )
        for title, body in (("家庭团圆", "第一份文案"), ("朋友相聚", "第二份文案"))
    ]
    bundle = services.export_task_bundle(task, artifacts)
    with zipfile.ZipFile(bundle) as archive:
        assert archive.namelist() == ["中秋宣传-文案-v1.docx", "中秋宣传-文案-v1-2.docx"]
        texts = [
            "\n".join(
                paragraph.text for paragraph in Document(io.BytesIO(archive.read(name))).paragraphs
            )
            for name in archive.namelist()
        ]
    assert texts == ["家庭团圆\n第一份文案", "朋友相聚\n第二份文案"]
