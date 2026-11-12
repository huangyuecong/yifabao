import io
import json
import zipfile
from uuid import uuid4

import pytest


def _stage(workflow: dict, key: str) -> dict:
    return next(item for item in workflow["stages"] if item["key"] == key)


def test_export_can_be_reconfirmed_after_reselecting_multiple_images(
    content_client,
    user_headers,
):
    created = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "image",
            "name": f"图片重选回归-{uuid4().hex[:8]}",
            "topic": "中秋团圆",
            "product": "醇樽雅酿",
        },
    )
    assert created.status_code == 200, created.text
    task = created.json()

    generated = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/image/generate",
        headers=user_headers,
        json={
            "count": 4,
            "parameters": {
                "prompt": "中秋团圆宴产品图",
                "scene": "家庭团圆晚宴",
                "style": "温暖写实摄影",
                "size": "64x64",
            },
        },
    )
    assert generated.status_code == 200, generated.text
    images = generated.json()["artifacts"]
    assert len(images) == 4
    assert all(item["content"]["scene"] == "家庭团圆晚宴" for item in images)
    assert all(item["content"]["style"] == "温暖写实摄影" for item in images)

    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow",
        headers=user_headers,
    ).json()
    image_stage = _stage(workflow, "image")
    initial_selection = content_client.put(
        f"/api/v1/tasks/{task['id']}/stages/image/selection",
        headers=user_headers,
        json={
            "expected_version": image_stage["version"],
            "artifact_ids": [images[0]["id"]],
        },
    )
    assert initial_selection.status_code == 200, initial_selection.text
    confirmed_image = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/image/confirm",
        headers=user_headers,
        json={"expected_version": initial_selection.json()["version"]},
    )
    assert confirmed_image.status_code == 200, confirmed_image.text

    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow",
        headers=user_headers,
    ).json()
    initial_export = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/export/confirm",
        headers=user_headers,
        json={"expected_version": _stage(workflow, "export")["version"]},
    )
    assert initial_export.status_code == 200, initial_export.text

    restored = content_client.post(
        f"/api/v1/tasks/{task['id']}/restore",
        headers=user_headers,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["id"] != task["id"]
    task = restored.json()
    images = content_client.get(
        f"/api/v1/tasks/{task['id']}/artifacts",
        headers=user_headers,
    ).json()
    images = sorted(
        (item for item in images if item["kind"] == "image"), key=lambda item: item["version"]
    )

    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow",
        headers=user_headers,
    ).json()
    reselection = content_client.put(
        f"/api/v1/tasks/{task['id']}/stages/image/selection",
        headers=user_headers,
        json={
            "expected_version": _stage(workflow, "image")["version"],
            "artifact_ids": [item["id"] for item in images],
        },
    )
    assert reselection.status_code == 200, reselection.text
    assert reselection.json()["selected_artifact_ids"] == {"image": [item["id"] for item in images]}

    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow",
        headers=user_headers,
    ).json()
    stale_export = _stage(workflow, "export")
    assert stale_export["state"] == "stale"
    premature_export = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/export/confirm",
        headers=user_headers,
        json={"expected_version": stale_export["version"]},
    )
    assert premature_export.status_code == 409, premature_export.text
    assert "图片" in premature_export.json()["detail"]

    reconfirmed_image = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/image/confirm",
        headers=user_headers,
        json={"expected_version": reselection.json()["version"]},
    )
    assert reconfirmed_image.status_code == 200, reconfirmed_image.text

    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow",
        headers=user_headers,
    ).json()
    export_stage = _stage(workflow, "export")
    assert export_stage["state"] == "stale"

    reconfirmed_export = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/export/confirm",
        headers=user_headers,
        json={"expected_version": export_stage["version"]},
    )
    assert reconfirmed_export.status_code == 200, reconfirmed_export.text
    assert reconfirmed_export.json()["state"] == "confirmed"

    exported = content_client.get(
        f"/api/v1/tasks/{task['id']}/export",
        headers=user_headers,
    )
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert set(archive.namelist()) == {
            f"{task['name']}-图片-v{item['version']}.png" for item in images
        }
        from content_service.services import storage

        for item in images:
            assert archive.read(f"{task['name']}-图片-v{item['version']}.png") == (
                storage.path_for(item["storage_key"]).read_bytes()
            )


@pytest.mark.parametrize(
    ("mode", "blocking_stage", "blocking_label"),
    [
        ("image", "image", "图片"),
        ("copy", "compliance", "合规检测"),
        ("video", "configuration", "任务配置"),
        ("full", "compliance", "合规检测"),
    ],
)
def test_stale_export_blocks_touched_applicable_stage_across_modes(
    content_client,
    user_headers,
    mode,
    blocking_stage,
    blocking_label,
):
    from content_service.db import SessionLocal
    from content_service.models import Artifact, ContentTask, TaskStage
    from content_service.workflow import applicable_stages

    created = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": mode,
            "name": f"导出前置校验-{mode}-{uuid4().hex[:8]}",
            "topic": "阶段状态回归",
            "product": "臻品雅酿",
        },
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]

    with SessionLocal() as session:
        task = session.get(ContentTask, task_id)
        assert task is not None
        visible_stage_keys = applicable_stages(task)
        stages = session.query(TaskStage).filter(TaskStage.task_id == task_id).all()
        by_key = {item.stage_key: item for item in stages}
        for item in stages:
            if item.stage_key not in visible_stage_keys:
                # In full mode these are operable legacy stages but no longer
                # part of the visible workflow, so they must not block export.
                item.state = "stale"
                continue
            if item.stage_key == "export":
                item.state = "stale"
            elif item.stage_key == "topic":
                item.state = "skipped"
                item.skipped_reason = "用户明确跳过标题"
            else:
                item.state = "confirmed"
        by_key[blocking_stage].state = "awaiting_confirmation"

        if mode in {"video", "full"}:
            final_video = Artifact(
                task_id=task_id,
                kind="video",
                stage_key="video_preview",
                version=1,
                status="confirmed",
                content_json="{}",
                created_by="user-a",
            )
            session.add(final_video)
            session.flush()
            selection = json.dumps({"video": [final_video.id]})
            by_key["video_preview"].state = "confirmed"
            by_key["video_preview"].selected_artifact_ids_json = selection
            by_key["video_preview"].confirmed_artifact_ids_json = selection
        session.commit()

    workflow = content_client.get(
        f"/api/v1/tasks/{task_id}/workflow",
        headers=user_headers,
    ).json()
    export_stage = _stage(workflow, "export")
    blocked = content_client.post(
        f"/api/v1/tasks/{task_id}/stages/export/confirm",
        headers=user_headers,
        json={"expected_version": export_stage["version"]},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocking_label in blocked.json()["detail"]

    with SessionLocal() as session:
        blocker = (
            session.query(TaskStage)
            .filter(
                TaskStage.task_id == task_id,
                TaskStage.stage_key == blocking_stage,
            )
            .one()
        )
        blocker.state = "confirmed"
        session.commit()

    workflow = content_client.get(
        f"/api/v1/tasks/{task_id}/workflow",
        headers=user_headers,
    ).json()
    ready_export = _stage(workflow, "export")
    confirmed = content_client.post(
        f"/api/v1/tasks/{task_id}/stages/export/confirm",
        headers=user_headers,
        json={"expected_version": ready_export["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["state"] == "confirmed"


@pytest.mark.parametrize(
    "legacy_state", ["not_started", "skipped", "stale", "awaiting_confirmation"]
)
def test_quick_video_stale_export_ignores_hidden_legacy_stages(
    content_client,
    user_headers,
    legacy_state,
):
    from content_service.db import SessionLocal
    from content_service.models import Artifact, TaskStage

    created = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "video",
            "name": f"快捷视频重导出-{uuid4().hex[:8]}",
            "topic": "编辑器直接成片",
            "product": "臻品雅酿",
        },
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]

    with SessionLocal() as session:
        final_video = Artifact(
            task_id=task_id,
            kind="video",
            stage_key="video_preview",
            version=1,
            status="selected",
            content_json="{}",
            created_by="user-a",
        )
        session.add(final_video)
        session.flush()
        stages = session.query(TaskStage).filter(TaskStage.task_id == task_id).all()
        by_key = {item.stage_key: item for item in stages}
        for key in ["image", "storyboard", "voice_subtitles"]:
            by_key[key].state = legacy_state
        selection = json.dumps({"video": [final_video.id]})
        video_stage = by_key["video_preview"]
        video_stage.state = "awaiting_confirmation"
        video_stage.selected_artifact_ids_json = selection
        video_stage.confirmed_artifact_ids_json = "{}"
        by_key["export"].state = "stale"
        video_version = video_stage.version
        final_video_id = final_video.id
        session.commit()

    reconfirmed_video = content_client.post(
        f"/api/v1/tasks/{task_id}/stages/video_preview/confirm",
        headers=user_headers,
        json={"expected_version": video_version},
    )
    assert reconfirmed_video.status_code == 200, reconfirmed_video.text
    assert reconfirmed_video.json()["confirmed_artifact_ids"] == {"video": [final_video_id]}

    workflow = content_client.get(
        f"/api/v1/tasks/{task_id}/workflow",
        headers=user_headers,
    ).json()
    for key in ["image", "storyboard", "voice_subtitles"]:
        assert _stage(workflow, key)["state"] == legacy_state
        assert _stage(workflow, key)["applicable"] is False
    export_stage = _stage(workflow, "export")
    assert export_stage["state"] == "stale"

    confirmed_export = content_client.post(
        f"/api/v1/tasks/{task_id}/stages/export/confirm",
        headers=user_headers,
        json={"expected_version": export_stage["version"]},
    )
    assert confirmed_export.status_code == 200, confirmed_export.text
    assert confirmed_export.json()["state"] == "confirmed"
    completed = content_client.get(
        f"/api/v1/tasks/{task_id}/workflow",
        headers=user_headers,
    ).json()
    visible = [item for item in completed["stages"] if item["applicable"]]
    assert len(visible) == 3
    assert all(item["state"] == "confirmed" for item in visible)
