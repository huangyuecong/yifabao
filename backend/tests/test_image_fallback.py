"""Image generation falls through to the next model the key can use."""

import pytest

from content_service import image_fallback
from content_service.image_fallback import FallbackImageGenerationPort
from content_service.llm_adapters import ProviderCallError
from content_service.providers import GeneratedAsset, ImageGenerationRequest, ProviderStatus


class Port:
    def __init__(self, model, error=None):
        self.status = ProviderStatus(provider_id="test", name="test", capability="image", mode="external",
                                     external=True, enabled=True, model=model)
        self.capabilities = {"model": model}
        self.error = error
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        if self.error:
            raise self.error
        return [GeneratedAsset(content=b"x", media_type="image/png", extension="png",
                               provider_id="test", model=self.status.model)]


REQUEST = ImageGenerationRequest(prompt="商品图")


def busy():
    return ProviderCallError("test 返回 HTTP 502：No available compatible accounts", provider_status_code=502)


def test_falls_back_and_reports_the_model_that_worked():
    first, second = Port("gpt-image-2.5-sunburst", busy()), Port("gpt-image-2.5-flare")
    port = FallbackImageGenerationPort([first, second])
    assert port.status.model == "gpt-image-2.5-sunburst"
    assert port.generate(REQUEST)[0].model == "gpt-image-2.5-flare"
    # The next image starts from the model that worked instead of waiting on the broken one.
    port.generate(REQUEST)
    assert first.calls == 1 and second.calls == 2


def test_sticky_choice_expires_and_primary_is_retried(monkeypatch):
    first, second = Port("a", busy()), Port("b")
    port = FallbackImageGenerationPort([first, second])
    port.generate(REQUEST)
    monkeypatch.setattr(image_fallback, "STICKY_SECONDS", 0)
    first.error = None
    assert port.generate(REQUEST)[0].model == "a"


def test_auth_errors_do_not_try_other_models():
    first = Port("a", ProviderCallError("bad key", provider_status_code=401))
    second = Port("b")
    with pytest.raises(ProviderCallError, match="bad key"):
        FallbackImageGenerationPort([first, second]).generate(REQUEST)
    assert second.calls == 0


def test_all_failures_name_every_model_tried():
    port = FallbackImageGenerationPort([Port("a", busy()), Port("b", busy())])
    with pytest.raises(ProviderCallError, match="a、b") as caught:
        port.generate(REQUEST)
    assert caught.value.code == "provider_unavailable"


def test_fallback_models_config_field_round_trips(tmp_path, monkeypatch):
    from content_service import ai_model_config
    from content_service.config import load_settings

    base = load_settings()
    fields = {field.key: field.kind for field in ai_model_config.config_fields("image", "openai_compatible")}
    assert fields["image_fallback_models"] == "models"
    assert "image_fallback_models" not in {field.key for field in ai_model_config.config_fields("image", "hiagent")}
    values = ai_model_config.validated_values(base, "image", "openai_compatible", {
        "image_base_url": "https://api.example.com/v1", "image_api_key": "k", "image_model": "gpt-image-2",
        "image_fallback_models": "gpt-image-2.5-flare，gpt-image-1.5",
    })
    assert values["image_fallback_models"] == ["gpt-image-2.5-flare", "gpt-image-1.5"]
    applied = ai_model_config._apply(base, "image", values)
    assert applied.image_fallback_models == ("gpt-image-2.5-flare", "gpt-image-1.5")
    with pytest.raises(ValueError, match="主模型"):
        ai_model_config.validated_values(base, "image", "openai_compatible", {
            "image_base_url": "https://api.example.com/v1", "image_api_key": "k", "image_model": "gpt-image-2",
            "image_fallback_models": ["gpt-image-2"],
        })


def test_factory_chains_configured_models(monkeypatch):
    from dataclasses import replace

    from content_service import provider_dependencies
    from content_service.config import load_settings

    settings = replace(load_settings(), image_provider="openai_compatible", image_base_url="https://api.example.com/v1",
                       image_api_key="k", image_model="gpt-image-2", image_fallback_models=("gpt-image-1.5",))
    port = provider_dependencies.build_image_generation_port(settings)
    assert isinstance(port, FallbackImageGenerationPort)
    assert port.models == ["gpt-image-2", "gpt-image-1.5"]
    single = provider_dependencies.build_image_generation_port(replace(settings, image_fallback_models=()))
    assert not isinstance(single, FallbackImageGenerationPort)


def test_responses_adapter_calls_image_tool_with_references(monkeypatch):
    import base64
    import io
    import json

    import httpx
    from PIL import Image

    from content_service import responses_image_adapter

    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(buffer, format="PNG")
    image_b64 = base64.b64encode(buffer.getvalue()).decode()
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"output": [
            {"type": "message"}, {"type": "image_generation_call", "result": image_b64, "quality": "high"}]})

    real_client = httpx.Client
    monkeypatch.setattr(responses_image_adapter.httpx, "Client",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler)))
    port = responses_image_adapter.OpenAIResponsesImageGenerationPort(
        driver_model="gpt-6.1-sol", provider_id="relay", base_url="https://relay.example/v1", api_key="k",
        model="gpt-image-2", timeout_seconds=30)
    assert port.endpoint == "https://relay.example/v1/responses"
    assets = port.generate(ImageGenerationRequest(prompt="商品场景", count=2, size="1024x1536",
                                                  reference_images=(buffer.getvalue(),)))
    assert len(assets) == 2 and assets[0].model == "gpt-image-2" and assets[0].media_type == "image/png"
    body = seen[0]
    assert body["model"] == "gpt-6.1-sol" and body["tool_choice"] == {"type": "image_generation"}
    assert body["tools"][0] == {"type": "image_generation", "model": "gpt-image-2", "quality": "high", "size": "1024x1536"}
    assert body["input"][0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,")


def test_responses_adapter_failure_falls_through(monkeypatch):
    import httpx

    from content_service import responses_image_adapter

    real_client = httpx.Client
    monkeypatch.setattr(responses_image_adapter.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(502, text="<html>Bad gateway</html>"))))
    port = responses_image_adapter.OpenAIResponsesImageGenerationPort(
        driver_model="gpt-6.1-sol", provider_id="relay", base_url="https://relay.example", api_key="k",
        model="gpt-image-2", timeout_seconds=30)
    with pytest.raises(ProviderCallError) as caught:
        port.generate(REQUEST)
    assert caught.value.provider_status_code == 502
