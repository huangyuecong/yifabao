"""Streaming replies (SSE) and function-calling tool routing for AI chat."""

import json
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from content_service import chat_generation, chat_live, chat_provider, chat_routes, chat_tools
from content_service.config import load_settings

URL = "/api/v1/chat"
OWNER = {"X-User-Id": "stream-user", "X-User-Name": "stream-user", "X-Roles": "admin"}
COMPLETIONS = "https://models.example/v1/chat/completions"


@pytest.fixture
def settings(tmp_path):
    return replace(
        load_settings(),
        data_dir=tmp_path,
        llm_provider="openai_compatible",
        llm_base_url="https://models.example/v1",
        llm_api_key="private-test-key",
        llm_model="text-model",
    )


@pytest.fixture
def chat_client(settings, monkeypatch):
    monkeypatch.setattr(chat_generation._executor, "submit", lambda function, *args: function(*args))
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "development")
    monkeypatch.setattr(chat_routes, "get_runtime_settings", lambda: settings)
    app = FastAPI()
    app.include_router(chat_routes.router)
    with TestClient(app) as client:
        yield client


def sse(*texts, finish="stop"):
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": None}]})
        for text in texts
    ]
    lines.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}))
    lines.append("data: [DONE]")
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content="\n\n".join(lines).encode()
    )


def tool_call(name, arguments="{}"):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "call-1", "type": "function", "function": {"name": name, "arguments": arguments}}
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )


def provider(monkeypatch, handle):
    original = httpx.Client
    monkeypatch.setattr(
        chat_provider.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )


def test_stream_chat_reports_each_fragment(settings, monkeypatch):
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        return sse("你好", "，雅酿")

    provider(monkeypatch, handle)
    seen = []
    reply = chat_provider.stream_chat(settings, {"model": "text-model"}, [], seen.append)
    assert reply == "你好，雅酿"
    assert seen == ["你好", "，雅酿"]
    assert payloads[0]["stream"] is True


def test_stream_chat_accepts_a_provider_that_ignores_streaming(settings, monkeypatch):
    provider(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": "完整回复"}, "finish_reason": "stop"}]}
        ),
    )
    seen = []
    assert chat_provider.stream_chat(settings, {"model": "m"}, [], seen.append) == "完整回复"
    assert seen == ["完整回复"]


@pytest.mark.parametrize("response", [httpx.Response(503), sse("半句", finish="length")])
def test_stream_chat_failures_raise_for_the_fallback(settings, monkeypatch, response):
    provider(monkeypatch, lambda request: response)
    with pytest.raises(chat_provider.ProviderCallError):
        chat_provider.stream_chat(settings, {"model": "m"}, [], lambda text: None)


def test_live_events_replay_from_the_start_and_end():
    chat_live.start("reply-1", "owner", "conversation")
    chat_live.stage("reply-1", "正在输出")
    chat_live.delta("reply-1", "第一段")
    chat_live.end("reply-1")
    events = list(chat_live.follow("reply-1", "owner", "conversation"))
    assert [kind for kind, _ in events] == ["reset", "stage", "delta", "end"]
    assert events[2][1] == {"text": "第一段"}


def test_live_events_are_private_to_their_owner():
    chat_live.start("reply-2", "owner", "conversation")
    assert list(chat_live.follow("reply-2", "someone-else", "conversation")) == [("end", {"live": False})]


def test_reply_is_streamed_routed_by_function_calling_and_saved(chat_client, monkeypatch):
    monkeypatch.setenv("CONTENT_CHAT_STREAMING", "true")
    monkeypatch.setenv("CONTENT_CHAT_TOOL_ROUTING", "true")
    requests = []
    streamed = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(request.url) == COMPLETIONS
        if "tools" in body:
            assert body["tool_choice"] == "required"
            assert {tool["function"]["name"] for tool in body["tools"]} == set(chat_tools.TOOL_LABELS)
            return tool_call("answer_in_chat")
        return sse("雅酿", "源自产区")

    provider(monkeypatch, handle)
    original_delta = chat_live.delta
    monkeypatch.setattr(chat_live, "delta", lambda rid, text: (streamed.append(text), original_delta(rid, text)))
    cid = chat_client.post(URL + "/conversations", json={}, headers=OWNER).json()["id"]
    response = chat_client.post(
        URL + f"/conversations/{cid}/messages", json={"content": "介绍一下雅酿"}, headers=OWNER
    )
    assert response.status_code == 200, response.text
    messages = response.json()["messages"]
    assert messages[0]["routing"] == {"output": "text", "tool": "answer_in_chat", "method": "function_calling"}
    assert messages[-1]["content"] == "雅酿源自产区"
    assert streamed == ["雅酿", "源自产区"]
    assert [("tools" in body, body["stream"]) for body in requests] == [(True, False), (False, True)]
    events = chat_client.get(
        URL + f"/conversations/{cid}/messages/{messages[-1]['id']}/stream", headers=OWNER
    )
    assert events.headers["content-type"].startswith("text/event-stream")
    assert "event: stage" in events.text and "函数调用" in events.text
    assert 'data: {"text": "雅酿"}' in events.text and "event: end" in events.text


def test_function_calling_chooses_a_document_format(chat_client, monkeypatch):
    monkeypatch.setenv("CONTENT_CHAT_TOOL_ROUTING", "true")

    def handle(request):
        body = json.loads(request.content)
        if "tools" in body:
            return tool_call("create_document", '{"format": "pdf"}')
        return httpx.Response(200, json={"choices": [{"message": {"content": "# 方案\n\n正文"}, "finish_reason": "stop"}]})

    provider(monkeypatch, handle)
    cid = chat_client.post(URL + "/conversations", json={}, headers=OWNER).json()["id"]
    messages = chat_client.post(
        URL + f"/conversations/{cid}/messages", json={"content": "整理成一份发布方案"}, headers=OWNER
    ).json()["messages"]
    assert messages[0]["routing"]["tool"] == "create_document"
    assert messages[-1]["artifacts"][0]["kind"] == "pdf"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": {"message": "tools not supported"}}),
        httpx.Response(200, json={"choices": [{"message": {"content": "直接回答"}}]}),
        tool_call("delete_everything"),
    ],
)
def test_routing_falls_back_to_rules(chat_client, monkeypatch, response):
    monkeypatch.setenv("CONTENT_CHAT_TOOL_ROUTING", "true")

    def handle(request):
        if "tools" in json.loads(request.content):
            return response
        return httpx.Response(200, json={"choices": [{"message": {"content": "规则回复"}, "finish_reason": "stop"}]})

    provider(monkeypatch, handle)
    cid = chat_client.post(URL + "/conversations", json={}, headers=OWNER).json()["id"]
    messages = chat_client.post(
        URL + f"/conversations/{cid}/messages", json={"content": "你好"}, headers=OWNER
    ).json()["messages"]
    assert messages[0]["routing"] == {"method": "rules", "tool": "answer_in_chat"}
    assert messages[-1]["content"] == "规则回复"


def test_stream_failure_falls_back_to_the_complete_reply(chat_client, monkeypatch):
    monkeypatch.setenv("CONTENT_CHAT_STREAMING", "true")

    def handle(request):
        if json.loads(request.content)["stream"]:
            return httpx.Response(502)
        return httpx.Response(200, json={"choices": [{"message": {"content": "完整回复"}, "finish_reason": "stop"}]})

    provider(monkeypatch, handle)
    cid = chat_client.post(URL + "/conversations", json={}, headers=OWNER).json()["id"]
    messages = chat_client.post(
        URL + f"/conversations/{cid}/messages", json={"content": "你好"}, headers=OWNER
    ).json()["messages"]
    assert messages[-1]["content"] == "完整回复"
