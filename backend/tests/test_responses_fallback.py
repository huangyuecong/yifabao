"""Chat requests fall back to the Responses API when /chat/completions is down."""

import json
from dataclasses import replace

import httpx
import pytest

from content_service import chat_provider, llm_adapters, responses_fallback
from content_service.config import load_settings
from content_service.llm_adapters import ProviderCallError

RESPONSE = {"output": [{"type": "reasoning"}, {"type": "message", "content": [
    {"type": "output_text", "text": '{"title": "标题"}'}]}], "usage": {"input_tokens": 3, "output_tokens": 4}}


@pytest.fixture(autouse=True)
def fresh_preference(monkeypatch):
    monkeypatch.setattr(responses_fallback, "_preferred", {})


def mock_client(monkeypatch, module, handler):
    real = httpx.Client
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: real(transport=httpx.MockTransport(handler)))


def test_payload_conversion_keeps_system_images_and_history():
    payload = responses_fallback.to_responses_payload({"model": "m", "max_tokens": 50, "messages": [
        {"role": "system", "content": "规则"},
        {"role": "user", "content": [{"type": "text", "text": "看图"},
                                     {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA"}}]},
        {"role": "assistant", "content": "上一轮"},
        {"role": "user", "content": "继续"},
    ]})
    assert payload["instructions"] == "规则" and payload["max_output_tokens"] == 50
    assert payload["input"][0]["content"] == [{"type": "input_text", "text": "看图"},
                                              {"type": "input_image", "image_url": "data:image/jpeg;base64,AA"}]
    assert payload["input"][1]["content"] == [{"type": "output_text", "text": "上一轮"}]


def test_call_chat_falls_back_on_gateway_timeout_then_prefers_responses(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(524, text="<html>timeout</html>")
        assert json.loads(request.content)["instructions"] == "系统"
        return httpx.Response(200, json=RESPONSE)

    mock_client(monkeypatch, chat_provider, handler)
    settings = replace(load_settings(), llm_base_url="https://relay.example/v1", llm_api_key="k")
    messages = [{"role": "system", "content": "系统"}, {"role": "user", "content": "你好"}]
    assert chat_provider.call_chat(settings, {"model": "m"}, messages) == '{"title": "标题"}'
    assert chat_provider.call_chat(settings, {"model": "m"}, messages) == '{"title": "标题"}'
    assert calls == ["/v1/chat/completions", "/v1/responses", "/v1/responses"]


def test_call_chat_does_not_fall_back_on_auth_errors(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    mock_client(monkeypatch, chat_provider, handler)
    settings = replace(load_settings(), llm_base_url="https://relay.example/v1", llm_api_key="k")
    with pytest.raises(ProviderCallError, match="密钥"):
        chat_provider.call_chat(settings, {"model": "m"}, [{"role": "user", "content": "你好"}])
    assert calls == ["/v1/chat/completions"]


def test_text_provider_falls_back_on_server_error(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json=RESPONSE)

    mock_client(monkeypatch, llm_adapters, handler)
    provider = llm_adapters.OpenAICompatibleTextProvider(
        provider_id="relay", base_url="https://relay.example/v1", api_key="k", model="m",
        timeout_seconds=30, max_output_tokens=100, temperature=0.5, max_attempts=1)
    result, _, usage = provider._request_json("copies", {"task": {}}, lambda value: value)
    assert result == {"title": "标题"} and usage.get("completion_tokens") == 4
    assert calls == ["/v1/chat/completions", "/v1/responses"]
