import json

import httpx
import pytest

from content_service.llm_adapters import (
    FailoverTextProvider,
    OpenAICompatibleTextProvider,
    ProviderCallError,
    VolcengineArkTextGenerationPort,
    _safe_usage,
    _source_reference_ids,
    _strip_code_fence,
)
from content_service.providers import (
    ContentGenerationInput,
    ProviderStatus,
    TextGenerationResult,
)


def _request() -> ContentGenerationInput:
    return ContentGenerationInput(
        task_name="官网资料文案",
        topic="臻品文化",
        product="雅酿",
        keywords=("臻品文化",),
        audience="成年消费者",
        content_type="品牌宣传文案",
        style="正式、克制",
        length="短",
        requirements="只使用可核验资料",
        instruction="",
        source_content={"source_reference_ids": ["doc-1", 2, None]},
    )


def test_strip_code_fence_and_usage_helpers():
    assert _strip_code_fence('{"ok": true}') == '{"ok": true}'
    assert _strip_code_fence('```json\n{"ok": true}\n```') == '{"ok": true}'
    assert _safe_usage("not-a-dict") == {}
    assert _safe_usage({"prompt_tokens": 3, "note": "x"}) == {"prompt_tokens": 3}
    assert _source_reference_ids(_request()) == {"doc-1"}
    empty = ContentGenerationInput(
        task_name="t",
        topic="t",
        product="p",
        keywords=(),
        audience="a",
        content_type="c",
        style="s",
        length="短",
        requirements="",
        instruction="",
        source_content="bad",
    )
    assert _source_reference_ids(empty) == set()


class _JsonErrorResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        raise ValueError("not json")


class _TransportErrorClient:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def post(self, *_args, **_kwargs):
        raise httpx.ConnectError("offline")


class _JsonErrorClient:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def post(self, *_args, **_kwargs):
        return _JsonErrorResponse()


def _provider(**changes) -> OpenAICompatibleTextProvider:
    values = {
        "provider_id": "configured-provider",
        "base_url": "https://model.example/v1",
        "api_key": "secret-value",
        "model": "configured-model",
        "timeout_seconds": 10,
        "max_output_tokens": 80,
        "temperature": 0.1,
        "max_attempts": 1,
    }
    values.update(changes)
    return OpenAICompatibleTextProvider(**values)


def test_text_adapter_wraps_transport_and_invalid_json(monkeypatch):
    monkeypatch.setattr(
        "content_service.llm_adapters.httpx.Client", lambda **_: _TransportErrorClient()
    )
    with pytest.raises(ProviderCallError, match="ConnectError"):
        _provider().copies(_request(), 1)

    monkeypatch.setattr("content_service.llm_adapters.httpx.Client", lambda **_: _JsonErrorClient())
    with pytest.raises(ProviderCallError, match="不是 JSON"):
        _provider().copies(_request(), 1)


def test_text_adapter_accepts_fenced_json(monkeypatch):
    payload = {
        "choices": [
            {
                "message": {
                    "content": "```json\n"
                    + json.dumps(
                        {
                            "candidates": [
                                {
                                    "title": "标题",
                                    "summary": "摘要",
                                    "body": "正文",
                                    "source_ids": [],
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                    + "\n```"
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 6},
    }

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def post(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr("content_service.llm_adapters.httpx.Client", lambda **_: _Client())
    result = _provider().copies(_request(), 1)
    assert result.content[0]["title"] == "标题"
    assert result.usage == {"prompt_tokens": 4, "completion_tokens": 6}


def test_failover_retries_all_operations_then_both_fail():
    class _Primary:
        model = "primary"
        status = ProviderStatus("p", "primary", "text-generation", "api", True, True, "primary")
        capabilities = {"operations": []}

        def topics(self, request, count):
            raise ProviderCallError("busy", retryable=True)

        def copies(self, request, count):
            raise ProviderCallError("busy", retryable=True)

        def transform_copy(self, request, operation):
            raise ProviderCallError("busy", retryable=True)

        def storyboard(self, request, count):
            raise ProviderCallError("busy", retryable=True)

        def timeline_edits(self, request):
            raise ProviderCallError("busy", retryable=True)

    class _Fallback(_Primary):
        model = "fallback"
        status = ProviderStatus("f", "fallback", "text-generation", "api", True, True, "fallback")

        def topics(self, request, count):
            return TextGenerationResult([{"title": "t"}], "f", "fallback")

        def copies(self, request, count):
            return TextGenerationResult([{"title": "c"}], "f", "fallback")

        def transform_copy(self, request, operation):
            return TextGenerationResult({"title": operation}, "f", "fallback")

        def storyboard(self, request, count):
            return TextGenerationResult([{"shot": 1}], "f", "fallback")

        def timeline_edits(self, request):
            return TextGenerationResult({"edits": []}, "f", "fallback")

        def failing_topics(self, request, count):
            raise ProviderCallError("down", retryable=True, code="provider_model_unavailable")

    provider = FailoverTextProvider(_Primary(), _Fallback())
    request = _request()
    assert provider.topics(request, 1).content[0]["title"] == "t"
    assert provider.copies(request, 1).content[0]["title"] == "c"
    assert provider.transform_copy(request, "rewrite").content["title"] == "rewrite"
    assert provider.storyboard(request, 1).content[0]["shot"] == 1
    assert provider.timeline_edits(request).content == {"edits": []}

    class _Dead(_Fallback):
        def copies(self, request, count):
            raise ProviderCallError("down", retryable=True, code="provider_model_unavailable")

    dead = FailoverTextProvider(_Primary(), _Dead())
    with pytest.raises(ProviderCallError, match="兜底模型"):
        dead.copies(request, 1)


def test_volcengine_port_keeps_ark_identity():
    port = VolcengineArkTextGenerationPort(
        provider_id="volcengine-ark",
        base_url="https://ark.example/api/v3",
        api_key="secret",
        model="doubao-test",
        timeout_seconds=5,
        max_output_tokens=100,
        temperature=0.1,
        max_attempts=1,
    )
    assert port.status.name == "volcengine-ark-text"
    assert port._request_overrides["reasoning_effort"] == "minimal"
