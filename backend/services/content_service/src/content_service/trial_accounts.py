"""Accounts for the online trial: who may log in, and what each account did.

Off unless CONTENT_TRIAL_ACCOUNTS=true, which only the trial server sets; the local
workbench never uses it. The reverse proxy asks ``/api/v1/trial/auth-check`` before it
serves any page or API call, so the browser's own login box (HTTP Basic) is checked
against these accounts. Every account works in the one shared demo workspace; each
request that changes something is written to the operation log under the account that
sent it, and the owner manages accounts from the 账号管理 page.

Accounts live in ``<data dir>/private/trial-accounts.json`` (PBKDF2 password hashes, never
the passwords) and the log in ``trial-operations.jsonl`` next to it.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import china_time

ITERATIONS = 200_000
CACHE_SECONDS = 300
FAILURE_WINDOW = 600
FAILURE_LIMIT = 20
LOG_KEEP = 20_000
REALM = 'Basic realm="yifabao-trial", charset="UTF-8"'
USERNAME = re.compile(r"[A-Za-z0-9_.-]{2,32}")
CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
# Endpoints that change nothing a person would look for in the log.
QUIET = re.compile(r"^/api/v1/(trial/auth-check|automation/browser/sessions|speech/transcribe|remote-login/[^/]+/(click|type)$)")


@dataclass(frozen=True)
class Account:
    username: str
    display_name: str
    is_owner: bool


current: ContextVar[Account | None] = ContextVar("trial_account", default=None)
_lock = threading.RLock()
_verified: dict[str, tuple[str, float]] = {}
_failures: dict[str, list[float]] = {}


def enabled() -> bool:
    return os.getenv("CONTENT_TRIAL_ACCOUNTS", "false").strip().lower() == "true"


def _private_dir() -> Path:
    from .provider_dependencies import get_runtime_settings

    return Path(get_runtime_settings().data_dir) / "private"


def _accounts_path() -> Path:
    return _private_dir() / "trial-accounts.json"


def _log_path() -> Path:
    return _private_dir() / "trial-operations.jsonl"


def _now() -> str:
    return datetime.now(china_time.CHINA).isoformat(timespec="seconds")


def _read_accounts() -> list[dict]:
    try:
        document = json.loads(_accounts_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        raise HTTPException(503, "试用账号数据无法读取，请联系管理员") from None
    items = document.get("accounts") if isinstance(document, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _write_accounts(items: list[dict]) -> None:
    path = _accounts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
        json.dump({"version": 1, "accounts": items}, handle, ensure_ascii=False, indent=2)
        temporary = Path(handle.name)
    os.replace(temporary, path)
    with suppress(OSError):
        os.chmod(path, 0o600)
    _verified.clear()  # a changed password or disabled account takes effect at once


def _hash(password: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()


def _set_password(item: dict, password: str) -> None:
    if not 6 <= len(password) <= 128:
        raise HTTPException(422, "密码长度需为 6–128 位")
    salt = secrets.token_bytes(16)
    item.update(salt=salt.hex(), iterations=ITERATIONS, password_hash=_hash(password, salt))


def _public(item: dict) -> dict:
    return {
        "username": item["username"],
        "display_name": item.get("display_name") or item["username"],
        "is_owner": bool(item.get("is_owner")),
        "enabled": item.get("enabled", True) is not False,
        "created_at": item.get("created_at"),
        "last_seen_at": item.get("last_seen_at"),
    }


def _account(item: dict) -> Account:
    return Account(item["username"], item.get("display_name") or item["username"], bool(item.get("is_owner")))


def _credentials(header: str | None) -> tuple[str, str] | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header[6:].strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    username, separator, password = text.partition(":")
    return (username, password) if separator and username else None


def _client(request: Request) -> str:
    host = request.client.host if request.client else ""
    # Only the local reverse proxy may name the real visitor address.
    if host in {"127.0.0.1", "::1"}:
        return (request.headers.get("x-real-ip") or host).strip()[:64]
    return host


def _locked_out(ip: str) -> bool:
    now = time.monotonic()
    recent = [moment for moment in _failures.get(ip, []) if now - moment < FAILURE_WINDOW]
    _failures[ip] = recent
    return len(recent) >= FAILURE_LIMIT


def authenticate(header: str | None, ip: str) -> Account | None:
    """The account for these Basic credentials, or None. Raises 403 after repeated failures."""
    with _lock:
        if _locked_out(ip):
            raise HTTPException(403, "登录失败次数过多，请 10 分钟后再试")
        pair = _credentials(header)
        if pair is None:
            return None
        key = hashlib.sha256(f"{pair[0]}\0{pair[1]}".encode()).hexdigest()
        cached = _verified.get(key)
        items = _read_accounts()
        item = next((value for value in items if value.get("username") == pair[0]), None)
        if cached and cached[1] > time.monotonic() and item and item.get("enabled", True) is not False:
            return _account(item)
        valid = False
        if item and item.get("enabled", True) is not False:
            try:
                salt = bytes.fromhex(item["salt"])
                expected = item["password_hash"]
                valid = hmac.compare_digest(_hash(pair[1], salt, int(item.get("iterations", ITERATIONS))), expected)
            except (KeyError, ValueError, TypeError):
                valid = False
        if not valid:
            _failures.setdefault(ip, []).append(time.monotonic())
            return None
        _verified[key] = (pair[0], time.monotonic() + CACHE_SECONDS)
        _touch(items, item)
        return _account(item)


def _touch(items: list[dict], item: dict) -> None:
    last = item.get("last_seen_at") or ""
    now = _now()
    if last[:15] != now[:15]:  # at most one write every ten minutes per account
        item["last_seen_at"] = now
        with suppress(OSError):
            _write_accounts(items)


def _challenge(status: int = 401, message: str = "请使用试用账号登录") -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = REALM
    return JSONResponse({"detail": message}, status_code=status, headers=headers)


# --------------------------------------------------------------------------- operation log

_ACTIONS: list[tuple[str, re.Pattern, str]] = [
    (method, re.compile(pattern), label)
    for method, pattern, label in [
        ("POST", r"^/api/v1/tasks$", "创建内容任务"),
        ("POST", r"^/api/v1/tasks/[^/]+/stages/(?P<stage>[^/]+)/generate$", "生成：{stage}"),
        ("POST", r"^/api/v1/tasks/[^/]+/stages/(?P<stage>[^/]+)/(confirm|approve)$", "确认：{stage}"),
        ("POST", r"^/api/v1/tasks/[^/]+/stages/(?P<stage>[^/]+)/skip$", "跳过：{stage}"),
        ("DELETE", r"^/api/v1/tasks/[^/]+$", "删除内容任务"),
        ("POST", r"^/api/v1/catalog/publish/listing$", "提交闲鱼上架"),
        ("POST", r"^/api/v1/catalog/publish/promotion$", "提交平台发布"),
        ("POST", r"^/api/v1/catalog/records/[^/]+/retry$", "重试发布"),
        ("POST", r"^/api/v1/catalog/records/[^/]+/resolve$", "核查发布结果"),
        ("DELETE", r"^/api/v1/catalog/records/[^/]+$", "删除发布记录"),
        ("POST", r"^/api/v1/catalog/records/delete-failed$", "清空失败的发布记录"),
        ("POST", r"^/api/v1/automation/browser/platforms/(?P<platform>[^/]+)/clear$", "清除登录缓存：{platform}"),
        ("POST", r"^/api/v1/catalog/products$", "新增商品"),
        ("PATCH", r"^/api/v1/catalog/products/[^/]+$", "修改商品"),
        ("PUT", r"^/api/v1/catalog/products/[^/]+$", "修改商品"),
        ("DELETE", r"^/api/v1/catalog/products/[^/]+$", "删除商品"),
        ("POST", r"^/api/v1/catalog/(generate|batches|batch)", "批量生成内容"),
        ("POST", r"^/api/v1/catalog/contents/[^/]+/(approve|publishable)", "审核通过内容"),
        ("POST", r"^/api/v1/catalog/contents/[^/]+/reject", "退回内容"),
        ("POST", r"^/api/v1/chat/conversations$", "新建 AI 对话"),
        ("POST", r"^/api/v1/chat/conversations/[^/]+/messages", "AI 对话提问"),
        ("DELETE", r"^/api/v1/chat/conversations/[^/]+$", "删除 AI 对话"),
        ("POST", r"^/api/v1/automation/browser/platforms/(?P<platform>[^/]+)/(open|inspect)$", "平台登录：{platform}"),
        ("POST", r"^/api/v1/remote-login/forget$", "清除服务器上的平台登录"),
        ("POST", r"^/api/v1/remote-login/(?P<platform>[^/]+)/start$", "扫码登录：{platform}"),
        ("POST", r"^/api/v1/trial/accounts$", "新建试用账号"),
        ("PATCH", r"^/api/v1/trial/accounts/[^/]+$", "修改试用账号"),
        ("DELETE", r"^/api/v1/trial/accounts/[^/]+$", "删除试用账号"),
        ("POST", r"^/api/v1/library", "上传资料"),
        ("DELETE", r"^/api/v1/library", "删除资料"),
        ("POST", r"^/api/v1/video-projects", "编辑视频"),
    ]
]
_STAGES = {
    "configuration": "任务配置", "topic": "智能标题", "copy": "智能文案", "compliance": "合规检测",
    "image": "智能图片", "storyboard": "分镜", "speech": "配音", "video": "视频生成", "export": "确认导出",
}
_PLATFORMS = {"xianyu": "闲鱼", "douyin": "抖音", "xiaohongshu": "小红书", "kuaishou": "快手"}
_AREAS = {
    "tasks": "内容任务", "catalog": "商品与发布", "chat": "AI 对话", "library": "资料与素材",
    "automation": "自动化发布", "knowledge": "知识库", "video-projects": "视频编辑", "models": "模型设置",
}
_VERBS = {"POST": "提交", "PUT": "保存", "PATCH": "修改", "DELETE": "删除"}


def action_label(method: str, path: str) -> str:
    for expected, pattern, label in _ACTIONS:
        match = pattern.search(path) if expected == method else None
        if match:
            names = match.groupdict()
            return label.format(
                stage=_STAGES.get(names.get("stage") or "", names.get("stage") or ""),
                platform=_PLATFORMS.get(names.get("platform") or "", names.get("platform") or ""),
            )
    parts = path.split("/")
    area = _AREAS.get(parts[3] if len(parts) > 3 else "", "其他")
    return f"{_VERBS.get(method, method)}{area}"


def log_operation(account: Account, method: str, path: str, status: int, ip: str) -> None:
    entry = {
        "at": _now(), "username": account.username, "display_name": account.display_name,
        "action": action_label(method, path), "method": method, "path": path[:300],
        "status": status, "ip": ip,
    }
    path_ = _log_path()
    with _lock:
        path_.parent.mkdir(parents=True, exist_ok=True)
        with path_.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # Keep the file bounded: trim to the newest entries once it grows past the limit.
        with suppress(OSError):
            if path_.stat().st_size > LOG_KEEP * 400:
                lines = path_.read_text(encoding="utf-8").splitlines()[-LOG_KEEP:]
                path_.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_log() -> list[dict]:
    try:
        lines = _log_path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    entries = []
    for line in lines:
        with suppress(ValueError):
            value = json.loads(line)
            if isinstance(value, dict):
                entries.append(value)
    return entries


# --------------------------------------------------------------------------- middleware

async def guard(request: Request, call_next):
    """HTTP middleware: on the trial server every request needs a trial account."""
    if not enabled() or request.url.path in {"/health", "/api/v1/trial/auth-check"}:
        return await call_next(request)
    ip = _client(request)
    try:
        account = authenticate(request.headers.get("authorization"), ip)
    except HTTPException as error:
        return _challenge(error.status_code, error.detail)
    if account is None:
        return _challenge()
    # Data stays in the shared demo workspace (owner "dev-user"); the name on every
    # identity is the signed-in account, so pages and logs show who is working.
    headers = [(key, value) for key, value in request.scope["headers"] if key not in {b"x-user-name", b"x-demo-visitor"}]
    headers += [(b"x-user-name", account.username.encode()), (b"x-demo-visitor", f"{account.username}@{ip}".encode())]
    request.scope["headers"] = headers
    token = current.set(account)
    try:
        response = await call_next(request)
    finally:
        current.reset(token)
    path = request.url.path
    if request.method in CHANGING and path.startswith("/api/") and not QUIET.search(path):
        with suppress(OSError):
            log_operation(account, request.method, path, response.status_code, ip)
    return response


# --------------------------------------------------------------------------- routes

router = APIRouter(prefix="/api/v1/trial", tags=["trial"])
NO_STORE = {"Cache-Control": "no-store"}


class AccountCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    display_name: str = Field(default="", max_length=40)
    password: str = Field(min_length=6, max_length=128)
    is_owner: bool = False


class AccountUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=40)
    password: str | None = Field(default=None, min_length=6, max_length=128)
    enabled: bool | None = None
    is_owner: bool | None = None


def _require_owner() -> Account:
    account = current.get()
    if not enabled() or account is None:
        raise HTTPException(404, "未开启试用账号")
    if not account.is_owner:
        raise HTTPException(403, "只有管理员账号可以管理试用账号")
    return account


@router.get("/auth-check", include_in_schema=False)
def auth_check(request: Request):
    """Asked by the reverse proxy before it serves anything: 204 lets the request through."""
    if not enabled():
        return Response(status_code=204)
    try:
        account = authenticate(request.headers.get("authorization"), _client(request))
    except HTTPException as error:
        return _challenge(error.status_code, error.detail)
    return Response(status_code=204) if account else _challenge()


@router.get("/me")
def me(response: Response) -> dict:
    response.headers.update(NO_STORE)
    account = current.get()
    if not enabled() or account is None:
        return {"enabled": False}
    from . import remote_browser

    return {"enabled": True, "username": account.username, "display_name": account.display_name,
            "is_owner": account.is_owner, "remote_browser": remote_browser.enabled()}


@router.get("/accounts")
def list_accounts(response: Response) -> dict:
    _require_owner()
    response.headers.update(NO_STORE)
    with _lock:
        return {"items": [_public(item) for item in _read_accounts()]}


@router.post("/accounts", status_code=201)
def create_account(body: AccountCreate) -> dict:
    _require_owner()
    if not USERNAME.fullmatch(body.username):
        raise HTTPException(422, "账号只能用 2–32 位字母、数字、点、下划线或短横线")
    with _lock:
        items = _read_accounts()
        if any(item.get("username") == body.username for item in items):
            raise HTTPException(409, "账号已存在")
        item = {"username": body.username, "display_name": body.display_name.strip() or body.username,
                "is_owner": body.is_owner, "enabled": True, "created_at": _now(), "last_seen_at": None}
        _set_password(item, body.password)
        items.append(item)
        _write_accounts(items)
        return _public(item)


@router.patch("/accounts/{username}")
def update_account(username: str, body: AccountUpdate) -> dict:
    me_ = _require_owner()
    with _lock:
        items = _read_accounts()
        item = next((value for value in items if value.get("username") == username), None)
        if item is None:
            raise HTTPException(404, "账号不存在")
        if username == me_.username and (body.enabled is False or body.is_owner is False):
            raise HTTPException(409, "不能停用或取消自己的管理员身份")
        if body.display_name is not None:
            item["display_name"] = body.display_name.strip() or username
        if body.password is not None:
            _set_password(item, body.password)
        if body.enabled is not None:
            item["enabled"] = body.enabled
        if body.is_owner is not None:
            item["is_owner"] = body.is_owner
        _write_accounts(items)
        return _public(item)


@router.delete("/accounts/{username}", status_code=204)
def delete_account(username: str) -> Response:
    me_ = _require_owner()
    if username == me_.username:
        raise HTTPException(409, "不能删除自己正在使用的账号")
    with _lock:
        items = _read_accounts()
        remaining = [item for item in items if item.get("username") != username]
        if len(remaining) == len(items):
            raise HTTPException(404, "账号不存在")
        _write_accounts(remaining)
    return Response(status_code=204)


@router.get("/operations")
def list_operations(
    response: Response,
    username: Annotated[str, Query(max_length=32)] = "",
    day: Annotated[str, Query(max_length=10)] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    account = current.get()
    if not enabled() or account is None:
        raise HTTPException(404, "未开启试用账号")
    response.headers.update(NO_STORE)
    # Everyone sees their own operations; the owner sees every account's.
    who = username if account.is_owner else account.username
    with _lock:
        entries = _read_log()
    entries.reverse()
    if who:
        entries = [entry for entry in entries if entry.get("username") == who]
    if day:
        entries = [entry for entry in entries if str(entry.get("at", "")).startswith(day)]
    start = (page - 1) * page_size
    return {"items": entries[start:start + page_size], "total": len(entries)}


# --------------------------------------------------------------------------- command line

def main(argv: list[str] | None = None) -> int:
    """Create or reset an account on the server: python -m content_service.trial_accounts set NAME --owner."""
    parser = argparse.ArgumentParser(description="试用账号")
    sub = parser.add_subparsers(dest="command", required=True)
    setter = sub.add_parser("set", help="新建账号或重设密码（密码从标准输入读取）")
    setter.add_argument("username")
    setter.add_argument("--display-name", default="")
    setter.add_argument("--owner", action="store_true")
    sub.add_parser("list", help="列出账号")
    args = parser.parse_args(argv)
    with _lock:
        items = _read_accounts()
        if args.command == "list":
            for item in items:
                print(json.dumps(_public(item), ensure_ascii=False))
            return 0
        if not USERNAME.fullmatch(args.username):
            print("账号只能用 2–32 位字母、数字、点、下划线或短横线", file=sys.stderr)
            return 2
        password = (sys.stdin.readline() if not sys.stdin.isatty() else getpass.getpass("密码：")).rstrip("\r\n")
        item = next((value for value in items if value.get("username") == args.username), None)
        if item is None:
            item = {"username": args.username, "created_at": _now(), "last_seen_at": None, "enabled": True}
            items.append(item)
        item["display_name"] = args.display_name or item.get("display_name") or args.username
        item["is_owner"] = bool(args.owner or item.get("is_owner"))
        try:
            _set_password(item, password)
        except HTTPException as error:
            print(error.detail, file=sys.stderr)
            return 2
        _write_accounts(items)
    print(f"已保存账号 {args.username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

