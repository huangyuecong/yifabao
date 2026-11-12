import base64
import hashlib
import hmac
import importlib.util
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient
from platform_auth import GatewayIdentityCodec
from platform_auth.models import Identity as SdkIdentity

from agent_core.identity import get_identity, verify_entry_identity

SECRET = "test-only-entry-secret-not-for-production"

# The entry gateway ships with the private deployment kit, not this repository.
GATEWAY = Path(__file__).resolve().parents[2] / "deployment/server/gateway.py"
needs_gateway = pytest.mark.skipif(not GATEWAY.exists(), reason="deployment kit not present")


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def token(
    *, subject="entry-user-a", claims=None, secret=SECRET, signing_input="base64url", **fields
):
    now = int(time.time())
    payload = {
        "sub": subject,
        "name": "联调用户",
        "iat": now,
        "exp": now + 60,
        "claims": {
            "department": "市场部",
            "roles": ["employee"],
            "clearance": "internal",
            "enabled": True,
            "agent_keys": ["CONTENT-TEST"],
        }
        if claims is None
        else claims,
        **fields,
    }
    raw = json.dumps(payload, ensure_ascii=False).encode()
    body = encode(raw)
    signed = raw if signing_input == "json" else body.encode()
    return body + "." + encode(hmac.digest(secret.encode(), signed, hashlib.sha256))


@pytest.fixture
def signed(monkeypatch):
    for key in (
        "CONTENT_IDENTITY_SECRET_FILE",
        "CONTENT_IDENTITY_ROLE_MAP",
        "CONTENT_IDENTITY_REQUIRE_AGENT_KEYS",
        "CONTENT_IDENTITY_SIGNING_INPUT",
        "CONTENT_IDENTITY_MAX_TTL_SECONDS",
        "CONTENT_IDENTITY_CLOCK_SKEW_SECONDS",
        "XJ_AUTH_GATEWAY_SECRET",
        "XJ_AUTH_GATEWAY_SECRET_FILE",
        "CONTENT_IDENTITY_PERMISSION_MAP",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "signed")
    monkeypatch.setenv("CONTENT_IDENTITY_SECRET", SECRET)
    monkeypatch.setenv("CONTENT_ENTRY_AGENT_KEY", "CONTENT-TEST")


def test_verified_identity_ignores_spoofed_headers(content_client, signed):
    result = content_client.get(
        "/api/v1/identity",
        headers={
            "x-entry-identity": token(),
            "X-User-Id": "attacker",
            "X-Roles": "admin",
        },
    )
    assert result.status_code == 200
    assert result.json() == {
        "subject": "entry-user-a",
        "display_name": "联调用户",
        "department": "市场部",
        "roles": ["user"],
        "clearance": "internal",
    }
    assert result.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("value", [None, "", "broken", "a.b.c", "***.???", "a" * 17000])
def test_missing_or_malformed_token_rejected(content_client, signed, value):
    headers = {"X-User-Id": "dev-user", "X-Roles": "admin"}
    if value is not None:
        headers["x-entry-identity"] = value
    assert content_client.get("/api/v1/identity", headers=headers).status_code == 401


@pytest.mark.parametrize(
    "fields",
    [
        {"secret": "wrong-key"},
        {"exp": 1},
        {"iat": int(time.time()) + 120},
        {"exp": int(time.time()) + 3600},
        {"iat": True},
        {"exp": "tomorrow"},
        {"sub": ""},
        {"name": None},
        {"claims": []},
        {"claims": {"roles": {"admin": True}}},
        {"claims": {"enabled": "false"}},
        {"claims": {"department": {}}},
        {"claims": {"agent_keys": "CONTENT-TEST"}},
        {"claims": {"clearance": None}},
    ],
)
def test_invalid_signed_payload_rejected(signed, fields):
    with pytest.raises(HTTPException) as error:
        verify_entry_identity(token(**fields))
    assert error.value.status_code == 401


@pytest.mark.parametrize(
    "claims",
    [
        {"enabled": False},
        {"agent_keys": []},
        {"agent_keys": ["OTHER"], "is_admin": True},
    ],
)
def test_disabled_or_ungranted_user_forbidden(content_client, signed, claims):
    assert (
        content_client.get(
            "/api/v1/tasks",
            headers={
                "x-entry-identity": token(claims=claims),
            },
        ).status_code
        == 403
    )


def test_explicit_roles_and_optional_grant_contract(signed, monkeypatch):
    claims = {"roles": ["admin", "content-reviewer"], "is_admin": True}
    assert verify_entry_identity(token(claims=claims)).roles == frozenset()
    monkeypatch.setenv("CONTENT_IDENTITY_ROLE_MAP", '{"content-reviewer":["user","reviewer"]}')
    assert verify_entry_identity(token(claims=claims)).roles == {"user", "reviewer"}
    monkeypatch.setenv("CONTENT_IDENTITY_REQUIRE_AGENT_KEYS", "true")
    with pytest.raises(HTTPException) as error:
        verify_entry_identity(token(claims=claims))
    assert error.value.status_code == 403


@pytest.mark.parametrize(
    "key,value",
    [
        ("CONTENT_IDENTITY_SECRET", ""),
        ("CONTENT_ENTRY_AGENT_KEY", ""),
        ("CONTENT_IDENTITY_SECRET_FILE", "/does-not-exist/secret"),
        ("CONTENT_IDENTITY_SIGNING_INPUT", "auto"),
        ("CONTENT_IDENTITY_ROLE_MAP", "[]"),
        ("CONTENT_IDENTITY_ROLE_MAP", '{"employee":["root"]}'),
        ("CONTENT_IDENTITY_MAX_TTL_SECONDS", "nan"),
        ("CONTENT_IDENTITY_CLOCK_SKEW_SECONDS", "-1"),
        ("CONTENT_IDENTITY_REQUIRE_AGENT_KEYS", "maybe"),
        ("CONTENT_IDENTITY_MODE", "singed"),
    ],
)
def test_bad_config_fails_closed(content_client, signed, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    response = content_client.get("/api/v1/identity", headers={"x-entry-identity": token()})
    assert response.status_code == 503
    assert SECRET not in response.text


def test_signing_input_is_explicit_and_secret_file_supported(signed, monkeypatch, tmp_path):
    raw_token = token(signing_input="json")
    with pytest.raises(HTTPException):
        verify_entry_identity(raw_token)
    monkeypatch.setenv("CONTENT_IDENTITY_SIGNING_INPUT", "json")
    secret_path = tmp_path / "entry.secret"
    secret_path.write_text(SECRET + "\n")
    monkeypatch.setenv("CONTENT_IDENTITY_SECRET_FILE", str(secret_path))
    monkeypatch.setenv("CONTENT_IDENTITY_SECRET", "ignored")
    assert verify_entry_identity(raw_token).user_id == "entry-user-a"


def test_development_mode_compatible_but_bad_signature_never_falls_back(signed, monkeypatch):
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "development")
    assert get_identity("dev", "Dev", "Department", "admin").roles == {"admin"}
    with pytest.raises(HTTPException):
        get_identity("dev", "Dev", "Department", "admin", "bad-token")


def test_real_api_ownership_and_admin_permission(content_client, signed):
    a = {"x-entry-identity": token(subject="signed-owner-a")}
    b = {"x-entry-identity": token(subject="signed-owner-b")}
    result = content_client.post(
        "/api/v1/tasks",
        headers=a,
        json={
            "mode": "copy",
            "name": "统一入口隔离测试",
            "topic": "日常生活",
            "audience": "成年消费者",
        },
    )
    assert result.status_code == 200, result.text
    task_id = result.json()["id"]
    assert content_client.get(f"/api/v1/tasks/{task_id}", headers=a).status_code == 200
    assert content_client.get(f"/api/v1/tasks/{task_id}", headers=b).status_code in {403, 404}
    assert content_client.get("/api/v1/tasks", headers={"X-Roles": "admin"}).status_code == 401
    # Existing admin-only business boundary remains authoritative.
    response = content_client.post(
        "/api/v1/sensitive-rules",
        headers=a,
        json={
            "phrase": "权限测试敏感词",
            "level": "forbidden",
        },
    )
    assert response.status_code == 403, response.text
    assert content_client.get("/health").status_code == 200


def test_upstream_role_mapping_cannot_grant_local_business_authority(content_client, signed, monkeypatch):
    claims = {"roles": ["content-admin"], "is_admin": True}
    headers = {"x-entry-identity": token(claims=claims), "X-Roles": "admin"}
    payload = {"phrase": "统一入口管理员权限回归", "level": "warning"}
    assert (
        content_client.post("/api/v1/sensitive-rules", headers=headers, json=payload).status_code
        == 403
    )
    monkeypatch.setenv("CONTENT_IDENTITY_ROLE_MAP", '{"content-admin":["user","admin"]}')
    assert (
        content_client.post("/api/v1/sensitive-rules", headers=headers, json=payload).status_code
        == 403
    )


def test_duplicate_json_keys_rejected(signed):
    now = int(time.time())
    body = encode(
        (f'{{"sub":"a","sub":"b","name":"n","claims":{{}},"iat":{now},"exp":{now + 60}}}').encode()
    )
    value = body + "." + encode(hmac.digest(SECRET.encode(), body.encode(), hashlib.sha256))
    with pytest.raises(HTTPException) as error:
        verify_entry_identity(value)
    assert error.value.status_code == 401


@needs_gateway
def test_page_gateway_enforces_same_identity(signed, monkeypatch, tmp_path):
    source = GATEWAY
    spec = importlib.util.spec_from_file_location("entry_test_gateway", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "index.html").write_text("<html>entry test</html>")
    with TestClient(module.create_app(tmp_path), follow_redirects=False) as client:
        for path in ["/content", "/content/", "/content/admin", "/content/agent/content/library"]:
            assert client.get(path).status_code == 401
            assert (
                client.get(
                    path, headers={"x-entry-identity": token(claims={"enabled": False})}
                ).status_code
                == 403
            )
        response = client.get("/content/", headers={"x-entry-identity": token()})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"

        monkeypatch.delenv("CONTENT_IDENTITY_SECRET")
        assert client.get("/content/").status_code == 503


@needs_gateway
def test_signed_identity_survives_gateway_to_real_backend(
    content_client, signed, monkeypatch, tmp_path
):
    from content_service.main import app

    source = GATEWAY
    spec = importlib.util.spec_from_file_location("entry_proxy_test_gateway", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return real_client(transport=httpx.ASGITransport(app=app), **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", client_factory)
    (tmp_path / "index.html").write_text("<html>entry</html>")
    with TestClient(module.create_app(tmp_path)) as client:
        assert client.get("/content/api/health").status_code == 200
        assert client.get("/content/api/v1/tasks", headers={"X-Roles": "admin"}).status_code == 401
        assert (
            client.get(
                "/content/api/v1/identity", headers={"x-entry-identity": token(secret="bad")}
            ).status_code
            == 401
        )
        response = client.get(
            "/content/api/v1/identity",
            headers={
                "x-entry-identity": token(),
                "X-User-Id": "spoofed",
                "X-Roles": "admin",
            },
        )
        assert response.status_code == 200
        assert response.json()["subject"] == "entry-user-a"
        assert response.json()["roles"] == ["user"]
        assert response.headers["cache-control"] == "private, no-store"

        # Exercise the supplied SDK through the actual gateway and backend,
        # including multipart upload and the Range request used by media players.
        sdk_headers = {
            "x-entry-identity": GatewayIdentityCodec(SECRET).encode(
                SdkIdentity("sdk-media-owner", "素材用户", {"roles": "employee"})
            )
        }
        media = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG2kAAAAASUVORK5CYII="
        )
        upload = client.post(
            "/content/api/v1/library/items/upload",
            headers=sdk_headers,
            params={"category": "SSO联调", "name": "参考图", "data_level": "restricted"},
            files={"file": ("reference.png", media, "image/png")},
        )
        assert upload.status_code == 200, upload.text
        download_url = "/content" + upload.json()["download_url"]
        download = client.get(download_url, headers=sdk_headers)
        assert download.status_code == 200
        assert download.content == media
        ranged = client.get(download_url, headers={**sdk_headers, "Range": "bytes=2-9"})
        assert ranged.status_code == 206
        assert ranged.content == media[2:10]
        assert ranged.headers["content-range"] == f"bytes 2-9/{len(media)}"
        assert client.get(download_url).status_code == 401
        other = {"x-entry-identity": token(subject="other-media-user")}
        assert client.get(download_url, headers=other).status_code in {403, 404}


@pytest.mark.parametrize("roles", ["employee", ["employee"]])
def test_supplied_sdk_token_preserves_subject_and_supports_role_shapes(signed, roles):
    signed_token = GatewayIdentityCodec(SECRET).encode(
        SdkIdentity(
            "sdk-owner",
            "",
            {"roles": roles, "permissions": ["content:read"], "department": "市场部"},
        )
    )
    identity = verify_entry_identity(signed_token)
    assert identity.user_id == "sdk-owner"
    assert identity.name == "sdk-owner"
    assert identity.roles == {"user"}
    assert identity.permissions == {"content:read"}


def test_sdk_environment_aliases_and_explicit_permission_mapping(signed, monkeypatch, tmp_path):
    monkeypatch.delenv("CONTENT_IDENTITY_SECRET")
    monkeypatch.setenv("XJ_AUTH_GATEWAY_SECRET", SECRET)
    value = GatewayIdentityCodec(SECRET).encode(
        SdkIdentity("sdk-owner", "用户", {"permissions": "content:manage", "is_admin": True})
    )
    assert verify_entry_identity(value).roles == set()
    monkeypatch.setenv("CONTENT_IDENTITY_PERMISSION_MAP", '{"content:manage":["user","admin"]}')
    assert verify_entry_identity(value).roles == {"user", "admin"}
    secret_file = tmp_path / "sdk.secret"
    secret_file.write_text(SECRET)
    monkeypatch.setenv("XJ_AUTH_GATEWAY_SECRET_FILE", str(secret_file))
    monkeypatch.setenv("XJ_AUTH_GATEWAY_SECRET", "ignored")
    assert verify_entry_identity(value).user_id == "sdk-owner"
    monkeypatch.setenv("XJ_AUTH_GATEWAY_SECRET_FILE", str(tmp_path / "missing"))
    with pytest.raises(HTTPException) as error:
        verify_entry_identity(value)
    assert error.value.status_code == 503


@pytest.mark.parametrize("permissions", [None, 123, {"admin": True}, ["read", None]])
def test_invalid_permissions_rejected(signed, permissions):
    with pytest.raises(HTTPException) as error:
        verify_entry_identity(token(claims={"permissions": permissions}))
    assert error.value.status_code == 401
