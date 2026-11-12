import io
import json
import time
import zipfile
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select


def create_task(client, headers, mode="full"):
    response = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "mode": mode,
            "name": f"工作流-{mode}-{uuid4().hex[:8]}",
            "topic": "清溪河谷与臻品文化",
            "product": "臻品雅酿",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def stage(workflow, key):
    return next(item for item in workflow["stages"] if item["key"] == key)


def test_full_workflow_exposes_seven_steps_and_normalizes_legacy_video_stage(
    content_client,
    user_headers,
):
    task = create_task(content_client, user_headers, "full")
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    visible = [item for item in workflow["stages"] if item["applicable"]]
    assert [item["key"] for item in visible] == [
        "configuration",
        "topic",
        "copy",
        "compliance",
        "image",
        "video_preview",
        "export",
    ]
    assert [item["order"] for item in visible] == list(range(1, 8))
    assert stage(workflow, "video_preview")["label"] == "视频生成"
    assert stage(workflow, "storyboard")["applicable"] is False
    assert stage(workflow, "voice_subtitles")["applicable"] is False

    from content_service.db import SessionLocal
    from content_service.models import ContentTask

    with SessionLocal() as session:
        stored = session.get(ContentTask, task["id"])
        stored.current_stage = "voice_subtitles"
        session.commit()
    normalized = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    assert normalized["current_stage"] == "video_preview"
    with SessionLocal() as session:
        assert session.get(ContentTask, task["id"]).current_stage == "video_preview"


@pytest.mark.parametrize("legacy_stage", ["image", "storyboard", "voice_subtitles"])
def test_quick_video_exposes_three_steps_and_preserves_legacy_stage_data(
    content_client, user_headers, legacy_stage
):
    from content_service.db import SessionLocal
    from content_service.models import ContentTask, TaskStage

    video_task = create_task(content_client, user_headers, "video")
    with SessionLocal() as session:
        stored_video_task = session.get(ContentTask, video_task["id"])
        stored_video_task.current_stage = legacy_stage
        legacy = session.scalar(
            select(TaskStage).where(
                TaskStage.task_id == video_task["id"], TaskStage.stage_key == legacy_stage
            )
        )
        legacy.state = "awaiting_confirmation"
        legacy.requirements = "保留旧任务的编辑内容"
        legacy_version = legacy.version
        session.commit()
    video_workflow = content_client.get(
        f"/api/v1/tasks/{video_task['id']}/workflow",
        headers=user_headers,
    ).json()
    assert video_workflow["current_stage"] == "video_preview"
    visible = [item for item in video_workflow["stages"] if item["applicable"]]
    assert [item["key"] for item in visible] == ["configuration", "video_preview", "export"]
    assert [item["label"] for item in visible] == ["任务配置", "视频生成", "确认导出"]
    assert [item["order"] for item in visible] == [1, 2, 3]
    hidden = stage(video_workflow, legacy_stage)
    assert hidden["applicable"] is False
    assert hidden["requirements"] == "保留旧任务的编辑内容"
    assert hidden["version"] == legacy_version
    assert hidden["state"] == "awaiting_confirmation"
    with SessionLocal() as session:
        assert session.get(ContentTask, video_task["id"]).current_stage == "video_preview"


def test_identical_task_patch_does_not_invalidate_confirmed_workflow(
    content_client,
    user_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import AuditLog, TaskStage

    task = create_task(content_client, user_headers, "full")
    confirmed_stages = {
        "topic": {"topic": ["confirmed-topic"]},
        "copy": {"copy": ["confirmed-copy"]},
        "compliance": {"compliance": ["confirmed-compliance"]},
        "image": {"image": ["confirmed-image"]},
    }
    with SessionLocal() as session:
        stages = session.scalars(select(TaskStage).where(TaskStage.task_id == task["id"])).all()
        for current in stages:
            selection = confirmed_stages.get(current.stage_key)
            if selection is None:
                continue
            current.state = "confirmed"
            current.selected_artifact_ids_json = json.dumps(selection)
            current.confirmed_artifact_ids_json = json.dumps(selection)
            current.confirmed_by = user_headers["X-User-Id"]
        session.commit()
        before = {
            current.stage_key: (
                current.state,
                current.version,
                current.selected_artifact_ids_json,
                current.confirmed_artifact_ids_json,
            )
            for current in stages
            if current.stage_key in confirmed_stages
        }
        update_audits_before = len(
            session.scalars(
                select(AuditLog).where(
                    AuditLog.resource_id == task["id"],
                    AuditLog.action == "task.updated",
                )
            ).all()
        )

    response = content_client.patch(
        f"/api/v1/tasks/{task['id']}",
        headers=user_headers,
        json={
            "expected_version": task["version"],
            "name": task["name"],
            "topic": task["topic"],
            "product": task["product"],
            "product_resource_id": task["product_resource_id"],
            "knowledge_resource_ids": task["knowledge_resource_ids"],
            "keywords": task["keywords"],
            "audience": task["audience"],
            "content_type": task["content_type"],
            "style": task["style"],
            "length": task["length"],
            "requirements": task["requirements"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == task["version"]

    with SessionLocal() as session:
        stages = session.scalars(select(TaskStage).where(TaskStage.task_id == task["id"])).all()
        after = {
            current.stage_key: (
                current.state,
                current.version,
                current.selected_artifact_ids_json,
                current.confirmed_artifact_ids_json,
            )
            for current in stages
            if current.stage_key in confirmed_stages
        }
        update_audits_after = len(
            session.scalars(
                select(AuditLog).where(
                    AuditLog.resource_id == task["id"],
                    AuditLog.action == "task.updated",
                )
            ).all()
        )
    assert after == before
    assert update_audits_after == update_audits_before


def test_video_copy_context_uses_current_selection_without_manual_confirmation(
    content_client,
    user_headers,
):
    from content_service import main as content_main
    from content_service.models import Artifact, ContentTask, TaskStage

    task = create_task(content_client, user_headers, "full")
    generated = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-copy",
        headers=user_headers,
        json={"count": 1},
    )
    assert generated.status_code == 200, generated.text
    draft = generated.json()
    edited_content = dict(draft["content"])
    edited_content["candidates"] = [
        {
            "title": "用于视频的标题",
            "summary": "SUMMARY-MUST-NOT-BE-SENT",
            "body": "用于视频的正式文案正文",
        }
    ]
    edited_content["selected_candidate_index"] = 0
    revised = content_client.patch(
        f"/api/v1/artifacts/{draft['id']}",
        headers=user_headers,
        json={"expected_version": draft["version"], "content": edited_content},
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["status"] == "confirmed"
    loaded_task = content_client.get(
        f"/api/v1/tasks/{task['id']}",
        headers=user_headers,
    ).json()

    with content_main.SessionLocal() as session:
        stored_task = session.get(ContentTask, task["id"])
        copy_stage = session.scalar(
            select(TaskStage).where(
                TaskStage.task_id == task["id"],
                TaskStage.stage_key == "copy",
            )
        )
        stored_copy = session.get(Artifact, revised.json()["id"])
        # Simulate a task saved before copy confirmation was removed: the
        # authoritative selection exists, but the old UI never confirmed it.
        copy_stage.state = "awaiting_confirmation"
        copy_stage.confirmed_artifact_ids_json = "{}"
        stored_copy.status = "draft"
        task_version_before = stored_task.version
        stage_version_before = copy_stage.version
        artifact, context = content_main.confirmed_copy_context_for_video(
            session,
            stored_task,
            None,
            user_id=user_headers["X-User-Id"],
        )
        assert artifact.id == revised.json()["id"]
        assert copy_stage.state == "confirmed"
        assert json.loads(copy_stage.confirmed_artifact_ids_json) == {
            "copy": [revised.json()["id"]]
        }
        assert stored_copy.status == "confirmed"
        assert "用于视频的标题" in context
        assert "用于视频的正式文案正文" in context
        assert "SUMMARY-MUST-NOT-BE-SENT" not in context
        assert stored_task.version == task_version_before == loaded_task["version"]
        assert copy_stage.version == stage_version_before + 1
        with pytest.raises(HTTPException) as rejected:
            content_main.confirmed_copy_context_for_video(
                session,
                stored_task,
                str(uuid4()),
            )
        assert rejected.value.status_code == 409
        session.commit()

    stale_stage_edit = content_client.put(
        f"/api/v1/tasks/{task['id']}/stages/copy/selection",
        headers=user_headers,
        json={
            "expected_version": stage_version_before,
            "artifact_ids": [revised.json()["id"]],
        },
    )
    assert stale_stage_edit.status_code == 409, stale_stage_edit.text

    edited_task = content_client.patch(
        f"/api/v1/tasks/{task['id']}",
        headers=user_headers,
        json={
            "expected_version": loaded_task["version"],
            "audience": "版本迁移后仍可继续编辑",
        },
    )
    assert edited_task.status_code == 200, edited_task.text


def test_current_copy_survives_replacement_generation_progress_and_failure(
    content_client,
    user_headers,
):
    from content_service import main as content_main
    from content_service.models import ContentTask, TaskStage
    from content_service.workflow import begin_generation_run, fail_generation_run

    task = create_task(content_client, user_headers, "full")
    generated = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-copy",
        headers=user_headers,
        json={"count": 1},
    )
    assert generated.status_code == 200, generated.text
    current_id = generated.json()["id"]

    with content_main.SessionLocal() as session:
        stored_task = session.get(ContentTask, task["id"])
        copy_stage = session.scalar(
            select(TaskStage).where(
                TaskStage.task_id == task["id"],
                TaskStage.stage_key == "copy",
            )
        )
        run, replayed = begin_generation_run(
            session,
            stored_task,
            "copy",
            count=1,
            instruction="生成替代文案",
            parameters={},
            idempotency_key=None,
            user_id=user_headers["X-User-Id"],
        )
        assert replayed is False
        assert copy_stage.state == "in_progress"
        assert (
            content_main.current_copy_artifact_for_downstream(
                session,
                stored_task,
                user_id=user_headers["X-User-Id"],
            ).id
            == current_id
        )

        fail_generation_run(session, stored_task, run, "替代文案生成失败")
        assert copy_stage.state == "failed"
        assert (
            content_main.current_copy_artifact_for_downstream(
                session,
                stored_task,
                user_id=user_headers["X-User-Id"],
            ).id
            == current_id
        )
        session.rollback()

    empty_task = create_task(content_client, user_headers, "full")
    with content_main.SessionLocal() as session:
        stored_empty_task = session.get(ContentTask, empty_task["id"])
        run, _ = begin_generation_run(
            session,
            stored_empty_task,
            "copy",
            count=1,
            instruction="首次生成",
            parameters={},
            idempotency_key=None,
            user_id=user_headers["X-User-Id"],
        )
        assert (
            content_main.current_copy_artifact_for_downstream(
                session,
                stored_empty_task,
                user_id=user_headers["X-User-Id"],
            )
            is None
        )
        fail_generation_run(session, stored_empty_task, run, "首次生成失败")
        assert (
            content_main.current_copy_artifact_for_downstream(
                session,
                stored_empty_task,
                user_id=user_headers["X-User-Id"],
            )
            is None
        )
        session.rollback()


def test_compliance_automatically_uses_only_the_current_copy(
    content_client,
    user_headers,
):
    task = create_task(content_client, user_headers, "full")
    previous = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-copy",
        headers=user_headers,
        json={"count": 1},
    )
    assert previous.status_code == 200, previous.text
    current = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-copy",
        headers=user_headers,
        json={"count": 1},
    )
    assert current.status_code == 200, current.text

    outdated = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/compliance/generate",
        headers=user_headers,
        json={"source_artifact_id": previous.json()["id"]},
    )
    assert outdated.status_code == 409, outdated.text
    assert "不是当前版本" in outdated.json()["detail"]

    checked = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/compliance/generate",
        headers=user_headers,
        json={},
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["parameters"]["source_artifact_id"] == current.json()["id"]
    assert checked.json()["artifacts"][0]["content"]["source_artifact_id"] == (current.json()["id"])


def test_ai_video_uses_current_copy_without_confirmation_and_retry_keeps_provenance(
    content_client,
    user_headers,
    monkeypatch,
):
    from content_service import main as content_main
    from content_service.models import ContentTask, Job
    from content_service.provider_dependencies import get_ai_video_generation_port
    from content_service.providers import AiVideoTask, ProviderStatus

    class QueuedAiVideoProvider:
        capabilities = {
            "async": True,
            "text_to_video": True,
            "image_to_video": True,
            "reference_images": {"max": 9},
            "reference_videos": {"max": 3, "total_duration_seconds": 15},
            "durations": [5],
            "ratios": ["16:9"],
            "resolutions": ["720p"],
        }

        def __init__(self):
            self.status = ProviderStatus(
                provider_id="copy-retry-provider",
                name="copy-retry-provider",
                capability="ai-video-generation",
                mode="test",
                external=False,
                enabled=True,
                model="test-model",
            )
            self.requests = []

        def submit(self, request):
            self.requests.append(request)
            return AiVideoTask(
                provider_task_id=uuid4().hex,
                status="queued",
                provider_id=self.status.provider_id,
                model=self.status.model,
                progress=0,
            )

        def get(self, provider_task_id):
            raise AssertionError("本测试不应启动后台轮询")

        def cancel(self, provider_task_id):
            return AiVideoTask(
                provider_task_id=provider_task_id,
                status="cancelled",
                provider_id=self.status.provider_id,
                model=self.status.model,
            )

    provider = QueuedAiVideoProvider()
    content_main.app.dependency_overrides[get_ai_video_generation_port] = lambda: provider
    monkeypatch.setattr(content_main.dispatcher, "submit", lambda callback: None)
    try:
        no_copy_task = create_task(content_client, user_headers, "full")
        with content_main.SessionLocal() as session:
            stored_no_copy_task = session.get(ContentTask, no_copy_task["id"])
            stored_no_copy_task.audience = "品牌用户"
            session.commit()
        no_copy_project = content_client.post(
            f"/api/v1/tasks/{no_copy_task['id']}/video-project",
            headers=user_headers,
        ).json()
        no_copy_job = content_client.post(
            f"/api/v1/video-projects/{no_copy_project['id']}/generate-video",
            headers=user_headers,
            json={
                "expected_revision": no_copy_project["current_revision"],
                "prompt": "没有当前文案也可生成视频",
            },
        )
        assert no_copy_job.status_code == 200, no_copy_job.text
        assert no_copy_job.json()["result"]["request"]["source_copy_artifact_id"] is None
        with content_main.SessionLocal() as session:
            session.get(Job, no_copy_job.json()["id"]).status = "cancelled"
            session.commit()

        task = create_task(content_client, user_headers, "full")
        with content_main.SessionLocal() as session:
            stored_task = session.get(ContentTask, task["id"])
            stored_task.audience = "品牌用户"
            session.commit()
        first_copy = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-copy",
            headers=user_headers,
            json={"count": 1},
        )
        assert first_copy.status_code == 200, first_copy.text
        first_copy_id = first_copy.json()["id"]
        project = content_client.post(
            f"/api/v1/tasks/{task['id']}/video-project",
            headers=user_headers,
        ).json()
        current_copy = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-copy",
            headers=user_headers,
            json={"count": 1},
        )
        assert current_copy.status_code == 200, current_copy.text
        copy_id = current_copy.json()["id"]

        outdated = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-video",
            headers=user_headers,
            json={
                "expected_revision": project["current_revision"],
                "prompt": "旧缓存不得覆盖当前文案",
                "source_copy_artifact_id": first_copy_id,
            },
        )
        assert outdated.status_code == 409, outdated.text
        assert "不是当前版本" in outdated.json()["detail"]
        assert len(provider.requests) == 1

        queued = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-video",
            headers=user_headers,
            json={
                "expected_revision": project["current_revision"],
                "prompt": "生成品牌视频",
            },
        )
        assert queued.status_code == 200, queued.text
        source_job_id = queued.json()["id"]
        assert queued.json()["result"]["request"]["source_copy_artifact_id"] == copy_id
        successor = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-copy",
            headers=user_headers,
            json={"count": 1},
        )
        assert successor.status_code == 200, successor.text
        assert successor.json()["id"] != copy_id
        with content_main.SessionLocal() as session:
            source_job = session.get(Job, source_job_id)
            source_job.status = "failed"
            source_job.error = "test failure"
            session.commit()

        retried = content_client.post(
            f"/api/v1/jobs/{source_job_id}/retry",
            headers=user_headers,
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["id"] != source_job_id
        assert retried.json()["result"]["request"]["source_copy_artifact_id"] == copy_id
        assert len(provider.requests) == 3
        with content_main.SessionLocal() as session:
            retry_job = session.get(Job, retried.json()["id"])
            retry_job.status = "cancelled"
            session.commit()
    finally:
        content_main.app.dependency_overrides.pop(
            get_ai_video_generation_port,
            None,
        )


def test_current_copy_never_guesses_from_stale_or_invalid_history(
    content_client,
    user_headers,
):
    from content_service import main as content_main
    from content_service.models import Artifact, ContentTask, TaskStage

    task = create_task(content_client, user_headers, "full")
    generated = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/copy/generate",
        headers=user_headers,
        json={"count": 2},
    )
    assert generated.status_code == 200, generated.text
    copy_ids = [item["id"] for item in generated.json()["artifacts"]]
    foreign_task = create_task(content_client, user_headers, "full")
    foreign_copy = content_client.post(
        f"/api/v1/tasks/{foreign_task['id']}/generate-copy",
        headers=user_headers,
        json={"count": 1},
    )
    assert foreign_copy.status_code == 200, foreign_copy.text

    latest_task = content_client.get(
        f"/api/v1/tasks/{task['id']}",
        headers=user_headers,
    ).json()
    changed = content_client.patch(
        f"/api/v1/tasks/{task['id']}",
        headers=user_headers,
        json={
            "expected_version": latest_task["version"],
            "requirements": "配置已改变，旧文案不能自动复活",
        },
    )
    assert changed.status_code == 200, changed.text

    with content_main.SessionLocal() as session:
        stored_task = session.get(ContentTask, task["id"])
        copy_stage = session.scalar(
            select(TaskStage).where(
                TaskStage.task_id == task["id"],
                TaskStage.stage_key == "copy",
            )
        )
        assert copy_stage.state == "stale"
        assert json.loads(copy_stage.selected_artifact_ids_json) == {}
        assert (
            content_main.current_copy_artifact_for_downstream(
                session,
                stored_task,
                user_id=user_headers["X-User-Id"],
            )
            is None
        )

        # Even if a legacy row retained IDs, non-ready states and ambiguous
        # selections never pick the first or latest historical artifact.
        copy_stage.selected_artifact_ids_json = json.dumps({"copy": [copy_ids[0]]})
        assert (
            content_main.current_copy_artifact_for_downstream(
                session,
                stored_task,
                user_id=user_headers["X-User-Id"],
            )
            is None
        )
        with pytest.raises(HTTPException):
            content_main.current_copy_artifact_for_downstream(
                session,
                stored_task,
                requested_artifact_id=copy_ids[0],
                user_id=user_headers["X-User-Id"],
            )

        for invalid_state in ("not_started", "skipped"):
            copy_stage.state = invalid_state
            assert (
                content_main.current_copy_artifact_for_downstream(
                    session,
                    stored_task,
                    user_id=user_headers["X-User-Id"],
                )
                is None
            )
            with pytest.raises(HTTPException):
                content_main.current_copy_artifact_for_downstream(
                    session,
                    stored_task,
                    requested_artifact_id=copy_ids[0],
                    user_id=user_headers["X-User-Id"],
                )

        copy_stage.state = "confirmed"
        copy_stage.selected_artifact_ids_json = json.dumps({"copy": copy_ids})
        assert (
            content_main.current_copy_artifact_for_downstream(
                session,
                stored_task,
                user_id=user_headers["X-User-Id"],
            )
            is None
        )

        copy_stage.selected_artifact_ids_json = json.dumps({"copy": [str(uuid4())]})
        assert (
            content_main.current_copy_artifact_for_downstream(
                session,
                stored_task,
                user_id=user_headers["X-User-Id"],
            )
            is None
        )

        selected_copy = session.get(Artifact, copy_ids[0])
        copy_stage.selected_artifact_ids_json = json.dumps({"copy": [selected_copy.id]})
        for invalid_status in ("discarded", "stale", "legacy_mock"):
            selected_copy.status = invalid_status
            assert (
                content_main.current_copy_artifact_for_downstream(
                    session,
                    stored_task,
                    user_id=user_headers["X-User-Id"],
                )
                is None
            )
            with pytest.raises(HTTPException):
                content_main.current_copy_artifact_for_downstream(
                    session,
                    stored_task,
                    requested_artifact_id=selected_copy.id,
                    user_id=user_headers["X-User-Id"],
                )
        selected_copy.status = "confirmed"
        copy_stage.selected_artifact_ids_json = json.dumps({"copy": [foreign_copy.json()["id"]]})
        assert (
            content_main.current_copy_artifact_for_downstream(
                session,
                stored_task,
                user_id=user_headers["X-User-Id"],
            )
            is None
        )
        with pytest.raises(HTTPException):
            content_main.current_copy_artifact_for_downstream(
                session,
                stored_task,
                requested_artifact_id=foreign_copy.json()["id"],
                user_id=user_headers["X-User-Id"],
            )
        session.rollback()


def test_copy_mode_generates_editable_titles_before_copy(content_client, user_headers):
    task = create_task(content_client, user_headers, "copy")
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    assert workflow["current_stage"] == "topic"
    assert stage(workflow, "topic")["applicable"] is True
    assert stage(workflow, "topic")["label"] == "智能标题"

    generated_titles = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/topic/generate",
        headers=user_headers,
        json={"count": 3},
    )
    assert generated_titles.status_code == 200, generated_titles.text
    title_artifact = generated_titles.json()["artifacts"][0]
    title_candidate = title_artifact["content"]["candidates"][0]
    assert title_candidate["title"]
    assert "summary" not in title_candidate

    transformed = content_client.post(
        f"/api/v1/artifacts/{title_artifact['id']}/transform-copy",
        headers=user_headers,
        json={"operation": "rewrite", "instruction": "标题更简洁", "preview": True},
    )
    assert transformed.status_code == 200, transformed.text
    transformed_title = transformed.json()["content"]["candidates"][0]["title"]
    assert transformed_title
    assert transformed_title != title_candidate["title"]

    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    confirmed = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/topic/confirm",
        headers=user_headers,
        json={
            "expected_version": stage(workflow, "topic")["version"],
            "artifact_ids": [title_artifact["id"]],
        },
    )
    assert confirmed.status_code == 200, confirmed.text

    generated_copy = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/copy/generate",
        headers=user_headers,
        json={"count": 1},
    )
    assert generated_copy.status_code == 200, generated_copy.text
    copy_artifact = generated_copy.json()["artifacts"][0]
    copy_candidate = copy_artifact["content"]["candidates"][0]
    assert title_candidate["title"] in copy_candidate["title"]

    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    copy_stage = stage(workflow, "copy")
    assert copy_stage["state"] == "confirmed"
    copy_id = copy_stage["selected_artifact_ids"]["copy"][0]
    checked = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/compliance/generate",
        headers=user_headers,
        json={"count": 1, "source_artifact_id": copy_id},
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["parameters"]["source_artifact_id"] == copy_id


def test_full_copy_is_pinned_to_confirmed_title_after_title_change(
    content_client,
    user_headers,
):
    from content_service import main as content_main
    from content_service.provider_dependencies import get_text_provider
    from content_service.providers import (
        MockTextProvider,
        TextGenerationResult,
        selected_text_candidate,
    )

    class RecordingCopyProvider(MockTextProvider):
        def __init__(self):
            self.copy_requests = []
            self.transform_requests = []

        def copies(self, request, count):
            self.copy_requests.append(request)
            return TextGenerationResult(
                [
                    {
                        "title": f"模型另拟标题 {index + 1}",
                        "summary": "模型摘要",
                        "body": f"模型正文 {len(self.copy_requests)}-{index + 1}",
                        "source_ids": [],
                    }
                    for index in range(count)
                ],
                self.status.provider_id,
                self.status.model or "",
            )

        def transform_copy(self, request, operation):
            self.transform_requests.append(request)
            source = selected_text_candidate(request.source_content)
            return TextGenerationResult(
                {
                    **source,
                    "title": "模型试图修改标题",
                    "body": f"{source.get('body', '')}（新的正文）",
                },
                self.status.provider_id,
                self.status.model or "",
            )

    def confirm_title(task_id: str, source: dict, title: str) -> dict:
        content = dict(source["content"])
        candidates = [dict(item) for item in content["candidates"]]
        candidates[0]["title"] = title
        content["candidates"] = candidates
        content["selected_candidate_index"] = 0
        revised = content_client.patch(
            f"/api/v1/artifacts/{source['id']}",
            headers=user_headers,
            json={"expected_version": source["version"], "content": content},
        )
        assert revised.status_code == 200, revised.text
        confirmed = content_client.post(
            f"/api/v1/artifacts/{revised.json()['id']}/confirm",
            headers=user_headers,
        )
        assert confirmed.status_code == 200, confirmed.text
        return revised.json()

    task = create_task(content_client, user_headers, "full")
    generated_title = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/topic/generate",
        headers=user_headers,
        json={"count": 1},
    ).json()["artifacts"][0]
    title_a = confirm_title(task["id"], generated_title, "完整流程唯一标题 A")

    provider = RecordingCopyProvider()
    content_main.app.dependency_overrides[get_text_provider] = lambda: provider
    try:
        first = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-copy",
            headers=user_headers,
            json={
                "count": 1,
                "instruction": "生成第一版正文",
                "idempotency_key": "confirmed-title-copy",
            },
        )
        assert first.status_code == 200, first.text
        first_artifact = first.json()
        first_content = first_artifact["content"]
        assert first_content["candidates"][0]["title"] == "完整流程唯一标题 A"
        assert first_content["source_title_artifact_id"] == title_a["id"]
        assert first_content["source_title_version"] == title_a["version"]
        assert first_artifact["parent_id"] == title_a["id"]
        assert (
            selected_text_candidate(provider.copy_requests[0].source_content)["title"]
            == "完整流程唯一标题 A"
        )

        transformed = content_client.post(
            f"/api/v1/artifacts/{first_artifact['id']}/transform-copy",
            headers=user_headers,
            json={
                "operation": "rewrite",
                "instruction": "正文更温暖，同时修改标题",
                "idempotency_key": "confirmed-title-transform",
            },
        )
        assert transformed.status_code == 200, transformed.text
        transformed_artifact = transformed.json()
        assert transformed_artifact["content"]["candidates"][0]["title"] == "完整流程唯一标题 A"
        assert transformed_artifact["content"]["source_title_artifact_id"] == title_a["id"]
        assert transformed_artifact["parent_id"] == first_artifact["id"]
        workflow = content_client.get(
            f"/api/v1/tasks/{task['id']}/workflow",
            headers=user_headers,
        ).json()
        transformed_copy_stage = stage(workflow, "copy")
        assert transformed_copy_stage["state"] == "confirmed"
        assert transformed_copy_stage["selected_artifact_ids"] == {
            "copy": [transformed_artifact["id"]]
        }
        assert transformed_copy_stage["confirmed_artifact_ids"] == {
            "copy": [transformed_artifact["id"]]
        }

        title_b = confirm_title(task["id"], title_a, "完整流程唯一标题 B")
        workflow = content_client.get(
            f"/api/v1/tasks/{task['id']}/workflow",
            headers=user_headers,
        ).json()
        assert stage(workflow, "copy")["state"] == "stale"
        assert stage(workflow, "copy")["selected_artifact_ids"] == {}
        assert stage(workflow, "compliance")["selected_artifact_ids"] == {}
        assert stage(workflow, "image")["selected_artifact_ids"] == {}
        assert stage(workflow, "video_preview")["selected_artifact_ids"] == {}

        reminted_transform = content_client.post(
            f"/api/v1/artifacts/{transformed_artifact['id']}/transform-copy",
            headers=user_headers,
            json={"operation": "rewrite", "instruction": "继续修改旧文案"},
        )
        assert reminted_transform.status_code == 200, reminted_transform.text
        assert reminted_transform.json()["content"]["candidates"][0]["title"] == (
            "完整流程唯一标题 B"
        )
        assert reminted_transform.json()["content"]["source_title_artifact_id"] == title_b["id"]

        stale_replay = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-copy",
            headers=user_headers,
            json={
                "count": 1,
                "instruction": "生成第一版正文",
                "idempotency_key": "confirmed-title-copy",
            },
        )
        assert stale_replay.status_code == 409, stale_replay.text
        assert "幂等键" in stale_replay.json()["detail"]

        regenerated = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-copy",
            headers=user_headers,
            json={"count": 1, "idempotency_key": "confirmed-title-copy-b"},
        )
        assert regenerated.status_code == 200, regenerated.text
        regenerated_artifact = regenerated.json()
        assert regenerated_artifact["id"] != first_artifact["id"]
        assert regenerated_artifact["version"] > first_artifact["version"]
        assert regenerated_artifact["content"]["candidates"][0]["title"] == ("完整流程唯一标题 B")
        assert regenerated_artifact["content"]["source_title_artifact_id"] == title_b["id"]
        assert regenerated_artifact["parent_id"] == title_b["id"]
        assert (
            selected_text_candidate(provider.copy_requests[-1].source_content)["title"]
            == "完整流程唯一标题 B"
        )

        standalone = create_task(content_client, user_headers, "copy")
        standalone_workflow = content_client.get(
            f"/api/v1/tasks/{standalone['id']}/workflow",
            headers=user_headers,
        ).json()
        skipped = content_client.post(
            f"/api/v1/tasks/{standalone['id']}/stages/topic/skip",
            headers=user_headers,
            json={
                "expected_version": stage(standalone_workflow, "topic")["version"],
                "reason": "单独文案入口没有独立标题",
            },
        )
        assert skipped.status_code == 200, skipped.text
        standalone_copy = content_client.post(
            f"/api/v1/tasks/{standalone['id']}/generate-copy",
            headers=user_headers,
            json={"count": 1},
        )
        assert standalone_copy.status_code == 200, standalone_copy.text
        assert standalone_copy.json()["content"]["candidates"][0]["title"].startswith(
            "模型另拟标题"
        )
    finally:
        content_main.app.dependency_overrides.pop(get_text_provider, None)


def test_legacy_copy_task_reactivates_smart_title_stage(content_client, user_headers):
    task = create_task(content_client, user_headers, "copy")

    from content_service.db import SessionLocal
    from content_service.models import ContentTask, TaskStage

    with SessionLocal() as session:
        stored_task = session.get(ContentTask, task["id"])
        topic_stage = session.scalar(
            select(TaskStage).where(
                TaskStage.task_id == task["id"],
                TaskStage.stage_key == "topic",
            )
        )
        assert stored_task is not None
        assert topic_stage is not None
        stored_task.current_stage = "copy"
        topic_stage.state = "skipped"
        topic_stage.skipped_reason = "当前入口不适用"
        session.commit()

    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    assert workflow["current_stage"] == "topic"
    assert stage(workflow, "topic")["applicable"] is True
    assert stage(workflow, "topic")["state"] == "not_started"
    assert stage(workflow, "topic")["skipped_reason"] is None


def test_workflow_candidates_confirmation_stale_and_optimistic_lock(
    content_client,
    user_headers,
):
    task = create_task(content_client, user_headers)
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    assert [item["key"] for item in workflow["stages"]] == [
        "configuration",
        "topic",
        "copy",
        "compliance",
        "image",
        "storyboard",
        "voice_subtitles",
        "video_preview",
        "export",
    ]
    topic = stage(workflow, "topic")
    skipped = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/topic/skip",
        headers=user_headers,
        json={"expected_version": topic["version"], "reason": "已有明确主题"},
    )
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["state"] == "skipped"

    generated = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/copy/generate",
        headers=user_headers,
        json={"count": 2, "idempotency_key": f"copy-{uuid4().hex}"},
    )
    assert generated.status_code == 200, generated.text
    run = generated.json()
    assert run["status"] == "succeeded"
    assert len(run["artifacts"]) == 2
    assert len({item["id"] for item in run["artifacts"]}) == 2
    assert all(len(item["content"]["candidates"]) == 1 for item in run["artifacts"])

    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    copy_stage = stage(workflow, "copy")
    second_id = run["artifacts"][1]["id"]
    ambiguous = content_client.put(
        f"/api/v1/tasks/{task['id']}/stages/copy/selection",
        headers=user_headers,
        json={
            "expected_version": copy_stage["version"],
            "artifact_ids": [run["artifacts"][0]["id"], second_id],
        },
    )
    assert ambiguous.status_code == 422, ambiguous.text
    assert "只能选择一个" in ambiguous.json()["detail"]
    selected = content_client.put(
        f"/api/v1/tasks/{task['id']}/stages/copy/selection",
        headers=user_headers,
        json={"expected_version": copy_stage["version"], "artifact_ids": [second_id]},
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["selected_artifact_ids"] == {"copy": [second_id]}
    assert selected.json()["confirmed_artifact_ids"] == {"copy": [second_id]}
    assert selected.json()["state"] == "confirmed"
    conflict = content_client.put(
        f"/api/v1/tasks/{task['id']}/stages/copy/selection",
        headers=user_headers,
        json={"expected_version": copy_stage["version"], "artifact_ids": [second_id]},
    )
    assert conflict.status_code == 409
    storyboard_run = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/storyboard/generate",
        headers=user_headers,
        json={"count": 2},
    )
    assert storyboard_run.status_code == 200, storyboard_run.text
    storyboard_id = storyboard_run.json()["artifact_ids"][0]
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    storyboard_stage = stage(workflow, "storyboard")
    confirmed_storyboard = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/storyboard/confirm",
        headers=user_headers,
        json={
            "expected_version": storyboard_stage["version"],
            "artifact_ids": [storyboard_id],
        },
    )
    assert confirmed_storyboard.status_code == 200, confirmed_storyboard.text

    voice_run = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/voice_subtitles/generate",
        headers=user_headers,
        json={"parameters": {"operation": "voice"}},
    )
    assert voice_run.status_code == 200, voice_run.text
    voice = voice_run.json()["artifacts"][0]
    storyboard = storyboard_run.json()["artifacts"][0]
    expected_narration = "\n".join(shot["narration"] for shot in storyboard["content"]["shots"])
    assert voice["content"]["text"] == expected_narration
    assert voice["content"]["source_artifact_id"] == storyboard_id
    assert voice["content"]["source_kind"] == "storyboard"

    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    copy_stage = stage(workflow, "copy")
    first_id = run["artifacts"][0]["id"]
    switched = content_client.put(
        f"/api/v1/tasks/{task['id']}/stages/copy/selection",
        headers=user_headers,
        json={"expected_version": copy_stage["version"], "artifact_ids": [first_id]},
    )
    assert switched.status_code == 200, switched.text
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    assert stage(workflow, "storyboard")["state"] == "stale"
    assert (
        content_client.get(f"/api/v1/tasks/{task['id']}/export", headers=user_headers).status_code
        == 409
    )


def test_confirmed_independent_artifact_links_into_full_task_without_copying(
    content_client,
    user_headers,
):
    source = create_task(content_client, user_headers, "copy")
    generated = content_client.post(
        f"/api/v1/tasks/{source['id']}/generate-copy",
        headers=user_headers,
        json={"count": 1},
    ).json()
    artifact_id = generated["id"]
    confirmed = content_client.post(
        f"/api/v1/artifacts/{artifact_id}/confirm", headers=user_headers
    )
    assert confirmed.status_code == 200, confirmed.text

    target = create_task(content_client, user_headers, "full")
    linked = content_client.post(
        f"/api/v1/tasks/{target['id']}/artifact-links",
        headers=user_headers,
        json={
            "source_task_id": source["id"],
            "artifact_ids": [artifact_id],
            "expected_version": target["version"],
        },
    )
    assert linked.status_code == 200, linked.text
    payload = linked.json()
    assert payload["links"][0]["artifact_id"] == artifact_id
    target_copy = stage(payload["workflow"], "copy")
    assert target_copy["selected_artifact_ids"] == {"copy": [artifact_id]}
    target_artifacts = content_client.get(
        f"/api/v1/tasks/{target['id']}/artifacts",
        headers=user_headers,
    ).json()
    linked_artifact = next(item for item in target_artifacts if item["id"] == artifact_id)
    assert linked_artifact["linked"] is True
    assert linked_artifact["source_task_id"] == source["id"]

    target_confirmed = content_client.post(
        f"/api/v1/tasks/{target['id']}/stages/copy/confirm",
        headers=user_headers,
        json={
            "expected_version": target_copy["version"],
            "artifact_ids": [artifact_id],
        },
    )
    assert target_confirmed.status_code == 200, target_confirmed.text
    source_after = content_client.get(
        f"/api/v1/tasks/{source['id']}/artifacts", headers=user_headers
    ).json()
    assert next(item for item in source_after if item["id"] == artifact_id)["status"] == "confirmed"

    compliance = content_client.post(
        f"/api/v1/tasks/{target['id']}/stages/compliance/generate",
        headers=user_headers,
        json={"source_artifact_id": artifact_id},
    )
    assert compliance.status_code == 200, compliance.text
    assert compliance.json()["artifacts"][0]["content"]["source_artifact_id"] == artifact_id

    exported = content_client.get(f"/api/v1/tasks/{target['id']}/export", headers=user_headers)
    assert exported.status_code == 409, exported.text
    assert "确认导出" in exported.json()["detail"]

    transformed = content_client.post(
        f"/api/v1/artifacts/{artifact_id}/transform-copy",
        headers=user_headers,
        json={
            "operation": "rewrite",
            "instruction": "改为更口语化的表达",
            "target_task_id": target["id"],
        },
    )
    assert transformed.status_code == 200, transformed.text
    assert transformed.json()["task_id"] == target["id"]
    assert transformed.json()["parent_id"] == artifact_id


def test_linking_multiple_copy_versions_cannot_create_an_ambiguous_current(
    content_client,
    user_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import Artifact

    source = create_task(content_client, user_headers, "copy")
    generated = content_client.post(
        f"/api/v1/tasks/{source['id']}/stages/copy/generate",
        headers=user_headers,
        json={"count": 2},
    )
    assert generated.status_code == 200, generated.text
    artifact_ids = generated.json()["artifact_ids"]
    with SessionLocal() as session:
        for artifact_id in artifact_ids:
            session.get(Artifact, artifact_id).status = "confirmed"
        session.commit()

    target = create_task(content_client, user_headers, "full")
    rejected = content_client.post(
        f"/api/v1/tasks/{target['id']}/artifact-links",
        headers=user_headers,
        json={
            "source_task_id": source["id"],
            "artifact_ids": artifact_ids,
            "expected_version": target["version"],
        },
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"] == "文案阶段只能选择一个当前版本"

    workflow = content_client.get(
        f"/api/v1/tasks/{target['id']}/workflow",
        headers=user_headers,
    ).json()
    assert stage(workflow, "copy")["selected_artifact_ids"] == {}


def test_source_reselection_preserves_artifact_required_by_linked_target(
    content_client,
    user_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import ContentTask
    from content_service.workflow import confirmed_selected_artifacts

    source = create_task(content_client, user_headers, "copy")
    original = content_client.post(
        f"/api/v1/tasks/{source['id']}/generate-copy",
        headers=user_headers,
        json={"count": 1},
    ).json()
    source_confirmed = content_client.post(
        f"/api/v1/artifacts/{original['id']}/confirm",
        headers=user_headers,
    )
    assert source_confirmed.status_code == 200, source_confirmed.text

    target = create_task(content_client, user_headers, "full")
    linked = content_client.post(
        f"/api/v1/tasks/{target['id']}/artifact-links",
        headers=user_headers,
        json={
            "source_task_id": source["id"],
            "artifact_ids": [original["id"]],
            "expected_version": target["version"],
        },
    )
    assert linked.status_code == 200, linked.text
    target_copy_stage = stage(linked.json()["workflow"], "copy")

    successor = content_client.post(
        f"/api/v1/tasks/{source['id']}/generate-copy",
        headers=user_headers,
        json={"count": 1},
    )
    assert successor.status_code == 200, successor.text
    source_artifacts = content_client.get(
        f"/api/v1/tasks/{source['id']}/artifacts",
        headers=user_headers,
    ).json()
    assert (
        next(item for item in source_artifacts if item["id"] == original["id"])["status"]
        == "confirmed"
    )

    target_confirmed = content_client.post(
        f"/api/v1/tasks/{target['id']}/stages/copy/confirm",
        headers=user_headers,
        json={
            "expected_version": target_copy_stage["version"],
            "artifact_ids": [original["id"]],
        },
    )
    assert target_confirmed.status_code == 200, target_confirmed.text

    successor_confirmed = content_client.post(
        f"/api/v1/artifacts/{successor.json()['id']}/confirm",
        headers=user_headers,
    )
    assert successor_confirmed.status_code == 200, successor_confirmed.text
    source_artifacts = content_client.get(
        f"/api/v1/tasks/{source['id']}/artifacts",
        headers=user_headers,
    ).json()
    assert (
        next(item for item in source_artifacts if item["id"] == original["id"])["status"]
        == "confirmed"
    )

    with SessionLocal() as session:
        stored_target = session.get(ContentTask, target["id"])
        assert stored_target is not None
        confirmed_ids = {
            artifact.id for artifact in confirmed_selected_artifacts(session, stored_target)
        }
    assert original["id"] in confirmed_ids
    refused = content_client.delete(
        f"/api/v1/artifacts/{original['id']}",
        headers=user_headers,
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == "当前已确认的内容版本不能删除"


def test_unified_resource_library_keeps_dynamic_categories_and_chunks(
    content_client,
    user_headers,
):
    category = f"动态分类-{uuid4().hex[:8]}"
    created = content_client.post(
        "/api/v1/library/items",
        headers=user_headers,
        json={
            "resource_kind": "spec",
            "category": category,
            "name": "品牌表达规范",
            "text_content": f"{category}：准确、克制、可核验。",
            "tags": ["品牌", "审核"],
            "source_url": "https://example.com/spec",
            "facts": ["对外表达需要人工确认"],
            "notes": "首版",
        },
    )
    assert created.status_code == 200, created.text
    created_payload = created.json()
    categories = content_client.get("/api/v1/library/categories", headers=user_headers).json()
    assert category in {item["id"] for item in categories}

    from content_service.db import SessionLocal
    from content_service.models import ResourceChunk, ResourceEntry, ResourceVersion

    with SessionLocal() as session:
        entry = session.get(ResourceEntry, created_payload["resource_entry_id"])
        version = session.get(ResourceVersion, created_payload["resource_version_id"])
        chunks = session.scalars(
            select(ResourceChunk).where(ResourceChunk.version_id == version.id)
        ).all()
        assert entry.category == category
        assert json.loads(entry.tags_json) == ["品牌", "审核"]
        assert version.source_url == "https://example.com/spec"
        assert chunks[0].text == f"{category}：准确、克制、可核验。"

    generated_task = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={"mode": "copy", "name": "动态分类检索", "topic": category},
    ).json()
    generated_copy = content_client.post(
        f"/api/v1/tasks/{generated_task['id']}/generate-copy",
        headers=user_headers,
        json={"count": 1},
    )
    assert generated_copy.status_code == 200, generated_copy.text
    assert any(
        item.get("resource_entry_id") == created_payload["resource_entry_id"]
        for item in generated_copy.json()["content"]["references"]
    )

    uploaded = content_client.post(
        "/api/v1/library/items/upload",
        headers=user_headers,
        params={
            "resource_kind": "document",
            "category": category,
            "name": "活动资料",
        },
        files={"file": ("activity.txt", "活动时间以审核版本为准。".encode(), "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    replacement = content_client.post(
        f"/api/v1/library/items/{uploaded.json()['id']}/versions/upload",
        headers=user_headers,
        params={
            "expected_version": uploaded.json()["version"],
            "resource_kind": "document",
            "category": category,
            "name": "活动资料（复核版）",
            "tags": "活动,复核",
            "facts": json.dumps(
                [{"statement": "时间以复核版为准", "verified": True}],
                ensure_ascii=False,
            ),
            "notes": "替换原文件",
        },
        files={
            "file": (
                "activity-v2.txt",
                "复核后的活动时间以正式发布为准。".encode(),
                "text/plain",
            )
        },
    )
    assert replacement.status_code == 200, replacement.text
    assert replacement.json()["version"] == 2
    assert replacement.json()["tags"] == ["活动", "复核"]
    assert replacement.json()["facts"] == [{"statement": "时间以复核版为准", "verified": True}]
    stale_update = content_client.post(
        f"/api/v1/library/items/{uploaded.json()['id']}/versions/upload",
        headers=user_headers,
        params={
            "expected_version": uploaded.json()["version"],
            "category": category,
            "name": "过期编辑",
        },
        files={"file": ("stale.txt", b"stale", "text/plain")},
    )
    assert stale_update.status_code == 409
    with SessionLocal() as session:
        version = session.get(ResourceVersion, replacement.json()["resource_version_id"])
        chunks = session.scalars(
            select(ResourceChunk).where(ResourceChunk.version_id == version.id)
        ).all()
        assert "复核后的活动时间" in version.text_content
        assert chunks


def test_library_asset_download_is_streamed_with_content_access_control(
    content_client,
    user_headers,
    other_headers,
):
    private_asset = content_client.post(
        "/api/v1/library/items/upload",
        headers=user_headers,
        params={
            "resource_kind": "image",
            "category": "product_image",
            "name": "受限产品参考图",
            "data_level": "internal",
        },
        files={"file": ("reference.png", b"private-image-bytes", "image/png")},
    )
    assert private_asset.status_code == 200, private_asset.text
    private_id = private_asset.json()["id"]

    downloaded = content_client.get(
        f"/api/v1/library/items/{private_id}/download",
        headers=user_headers,
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == b"private-image-bytes"
    assert downloaded.headers["content-type"] == "image/png"

    forbidden = content_client.get(
        f"/api/v1/library/items/{private_id}/download",
        headers=other_headers,
    )
    assert forbidden.status_code == 403

    public_asset = content_client.post(
        "/api/v1/library/items/upload",
        headers=user_headers,
        params={
            "resource_kind": "image",
            "category": "brand_image",
            "name": "公开品牌参考图",
            "data_level": "public",
        },
        files={"file": ("brand.png", b"public-image-bytes", "image/png")},
    )
    assert public_asset.status_code == 200, public_asset.text
    public_download = content_client.get(
        f"/api/v1/library/items/{public_asset.json()['id']}/download",
        headers=other_headers,
    )
    assert public_download.status_code == 200, public_download.text
    assert public_download.content == b"public-image-bytes"

    deleted = content_client.delete(
        f"/api/v1/library/items/{private_id}",
        headers=user_headers,
    )
    assert deleted.status_code == 200, deleted.text
    inactive = content_client.get(
        f"/api/v1/library/items/{private_id}/download",
        headers=user_headers,
    )
    assert inactive.status_code == 404


def test_content_task_keeps_stable_product_resource_reference(
    content_client,
    user_headers,
    other_headers,
):
    product = content_client.post(
        "/api/v1/library/items",
        headers=user_headers,
        json={
            "resource_kind": "product",
            "category": "动态产品目录",
            "name": "目录产品 A",
            "text_content": "规格与卖点由企业资料维护。",
            "data_level": "internal",
            "metadata": {"series_id": "series-a", "series_name": "产品系列 A"},
        },
    )
    assert product.status_code == 200, product.text
    product_entry_id = product.json()["resource_entry_id"]

    product_image = content_client.post(
        "/api/v1/library/items/upload",
        headers=user_headers,
        params={
            "resource_kind": "image",
            "category": "product_image",
            "name": "目录产品 A 主图",
            "data_level": "internal",
            "parent_id": product_entry_id,
        },
        files={"file": ("product-a.png", b"product-a-image", "image/png")},
    )
    assert product_image.status_code == 200, product_image.text
    detail = content_client.get(
        f"/api/v1/library/items/{product.json()['id']}",
        headers=user_headers,
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["resource_entry_id"] == product_entry_id
    assert detail.json()["children"][0]["item_id"] == product_image.json()["id"]
    assert detail.json()["children"][0]["has_asset"] is True
    assert detail.json()["children"][0]["download_url"].endswith("/download")
    assert (
        content_client.get(
            f"/api/v1/library/items/{product.json()['id']}",
            headers=other_headers,
        ).status_code
        == 403
    )
    dynamic_categories = content_client.get(
        "/api/v1/library/categories",
        headers=user_headers,
    ).json()
    product_category = next(item for item in dynamic_categories if item["id"] == "动态产品目录")
    assert product_category["resource_kinds"] == ["product"]

    created = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "image",
            "name": "产品引用任务",
            "product": "客户端过期名称",
            "product_resource_id": product_entry_id,
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["product"] == "目录产品 A"
    assert created.json()["product_resource_id"] == product_entry_id

    forbidden = content_client.post(
        "/api/v1/tasks",
        headers=other_headers,
        json={
            "mode": "image",
            "name": "越权产品引用",
            "product_resource_id": product_entry_id,
        },
    )
    assert forbidden.status_code == 403

    detached = content_client.patch(
        f"/api/v1/tasks/{created.json()['id']}",
        headers=user_headers,
        json={
            "expected_version": created.json()["version"],
            "product": "临时自定义产品",
        },
    )
    assert detached.status_code == 200, detached.text
    assert detached.json()["product"] == "临时自定义产品"
    assert detached.json()["product_resource_id"] is None

    not_a_product = content_client.post(
        "/api/v1/library/items",
        headers=user_headers,
        json={
            "resource_kind": "document",
            "category": "动态资料",
            "name": "企业介绍",
            "text_content": "企业资料。",
        },
    )
    invalid = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "copy",
            "name": "错误资料类型",
            "product_resource_id": not_a_product.json()["resource_entry_id"],
        },
    )
    assert invalid.status_code == 422


def test_manual_content_import_direct_subtitles_and_image_prompt_composition(
    content_client,
    user_headers,
):
    task = create_task(content_client, user_headers)
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    copy_stage = stage(workflow, "copy")
    copy = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/copy/import",
        headers=user_headers,
        json={
            "text": "这是人工输入、无需模型生成的完整文案。",
            "expected_version": copy_stage["version"],
        },
    )
    assert copy.status_code == 200, copy.text
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    copy_stage = stage(workflow, "copy")
    assert copy_stage["state"] == "confirmed"
    assert copy_stage["selected_artifact_ids"] == {"copy": [copy.json()["id"]]}
    assert copy_stage["confirmed_artifact_ids"] == {"copy": [copy.json()["id"]]}

    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    storyboard_stage = stage(workflow, "storyboard")
    storyboard = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/storyboard/import",
        headers=user_headers,
        json={
            "text": "第一镜介绍品牌。\n第二镜展示真实使用场景。",
            "expected_version": storyboard_stage["version"],
        },
    )
    assert storyboard.status_code == 200, storyboard.text
    assert len(storyboard.json()["content"]["shots"]) == 2
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    storyboard_stage = stage(workflow, "storyboard")
    assert (
        content_client.post(
            f"/api/v1/tasks/{task['id']}/stages/storyboard/confirm",
            headers=user_headers,
            json={"expected_version": storyboard_stage["version"]},
        ).status_code
        == 200
    )

    subtitles = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/voice_subtitles/generate",
        headers=user_headers,
        json={"parameters": {"operation": "subtitle"}},
    )
    assert subtitles.status_code == 200, subtitles.text
    subtitle = subtitles.json()["artifacts"][0]
    assert subtitle["content"]["source_artifact_id"] == storyboard.json()["id"]
    assert "第一镜介绍品牌" in subtitle["content"]["text"]

    image_task = create_task(content_client, user_headers, "image")
    generated_image = content_client.post(
        f"/api/v1/tasks/{image_task['id']}/stages/image/generate",
        headers=user_headers,
        json={
            "count": 1,
            "instruction": "画面不要出现文字",
            "parameters": {
                "prompt": "臻品雅酿产品主视觉",
                "scene": "清溪河谷晨雾",
                "style": "品牌纪实摄影",
            },
        },
    )
    assert generated_image.status_code == 200, generated_image.text
    prompt = generated_image.json()["artifacts"][0]["content"]["prompt"]
    assert "臻品雅酿产品主视觉" in prompt
    assert "清溪河谷晨雾" in prompt
    assert "品牌纪实摄影" in prompt
    assert "画面不要出现文字" in prompt


def test_official_jsonl_import_job_populates_unified_chunks_and_search(
    content_client,
    user_headers,
    monkeypatch,
):
    from content_service import main as content_main
    from content_service.db import SessionLocal
    from content_service.knowledge import LibraryKnowledgeProvider
    from content_service.models import ExternalRef, ResourceChunk
    from content_service.providers import KnowledgeQuery

    fixture = (
        content_main.Path(__file__).resolve().parent
        / "fixtures"
        / "content_knowledge"
        / "yifabao_official"
    )
    monkeypatch.setattr(
        content_main,
        "settings",
        replace(content_main.settings, knowledge_dir=fixture),
    )
    queued = content_client.post(
        "/api/v1/library/import-jsonl",
        headers=user_headers,
    )
    assert queued.status_code == 200, queued.text
    job_id = queued.json()["id"]
    for _ in range(100):
        job = content_client.get(f"/api/v1/jobs/{job_id}", headers=user_headers).json()
        if job["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert job["status"] == "succeeded", job
    assert job["result"]["document_count"] > 0

    with SessionLocal() as session:
        assert session.scalar(select(ResourceChunk.id)) is not None
        assert (
            session.scalar(select(ExternalRef.id).where(ExternalRef.system == "official-jsonl"))
            is not None
        )
        results = LibraryKnowledgeProvider(session).search(
            KnowledgeQuery(
                text="清溪河谷",
                requester_id="user-a",
                task_id="knowledge-import-test",
                categories=(),
                allowed_data_levels=frozenset({"public", "internal"}),
                limit=5,
            )
        )
        assert results
        assert all("entry_id" in item.metadata for item in results)


def test_quick_video_composes_confirmed_storyboard_from_ordered_library_images(
    content_client,
    user_headers,
    other_headers,
):
    task = create_task(content_client, user_headers, "video")
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    storyboard_stage = stage(workflow, "storyboard")
    imported = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/storyboard/import",
        headers=user_headers,
        json={
            "text": "第一镜展示红色产品场景。\n第二镜展示蓝色品牌场景。",
            "expected_version": storyboard_stage["version"],
        },
    )
    assert imported.status_code == 200, imported.text
    storyboard_content = dict(imported.json()["content"])
    edited_shots = [dict(item) for item in storyboard_content["shots"]]
    edited_shots[0]["duration_seconds"] = 0.6
    edited_shots[0]["transition"] = "fade"
    storyboard_content["shots"] = edited_shots
    revised_storyboard = content_client.patch(
        f"/api/v1/artifacts/{imported.json()['id']}",
        headers=user_headers,
        json={
            "expected_version": imported.json()["version"],
            "content": storyboard_content,
        },
    )
    assert revised_storyboard.status_code == 200, revised_storyboard.text
    first_shot = revised_storyboard.json()["content"]["shots"][0]
    assert first_shot["duration_seconds"] == 0.6
    assert first_shot["sequence"] == first_shot["order"] == 1
    assert "duration" not in first_shot
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    storyboard_stage = stage(workflow, "storyboard")
    confirmed_storyboard = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/storyboard/confirm",
        headers=user_headers,
        json={"expected_version": storyboard_stage["version"]},
    )
    assert confirmed_storyboard.status_code == 200, confirmed_storyboard.text

    voice = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/voice_subtitles/generate",
        headers=user_headers,
        json={"parameters": {"operation": "voice", "audio_format": "wav"}},
    )
    assert voice.status_code == 200, voice.text
    subtitle = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/voice_subtitles/generate",
        headers=user_headers,
        json={"parameters": {"operation": "subtitle"}},
    )
    assert subtitle.status_code == 200, subtitle.text
    subtitle_artifact = subtitle.json()["artifacts"][0]
    revised_subtitle_content = dict(subtitle_artifact["content"])
    revised_segments = [dict(item) for item in revised_subtitle_content["segments"]]
    revised_segments[0]["text"] = "人工修改后的字幕"
    revised_subtitle_content["segments"] = revised_segments
    revised_subtitle = content_client.patch(
        f"/api/v1/artifacts/{subtitle_artifact['id']}",
        headers=user_headers,
        json={
            "expected_version": subtitle_artifact["version"],
            "content": revised_subtitle_content,
        },
    )
    assert revised_subtitle.status_code == 200, revised_subtitle.text
    downloaded_subtitle = content_client.get(
        f"/api/v1/artifacts/{revised_subtitle.json()['id']}/download",
        headers=user_headers,
    )
    assert downloaded_subtitle.status_code == 200, downloaded_subtitle.text
    assert "人工修改后的字幕" in downloaded_subtitle.content.decode("utf-8")
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    voice_stage = stage(workflow, "voice_subtitles")
    confirmed_voice = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/voice_subtitles/confirm",
        headers=user_headers,
        json={"expected_version": voice_stage["version"]},
    )
    assert confirmed_voice.status_code == 200, confirmed_voice.text

    asset_ids = []
    for index, color in enumerate(("red", "blue"), start=1):
        buffer = io.BytesIO()
        Image.new("RGB", (160, 96), color).save(buffer, format="PNG")
        uploaded = content_client.post(
            "/api/v1/library/items/upload",
            headers=user_headers,
            params={
                "resource_kind": "image",
                "category": "video-material",
                "name": f"快捷视频镜头-{index}",
            },
            files={"file": (f"shot-{index}.png", buffer.getvalue(), "image/png")},
        )
        assert uploaded.status_code == 200, uploaded.text
        asset_ids.append(uploaded.json()["id"])

    generated_voice = voice.json()["artifacts"][0]
    voice_download = content_client.get(
        f"/api/v1/artifacts/{generated_voice['id']}/download",
        headers=user_headers,
    )
    assert voice_download.status_code == 200, voice_download.text
    uploaded_voice = content_client.post(
        "/api/v1/library/items/upload",
        headers=user_headers,
        params={
            "resource_kind": "audio",
            "category": "video-material",
            "name": "用户上传主配音",
        },
        files={"file": ("voice.wav", voice_download.content, "audio/wav")},
    )
    assert uploaded_voice.status_code == 200, uploaded_voice.text

    # Per-shot storyboard bindings are authoritative and may intentionally repeat
    # one asset.  Explicit source_asset_ids below are only fallbacks and must not
    # replace these two blue shots.
    bound_storyboard_content = dict(revised_storyboard.json()["content"])
    bound_shots = [dict(item) for item in bound_storyboard_content["shots"]]
    bound_shots[0]["asset_id"] = asset_ids[1]
    bound_shots[1]["asset_id"] = asset_ids[1]
    bound_shots[1]["fit_mode"] = "cover"
    bound_shots[1]["transition_duration_seconds"] = 0.2
    bound_storyboard_content["shots"] = bound_shots
    bound_storyboard = content_client.patch(
        f"/api/v1/artifacts/{revised_storyboard.json()['id']}",
        headers=user_headers,
        json={
            "expected_version": revised_storyboard.json()["version"],
            "content": bound_storyboard_content,
        },
    )
    assert bound_storyboard.status_code == 200, bound_storyboard.text
    assert (
        content_client.post(
            f"/api/v1/artifacts/{bound_storyboard.json()['id']}/confirm",
            headers=user_headers,
        ).status_code
        == 200
    )

    composed = content_client.post(
        f"/api/v1/tasks/{task['id']}/compose-video",
        headers=user_headers,
        json={
            "idempotency_key": f"compose-{uuid4().hex}",
            "parameters": {
                "source_asset_ids": [asset_ids[0], asset_ids[0]],
                "clip_duration_seconds": 0.5,
                "voice_asset_id": uploaded_voice.json()["id"],
                "include_subtitles": False,
            },
            "composition_spec": {
                "width": 160,
                "height": 96,
                "fit_mode": "contain",
                "transition": "fade",
                "transition_duration_seconds": 0.15,
                "source_audio_policy": "replace_by_voice",
                "subtitle_style": {"font_size": 32, "margin_v": 20},
            },
        },
    )
    assert composed.status_code == 200, composed.text
    job_id = composed.json()["id"]
    for _ in range(200):
        job = content_client.get(f"/api/v1/jobs/{job_id}", headers=user_headers).json()
        if job["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert job["status"] == "succeeded", job
    assert job["result"]["source_asset_ids"] == [asset_ids[1], asset_ids[1]]
    assert revised_subtitle.json()["id"] not in job["result"]["source_artifact_ids"]
    spec = job["result"]["composition_spec"]
    assert spec["voice_asset_id"] == uploaded_voice.json()["id"]
    assert spec["subtitle_artifact_id"] is None
    assert [item["source_asset_id"] for item in spec["clips"]] == [
        asset_ids[1],
        asset_ids[1],
    ]
    assert spec["clips"][1]["fit_mode"] == "cover"
    assert spec["clips"][1]["transition_duration_seconds"] == 0.2
    artifacts = content_client.get(
        f"/api/v1/tasks/{task['id']}/artifacts", headers=user_headers
    ).json()
    output = next(item for item in artifacts if item["kind"] == "video")
    assert output["content"]["source_asset_ids"] == [asset_ids[1], asset_ids[1]]
    assert output["content"]["clip_count"] == 2
    assert output["content"]["voice_applied"] is True
    assert output["content"]["subtitle_burned"] is False
    assert output["content"]["composition_spec"] == spec

    confirmed_output = content_client.post(
        f"/api/v1/artifacts/{output['id']}/confirm",
        headers=user_headers,
    )
    assert confirmed_output.status_code == 200, confirmed_output.text
    output_download = content_client.get(
        f"/api/v1/artifacts/{output['id']}/download",
        headers=user_headers,
    )
    assert output_download.status_code == 200, output_download.text
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    export_stage = stage(workflow, "export")
    confirmed_export = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/export/confirm",
        headers=user_headers,
        json={"expected_version": export_stage["version"]},
    )
    assert confirmed_export.status_code == 200, confirmed_export.text
    archived_task = content_client.get(f"/api/v1/tasks/{task['id']}", headers=user_headers)
    assert archived_task.status_code == 200, archived_task.text
    assert archived_task.json()["archived"] is True
    assert (
        content_client.get(
            f"/api/v1/tasks/{task['id']}/export",
            headers=other_headers,
        ).status_code
        == 403
    )
    exported = content_client.get(
        f"/api/v1/tasks/{task['id']}/export",
        headers=user_headers,
    )
    assert exported.status_code == 200, exported.text

    expected_names = {
        f"{task['name']}-配音-1.wav": voice_download.content,
        f"{task['name']}-成片-v{output['version']}.mp4": output_download.content,
    }
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert set(archive.namelist()) == set(expected_names)
        assert all("/" not in name and "\\" not in name for name in archive.namelist())
        assert not any(name.lower().endswith(".json") for name in archive.namelist())
        for name, expected_content in expected_names.items():
            assert archive.read(name) == expected_content


def test_ai_video_can_regenerate_one_shot_without_replacing_siblings(
    content_client,
    user_headers,
    monkeypatch,
):
    from content_service import main as content_main
    from content_service.provider_dependencies import get_ai_video_generation_port
    from content_service.providers import AiVideoTask, ProviderStatus

    class FakeAiVideoProvider:
        capabilities = {"async": True, "text_to_video": True}

        def __init__(self):
            self.status = ProviderStatus(
                provider_id="test-ai-video",
                name="test-ai-video",
                capability="ai-video-generation",
                mode="test",
                external=True,
                enabled=True,
                model="test-model",
            )
            self.requests = {}

        def submit(self, request):
            provider_task_id = uuid4().hex
            self.requests[provider_task_id] = request
            return AiVideoTask(
                provider_task_id=provider_task_id,
                status="queued",
                provider_id=self.status.provider_id,
                model=self.status.model,
                progress=0,
            )

        def get(self, provider_task_id):
            return AiVideoTask(
                provider_task_id=provider_task_id,
                status="succeeded",
                provider_id=self.status.provider_id,
                model=self.status.model,
                progress=100,
                video_url=f"https://example.invalid/{provider_task_id}.mp4",
            )

        def cancel(self, provider_task_id):
            return AiVideoTask(
                provider_task_id=provider_task_id,
                status="cancelled",
                provider_id=self.status.provider_id,
                model=self.status.model,
            )

    provider = FakeAiVideoProvider()

    def fake_download(result, *, asset_store, storage_key):
        return asset_store.put_bytes(storage_key, b"test-video", "video/mp4")

    content_main.app.dependency_overrides[get_ai_video_generation_port] = lambda: provider
    monkeypatch.setattr(content_main, "download_generated_video", fake_download)
    monkeypatch.setattr(
        content_main,
        "settings",
        replace(
            content_main.settings,
            ai_video_poll_interval_seconds=0.01,
            ai_video_max_wait_seconds=1,
        ),
    )
    try:
        task = create_task(content_client, user_headers, "video")
        workflow = content_client.get(
            f"/api/v1/tasks/{task['id']}/workflow",
            headers=user_headers,
        ).json()
        imported = content_client.post(
            f"/api/v1/tasks/{task['id']}/stages/storyboard/import",
            headers=user_headers,
            json={
                "text": "第一镜展示产品。\n第二镜展示品牌文化。",
                "expected_version": stage(workflow, "storyboard")["version"],
            },
        )
        assert imported.status_code == 200, imported.text
        storyboard_content = dict(imported.json()["content"])
        shots = [dict(item) for item in storyboard_content["shots"]]
        shots[1].update(
            {
                "duration_seconds": 4,
                "ratio": "9:16",
                "resolution": "720p",
            }
        )
        storyboard_content["shots"] = shots
        revised = content_client.patch(
            f"/api/v1/artifacts/{imported.json()['id']}",
            headers=user_headers,
            json={
                "expected_version": imported.json()["version"],
                "content": storyboard_content,
            },
        )
        assert revised.status_code == 200, revised.text
        assert (
            content_client.post(
                f"/api/v1/artifacts/{revised.json()['id']}/confirm",
                headers=user_headers,
            ).status_code
            == 200
        )

        generated_all = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-ai-video",
            headers=user_headers,
            json={"idempotency_key": f"all-{uuid4().hex}"},
        )
        assert generated_all.status_code == 200, generated_all.text
        for job_id in generated_all.json()["jobs"]:
            assert job_id
        full_job_ids = [item["id"] for item in generated_all.json()["jobs"]]
        for job_id in full_job_ids:
            for _ in range(100):
                job = content_client.get(f"/api/v1/jobs/{job_id}", headers=user_headers).json()
                if job["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.02)
            assert job["status"] == "succeeded", job

        first_workflow = content_client.get(
            f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
        ).json()
        first_selection = stage(first_workflow, "video_preview")["selected_artifact_ids"][
            "ai_video_clip"
        ]
        assert len(first_selection) == 2

        regenerated = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-ai-video",
            headers=user_headers,
            json={
                "instruction": "加强文化氛围",
                "idempotency_key": f"shot-{uuid4().hex}",
                "parameters": {"shot_index": 1},
            },
        )
        assert regenerated.status_code == 200, regenerated.text
        assert len(regenerated.json()["jobs"]) == 1
        regenerated_job_id = regenerated.json()["jobs"][0]["id"]
        for _ in range(100):
            regenerated_job = content_client.get(
                f"/api/v1/jobs/{regenerated_job_id}", headers=user_headers
            ).json()
            if regenerated_job["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.02)
        assert regenerated_job["status"] == "succeeded", regenerated_job

        artifacts = content_client.get(
            f"/api/v1/tasks/{task['id']}/artifacts?kind=ai_video_clip",
            headers=user_headers,
        ).json()
        newest = artifacts[-1]
        assert newest["content"]["shot_index"] == 1
        assert "加强文化氛围" in newest["content"]["prompt"]
        assert newest["content"]["duration_seconds"] == 4
        assert newest["content"]["ratio"] == "9:16"
        assert newest["content"]["resolution"] == "720p"

        final_workflow = content_client.get(
            f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
        ).json()
        final_selection = stage(final_workflow, "video_preview")["selected_artifact_ids"][
            "ai_video_clip"
        ]
        assert final_selection == [first_selection[0], newest["id"]]

        with content_main.SessionLocal() as session:
            selected_storyboard = session.get(
                content_main.Artifact,
                revised.json()["id"],
            )
            restricted_content = content_main.load(selected_storyboard.content_json)
            restricted_content["content_data_level"] = "restricted"
            selected_storyboard.content_json = content_main.dump(restricted_content)
            session.commit()
        submitted_before = len(provider.requests)
        blocked = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-ai-video",
            headers=user_headers,
            json={"idempotency_key": f"restricted-{uuid4().hex}"},
        )
        assert blocked.status_code == 403, blocked.text
        assert "受限" in blocked.json()["detail"]
        assert len(provider.requests) == submitted_before
    finally:
        content_main.app.dependency_overrides.pop(
            get_ai_video_generation_port,
            None,
        )
