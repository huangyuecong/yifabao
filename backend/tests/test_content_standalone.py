"""Local standalone model management keeps enterprise auth and secret storage intact."""

import pytest
from fastapi.testclient import TestClient
from test_content_ai_model_config import SECRET, URL, headers, text_payload
from test_content_ai_model_config import manager as manager
from test_content_entry_identity import signed as signed

from agent_core import Identity
from content_service import access_control, access_routes
from content_service import ai_model_config as config
from content_service import provider_dependencies as dependencies


@pytest.fixture
def local_manager(manager, monkeypatch):
    existing, base = manager
    monkeypatch.setenv("CONTENT_STANDALONE_MODE", "true")
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "development")
    existing.app.include_router(access_routes.router)
    with TestClient(existing.app, base_url="http://127.0.0.1:8002", client=("127.0.0.1", 43210)) as client:
        yield client, base


def test_local_without_login_can_manage_models_and_preserve_existing_key(local_manager):
    client, base = local_manager
    access = client.get(URL + "/access")
    assert access.status_code == 200
    assert access.json() == {"can_manage": True, "authentication_ready": True, "message": ""}
    me = client.get("/api/v1/access/me").json()
    assert me["subject"] == "dev-user"
    assert set(me["permissions"]) == access_control.PERMISSIONS
    assert me["authentication_ready"] and not me["can_manage_users"]
    assert not me["is_project_admin"]
    original = client.get(URL)
    assert original.status_code == 200 and "test-only-old-key" not in original.text
    payload = text_payload(llm_base_url="https://old.example/v1", llm_api_key="", llm_model="local-new")
    changed = client.put(URL + "/text", json=payload, headers={"X-Content-Config-Request": "1"})
    assert changed.status_code == 200, changed.text
    assert "test-only-old-key" not in changed.text
    runtime = dependencies.get_text_provider()
    assert runtime.model == "local-new"
    assert runtime.api_key == "test-only-old-key"
    assert config.read_document(base)["events"][-1]["actor"] == "dev-user"


def test_local_identity_headers_cannot_select_other_owner(local_manager):
    client, _ = local_manager
    result = client.get("/api/v1/access/me", headers={
        "X-User-Id": "someone-else", "X-Roles": "", "X-Entry-Identity": "stale-token",
    })
    assert result.status_code == 200
    assert result.json()["subject"] == "dev-user"
    assert result.json()["display_name"] == "本机用户"


def test_local_new_key_is_write_only_and_revision_is_required(local_manager):
    client, base = local_manager
    assert client.put(URL + "/text", json=text_payload()).status_code == 403
    write_headers = {"X-Content-Config-Request": "1", "Origin": "http://127.0.0.1:8200"}
    saved = client.put(URL + "/text", json=text_payload(), headers=write_headers)
    assert saved.status_code == 200 and SECRET not in saved.text
    assert config.model_settings(base, "text").llm_api_key == SECRET
    assert SECRET not in client.get(URL).text
    assert client.put(URL + "/text", json=text_payload(), headers=write_headers).status_code == 409


@pytest.mark.parametrize("peer,host,request_headers", [
    ("192.168.1.5", "127.0.0.1:8002", {}),
    ("192.168.1.5", "127.0.0.1:8002", {"X-Forwarded-For": "127.0.0.1"}),
    ("127.0.0.1", "outside.example:8002", {}),
    ("127.0.0.1", "127.0.0.1:8002", {"Origin": "https://outside.example"}),
    ("127.0.0.1", "127.0.0.1:8002", {"Origin": "null"}),
    ("127.0.0.1", "127.0.0.1:8002", {"Origin": "http://127.0.0.1.evil.example"}),
    ("127.0.0.1", "127.0.0.1:8002", {"Sec-Fetch-Site": "cross-site"}),
])
def test_standalone_rejects_network_and_external_origin(local_manager, peer, host, request_headers):
    client, base = local_manager
    with TestClient(client.app, base_url="http://" + host, client=(peer, 23456)) as caller:
        response = caller.get(URL, headers=request_headers)
        assert response.status_code == 403
        result = caller.put(URL + "/text", json=text_payload(), headers={**request_headers, "X-Content-Config-Request": "1"})
        assert result.status_code == 403
    assert not config.configuration_path(base).exists()


def test_flag_is_explicit_and_cannot_grant_models_to_raw_identity(manager, monkeypatch):
    client, _ = manager
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "development")
    monkeypatch.delenv("CONTENT_STANDALONE_MODE", raising=False)
    with TestClient(client.app, base_url="http://127.0.0.1", client=("127.0.0.1", 23456)) as local:
        assert local.get(URL + "/access").json()["can_manage"] is False
        assert local.get(URL).status_code == 403
    monkeypatch.setenv("CONTENT_STANDALONE_MODE", "true")
    assert not access_control.has_permission(Identity("dev-user", "", "", frozenset({"admin"})), "models.manage")


def test_standalone_flag_does_not_override_signed_authentication(manager, monkeypatch):
    client, _ = manager
    monkeypatch.setenv("CONTENT_STANDALONE_MODE", "true")
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers=headers()).status_code == 200
    assert client.get(URL, headers=headers(manage=False)).status_code == 403
