"""Entry authenticates; only locally stored grants authorize model management."""

import pytest
from sqlalchemy import select
from test_content_ai_model_config import URL, text_payload
from test_content_ai_model_config import manager as manager
from test_content_entry_identity import signed as signed
from test_content_entry_identity import token

from content_service import db
from content_service.models import LocalUser


def auth(claims=None, **kwargs):
    return {
        "X-Entry-Identity": token(claims=claims, **kwargs),
        "X-Content-Config-Request": "1",
    }


@pytest.mark.parametrize("policy", [
    {"is_admin": True},
    {"manageable_agent_keys": ["CONTENT-TEST"]},
    {"roles": ["admin"], "permissions": ["models.manage", "content:ai:manage"]},
    {},
])
def test_upstream_authority_never_grants_local_model_access(manager, policy):
    client, _ = manager
    headers = auth({"agent_keys": ["CONTENT-TEST"], **policy}, subject="entry-user-ordinary")
    result = client.get(URL + "/access", headers=headers)
    assert result.status_code == 200 and not result.json()["can_manage"]
    assert result.json()["authentication_ready"]
    assert client.get(URL, headers=headers).status_code == 403


def test_local_grant_works_without_entry_management_fields_or_session_cookie(manager):
    client, _ = manager
    headers = auth({"agent_keys": ["CONTENT-TEST"]})
    assert client.get(URL + "/access", headers=headers).json()["can_manage"]
    assert client.get(URL, headers=headers).status_code == 200


@pytest.mark.parametrize("changes,status", [
    ({"secret": "invalid"}, 401),
    ({"exp": 1}, 401),
    ({"claims": {"enabled": False, "is_admin": True}}, 403),
    ({"claims": {"agent_keys": [], "is_admin": True}}, 403),
])
def test_local_grant_cannot_bypass_entry_identity_and_access(manager, changes, status):
    client, _ = manager
    assert client.get(URL, headers=auth(**changes)).status_code == status


def test_revoked_local_grant_denied_on_every_model_route(manager):
    client, base = manager
    headers = auth()
    assert client.get(URL + "/access", headers=headers).json()["can_manage"]
    with db.SessionLocal() as session:
        user = session.scalar(select(LocalUser).where(LocalUser.subject == "entry-user-a"))
        user.permissions_json = "[]"
        session.commit()
    assert not client.get(URL + "/access", headers=headers).json()["can_manage"]
    for method, suffix, payload in [
        ("GET", "", None),
        ("PUT", "/text", text_payload()),
        ("POST", "/text/models", text_payload()),
        ("POST", "/text/rollback", {"expected_revision": 0}),
    ]:
        assert client.request(method, URL + suffix, headers=headers, json=payload).status_code == 403
    assert not (base.data_dir / "private/ai-models.json").exists()


def test_development_headers_cannot_manage_models(manager, monkeypatch):
    client, _ = manager
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "development")
    headers = {"X-Roles": "admin", "X-Is-Admin": "true"}
    result = client.get(URL + "/access", headers=headers)
    assert result.status_code == 200 and not result.json()["can_manage"]
    assert not result.json()["authentication_ready"]
    assert client.get(URL, headers=headers).status_code == 403
