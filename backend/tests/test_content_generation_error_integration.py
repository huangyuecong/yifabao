"""Real local API/worker boundaries with fake providers; no external generation."""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_content_transform_preview_drafts import create_source, snapshot
from test_content_video_editor_api import _create_project, _wait_for_job
from test_content_video_project_generation import FakeAiVideoProvider, FakeImageProvider

from content_service import main
from content_service.db import SessionLocal
from content_service.llm_adapters import ProviderCallError
from content_service.models import Artifact, ContentTask, GenerationRun, Job
from content_service.provider_dependencies import (
    get_ai_video_generation_port,
    get_image_generation_port,
)
from content_service.providers import AiVideoTask, MockTextProvider
from content_service.schemas import GenerateRequest
from content_service.services import dump, load
from content_service.video_project_generation import normalize_ai_video_provider_failure


def rejected_text():
    return ProviderCallError(
        "upstream HTTP 400: InputTextSensitiveContentDetected / content[0] may contain sensitive information",
        status_code=400,
        provider_code="InputTextSensitiveContentDetected",
        provider_request_id="request-123-safe",
        provider_status_code=400,
        retryable=False,
    )


class RejectText(MockTextProvider):
    def topics(self, *_args):
        raise rejected_text()

    def copies(self, *_args):
        raise rejected_text()

    def transform_copy(self, *_args):
        raise rejected_text()


class RejectImage(FakeImageProvider):
    status = replace(FakeImageProvider.status, external=False)

    def generate(self, _request):
        raise rejected_text()


class RejectVideo(FakeAiVideoProvider):
    status = replace(FakeAiVideoProvider.status, external=False)

    def __init__(self, phase="submit", error=None):
        super().__init__()
        self.phase = phase
        self.error = error

    def submit(self, request):
        if self.phase == "submit":
            self.requests.append(request)
            raise self.error or rejected_text()
        return super().submit(request)

    def get(self, provider_task_id):
        return AiVideoTask(
            provider_task_id=provider_task_id,
            provider_id=self.status.provider_id,
            model=self.status.model,
            status="failed",
            error_code="InputTextSensitiveContentDetected",
            error_message="Input text content[0] may contain sensitive information",
        )


@contextmanager
def override(dependency, provider):
    previous = main.app.dependency_overrides.get(dependency)
    main.app.dependency_overrides[dependency] = lambda: provider
    try:
        yield
    finally:
        if previous is None:
            main.app.dependency_overrides.pop(dependency, None)
        else:
            main.app.dependency_overrides[dependency] = previous


def new_task(client, headers, mode):
    response = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "mode": mode,
            "name": f"错误隔离-{uuid4().hex[:8]}",
            "topic": "产品宣传",
            "content_type": "短视频",
            "style": "自然",
            "audience": "成年消费者",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def assert_text_rejection(detail, *, metadata=True):
    assert detail["code"] == "content_input_rejected"
    assert detail["retryable"] is False
    assert "文字" in detail["message"] or "文本" in detail["message"]
    assert "暂时无法完成请求" not in detail["message"]
    assert "InputText" not in detail["message"]
    assert "content[0]" not in detail["message"]
    if metadata:
        assert detail["provider_code"] == "InputTextSensitiveContentDetected"
        assert detail["provider_status_code"] == 400
        assert detail["provider_request_id"] == "request-123-safe"


@pytest.mark.parametrize("mode", ["full", "copy"])
@pytest.mark.parametrize("stage", ["topic", "copy"])
def test_title_and_copy_failures_return_chinese_and_persist_plain_message(
    content_client, user_headers, mode, stage
):
    if stage == "copy" and mode == "full":
        task_id, _, _ = create_source(content_client, user_headers, mode=mode, canonical_title=True)
    else:
        task_id = new_task(content_client, user_headers, mode)["id"]
    with override(main.get_text_provider, RejectText()):
        response = content_client.post(
            f"/api/v1/tasks/{task_id}/stages/{stage}/generate",
            headers=user_headers,
            json={"count": 1, "instruction": "创作一段内容"},
        )
    assert response.status_code == 502, response.text
    detail = response.json()["detail"]
    assert_text_rejection(detail)
    with SessionLocal() as session:
        run = session.scalar(
            select(GenerationRun)
            .where(GenerationRun.task_id == task_id, GenerationRun.stage_key == stage)
            .order_by(GenerationRun.created_at.desc())
        )
        assert run.status == "failed"
        assert run.error == detail["message"]
        assert load(run.result_artifact_ids_json) == []


@pytest.mark.parametrize("kind", ["topic", "copy"])
@pytest.mark.parametrize("operation", ["rewrite", "style"])
@pytest.mark.parametrize("preview", [True, False])
def test_preview_rewrite_and_style_keep_reason_without_adding_versions(
    content_client, user_headers, kind, operation, preview
):
    task_id, artifact_id, _ = create_source(content_client, user_headers, kind=kind)
    before = snapshot(task_id)
    with override(main.get_text_provider, RejectText()):
        response = content_client.post(
            f"/api/v1/artifacts/{artifact_id}/transform-copy",
            headers=user_headers,
            json={"preview": preview, "operation": operation, "instruction": "修改文案"},
        )
    assert response.status_code == 502, response.text
    assert_text_rejection(response.json()["detail"])
    if preview:
        assert snapshot(task_id) == before
    else:
        after = snapshot(task_id)
        assert after["artifacts"] == before["artifacts"]
        assert after["generation_runs"][-1]["error"] == response.json()["detail"]["message"]


@pytest.mark.parametrize("mode", ["full", "image"])
def test_task_image_generation_failure_keeps_real_reason(content_client, user_headers, mode):
    task = new_task(content_client, user_headers, mode)
    with override(get_image_generation_port, RejectImage()):
        response = content_client.post(
            f"/api/v1/tasks/{task['id']}/stages/image/generate",
            headers=user_headers,
            json={"count": 1, "instruction": "产品展示"},
        )
    assert response.status_code == 502, response.text
    detail = response.json()["detail"]
    assert_text_rejection(detail)
    with SessionLocal() as session:
        run = session.scalar(select(GenerationRun).where(GenerationRun.task_id == task["id"]))
        assert run.error == detail["message"]
        assert session.scalar(select(Artifact.id).where(Artifact.task_id == task["id"])) is None


@pytest.mark.parametrize("mode", ["full", "video"])
@pytest.mark.parametrize("kind", ["images", "video"])
def test_project_image_video_submission_reasons_not_replaced_again(
    content_client, user_headers, mode, kind
):
    task = new_task(content_client, user_headers, mode)
    project = _create_project(content_client, user_headers, task["id"])
    dependency = get_image_generation_port if kind == "images" else get_ai_video_generation_port
    provider = RejectImage() if kind == "images" else RejectVideo()
    with override(dependency, provider):
        response = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-{kind}",
            headers=user_headers,
            json={"expected_revision": project["current_revision"], "prompt": "产品展示"},
        )
    assert response.status_code == 502, response.text
    assert_text_rejection(response.json()["detail"])
    with SessionLocal() as session:
        assert session.scalar(select(Job.id).where(Job.task_id == task["id"])) is None
        assert session.scalar(select(Artifact.id).where(Artifact.task_id == task["id"])) is None


@pytest.mark.parametrize("mode", ["full", "video"])
def test_project_video_worker_persists_classified_error_without_retry_changes(
    content_client, user_headers, mode
):
    task = new_task(content_client, user_headers, mode)
    project = _create_project(content_client, user_headers, task["id"])
    with override(get_ai_video_generation_port, RejectVideo("poll")):
        response = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-video",
            headers=user_headers,
            json={"expected_revision": project["current_revision"], "prompt": "产品展示"},
        )
        assert response.status_code == 200, response.text
        job = _wait_for_job(content_client, user_headers, response.json()["id"])
    assert job["status"] == "failed"
    assert_text_rejection(job["result"]["error_detail"], metadata=False)
    assert job["error"] == job["result"]["error_detail"]["message"]
    with SessionLocal() as session:
        saved = session.get(Job, job["id"])
        assert saved.attempt == 1
        assert session.scalar(select(Artifact.id).where(Artifact.task_id == task["id"])) is None


def test_legacy_video_worker_keeps_reason_in_job_and_generation_run(content_client, user_headers):
    task = new_task(content_client, user_headers, "video")
    with SessionLocal() as session:
        run = GenerationRun(
            task_id=task["id"], stage_key="video_preview", created_by=user_headers["X-User-Id"]
        )
        session.add(run)
        session.flush()
        job = Job(
            task_id=task["id"],
            generation_run_id=run.id,
            owner_id=user_headers["X-User-Id"],
            kind="ai_video_generation",
            status="queued",
            result_json=dump({"provider_task_id": "old-video-job"}),
        )
        session.add(job)
        session.commit()
        job_id, run_id = job.id, run.id
    main.run_ai_video_job(job_id, RejectVideo("poll"), None)
    with SessionLocal() as session:
        saved = session.get(Job, job_id)
        details = load(saved.result_json)["error_detail"]
        assert_text_rejection(details, metadata=False)
        assert saved.error == session.get(GenerationRun, run_id).error == details["message"]


def test_workflow_video_submit_keeps_reason_and_creates_no_result(content_client, user_headers):
    task_value = new_task(content_client, user_headers, "video")
    with SessionLocal() as session:
        task = session.get(ContentTask, task_value["id"])
        run = GenerationRun(
            task_id=task.id, stage_key="video_preview", created_by=user_headers["X-User-Id"]
        )
        session.add(run)
        session.flush()
        with pytest.raises(main.HTTPException) as caught:
            main.queue_ai_video_job(
                session,
                task,
                run,
                GenerateRequest(instruction="产品视频"),
                SimpleNamespace(user_id=user_headers["X-User-Id"]),
                RejectVideo(),
            )
        assert_text_rejection(caught.value.detail)
        assert run.error == caught.value.detail["message"]
        assert run.status == "failed"
        assert session.scalar(select(Job.id).where(Job.task_id == task.id)) is None


def test_shared_voice_music_and_other_text_helpers_keep_existing_behavior():
    exc = rejected_text()
    legacy = main.video_project_generation_http_error(exc)
    assert legacy.detail["code"] == "ai_video_generation_failed"
    assert "暂时无法完成请求" in legacy.detail["message"]
    with pytest.raises(main.HTTPException) as caught:
        main.call_text_generation(lambda: (_ for _ in ()).throw(exc))
    assert isinstance(caught.value.detail, str)
    assert caught.value.detail.startswith("内容模型调用失败：")


@pytest.mark.parametrize(
    "provider_code,status,message,expected_code",
    [
        ("InvalidApiKey", 401, "invalid API key", "authentication_failed"),
        ("AccountOverdue", 403, "account overdue", "account_payment_required"),
        ("TooManyRequests", 429, "too many requests", "rate_limited"),
        ("InvalidParameter", 400, "duration must be between 5 and 15", "invalid_parameters"),
        ("RequestTimeout", 504, "request timeout", "timeout"),
        ("ServiceUnavailable", 503, "service unavailable", "provider_unavailable"),
    ],
)
def test_video_submit_specific_causes_survive_http_502_boundary(
    content_client, user_headers, provider_code, status, message, expected_code
):
    task = new_task(content_client, user_headers, "video")
    project = _create_project(content_client, user_headers, task["id"])
    error = ProviderCallError(
        message,
        status_code=status,
        provider_status_code=status,
        provider_code=provider_code,
        provider_request_id="case-request-1",
        retryable=False,
    )
    with override(get_ai_video_generation_port, RejectVideo(error=error)):
        response = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-video",
            headers=user_headers,
            json={"expected_revision": 1, "prompt": "产品展示"},
        )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == expected_code
    assert detail["provider_status_code"] == status
    assert detail["provider_code"] == provider_code
    assert detail["provider_request_id"] == "case-request-1"
    assert detail["retryable"] is False
    assert "暂时无法完成请求" not in detail["message"]
    if expected_code == "invalid_parameters":
        assert (
            "时长" in detail["message"] and "5" in detail["message"] and "15" in detail["message"]
        )


@pytest.mark.parametrize("retryable", [False, True])
def test_classification_does_not_change_provider_retry_decision(retryable):
    exc = ProviderCallError("Too many requests", status_code=429, retryable=retryable)
    message, detail = normalize_ai_video_provider_failure(exc, retryable=exc.retryable)
    assert detail["code"] == "rate_limited"
    assert detail["retryable"] is retryable
    assert "频率" in message and "重试" in message
