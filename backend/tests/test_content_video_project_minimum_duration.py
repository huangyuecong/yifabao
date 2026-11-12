import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_content_video_project_generation import (
    FakeAiVideoProvider,
    context,
)
from test_content_video_project_generation import (
    generation_database as isolated_generation_database,
)

from content_service.models import Job
from content_service.schemas import VideoProjectAiVideoGenerationRequest
from content_service.services import load
from content_service.video_project_generation import (
    ProjectAiVideoGenerationSpec,
    VideoProjectGenerationValidationError,
    enqueue_project_ai_video,
)

generation_database = isolated_generation_database


@pytest.mark.parametrize("mode", ["full", "video"])
def test_new_project_ai_video_starts_at_five_and_rejects_shorter_requests(
    generation_database, mode
):
    factory, store, task_id, project_id = generation_database
    provider = FakeAiVideoProvider()
    assert provider.capabilities["durations"][0] == 5
    with pytest.raises(ValidationError, match="至少为 5 秒"):
        VideoProjectAiVideoGenerationRequest(expected_revision=1, prompt="场景", duration_seconds=4)
    with factory() as session:
        task, project = context(session, task_id, project_id)
        task.mode = mode
        for duration in (1, 4, 4.5):
            with pytest.raises(VideoProjectGenerationValidationError, match="至少为 5 秒"):
                enqueue_project_ai_video(
                    session,
                    project=project,
                    task=task,
                    created_by="owner-a",
                    provider=provider,
                    asset_store=store,
                    spec=ProjectAiVideoGenerationSpec(prompt="场景", duration_seconds=duration),
                )
        assert provider.requests == []
        assert not list(session.scalars(select(Job).where(Job.task_id == task_id)))
        request = VideoProjectAiVideoGenerationRequest(
            expected_revision=1, prompt="场景", duration_seconds=5
        )
        result = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=ProjectAiVideoGenerationSpec(
                prompt=request.prompt,
                duration_seconds=request.duration_seconds,
            ),
        )
        assert provider.requests[-1].duration_seconds == 5
        assert load(result.job.result_json)["request"]["duration_seconds"] == 5
        assert provider.capabilities["durations"] == [5, 10, 15]


def test_project_ai_video_default_never_falls_back_to_four(generation_database):
    factory, store, task_id, project_id = generation_database
    provider = FakeAiVideoProvider()
    provider.capabilities = {**provider.capabilities, "durations": [4, 10]}
    with factory() as session:
        task, project = context(session, task_id, project_id)
        enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=ProjectAiVideoGenerationSpec(prompt="模型默认"),
        )
        assert provider.requests[-1].duration_seconds == 10
        provider.capabilities = {**provider.capabilities, "durations": [4]}
        with pytest.raises(VideoProjectGenerationValidationError, match="5 秒及以上"):
            enqueue_project_ai_video(
                session,
                project=project,
                task=task,
                created_by="owner-a",
                provider=provider,
                asset_store=store,
                spec=ProjectAiVideoGenerationSpec(prompt="模型不兼容"),
            )
        assert len(provider.requests) == 1


def test_video_candidate_openapi_contract_matches_runtime_rules():
    from content_service.main import app

    runtime_schemas = app.openapi()["components"]["schemas"]
    contract_path = Path(__file__).resolve().parents[1] / "contracts" / "content.openapi.json"
    checked_in_schemas = json.loads(contract_path.read_text(encoding="utf-8"))["components"][
        "schemas"
    ]
    for schemas in (runtime_schemas, checked_in_schemas):
        video = schemas["VideoProjectAiVideoGenerationRequest"]["properties"]
        assert video["duration_seconds"]["anyOf"][0]["minimum"] == 5
        image = schemas["VideoProjectImageGenerationRequest"]["properties"]
        assert image["size"]["anyOf"][0]["pattern"] == r"^(?:\d{2,5}[xX×]\d{2,5}|[1-9]\d?K)$"
        assert image["ratio"]["anyOf"][0]["pattern"] == r"^(?:adaptive|[1-9]\d{0,4}:[1-9]\d{0,4})$"
