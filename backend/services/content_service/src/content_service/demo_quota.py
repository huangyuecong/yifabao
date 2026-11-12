"""Daily video allowance per visitor for a shared online trial.

Off unless CONTENT_DEMO_VIDEO_DAILY_LIMIT is set. A visitor is the "user@ip" key the
reverse proxy sends in X-Demo-Visitor (trusted only from the local proxy), so testers
sharing one trial login are still counted separately. Users listed in
CONTENT_DEMO_EXEMPT_USERS (e.g. the owner's login) are never limited. Only model-billed
video generation counts; rendering and exports run locally and are not limited.
"""

from __future__ import annotations

import json
import os
import threading
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

visitor: ContextVar[str] = ContextVar("demo_visitor", default="")
_lock = threading.Lock()
_DAY_ZONE = timezone(timedelta(hours=8), "Asia/Shanghai")  # China time, no daylight saving
_LOCAL_PROXIES = {"127.0.0.1", "::1"}


def daily_limit() -> int:
    try:
        return max(0, int(os.getenv("CONTENT_DEMO_VIDEO_DAILY_LIMIT", "0").strip() or 0))
    except ValueError:
        return 0


def visitor_key(client_host: str | None, forwarded: str | None) -> str:
    if client_host in _LOCAL_PROXIES and forwarded and forwarded.strip():
        return forwarded.strip()[:160]
    return client_host or ""


async def tag_visitor(request, call_next):
    """HTTP middleware: remember who is asking, for the video allowance."""
    client = request.client.host if request.client else None
    token = visitor.set(visitor_key(client, request.headers.get("x-demo-visitor")))
    try:
        return await call_next(request)
    finally:
        visitor.reset(token)


def _exempt(key: str) -> bool:
    users = {item.strip() for item in os.getenv("CONTENT_DEMO_EXEMPT_USERS", "").split(",") if item.strip()}
    return bool(users) and key.split("@", 1)[0] in users


def _usage_path() -> Path:
    from .provider_dependencies import get_runtime_settings

    return Path(get_runtime_settings().data_dir) / "demo-usage.json"


def _today() -> str:
    return datetime.now(_DAY_ZONE).date().isoformat()


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _limited_key() -> str | None:
    """The visitor to count, or None when no limit applies."""
    key = visitor.get()
    if not daily_limit() or not key or _exempt(key):
        return None
    return key


def _used(data: dict, key: str) -> int:
    day = data.get(_today())
    return int(day.get(key, 0)) if isinstance(day, dict) else 0


def _refuse(limit: int, used: int) -> HTTPException:
    left = max(0, limit - used)
    tail = f"今天还能生成 {left} 条" if left else "今天的次数已用完，请明天再试"
    return HTTPException(429, f"试用版每人每天最多生成 {limit} 条视频，{tail}")


def check(count: int = 1) -> None:
    """Refuse when the visitor has fewer than `count` videos left today."""
    key = _limited_key()
    if key is None or count <= 0:
        return
    with _lock:
        used = _used(_read(_usage_path()), key)
    if used + count > daily_limit():
        raise _refuse(daily_limit(), used)


def _add(count: int, *, enforce: bool) -> None:
    key = _limited_key()
    if key is None or count <= 0:
        return
    path = _usage_path()
    with _lock:
        data = _read(path)
        used = _used(data, key)
        if enforce and used + count > daily_limit():
            raise _refuse(daily_limit(), used)
        # Only today's counts are kept, so the file never grows.
        day = data.get(_today())
        today = dict(day) if isinstance(day, dict) else {}
        today[key] = used + count
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({_today(): today}, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)


def record(count: int = 1) -> None:
    """Count videos already started (after a separate check)."""
    _add(count, enforce=False)


def use_videos(count: int = 1) -> None:
    """Check and count in one step, for requests that always start new videos."""
    _add(count, enforce=True)
