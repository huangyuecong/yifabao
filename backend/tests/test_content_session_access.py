"""Cookie identity keeps local business grants and gateway forwarding independent."""

import importlib.util
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from agent_core.identity import Identity
from content_service import access_control as access
from content_service import db
from content_service.access_routes import router as access_router
from content_service.bootstrap_admin import bootstrap_admin
from content_service.entry_management import management_access
from content_service.models import AuditLog, LocalAccessLock, LocalUser

# The entry gateway ships with the private deployment kit, not this repository.
GATEWAY = Path(__file__).resolve().parents[2] / "deployment/server/gateway.py"


def verified_account(subject="session-user"):
    return Identity(
        user_id=subject,
        name="真实登录用户",
        department="市场部",
        roles=frozenset({"admin", "user"}),
        permissions=access.PERMISSIONS,
        entry_is_admin=True,
        manageable_agent_keys=frozenset({"B8"}),
        entry_agent_key="B8",
        entry_verified=True,
    )


@pytest.fixture
def session_users(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "session")
    engine = create_engine(f"sqlite:///{tmp_path / 'session-users.db'}")
    factory = sessionmaker(engine, expire_on_commit=False)
    for model in (LocalAccessLock, LocalUser, AuditLog):
        model.__table__.create(engine)
    with factory.begin() as session:
        session.add(LocalAccessLock(id=1))
    monkeypatch.setattr(db, "SessionLocal", factory)
    yield factory
    engine.dispose()


def model_access(identity):
    return management_access(identity, Request({"type": "http", "headers": []}))


def test_session_login_registers_ordinary_user_without_importing_entry_grants(session_users):
    identity = access.get_content_identity(verified_account())
    assert identity.roles == {"user"}
    assert identity.permissions == set()
    assert not identity.is_project_admin
    assert access.is_signed(identity)
    result = model_access(identity)
    assert result.authentication_ready and not result.can_manage
    with session_users() as session:
        assert session.scalar(select(func.count()).select_from(LocalUser)) == 1
        with pytest.raises(HTTPException) as error:
            access.require_project_admin(session, identity)
        assert error.value.status_code == 403


def test_session_bootstrap_and_revocation_use_local_database(session_users):
    raw = verified_account()
    access.get_content_identity(raw)
    result = bootstrap_admin(raw.user_id)
    assert result["subject"] == raw.user_id
    admin = access.get_content_identity(raw)
    assert admin.is_project_admin and model_access(admin).can_manage
    with session_users() as session:
        assert access.require_project_admin(session, admin).subject == raw.user_id
        log = session.scalar(select(AuditLog).where(AuditLog.action == "access.admin.bootstrap"))
        assert log is not None
    with pytest.raises(HTTPException) as error:
        bootstrap_admin(raw.user_id)
    assert error.value.status_code == 409

    ordinary = verified_account("model-operator")
    access.get_content_identity(ordinary)
    with session_users.begin() as session:
        user = session.scalar(select(LocalUser).where(LocalUser.subject == ordinary.user_id))
        user.permissions_json = '["models.manage"]'
    granted = access.get_content_identity(ordinary)
    assert model_access(granted).can_manage and not granted.is_project_admin
    with session_users.begin() as session:
        user = session.scalar(select(LocalUser).where(LocalUser.subject == ordinary.user_id))
        user.permissions_json = "[]"
    assert not model_access(access.get_content_identity(ordinary)).can_manage


def test_unverified_session_never_registers_or_manages_users(session_users):
    raw = replace(verified_account(), entry_verified=False)
    assert not model_access(raw).authentication_ready
    assert not model_access(raw).can_manage
    with pytest.raises(HTTPException) as error:
        access.get_content_identity(raw)
    assert error.value.status_code == 403
    with session_users() as session:
        assert session.scalar(select(func.count()).select_from(LocalUser)) == 0


@pytest.mark.parametrize("subject,status", [("missing-user", 404), ("dev-user", 400)])
def test_session_bootstrap_still_requires_registered_real_user(session_users, subject, status):
    with pytest.raises(HTTPException) as error:
        bootstrap_admin(subject)
    assert error.value.status_code == status


def test_development_cannot_use_session_business_grants(session_users, monkeypatch):
    raw = verified_account()
    access.get_content_identity(raw)
    bootstrap_admin(raw.user_id)
    admin = access.get_content_identity(raw)
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "development")
    assert not model_access(admin).can_manage
    with pytest.raises(HTTPException) as error:
        bootstrap_admin(raw.user_id)
    assert error.value.status_code == 409


@pytest.fixture
def session_gateway(monkeypatch, tmp_path):
    if not GATEWAY.exists():
        pytest.skip("deployment kit not present")
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "session")
    source = GATEWAY
    spec = importlib.util.spec_from_file_location("session_access_test_gateway", source)
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    (tmp_path / "index.html").write_text("<html>public shell</html>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/app.js").write_text("/* application */", encoding="utf-8")
    return gateway, tmp_path


def test_session_gateway_serves_shell_ignoring_signed_header(session_gateway):
    gateway, dist = session_gateway
    with TestClient(gateway.create_app(dist)) as client:
        for path in ("/content/", "/content/admin", "/content/agent/content/library"):
            result = client.get(path, headers={"X-Entry-Identity": "forged"})
            assert result.status_code == 200
            assert result.text == "<html>public shell</html>"
            assert result.headers["cache-control"] == "private, no-store"
        assert client.get("/content/assets/app.js").status_code == 200
        assert client.get("/content/assets/missing.js").status_code == 404
        assert client.get("/content/private.pdf").status_code == 404


def test_session_gateway_preserves_cookies_range_and_backend_denials(session_gateway, monkeypatch):
    gateway, dist = session_gateway
    observed = []

    def backend(request):
        observed.append(request)
        assert "x-entry-identity" not in request.headers
        cookie = request.headers.get("cookie")
        if not cookie:
            return httpx.Response(401, stream=httpx.ByteStream(b'{"detail":"not logged in"}'))
        if "revoked" in cookie:
            return httpx.Response(403, stream=httpx.ByteStream(b'{"detail":"no B8 access"}'))
        return httpx.Response(
            206,
            stream=httpx.ByteStream(b"2345"),
            headers={"Content-Range": "bytes 2-5/10", "Content-Type": "audio/wav"},
        )

    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(backend), **kwargs)

    monkeypatch.setattr(gateway.httpx, "AsyncClient", client_factory)
    with TestClient(gateway.create_app(dist)) as client:
        url = "/content/api/v1/video-projects/media/example.wav?download=1"
        assert client.get(url).status_code == 401
        assert client.get(url, headers={"Cookie": "entry_session=revoked"}).status_code == 403
        response = client.get(
            url,
            headers={
                "Cookie": "entry_session=opaque-value; preference=light",
                "X-Entry-Identity": "forged",
                "Range": "bytes=2-5",
            },
        )
        assert response.status_code == 206
        assert response.content == b"2345"
        assert response.headers["content-range"] == "bytes 2-5/10"
        assert response.headers["cache-control"] == "private, no-store"
    assert observed[-1].headers["cookie"] == "entry_session=opaque-value; preference=light"
    assert observed[-1].headers["range"] == "bytes=2-5"
    assert observed[-1].url.path == "/api/v1/video-projects/media/example.wav"
    assert observed[-1].url.query == b"download=1"


def test_invalid_gateway_auth_mode_fails_closed_for_pages_and_api(session_gateway, monkeypatch):
    gateway, dist = session_gateway
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "sessoin")
    with TestClient(gateway.create_app(dist)) as client:
        for path in ("/content/", "/content/api/v1/access/me"):
            response = client.get(path)
            assert response.status_code == 503
            assert response.headers["cache-control"] == "private, no-store"


def test_gateway_to_backend_session_admission_and_local_models(
    session_users, session_gateway, monkeypatch
):
    from agent_core import entry_session

    gateway, dist = session_gateway
    monkeypatch.setenv("ENTRY_PUBLIC_BASE_URL", "https://entry.example")
    monkeypatch.setenv("CONTENT_ENTRY_AGENT_KEY", "B8")
    monkeypatch.setenv("CONTENT_ENTRY_ACCESS_MODE", "policy")
    monkeypatch.delenv("CONTENT_ENTRY_SESSION_BASE_URL", raising=False)
    entry_calls = []
    admitted = [True]

    def entry(request):
        entry_calls.append((request.url.path, request.headers.get("cookie")))
        assert request.headers["cookie"] == "entry_session=real-session"
        assert "x-entry-identity" not in request.headers
        user = {
            "subject": "2102729372629487617",
            "display_name": "testEntry",
            "department": "1833788600739696643",
            "roles": ["admin"],
        }
        if request.url.path == "/api/auth/me":
            return httpx.Response(200, json={"authenticated": True, "user": user})
        assert request.url.path == "/api/entry/me"
        return httpx.Response(
            200,
            json={"user": {**user, "enabled": True, "agent_keys": ["B8"] if admitted[0] else []}},
        )

    real_entry_client = httpx.Client

    def entry_client_factory(**kwargs):
        return real_entry_client(transport=httpx.MockTransport(entry), **kwargs)

    monkeypatch.setattr(entry_session, "Client", entry_client_factory)
    backend = FastAPI()
    backend.include_router(access_router)

    @backend.get("/api/v1/ai-models/access")
    def models(identity: Identity = Depends(access.get_content_identity)):
        return model_access(identity)

    real_gateway_client = httpx.AsyncClient

    def gateway_client_factory(**kwargs):
        return real_gateway_client(transport=httpx.ASGITransport(app=backend), **kwargs)

    monkeypatch.setattr(gateway.httpx, "AsyncClient", gateway_client_factory)
    with TestClient(gateway.create_app(dist)) as client:
        endpoint = "/content/api/v1/access/me"
        assert client.get(endpoint).status_code == 401
        assert not entry_calls
        headers = {"Cookie": "entry_session=real-session", "X-Entry-Identity": "forged"}
        response = client.get(endpoint, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["subject"] == "2102729372629487617"
        assert response.json()["authentication_ready"]
        assert not response.json()["is_project_admin"]
        assert response.json()["permissions"] == []
        assert [path for path, _ in entry_calls] == ["/api/auth/me", "/api/entry/me"]
        bootstrap_admin(response.json()["subject"])
        model_url = "/content/api/v1/ai-models/access"
        assert client.get(model_url, headers=headers).json()["can_manage"]
        admitted[0] = False
        assert client.get(endpoint, headers=headers).status_code == 403
        assert client.get(model_url, headers=headers).status_code == 403
        with session_users() as session:
            # Revoking Entry admission does not erase local grants; it denies
            # every request before those grants can authorize an operation.
            user = session.scalar(select(LocalUser))
            assert user.is_project_admin
