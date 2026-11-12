"""Entry Cookie authentication, admission, and local business authority boundaries."""

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from agent_core import entry_session
from agent_core.identity import Identity, get_identity
from content_service import access_control as access
from content_service import db
from content_service.access_routes import router
from content_service.models import AuditLog, LocalAccessLock, LocalUser

ORIGIN = "https://portal.example.com"


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "session")
    monkeypatch.setenv("ENTRY_PUBLIC_BASE_URL", ORIGIN)
    monkeypatch.setenv("CONTENT_ENTRY_AGENT_KEY", "B8")
    monkeypatch.setenv("CONTENT_ENTRY_ACCESS_MODE", "policy")
    for key in (
        "CONTENT_ENTRY_SESSION_BASE_URL",
        "CONTENT_ENTRY_SESSION_TIMEOUT_SECONDS",
        "CONTENT_IDENTITY_SECRET",
        "CONTENT_IDENTITY_SECRET_FILE",
        "XJ_AUTH_GATEWAY_SECRET",
        "XJ_AUTH_GATEWAY_SECRET_FILE",
    ):
        monkeypatch.delenv(key, raising=False)


def auth(subject="alice", **values):
    return {
        "authenticated": True,
        "user": {
            "subject": subject,
            "display_name": "登录用户",
            "department": "市场部",
            "roles": ["admin"],
            "permissions": ["models.manage"],
            **values,
        },
    }


def policy(subject="alice", **values):
    return {"user": {"subject": subject, "enabled": True, "agent_keys": ["B8"], **values}}


@pytest.fixture
def entry(monkeypatch, settings):
    state = {"auth": auth(), "policy": policy()}
    requests, options = [], []

    def handle(request):
        requests.append(request)
        handler = state.get("handler")
        if handler:
            return handler(request)
        item = state[{"/api/auth/me": "auth", "/api/entry/me": "policy"}[request.url.path]]
        return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)

    def client(**kwargs):
        options.append(kwargs)
        return httpx.Client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(entry_session, "Client", client)
    return state, requests, options


@pytest.fixture
def browser(entry):
    app = FastAPI()

    @app.api_route("/identity", methods=["GET", "POST", "PUT", "DELETE"])
    def identity(identity: Identity = Depends(get_identity)):
        return {
            "subject": identity.user_id,
            "roles": sorted(identity.roles),
            "permissions": sorted(identity.permissions),
            "verified": identity.entry_verified,
            "clearance": identity.clearance,
        }

    with TestClient(app) as client:
        yield client


def cookie(value="alice", **headers):
    return {"Cookie": f"entry_session={value}", **headers}


def test_session_uses_cookie_and_never_imports_browser_or_entry_authority(browser, entry):
    response = browser.get(
        "/identity",
        headers=cookie(
            **{
                "X-Entry-Identity": "forged.signature",
                "X-User-Id": "attacker",
                "X-Roles": "admin",
            }
        ),
    )
    assert response.status_code == 200
    assert response.json() == {
        "subject": "alice",
        "roles": ["user"],
        "permissions": [],
        "verified": True,
        "clearance": "public",
    }
    _, requests, options = entry
    assert [request.url.path for request in requests] == ["/api/auth/me", "/api/entry/me"]
    assert all(request.headers["cookie"] == "entry_session=alice" for request in requests)
    assert all(
        "x-entry-identity" not in request.headers
        and "x-yifabao-gateway-secret" not in request.headers
        for request in requests
    )
    assert options == [
        {"base_url": ORIGIN, "timeout": 5.0, "trust_env": False, "follow_redirects": False}
    ]


def test_acl_is_checked_fresh_and_revocation_takes_effect(browser, entry):
    state, requests, _ = entry
    assert browser.get("/identity", headers=cookie()).status_code == 200
    state["policy"] = policy(agent_keys=[])
    response = browser.get("/identity", headers=cookie())
    assert response.status_code == 403
    assert len(requests) == 4
    assert response.headers["cache-control"] == "private, no-store"


def test_requests_do_not_share_or_replace_browser_cookies(browser, entry):
    state, requests, options = entry

    def handler(request):
        subject = request.headers["cookie"].split("=")[1]
        if request.url.path == "/api/auth/me":
            return httpx.Response(
                200, json=auth(subject), headers={"Set-Cookie": "entry_session=intruder; Path=/"}
            )
        return httpx.Response(200, json=policy(subject))

    state["handler"] = handler
    assert browser.get("/identity", headers=cookie("alice")).json()["subject"] == "alice"
    assert browser.get("/identity", headers=cookie("bob")).json()["subject"] == "bob"
    assert [request.headers["cookie"] for request in requests] == ["entry_session=alice"] * 2 + [
        "entry_session=bob"
    ] * 2
    assert len(options) == 2


@pytest.mark.parametrize(
    "value,status",
    [
        ({"authenticated": False}, 401),
        ({"authenticated": "true", "user": {}}, 503),
        (auth(subject=""), 503),
        (auth(subject=123), 503),
        (auth(subject=" alice "), 503),
        (auth(enabled=False), 403),
        (auth(enabled="true"), 503),
        (auth(display_name=4), 503),
        (auth(department={}), 503),
    ],
)
def test_invalid_login_data(browser, entry, value, status):
    entry[0]["auth"] = value
    assert browser.get("/identity", headers=cookie()).status_code == status


@pytest.mark.parametrize(
    "value,status",
    [
        (policy(enabled=False), 403),
        (policy(agent_keys=[]), 403),
        (policy(subject="other"), 503),
        (policy(subject="other", enabled=False), 503),
        (policy(enabled="true"), 503),
        (policy(agent_keys="B8"), 503),
        ({"user": {"subject": "alice", "agent_keys": ["B8"]}}, 503),
    ],
)
def test_policy_must_be_enabled_and_match_identity(browser, entry, value, status):
    entry[0]["policy"] = value
    assert browser.get("/identity", headers=cookie()).status_code == status


@pytest.mark.parametrize(
    "code,expected", [(401, 401), (403, 403), (404, 503), (500, 503), (302, 503)]
)
@pytest.mark.parametrize("endpoint", ["auth", "policy"])
def test_upstream_failure_does_not_become_login_success(browser, entry, endpoint, code, expected):
    entry[0][endpoint] = httpx.Response(
        code, headers={"location": "https://other.example/"}, json={}
    )
    assert browser.get("/identity", headers=cookie()).status_code == expected


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>Entry</html>", headers={"content-type": "text/html"}),
        httpx.Response(200, text="bad", headers={"content-type": "application/json"}),
        httpx.Response(200, json=[]),
    ],
)
def test_non_contract_responses_fail_closed(browser, entry, response):
    entry[0]["auth"] = response
    assert browser.get("/identity", headers=cookie()).status_code == 503


def test_unreachable_entry_fails_closed(browser, entry):
    def fail(request):
        raise httpx.ConnectError("unreachable", request=request)

    entry[0]["handler"] = fail
    assert browser.get("/identity", headers=cookie()).status_code == 503


def test_cookie_is_required_even_with_signed_or_development_headers(browser, entry):
    assert (
        browser.get(
            "/identity", headers={"X-User-Id": "admin", "X-Entry-Identity": "a.b"}
        ).status_code
        == 401
    )
    assert not entry[1]


@pytest.mark.parametrize(
    "variable,value",
    [
        ("CONTENT_ENTRY_ACCESS_MODE", ""),
        ("CONTENT_ENTRY_ACCESS_MODE", "allow"),
        ("CONTENT_ENTRY_ACCESS_MODE", "catalog"),
        ("ENTRY_PUBLIC_BASE_URL", ""),
        ("ENTRY_PUBLIC_BASE_URL", "https://host/content"),
        ("ENTRY_PUBLIC_BASE_URL", "https://user:pass@host"),
        ("CONTENT_ENTRY_SESSION_BASE_URL", "http://public.example"),
        ("CONTENT_ENTRY_SESSION_BASE_URL", "https://host/path"),
        ("CONTENT_ENTRY_SESSION_BASE_URL", "https://host\\other"),
        ("CONTENT_ENTRY_AGENT_KEY", ""),
        ("CONTENT_ENTRY_SESSION_TIMEOUT_SECONDS", "0"),
        ("CONTENT_ENTRY_SESSION_TIMEOUT_SECONDS", "16"),
        ("CONTENT_ENTRY_SESSION_TIMEOUT_SECONDS", "nan"),
        ("CONTENT_ENTRY_SESSION_TIMEOUT_SECONDS", "inf"),
        ("CONTENT_ENTRY_SESSION_TIMEOUT_SECONDS", "-inf"),
    ],
)
def test_misconfiguration_is_rejected_before_network_and_cookie(
    browser, entry, monkeypatch, variable, value
):
    monkeypatch.setenv(variable, value)
    assert browser.get("/identity").status_code == 503
    assert not entry[1]


def test_internal_origin_is_explicit_and_public_origin_remains_csrf_source(
    browser, entry, monkeypatch
):
    monkeypatch.setenv("CONTENT_ENTRY_SESSION_BASE_URL", "http://10.0.2.8:18000")
    monkeypatch.setenv("CONTENT_ENTRY_SESSION_TIMEOUT_SECONDS", "8")
    assert browser.post("/identity", headers=cookie(Origin=ORIGIN)).status_code == 200
    assert entry[2][0]["base_url"] == "http://10.0.2.8:18000"
    assert entry[2][0]["timeout"] == 8


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "null"},
        {"Origin": "https://evil.example"},
        {"Origin": "https://portal.example.com.evil.example"},
        {"Origin": "https://evil.example", "Referer": ORIGIN + "/content"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
        {"Referer": "https://evil.example/content"},
    ],
)
def test_cookie_write_requests_need_trusted_origin(browser, entry, headers):
    assert browser.post("/identity", headers=cookie(**headers)).status_code == 403
    assert not entry[1]


@pytest.mark.parametrize("headers", [{"Origin": ORIGIN}, {"Referer": ORIGIN + "/content?task=1"}])
def test_same_origin_cookie_writes_work(browser, headers):
    assert browser.post("/identity", headers=cookie(**headers)).status_code == 200


def test_get_media_requests_do_not_need_origin(browser):
    assert browser.get("/identity", headers=cookie(Range="bytes=0-1023")).status_code == 200


@pytest.mark.parametrize("clearance", ["public", "internal", "restricted", "confidential"])
def test_clearance_comes_only_from_verified_policy(browser, entry, clearance):
    entry[0]["auth"] = auth(clearance="restricted", is_admin=True)
    entry[0]["policy"] = policy(clearance=clearance, is_admin=True)
    result = browser.get("/identity", headers=cookie(**{"X-Clearance": "restricted"}))
    assert result.status_code == 200
    assert result.json()["clearance"] == clearance
    assert result.json()["roles"] == ["user"]
    assert result.json()["permissions"] == []


@pytest.mark.parametrize(
    "clearance", [None, False, 3, {}, [], "", " internal", "internal\n", "a" * 65]
)
def test_malformed_policy_clearance_fails_closed(browser, entry, clearance):
    entry[0]["policy"] = policy(clearance=clearance)
    assert browser.get("/identity", headers=cookie()).status_code == 503


def test_missing_clearance_defaults_public_despite_auth_and_browser_claims(browser, entry):
    entry[0]["auth"] = auth(clearance="restricted", is_admin=True)
    result = browser.get("/identity", headers=cookie(**{"X-Clearance": "restricted"}))
    assert result.json()["clearance"] == "public"


def test_ipv6_origins_and_default_port_normalization(browser, entry, monkeypatch):
    monkeypatch.setenv("ENTRY_PUBLIC_BASE_URL", "https://[::1]:443")
    monkeypatch.setenv("CONTENT_ENTRY_SESSION_BASE_URL", "http://[::1]:18000")
    result = browser.post("/identity", headers=cookie(Origin="https://[::1]"))
    assert result.status_code == 200
    assert entry[2][0]["base_url"] == "http://[::1]:18000"


def test_direct_dependency_without_request_fails_closed(settings):
    with pytest.raises(HTTPException) as error:
        get_identity("attacker", "name", "dept", "admin", "forged")
    assert error.value.status_code == 503


def test_local_api_authority_comes_from_database_and_entry_revoke_wins(
    entry, tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'session-permissions.db'}",
        connect_args={"check_same_thread": False},
    )
    factory = sessionmaker(engine, expire_on_commit=False)
    for model in (LocalAccessLock, LocalUser, AuditLog):
        model.__table__.create(engine)
    with factory.begin() as session:
        session.add(LocalAccessLock(id=1))
    monkeypatch.setattr(db, "SessionLocal", factory)
    app = FastAPI()
    app.include_router(router)

    @app.get("/models")
    def models(identity: Identity = Depends(access.get_content_identity)):
        if not access.has_permission(identity, "models.manage"):
            raise HTTPException(403)
        return {"allowed": True}

    with TestClient(app) as client:
        result = client.get("/api/v1/access/me", headers=cookie())
        assert result.status_code == 200, result.text
        assert result.json()["authentication_ready"] is True
        assert result.json()["permissions"] == []
        assert result.json()["is_project_admin"] is False
        assert client.get("/models", headers=cookie()).status_code == 403
        with factory.begin() as session:
            user = session.scalar(select(LocalUser).where(LocalUser.subject == "alice"))
            user.permissions_json = '["models.manage"]'
        assert client.get("/models", headers=cookie()).status_code == 200
        with factory.begin() as session:
            user = session.scalar(select(LocalUser).where(LocalUser.subject == "alice"))
            user.is_project_admin = True
        assert client.get("/api/v1/access/users", headers=cookie()).status_code == 200
        entry[0]["policy"] = policy(agent_keys=[])
        assert client.get("/models", headers=cookie()).status_code == 403
        assert client.get("/api/v1/access/users", headers=cookie()).status_code == 403
    engine.dispose()
