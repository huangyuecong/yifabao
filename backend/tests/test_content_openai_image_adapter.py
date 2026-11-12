import base64
import io
import json
from dataclasses import replace

import httpx
import pytest
from PIL import Image

from content_service import chat_media, chat_models
from content_service.config import load_settings
from content_service.llm_adapters import ProviderCallError
from content_service.openai_image_adapter import OpenAICompatibleImageGenerationPort
from content_service.providers import ImageGenerationRequest


@pytest.fixture
def png():
    stream = io.BytesIO()
    Image.new("RGB", (24, 16), "blue").save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture
def provider():
    return OpenAICompatibleImageGenerationPort(
        provider_id="gpt-image",
        base_url="https://models.example/v1",
        api_key="test-only-secret",
        model="gpt-image-2.5-sunburst",
        timeout_seconds=30,
        max_candidates=4,
    )


def transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_generations_use_gpt_contract_and_record_actual_dimensions(monkeypatch, png, provider):
    def handler(request):
        assert request.url.path == "/v1/images/generations"
        assert request.headers["authorization"] == "Bearer test-only-secret"
        assert json.loads(request.content) == {
            "model": "gpt-image-2.5-sunburst",
            "prompt": "cup",
            "n": 2,
            "size": "1536x1024",
        }
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(png).decode()}] * 2}
        )

    transport(monkeypatch, handler)
    assets = provider.generate(ImageGenerationRequest(prompt="cup", count=2, ratio="3:2"))
    assert len(assets) == 2
    assert assets[0].content == png
    assert assets[0].metadata["size"] == "24x16"
    assert assets[0].metadata["requested_size"] == "1536x1024"
    assert assets[1].metadata["candidate_index"] == 1


def test_reference_images_use_multipart_edits(monkeypatch, png, provider):
    def handler(request):
        assert request.url.path == "/v1/images/edits"
        assert request.headers["content-type"].startswith("multipart/form-data;")
        assert b'name="image[]"; filename="reference-0.png"' in request.content
        assert b'name="image[]"; filename="reference-1.png"' in request.content
        assert png in request.content
        assert b"gpt-image-2.5-sunburst" in request.content
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png).decode()}]})

    transport(monkeypatch, handler)
    assets = provider.generate(
        ImageGenerationRequest(
            prompt="edit cup",
            reference_images=(png, png),
            reference_media_types=("image/png", "image/png"),
        )
    )
    assert assets[0].media_type == "image/png"


@pytest.mark.parametrize(
    "values",
    [
        {"count": 0},
        {"count": 5},
        {"size": "4K"},
        {"ratio": "9:16"},
        {"size": "1024x1024", "ratio": "3:2"},
        {"watermark": True},
        {"seed": 42},
        {"extra": {"model": "other-model"}},
        {"reference_images": (b"not an image",)},
    ],
)
def test_invalid_or_unsupported_options_never_call_api(monkeypatch, provider, values):
    transport(monkeypatch, lambda request: pytest.fail("Invalid input reached provider"))
    with pytest.raises(ValueError):
        provider.generate(ImageGenerationRequest(prompt="cup", **values))


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {"data": [{"b64_json": "!invalid"}]},
        {"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]},
        {"data": [{"b64_json": ""}]},
        {"error": {"message": "test-only-secret"}},
    ],
)
def test_invalid_results_fail_without_leaking_response(monkeypatch, provider, payload):
    transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ProviderCallError, match="图片结果无效") as error:
        provider.generate(ImageGenerationRequest(prompt="cup"))
    assert "test-only-secret" not in str(error.value)


def test_authentication_failure_is_safe_and_not_retryable(monkeypatch, provider):
    transport(
        monkeypatch,
        lambda request: httpx.Response(
            401,
            json={
                "error": {
                    "message": "Authorization: Bearer test-only-secret",
                    "code": "invalid_api_key",
                }
            },
        ),
    )
    with pytest.raises(ProviderCallError) as error:
        provider.generate(ImageGenerationRequest(prompt="cup"))
    assert error.value.status_code == 401
    assert not error.value.retryable
    assert "test-only-secret" not in str(error.value)


def test_url_download_never_receives_api_key(monkeypatch, png, provider):
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"data": [{"url": "https://8.8.8.8/image.png"}]})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=png)

    transport(monkeypatch, handler)
    assert provider.generate(ImageGenerationRequest(prompt="cup"))[0].content == png
    assert len(seen) == 2


def test_private_result_url_is_rejected(monkeypatch, provider):
    def handler(request):
        assert request.method == "POST"
        return httpx.Response(200, json={"data": [{"url": "https://127.0.0.1/private"}]})

    transport(monkeypatch, handler)
    with pytest.raises(ProviderCallError):
        provider.generate(ImageGenerationRequest(prompt="cup"))


def test_chat_uses_openai_image_adapter(tmp_path):
    settings = replace(
        load_settings(),
        data_dir=tmp_path,
        image_provider="openai_compatible",
        image_base_url="https://models.example/v1",
        image_api_key="test-only-secret",
        image_model="gpt-image-2.5-sunburst",
    )
    assert chat_models.configured(settings, "image")
    assert isinstance(chat_media._image_provider(settings), OpenAICompatibleImageGenerationPort)


def test_environment_requires_own_key(monkeypatch):
    monkeypatch.setenv("CONTENT_IMAGE_PROVIDER", "openai_compatible")
    monkeypatch.setenv("CONTENT_IMAGE_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("CONTENT_IMAGE_MODEL", "gpt-image-2.5-sunburst")
    monkeypatch.delenv("CONTENT_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("CONTENT_IMAGE_API_KEY_FILE", raising=False)
    monkeypatch.setenv("HIAGENT_AK", "old-hiagent-key")
    with pytest.raises(ValueError, match="CONTENT_IMAGE_API_KEY"):
        load_settings()
    monkeypatch.setenv("CONTENT_IMAGE_API_KEY", "test-only-secret")
    assert load_settings().image_provider == "openai_compatible"


def test_configured_size_default_is_used_when_auto_is_not_available(monkeypatch, png):
    provider = OpenAICompatibleImageGenerationPort(
        provider_id="gpt-image",
        base_url="https://models.example/v1",
        api_key="test-only-secret",
        model="gpt-image-2.5-sunburst",
        timeout_seconds=30,
        supported_sizes=("1024x1024",),
    )

    def handler(request):
        assert json.loads(request.content)["size"] == "1024x1024"
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png).decode()}]})

    transport(monkeypatch, handler)
    assert provider.generate(ImageGenerationRequest(prompt="cup"))[0].content == png
