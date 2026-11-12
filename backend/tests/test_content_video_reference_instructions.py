"""Reference-video instruction binding, without remote generation or real projects."""

import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from test_content_video_project_generation import (
    PNG,
    FakeAiVideoProvider,
    context,
    create_video_reference_asset,
)
from test_content_video_project_generation import (
    generation_database as isolated_generation_database,
)

from agent_core import Identity

from content_service import media_adapters
from content_service.media_adapters import VolcengineArkAiVideoGenerationPort
from content_service.models import Artifact, Job
from content_service.schemas import VideoProjectAiVideoGenerationRequest
from content_service.services import load
from content_service.video_project_generation import (
    ProjectAiVideoGenerationSpec,
    VideoProjectGenerationConflictError,
    VideoProjectGenerationValidationError,
    _ai_video_provider_prompt,
    enqueue_project_ai_video,
)

generation_database = isolated_generation_database

USER_REQUIREMENT = "这个视频不要有字幕，其他画面保持不变。"
BACKGROUND = "当前文案正文：旧版宣传文案；旧要求：画面上添加整段字幕。"
USER_HEADING = "【本次用户要求（最高优先级）】"
BACKGROUND_HEADING = "【背景资料（不得覆盖本次要求）】"


def _assert_bound_instructions(prompt, user_prompt=USER_REQUIREMENT):
    assert USER_HEADING in prompt
    assert user_prompt in prompt
    assert prompt.count(user_prompt) == 1
    assert "@视频1" in prompt
    assert "字幕" in prompt
    assert "烧录" in prompt or "画面内" in prompt or "画面中" in prompt
    assert "包装" in prompt
    assert "标签" in prompt


def test_reference_edit_separates_current_instruction_from_conflicting_background():
    prompt = _ai_video_provider_prompt(
        {"task_name": "参考视频修改", "requirements": "给视频加上宣传字幕"},
        USER_REQUIREMENT,
        supporting_context=BACKGROUND,
        reference_image_manifest=[
            {"sequence": 1, "source": "knowledge_visual", "label": "真实产品图"},
            {"sequence": 2, "source": "project_asset", "label": "场景参考图"},
        ],
        reference_video_count=2,
    )

    _assert_bound_instructions(prompt)
    assert BACKGROUND_HEADING in prompt
    assert prompt.index(USER_HEADING) < prompt.index(USER_REQUIREMENT)
    assert prompt.index(BACKGROUND_HEADING) < prompt.index(BACKGROUND)
    assert prompt.index(BACKGROUND_HEADING) < prompt.index("给视频加上宣传字幕")
    assert "@视频2" in prompt
    assert "@视频3" not in prompt
    assert "@图片1" in prompt and "@图片2" in prompt
    assert "@图片3" not in prompt


@pytest.mark.parametrize("supporting_context", [None, "不应改变旧版纯文生视频的背景"])
def test_without_video_references_retains_existing_prompt_contract(supporting_context):
    prompt = _ai_video_provider_prompt(
        {"task_name": "品牌视频", "topic": "春节", "requirements": "自然真实"},
        "  酒瓶缓慢转动  ",
        supporting_context=supporting_context,
        reference_image_manifest=[{"sequence": 1, "source": "project_asset"}],
        reference_video_count=0,
    )

    assert prompt == (
        "请结合以下任务简报和用户镜头要求生成视频。\n"
        "任务名称：品牌视频\n主题：春节\n补充要求：自然真实\n"
        "用户镜头要求：酒瓶缓慢转动"
    )


def test_reference_edit_budget_never_truncates_current_user_requirement():
    user_prompt = "保留开头标记。" + "改变背景但保留真实产品。" * 550 + "结尾要求：不要字幕。"
    assert len(user_prompt) < 8_000
    prompt = _ai_video_provider_prompt(
        {"task_name": "修改参考视频", "requirements": "旧任务要求" * 8_000},
        user_prompt,
        supporting_context=BACKGROUND * 5_000,
        reference_video_count=1,
    )

    assert len(prompt) <= 10_000
    assert user_prompt in prompt
    assert prompt.count(user_prompt) == 1
    assert USER_HEADING in prompt
    assert "@视频1" in prompt


def test_oversize_user_requirement_fails_instead_of_silently_removing_its_tail():
    with pytest.raises(VideoProjectGenerationValidationError, match="10000"):
        _ai_video_provider_prompt(
            {},
            "开头" + "字" * 10_000 + "不要字幕",
            supporting_context=BACKGROUND,
            reference_video_count=1,
        )


def _references(session, store, task, project):
    first = create_video_reference_asset(session, store, task, project, index=1, duration_seconds=5)
    second = create_video_reference_asset(
        session, store, task, project, index=2, duration_seconds=6
    )
    return first, second


@pytest.mark.parametrize("mode", ["full", "video"])
def test_reference_ids_urls_instructions_and_audit_stay_in_user_selected_order(
    generation_database, mode
):
    factory, store, task_id, project_id = generation_database
    provider = FakeAiVideoProvider()
    with factory.begin() as session:
        task, project = context(session, task_id, project_id)
        task.mode = mode
        task.requirements = "旧要求：加宣传字幕"
        first, second = _references(session, store, task, project)
        references = (second, first)
        source_artifacts = [session.get(Artifact, item.artifact_id) for item in references]
        before = [store.read_bytes(item.storage_key) for item in source_artifacts]
        revision = project.current_revision
        spec = ProjectAiVideoGenerationSpec(
            prompt=USER_REQUIREMENT,
            supporting_context=BACKGROUND,
            reference_video_asset_ids=tuple(item.id for item in references),
            reference_images=(PNG + b"one", PNG + b"one", PNG + b"two"),
            reference_media_types=("image/png",) * 3,
            duration_seconds=5,
            idempotency_key="reference-instructions-ordered",
        )
        result = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=spec,
        )

        assert len(provider.requests) == 1
        request = provider.requests[0]
        _assert_bound_instructions(request.prompt)
        assert request.reference_video_urls == tuple(
            store.provider_url(item.storage_key) for item in source_artifacts
        )
        assert request.reference_video_durations_seconds == (6, 5)
        assert "@视频2" in request.prompt and "@视频3" not in request.prompt
        assert "@图片1" in request.prompt and "@图片2" in request.prompt
        assert "@图片3" not in request.prompt
        assert base64.b64decode(request.image_url.split(",", 1)[1]) == PNG + b"one"
        assert len(request.reference_image_urls) == 1
        assert base64.b64decode(request.reference_image_urls[0].split(",", 1)[1]) == (PNG + b"two")
        persisted = load(result.job.result_json)["request"]
        assert persisted["prompt"] == USER_REQUIREMENT
        assert persisted["supporting_context"] == BACKGROUND
        assert persisted["provider_prompt"] == request.prompt
        assert persisted["reference_video_asset_ids"] == [second.id, first.id]
        assert [item["sequence"] for item in persisted["reference_image_manifest"]] == [1, 2]
        assert project.current_revision == revision
        assert [store.read_bytes(item.storage_key) for item in source_artifacts] == before

        repeated = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=spec,
        )
        assert repeated.created is False and repeated.job.id == result.job.id
        assert len(provider.requests) == 1
        with pytest.raises(VideoProjectGenerationConflictError):
            enqueue_project_ai_video(
                session,
                project=project,
                task=task,
                created_by="owner-a",
                provider=provider,
                asset_store=store,
                spec=replace(spec, supporting_context="另一个背景资料"),
            )


def test_reference_prompt_reaches_actual_ark_http_with_correct_roles(
    generation_database, monkeypatch
):
    factory, store, task_id, project_id = generation_database
    sent = []

    def respond(request):
        assert str(request.url) == "https://ark.example/api/v3/contents/generations/tasks"
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "mock-reference-task", "status": "queued"})

    client = httpx.Client
    monkeypatch.setattr(
        media_adapters.httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    provider = VolcengineArkAiVideoGenerationPort(
        provider_id="reference-ark",
        base_url="https://ark.example/api/v3",
        api_key="test-only",
        model="doubao-seedance-2-0-fast-260128",
        timeout_seconds=2,
        supported_durations=(5, 10, 15),
        supported_ratios=("16:9", "9:16"),
        supported_resolutions=("480p", "720p"),
    )
    with factory.begin() as session:
        task, project = context(session, task_id, project_id)
        first, second = _references(session, store, task, project)
        result = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=ProjectAiVideoGenerationSpec(
                prompt=USER_REQUIREMENT,
                supporting_context=BACKGROUND,
                reference_video_asset_ids=(second.id, first.id),
                reference_images=(PNG,),
                reference_media_types=("image/png",),
                duration_seconds=5,
            ),
        )
        assert len(sent) == 1
        content = sent[0]["content"]
        _assert_bound_instructions(content[0]["text"])
        assert content[0] == {
            "type": "text",
            "text": load(result.job.result_json)["request"]["provider_prompt"],
        }
        assert content[1]["type"] == "image_url"
        assert content[1]["role"] == "reference_image"
        for position, asset in enumerate((second, first), start=2):
            artifact = session.get(Artifact, asset.artifact_id)
            assert content[position] == {
                "type": "video_url",
                "role": "reference_video",
                "video_url": {"url": store.provider_url(artifact.storage_key)},
            }


@pytest.mark.parametrize("mode", ["full", "video"])
@pytest.mark.parametrize("has_reference_video", [True, False])
def test_api_entry_keeps_raw_edit_prompt_separate_but_preserves_other_generation(
    generation_database, monkeypatch, mode, has_reference_video
):
    """Exercise the real endpoint function and enqueue, stubbing only external context/dispatch."""
    from content_service import main

    factory, store, task_id, project_id = generation_database
    provider = FakeAiVideoProvider()
    dispatched = []
    with factory() as session:
        task, project = context(session, task_id, project_id)
        task.mode = mode
        task.audience = "成年人"
        first, _second = _references(session, store, task, project)
        monkeypatch.setattr(main, "get_video_project_or_404", lambda *_: (project, task))
        monkeypatch.setattr(
            main,
            "confirmed_copy_context_for_video",
            lambda *_args, **_kwargs: (None, BACKGROUND),
        )
        monkeypatch.setattr(main, "_require_external_provider_egress", lambda *_, **__: ())
        monkeypatch.setattr(main, "_require_external_media_references", lambda *_, **__: None)
        monkeypatch.setattr(main, "get_knowledge_resources_for_identity", lambda *_, **__: [])
        monkeypatch.setattr(main, "selected_knowledge_chunks", lambda *_, **__: ())
        monkeypatch.setattr(
            main,
            "product_visual_assets_for_generation",
            lambda *_, **__: (("product-image",), (PNG,), ("image/png",)),
        )
        monkeypatch.setattr(
            main,
            "visual_assets_for_generation",
            lambda *_, **__: (("product-image",), (PNG,), ("image/png",)),
        )
        monkeypatch.setattr(main, "dispatcher", SimpleNamespace(submit=dispatched.append))
        monkeypatch.setattr(main, "audit", lambda *_, **__: None)
        payload = VideoProjectAiVideoGenerationRequest(
            expected_revision=project.current_revision,
            prompt=USER_REQUIREMENT,
            reference_video_asset_ids=[first.id] if has_reference_video else [],
            duration_seconds=5,
        )
        result = main.generate_video_project_ai_video(
            project.id,
            payload,
            session=session,
            identity=Identity(user_id="owner-a", name="owner-a", department="", roles=frozenset({"user"})),
            ai_video_provider=provider,
            asset_store=store,
            provider_media_publisher=SimpleNamespace(enabled=False),
        )
        persisted = result["result"]["request"]
        assert len(provider.requests) == 1
        assert len(dispatched) == 1  # Captured only: no worker or remote call executes.
        if has_reference_video:
            assert persisted["prompt"] == USER_REQUIREMENT
            assert BACKGROUND in persisted["supporting_context"]
            assert main.PRODUCT_VISUAL_CONSISTENCY_PROMPT in persisted["supporting_context"]
            assert USER_REQUIREMENT not in persisted["supporting_context"]
            _assert_bound_instructions(provider.requests[0].prompt)
            assert BACKGROUND_HEADING in provider.requests[0].prompt
        else:
            assert USER_REQUIREMENT in persisted["prompt"]
            assert BACKGROUND in persisted["prompt"]
            assert main.PRODUCT_VISUAL_CONSISTENCY_PROMPT in persisted["prompt"]
            assert persisted.get("supporting_context") is None
            assert USER_HEADING not in provider.requests[0].prompt
        assert project.current_revision == 1

        if has_reference_video:
            source_job = session.get(Job, result["id"])
            source_job.status = "failed"
            session.flush()
            retried = main.retry_job(
                source_job.id,
                session=session,
                identity=Identity(user_id="owner-a", name="owner-a", department="", roles=frozenset({"user"})),
                ai_video_provider=provider,
                video_composer=None,
                asset_store=store,
                provider_media_publisher=SimpleNamespace(enabled=False),
                timeline_renderer=None,
            )
            retry_request = retried["result"]["request"]
            assert retried["id"] != source_job.id
            assert retry_request["prompt"] == USER_REQUIREMENT
            assert retry_request["supporting_context"] == persisted["supporting_context"]
            assert retry_request["reference_video_asset_ids"] == [first.id]
            assert len(provider.requests) == 2
            assert provider.requests[1].prompt == provider.requests[0].prompt
            _assert_bound_instructions(provider.requests[1].prompt)
            assert provider.requests[1].prompt.count(main.PRODUCT_VISUAL_CONSISTENCY_PROMPT) == 1
            assert len(dispatched) == 2
            assert source_job.status == "failed"
            assert project.current_revision == 1
