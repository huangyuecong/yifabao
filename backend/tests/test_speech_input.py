"""Voice input for AI chat: optional server transcription through /audio/transcriptions."""

from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from content_service import chat_routes, speech_input
from content_service.config import load_settings

HEADERS = {"X-User-Id": "alice", "X-User-Name": "Alice", "X-Roles": "admin"}
REAL_CLIENT = httpx.Client


@pytest.fixture
def chat(tmp_path, monkeypatch):
    settings = replace(load_settings(), data_dir=tmp_path, llm_provider="openai_compatible",
                       llm_base_url="https://models.example/v1", llm_api_key="private-test-key", llm_model="text-model")
    monkeypatch.setattr(chat_routes, "get_runtime_settings", lambda: settings)
    monkeypatch.delenv("CONTENT_ASR_MODEL", raising=False)
    app = FastAPI()
    app.include_router(chat_routes.router)
    with TestClient(app) as client:
        yield client


def upstream(monkeypatch, handler):
    requests = []

    def handle(request):
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(speech_input.httpx, "Client",
                        lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handle), **kwargs))
    return requests


def send(chat, content=b"\x1aE\xdf\xa3 fake webm", content_type="audio/webm;codecs=opus"):
    return chat.post("/api/v1/chat/transcriptions", headers=HEADERS,
                     files={"file": ("speech.webm", content, content_type)})


def test_off_until_a_recognition_model_is_configured(chat):
    assert chat.get("/api/v1/chat/models", headers=HEADERS).json()["speech_input"] is False
    response = send(chat)
    assert response.status_code == 503 and "CONTENT_ASR_MODEL" in response.json()["detail"]


def test_recording_is_transcribed_by_the_configured_model(chat, monkeypatch):
    monkeypatch.setenv("CONTENT_ASR_MODEL", "asr-model")
    requests = upstream(monkeypatch, lambda request: httpx.Response(200, json={"text": " 查看待审核的内容 "}))
    assert chat.get("/api/v1/chat/models", headers=HEADERS).json()["speech_input"] is True

    response = send(chat)

    assert response.status_code == 200 and response.json() == {"text": "查看待审核的内容"}
    request = requests[0]
    assert str(request.url) == "https://models.example/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer private-test-key"
    body = request.content
    assert b'name="model"' in body and b"asr-model" in body and b'name="language"' in body
    assert b"fake webm" in body and b"audio/webm" in body


def test_endpoint_and_key_can_be_set_separately(chat, monkeypatch):
    monkeypatch.setenv("CONTENT_ASR_MODEL", "asr-model")
    monkeypatch.setenv("CONTENT_ASR_BASE_URL", "https://speech.example")
    monkeypatch.setenv("CONTENT_ASR_API_KEY", "speech-key")
    requests = upstream(monkeypatch, lambda request: httpx.Response(200, json={"text": "好"}))
    assert send(chat).json() == {"text": "好"}
    assert str(requests[0].url) == "https://speech.example/v1/audio/transcriptions"
    assert requests[0].headers["authorization"] == "Bearer speech-key"


@pytest.mark.parametrize(("content", "content_type", "status"), [
    (b"", "audio/webm", 422),
    (b"not audio", "text/plain", 422),
    (b"x" * (speech_input.MAX_AUDIO_BYTES + 1), "audio/webm", 413),
], ids=["empty", "not-audio", "too-large"])
def test_rejects_bad_recordings_before_calling_the_model(chat, monkeypatch, content, content_type, status):
    monkeypatch.setenv("CONTENT_ASR_MODEL", "asr-model")
    requests = upstream(monkeypatch, lambda request: httpx.Response(200, json={"text": "不应调用"}))
    assert send(chat, content, content_type).status_code == status
    assert requests == []


@pytest.mark.parametrize("handler", [
    lambda request: httpx.Response(500, json={"error": "busy"}),
    lambda request: httpx.Response(200, content=b"not json"),
    lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")),
])
def test_upstream_failures_become_a_friendly_error(chat, monkeypatch, handler):
    monkeypatch.setenv("CONTENT_ASR_MODEL", "asr-model")
    upstream(monkeypatch, handler)
    response = send(chat)
    assert response.status_code == 502 and "语音识别" in response.json()["detail"]
