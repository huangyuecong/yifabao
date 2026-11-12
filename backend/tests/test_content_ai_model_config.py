import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from test_content_entry_identity import signed as signed
from test_content_entry_identity import token

from content_service import ai_model_config as config
from content_service import db
from content_service import provider_dependencies as dependencies
from content_service.config import load_settings
from content_service.hiagent_adapters import HiAgentClient
from content_service.models import LocalAccessLock, LocalUser

SECRET = "test-only-new-provider-key"
URL = "/api/v1/ai-models"


def headers(*, manage=True, roles=None):
    return {
        "X-Content-Config-Request": "1",
        "X-Entry-Identity": token(
            subject="entry-user-a" if manage else "entry-user-no-grant",
            claims={
                "agent_keys": ["CONTENT-TEST"],
                "roles": roles or ["employee"],
                "is_admin": False,
                "manageable_agent_keys": ["CONTENT-TEST"] if manage else [],
            }
        ),
    }


def text_payload(revision=0, **values):
    return {
        "provider": "openai_compatible",
        "expected_revision": revision,
        "values": {
            "llm_base_url": "https://models.example/v1",
            "llm_api_key": SECRET,
            "llm_model": "text-new",
            "llm_fallback_model": "",
            **values,
        },
    }


@pytest.fixture
def manager(tmp_path, monkeypatch, signed):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'access.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    with sessions() as session:
        session.add(LocalAccessLock(id=1))
        session.add(LocalUser(
            subject="entry-user-a", display_name="联调用户", department="市场部",
            permissions_json='["models.manage"]', is_project_admin=False,
        ))
        session.commit()
    base = replace(
        load_settings(),
        data_dir=tmp_path,
        llm_provider="openai_compatible",
        llm_base_url="https://old.example/v1",
        llm_api_key="test-only-old-key",
        llm_model="text-old",
        llm_fallback_model="",
        image_provider="mock",
        ai_video_provider="mock",
        speech_provider="mock",
        music_provider="mock",
    )
    monkeypatch.setattr(config, "_base", lambda: base)
    monkeypatch.setattr(config, "video_jobs_active", lambda: False)
    monkeypatch.setattr(dependencies, "get_runtime_settings", lambda: base)
    app = FastAPI()
    app.include_router(config.router)
    with TestClient(app) as client:
        yield client, base
    engine.dispose()
    for factory in (
        dependencies.get_text_provider,
        dependencies.get_image_generation_port,
        dependencies.get_ai_video_generation_port,
        dependencies.get_music_generation_port,
        dependencies.get_speech_synthesis_port,
    ):
        factory.cache_clear()


@pytest.mark.parametrize("roles", [["employee"], ["admin"], ["reviewer"]])
def test_business_role_alone_cannot_read_keys_or_change_models(manager, roles):
    client, base = manager
    identity = headers(manage=False, roles=roles)
    assert client.get(URL + "/access", headers=identity).json()["can_manage"] is False
    assert client.get(URL, headers=identity).status_code == 403
    assert client.put(URL + "/text", json=text_payload(), headers=identity).status_code == 403
    assert (
        client.post(URL + "/text/models", json=text_payload(), headers=identity).status_code == 403
    )
    assert (
        client.post(
            URL + "/text/rollback", json={"expected_revision": 0}, headers=identity
        ).status_code
        == 403
    )
    assert not config.configuration_path(base).exists()


def test_unsigned_and_forged_identity_cannot_manage(manager, monkeypatch):
    client, base = manager
    forged = {
        "X-Roles": "admin",
        "X-Permissions": "content:ai:manage",
        "X-Content-Config-Request": "1",
    }
    assert client.put(URL + "/text", json=text_payload(), headers=forged).status_code == 401
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "development")
    assert client.get(URL + "/access", headers=headers()).json()["can_manage"] is False
    assert client.put(URL + "/text", json=text_payload(), headers=forged).status_code == 403
    assert not config.configuration_path(base).exists()


def test_save_read_runtime_reload_and_rollback(manager):
    client, base = manager
    identity = headers()
    old = dependencies.get_text_provider()
    saved = client.put(URL + "/text", json=text_payload(), headers=identity)
    assert saved.status_code == 200, saved.text
    assert saved.headers["cache-control"] == "private, no-store"
    assert SECRET not in saved.text
    assert "test-only-old-key" not in saved.text
    assert saved.json()["revision"] == 1
    row = saved.json()["configurations"][0]
    assert row["configured_secrets"] == ["llm_api_key"]
    assert row["values"]["llm_model"] == "text-new"
    assert row["can_rollback"]
    assert saved.json()["history"][0]["actor"] == "entry-user-a"
    current = dependencies.get_text_provider()
    assert current.model == "text-new" and current.api_key == SECRET
    assert old.model == "text-old" and old.api_key == "test-only-old-key"
    dependencies.get_text_provider.cache_clear()
    assert dependencies.get_text_provider().api_key == SECRET
    assert SECRET not in client.get(URL, headers=identity).text
    restored = client.post(URL + "/text/rollback", json={"expected_revision": 1}, headers=identity)
    assert restored.status_code == 200, restored.text
    assert dependencies.get_text_provider().model == "text-old"
    assert not restored.json()["configurations"][0]["can_rollback"]
    assert config.read_document(base)["revision"] == 2


def test_old_failure_does_not_mark_new_configuration_unavailable(manager):
    from content_service.provider_health import current_health, record_failure

    client, _ = manager
    old = dependencies.get_text_provider()
    assert client.put(URL + "/text", json=text_payload(), headers=headers()).status_code == 200
    current = dependencies.get_text_provider()
    record_failure(old.status, RuntimeError("old credentials expired"), old.health_configuration)
    assert current_health(current.status, current.health_configuration) is None
    assert current_health(old.status, old.health_configuration).status == "unavailable"


def test_preserve_secret_only_for_same_endpoint_and_adapter(manager):
    client, base = manager
    payload = text_payload(llm_base_url="https://old.example/v1/", llm_api_key="")
    result = client.put(URL + "/text", json=payload, headers=headers())
    assert result.status_code == 200, result.text
    assert config.model_settings(base, "text").llm_api_key == "test-only-old-key"
    payload["expected_revision"] = 1
    payload["values"]["llm_base_url"] = "https://different.example/v1"
    result = client.put(URL + "/text", json=payload, headers=headers())
    assert result.status_code == 422 and "重新填写密钥" in result.text
    payload["provider"] = "volcengine_ark"
    assert client.put(URL + "/text", json=payload, headers=headers()).status_code == 422
    assert config.read_document(base)["revision"] == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(unexpected=SECRET),
        lambda p: p["values"].update(llm_api_key={"bad": SECRET}),
        lambda p: p.update(expected_revision=SECRET),
        lambda p: p["values"].update(data_dir=SECRET),
        lambda p: p["values"].update(llm_base_url="https://user:password@example.com/v1"),
        lambda p: p["values"].update(llm_api_key=SECRET + "\nInjected: value"),
    ],
)
def test_invalid_inputs_never_echo_credentials_or_persist(manager, mutate):
    client, base = manager
    payload = text_payload()
    mutate(payload)
    result = client.put(URL + "/text", json=payload, headers=headers())
    assert result.status_code == 422, result.text
    assert SECRET not in result.text
    assert not config.configuration_path(base).exists()


def test_explicit_request_header_and_revision_required(manager):
    client, base = manager
    identity = headers()
    identity.pop("X-Content-Config-Request")
    assert client.put(URL + "/text", json=text_payload(), headers=identity).status_code == 403
    assert client.put(URL + "/text", json=text_payload(), headers=headers()).status_code == 200
    conflict = client.put(URL + "/text", json=text_payload(), headers=headers())
    assert conflict.status_code == 409
    assert config.read_document(base)["revision"] == 1


def test_atomic_write_failure_leaves_previous_config(manager, monkeypatch):
    client, base = manager
    assert client.put(URL + "/text", json=text_payload(), headers=headers()).status_code == 200
    before = config.configuration_path(base).read_bytes()

    def fail(*args):
        raise OSError("test-only-full-disk")

    monkeypatch.setattr(config.os, "replace", fail)
    result = client.put(
        URL + "/text", json=text_payload(1, llm_model="cannot-save"), headers=headers()
    )
    assert result.status_code == 503
    assert config.configuration_path(base).read_bytes() == before
    assert not list(config.configuration_path(base).parent.glob(".ai-models-*"))


def test_concurrent_updates_do_not_overwrite_each_other(manager):
    client, base = manager
    barrier = Barrier(2)

    def save(model):
        barrier.wait(timeout=10)
        return client.put(
            URL + "/text", json=text_payload(llm_model=model), headers=headers()
        ).status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(save, ["choice-a", "choice-b"])) == [200, 409]
    assert config.read_document(base)["revision"] == 1


def test_rollback_is_per_capability(manager):
    client, base = manager
    assert client.put(URL + "/text", json=text_payload(), headers=headers()).status_code == 200
    speech = {
        "provider": "chat-tts",
        "expected_revision": 1,
        "values": {
            "speech_base_url": "https://speech.example",
            "speech_api_key": "test-speech-key",
            "speech_resource_id": "chat-tts",
            "speech_voice_type": "voice-a",
            "speech_supported_voices": "voice-a,voice-b",
        },
    }
    response = client.put(URL + "/speech", json=speech, headers=headers())
    assert response.status_code == 200, response.text
    response = client.post(URL + "/text/rollback", json={"expected_revision": 2}, headers=headers())
    assert response.status_code == 200, response.text
    assert config.model_settings(base, "speech").speech_api_key == "test-speech-key"
    assert config.model_settings(base, "text").llm_model == "text-old"


def test_model_discovery_uses_unsaved_credentials_without_writing(manager, monkeypatch):
    client, base = manager
    seen = []

    def transport(request):
        seen.append(request)
        return httpx.Response(
            200, json={"data": [{"id": "chosen", "name": "模型"}, {"bad": "row"}]}
        )

    original = httpx.Client
    monkeypatch.setattr(
        config.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(transport), **kwargs),
    )
    result = client.post(URL + "/text/models", json=text_payload(), headers=headers())
    assert result.status_code == 200, result.text
    assert result.json()["models"] == [{"id": "chosen", "name": "模型"}]
    assert str(seen[0].url) == "https://models.example/v1/models"
    assert seen[0].headers["authorization"] == f"Bearer {SECRET}"
    assert not config.configuration_path(base).exists()
    assert SECRET not in result.text


@pytest.mark.parametrize(
    "status,body",
    [(401, SECRET), (302, SECRET), (200, "[1]"), (200, "x" * 2_000_001)],
    ids=["unauthorized", "redirect", "invalid-json-shape", "oversized"],
)
def test_discovery_failure_is_bounded_and_redacted(manager, monkeypatch, status, body):
    client, base = manager
    original = httpx.Client
    seen = []

    def transport(request):
        seen.append(request)
        return httpx.Response(status, text=body, headers={"location": "https://redirect.example"})

    monkeypatch.setattr(
        config.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(transport), **kwargs),
    )
    result = client.post(URL + "/text/models", json=text_payload(), headers=headers())
    assert result.status_code == 422
    assert len(seen) == 1 and SECRET not in result.text
    assert not config.configuration_path(base).exists()


def test_hiagent_credentials_are_isolated_per_request_and_gateway(monkeypatch):
    from content_service import hiagent_quickstart as quickstart

    barrier = Barrier(2)
    seen = []

    def post(url, **kwargs):
        barrier.wait(timeout=10)
        seen.append((url, kwargs))
        return httpx.Response(200, json={"Result": {"Items": []}})

    monkeypatch.setattr(quickstart.requests, "post", post)
    clients = [
        HiAgentClient(
            access_key=f"test-ak-{i}",
            secret_key=f"test-sk-{i}",
            workspace_id=f"space-{i}",
            host=f"host-{i}.example",
            port=443,
        )
        for i in range(2)
    ]
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda client: client.list_granted_models(), clients))
    for url, kwargs in seen:
        index = 0 if "host-0" in url else 1
        assert f"test-ak-{index}/" in kwargs["headers"]["Authorization"]
        assert kwargs["headers"]["WorkspaceID"] == f"space-{index}"
        assert kwargs["verify"] is True
    assert quickstart._client_configuration.get() is None


def test_hiagent_config_updates_one_capability_and_uses_parameters(manager, monkeypatch):
    client, base = manager
    monkeypatch.setattr(
        HiAgentClient, "list_granted_models", lambda self: [{"ID": "video-new", "Granted": True}]
    )
    payload = {
        "provider": "hiagent",
        "expected_revision": 0,
        "values": {
            "hiagent_base_url": "https://gateway.example:30040",
            "hiagent_access_key": "test-ak",
            "hiagent_secret_key": "test-sk",
            "hiagent_workspace_id": "space-new",
            "ai_video_model": "video-new",
            "hiagent_video_fallback_model": "",
            "ai_video_supported_durations": "5,10",
            "ai_video_supported_ratios": "16:9",
            "ai_video_supported_resolutions": "720p",
        },
    }
    response = client.put(URL + "/video", json=payload, headers=headers())
    assert response.status_code == 200, response.text
    provider = dependencies.get_ai_video_generation_port()
    assert provider.pro_model == "video-new" and provider.fast_model == "video-new"
    assert provider.client.access_key == "test-ak" and provider.client.secret_key == "test-sk"
    assert provider.client.host == "gateway.example:30040"
    assert provider.capabilities["resolutions"] == ["720p"]
    assert provider.capabilities["durations"] == [5, 10]
    assert config.model_settings(base, "image") == base
    assert "test-ak" not in response.text and "test-sk" not in response.text


def test_broken_configuration_fails_closed_without_overwriting(manager):
    client, base = manager
    path = config.configuration_path(base)
    path.parent.mkdir(parents=True)
    raw = json.dumps({"revision": 4, "overrides": {"text": ["bad"]}, "previous": {}, "events": []})
    path.write_text(raw)
    result = client.get(URL, headers=headers())
    assert result.status_code == 503
    assert path.read_text() == raw


def test_pending_video_blocks_save_and_rollback(manager, monkeypatch):
    client, base = manager
    monkeypatch.setattr(config, "video_jobs_active", lambda: True)
    for method, suffix, payload in [
        ("put", "", text_payload()),
        ("post", "/rollback", {"expected_revision": 0}),
    ]:
        result = getattr(client, method)(URL + "/video" + suffix, json=payload, headers=headers())
        assert result.status_code == 409 and "视频正在排队或生成" in result.text
    assert not config.configuration_path(base).exists()


def test_model_routes_are_registered_and_protected_in_real_app(manager, content_client):
    result = content_client.get(URL + "/access", headers=headers())
    assert result.status_code == 200 and result.json()["can_manage"]
    result = content_client.get(URL, headers=headers(manage=False))
    assert result.status_code == 403
    result = content_client.put(URL + "/text", json=text_payload(), headers=headers())
    assert result.status_code == 200 and SECRET not in result.text


def test_active_video_query_only_blocks_unfinished_video_jobs(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from content_service import db
    from content_service.models import Job

    engine = create_engine("sqlite://")
    Job.__table__.create(engine)
    sessions = sessionmaker(engine)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    try:
        with sessions() as session:
            session.add(
                Job(
                    id="finished",
                    task_id="task",
                    owner_id="test",
                    kind="ai_video_generation",
                    status="succeeded",
                )
            )
            session.add(
                Job(
                    id="unrelated",
                    task_id="task",
                    owner_id="test",
                    kind="video_project_render",
                    status="running",
                )
            )
            session.commit()
        assert not config.video_jobs_active()
        with sessions() as session:
            session.add(
                Job(
                    id="pending",
                    task_id="task",
                    owner_id="test",
                    kind="video_project_ai_generation",
                    status="queued",
                )
            )
            session.commit()
        assert config.video_jobs_active()
    finally:
        engine.dispose()


def test_primary_without_fallback_keeps_requested_resolution(monkeypatch):
    from content_service.hiagent_adapters import HiAgentVideoGenerationPort
    from content_service.providers import AiVideoGenerationRequest

    client = HiAgentClient(access_key="test-ak", secret_key="test-sk", workspace_id="test-space")
    monkeypatch.setattr(client, "list_granted_models", lambda: [{"ID": "primary"}])
    seen = []

    def request(action, payload):
        seen.append(payload)
        return {"TaskID": "task-test"}

    monkeypatch.setattr(client, "_request", request)
    provider = HiAgentVideoGenerationPort(
        client=client, pro_model="primary", fast_model="primary", resolutions=("1080p",)
    )
    provider.submit(
        AiVideoGenerationRequest(
            prompt="test scene", duration_seconds=5, ratio="16:9", resolution="1080p"
        )
    )
    assert seen[0]["Model"] == "primary" and seen[0]["Resolution"] == "1080p"


def test_openai_image_save_selects_correct_runtime_adapter(manager):
    client, base = manager
    response = client.put(URL + "/image", headers=headers(), json={
        "provider": "openai_compatible", "expected_revision": 0,
        "values": {
            "image_base_url": "https://models.example/v1", "image_api_key": SECRET,
            "image_model": "gpt-image-2.5-sunburst",
            "image_supported_sizes": [], "image_supported_ratios": [],
        },
    })
    assert response.status_code == 200
    assert SECRET not in response.text
    provider = dependencies.get_image_generation_port()
    assert provider.status.name == "openai-compatible-image"
    assert provider.status.model == "gpt-image-2.5-sunburst"
    assert "auto" in provider.capabilities["sizes"]
