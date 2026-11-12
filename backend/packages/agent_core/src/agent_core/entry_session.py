"""Validate Entry's browser session and agent admission without trusting browser claims."""

import ipaddress
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from httpx import Client

from .identity import Identity

_NO_STORE = {"Cache-Control": "private, no-store"}


def _error(status: int, detail: str) -> HTTPException:
    return HTTPException(status, detail, headers=_NO_STORE)


def _unavailable() -> HTTPException:
    return _error(503, "超级入口身份或访问授权服务暂不可用，请联系管理员")


def _origin(value: str, *, allow_path: bool = False) -> str:
    if (
        not value
        or value != value.strip()
        or "\\" in value
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Invalid origin")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (not allow_path and (parsed.path not in {"", "/"} or parsed.query))
    ):
        raise ValueError("Invalid origin")
    host = parsed.hostname.lower()
    if parsed.scheme == "http":
        try:
            address = ipaddress.ip_address(host)
            private = address.is_private or address.is_loopback
        except ValueError:
            private = host == "localhost" or host.endswith((".internal", ".local"))
        if not private:
            raise ValueError("Plain HTTP is limited to internal Entry addresses")
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid port")
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        authority += f":{port}"
    return f"{parsed.scheme}://{authority}"


@dataclass(frozen=True, slots=True)
class SessionConfig:
    public_origin: str
    base_url: str
    agent_key: str
    access_mode: str
    timeout_seconds: float


def validate_session_config() -> SessionConfig:
    """Validate explicit deployment settings; never infer admission from a public catalog."""
    try:
        public_origin = _origin(os.getenv("ENTRY_PUBLIC_BASE_URL", ""))
        base_url = _origin(os.getenv("CONTENT_ENTRY_SESSION_BASE_URL") or public_origin)
        agent_key = os.getenv("CONTENT_ENTRY_AGENT_KEY", "")
        access_mode = os.getenv("CONTENT_ENTRY_ACCESS_MODE", "")
        timeout = float(os.getenv("CONTENT_ENTRY_SESSION_TIMEOUT_SECONDS", "5"))
        if (
            not agent_key
            or agent_key != agent_key.strip()
            or len(agent_key) > 128
            or any(ord(char) < 32 for char in agent_key)
            or access_mode != "policy"
            or not 1 <= timeout <= 15
        ):
            raise ValueError("Invalid session configuration")
        return SessionConfig(public_origin, base_url, agent_key, access_mode, timeout)
    except (TypeError, ValueError):
        raise _error(503, "超级入口 Session 认证配置未就绪") from None


def _check_request_origin(request: Request, config: SessionConfig) -> None:
    if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise _error(403, "请求来源校验失败，请从超级入口操作")
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    try:
        if origin is not None:
            valid = _origin(origin) == config.public_origin
        else:
            valid = bool(referer) and _origin(referer, allow_path=True) == config.public_origin
    except ValueError:
        valid = False
    if not valid:
        raise _error(403, "请求来源校验失败，请从超级入口操作")


def _get_json(client: Client, path: str, cookie: str) -> dict:
    # A Set-Cookie response must not switch the principal between identity and ACL queries.
    client.cookies.clear()
    try:
        response = client.get(path, headers={"Cookie": cookie, "Accept": "application/json"})
    except (httpx.HTTPError, ValueError):
        raise _unavailable() from None
    if response.status_code == 401:
        raise _error(401, "登录状态已失效，请从超级入口重新登录")
    if response.status_code == 403:
        raise _error(403, "当前账号已停用或无权访问此智能体")
    if response.status_code != 200:
        raise _unavailable()
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json" and not content_type.endswith("+json"):
        raise _unavailable()
    try:
        payload = response.json()
    except (ValueError, RecursionError):
        raise _unavailable() from None
    if not isinstance(payload, dict):
        raise _unavailable()
    return payload


def _user(payload: dict) -> dict:
    user = payload.get("user")
    if not isinstance(user, dict):
        raise _unavailable()
    subject = user.get("subject")
    if (
        not isinstance(subject, str)
        or not 1 <= len(subject) <= 128
        or subject != subject.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in subject)
    ):
        raise _unavailable()
    if "enabled" in user:
        if type(user["enabled"]) is not bool:
            raise _unavailable()
        if not user["enabled"]:
            raise _error(403, "当前用户已停用")
    return user


def _check_access(payload: dict, user: dict, config: SessionConfig) -> str:
    candidate = payload.get("user")
    if not isinstance(candidate, dict) or candidate.get("subject") != user["subject"]:
        raise _unavailable()
    policy = _user(payload)
    keys = policy.get("agent_keys")
    if (
        type(policy.get("enabled")) is not bool
        or not isinstance(keys, list)
        or not all(isinstance(key, str) and key.strip() == key and key for key in keys)
    ):
        raise _unavailable()
    if config.agent_key not in keys:
        raise _error(403, "无权访问此智能体")
    # Entry's SDK defines this as a string, not an enum; preserve it as identity
    # metadata without converting it into any local business permission.
    clearance = policy.get("clearance", "public")
    if (
        not isinstance(clearance, str)
        or not 1 <= len(clearance) <= 64
        or clearance != clearance.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in clearance)
    ):
        raise _unavailable()
    return clearance


def resolve_entry_session(request: Request | None) -> Identity:
    config = validate_session_config()
    if request is None:
        raise _error(503, "Session 认证需要 HTTP 请求上下文")
    cookie = request.headers.get("cookie", "")
    if not cookie.strip() or len(cookie) > 16384 or any(ord(char) < 32 for char in cookie):
        raise _error(401, "请先通过超级入口登录")
    _check_request_origin(request, config)
    # Per-request clients prevent session cookies leaking across simultaneous users.
    with Client(
        base_url=config.base_url,
        timeout=config.timeout_seconds,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        auth = _get_json(client, "/api/auth/me", cookie)
        if type(auth.get("authenticated")) is not bool:
            raise _unavailable()
        if not auth["authenticated"]:
            raise _error(401, "登录状态已失效，请从超级入口重新登录")
        user = _user(auth)
        clearance = _check_access(_get_json(client, "/api/entry/me", cookie), user, config)
    name, department = user.get("display_name", user["subject"]), user.get("department")
    if (
        not isinstance(name, str)
        or len(name) > 255
        or department is not None
        and (not isinstance(department, str) or len(department) > 255)
    ):
        raise _unavailable()
    return Identity(
        user_id=user["subject"],
        name=name.strip() or user["subject"],
        department=department or "",
        roles=frozenset({"user"}),
        clearance=clearance,
        entry_agent_key=config.agent_key,
        entry_verified=True,
    )
