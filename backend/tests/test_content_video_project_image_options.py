import base64
import io
import json
from dataclasses import replace

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from test_content_video_project_generation import (
    FakeImageProvider,
    context,
)
from test_content_video_project_generation import (
    generation_database as isolated_generation_database,
)

from content_service import media_adapters
from content_service.media_adapters import MockImageGenerationPort, VolcengineArkImageGenerationPort
from content_service.models import Artifact, ProjectAsset
from content_service.schemas import VideoProjectImageGenerationRequest
from content_service.services import load
from content_service.video_project_generation import (
    ProjectImageGenerationSpec,
    VideoProjectGenerationValidationError,
    generate_project_image_candidates,
)

generation_database = isolated_generation_database


def _ark():
    return VolcengineArkImageGenerationPort(
        provider_id="ark-image",
        base_url="https://ark.example/api/v3",
        api_key="test-only",
        model="doubao-seedream-5-0-260128",
        timeout_seconds=2,
    )


def _generate(fixture, provider, spec, *, mode="video"):
    factory, store, task_id, project_id = fixture
    with factory.begin() as session:
        task, project = context(session, task_id, project_id)
        task.mode = mode
        result = generate_project_image_candidates(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=spec,
        )
        assert project.current_revision == 1
        return result


@pytest.mark.parametrize("mode", ["full", "video"])
@pytest.mark.parametrize(
    "size,ratio", [("2K", "9:16"), ("3K", "16:9"), ("4K", "adaptive"), (None, None)]
)
def test_video_image_options_reach_actual_ark_http_payload(
    generation_database,
    monkeypatch,
    mode,
    size,
    ratio,
):
    image = io.BytesIO()
    Image.new("RGB", (96, 64), "#176b5f").save(image, "PNG")
    sent = []

    def respond(request):
        assert str(request.url) == "https://ark.example/api/v3/images/generations"
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(image.getvalue()).decode()}]}
        )

    client = httpx.Client
    monkeypatch.setattr(
        media_adapters.httpx,
        "Client",
        lambda **kwargs: client(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    schema = VideoProjectImageGenerationRequest(
        expected_revision=1,
        prompt="品牌新春场景",
        size=size,
        ratio=ratio,
    )
    result = _generate(
        generation_database,
        _ark(),
        ProjectImageGenerationSpec(
            prompt=schema.prompt,
            size=schema.size,
            ratio=schema.ratio,
        ),
        mode=mode,
    )
    assert len(sent) == 1
    assert sent[0].get("size") == size
    assert "ratio" not in sent[0]
    assert sent[0]["prompt"] == (
        f"品牌新春场景；画面比例：{ratio}" if ratio and ratio != "adaptive" else "品牌新春场景"
    )
    content = load(result.candidates[0].artifact.content_json)
    assert content["prompt"] == "品牌新春场景"
    assert content["provenance"]["request"]["size"] == size
    assert content["provenance"]["request"]["ratio"] == ratio
    assert content["provenance"]["request"]["prompt"] == "品牌新春场景"


@pytest.mark.parametrize(
    "size,ratio,message",
    [
        ("1K", None, "不支持清晰度"),
        ("3K", "17:1", "超出当前图片模型"),
        ("1280x720", "16:9", "尺寸过小"),
        ("5000x5000", "1:1", "尺寸过大"),
        ("2048x2048", "9:16", "与所选画面比例不一致"),
    ],
)
def test_video_image_invalid_selection_fails_before_provider_or_new_artifacts(
    generation_database,
    size,
    ratio,
    message,
):
    provider = _ark()
    calls = []
    provider.generate = lambda request: calls.append(request)
    with pytest.raises(VideoProjectGenerationValidationError, match=message):
        _generate(
            generation_database,
            provider,
            ProjectImageGenerationSpec(
                prompt="品牌场景",
                size=size,
                ratio=ratio,
            ),
        )
    assert calls == []
    factory, store, task_id, project_id = generation_database
    with factory() as session:
        assert session.scalar(select(Artifact).where(Artifact.task_id == task_id)) is None
        assert (
            session.scalar(select(ProjectAsset).where(ProjectAsset.project_id == project_id))
            is None
        )
    assert not list(store.root.rglob("*"))


def test_video_image_capabilities_not_hardcoded_and_unknown_options_use_model_default(
    generation_database,
):
    provider = FakeImageProvider()
    provider.capabilities = {"sizes": ["3K"], "ratios": ["1:1"], "count": {"max": 8}}
    _generate(
        generation_database,
        provider,
        ProjectImageGenerationSpec(prompt="场景", size="3K", ratio="1:1"),
    )
    assert provider.requests[-1].size == "3K"
    for spec in [
        ProjectImageGenerationSpec(prompt="场景", size="2K"),
        ProjectImageGenerationSpec(prompt="场景", ratio="9:16"),
        ProjectImageGenerationSpec(prompt="场景", size="2048x2048"),
    ]:
        with pytest.raises(VideoProjectGenerationValidationError, match="当前图片模型不支持"):
            _generate(generation_database, provider, spec)
    assert len(provider.requests) == 1
    provider.capabilities = {}
    _generate(generation_database, provider, ProjectImageGenerationSpec(prompt="模型默认"))
    assert provider.requests[-1].size is None and provider.requests[-1].ratio is None
    with pytest.raises(VideoProjectGenerationValidationError, match="请选择模型默认"):
        _generate(
            generation_database, provider, ProjectImageGenerationSpec(prompt="场景", size="2K")
        )


def test_video_image_custom_dimensions_stay_exact_and_mock_legacy_custom_bounds(
    generation_database,
):
    provider = FakeImageProvider()
    provider.capabilities = _ark().capabilities
    _generate(
        generation_database,
        provider,
        ProjectImageGenerationSpec(
            prompt="场景",
            size="1440x2560",
            ratio="9:16",
        ),
    )
    assert provider.requests[-1].size == "1440x2560"
    assert provider.requests[-1].ratio == "9:16"
    assert provider.requests[-1].prompt.endswith("画面比例：9:16")
    provider.capabilities = MockImageGenerationPort.capabilities
    valid = ProjectImageGenerationSpec(prompt="本地模拟", size="96x64")
    _generate(generation_database, provider, valid)
    with pytest.raises(VideoProjectGenerationValidationError, match="宽高范围"):
        _generate(generation_database, provider, replace(valid, size="32x64"))
