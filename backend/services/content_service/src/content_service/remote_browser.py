"""Server-side publishing browsers for the online trial, one per trial account.

The local workbench opens a visible browser on the user's own computer. On the trial
server there is no screen, so with CONTENT_REMOTE_BROWSER=true each trial account gets
its own Chromium profile and debugging port on a virtual display, and the web page
shows that browser's login tab as a picture: testers scan the platform's QR code with
their phone, and may click or type into the page (for a verification code). Publish
tasks then run in the account's own browser, so one tester never publishes with
another tester's platform login.

Only one browser runs at a time (``CONTENT_REMOTE_BROWSER_MAX``) on the small server;
an idle one is closed after ``IDLE_SECONDS`` and its logins stay in its profile. A
tester can clear their platform logins from the server with ``forget``.

The screen and input use the browser's own DevTools protocol over a local websocket,
so a poll every couple of seconds does not start a Playwright driver each time.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from contextlib import suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from . import automation_browser as adapter
from . import trial_accounts

PORT_BASE = 9300
PORT_SPAN = 500
IDLE_SECONDS = 10 * 60
START_SECONDS = 20
WINDOW = (1280, 860)
PLATFORMS = ("xianyu", "douyin", "xiaohongshu", "kuaishou")
LABELS = {"xianyu": "闲鱼", "douyin": "抖音", "xiaohongshu": "小红书", "kuaishou": "快手"}

# A publish task runs outside any request; it names the account whose browser to use.
task_owner: ContextVar[str | None] = ContextVar("remote_browser_owner", default=None)
_lock = threading.RLock()
_last_used: dict[int, float] = {}
_reaper: threading.Thread | None = None


def enabled() -> bool:
    return os.getenv("CONTENT_REMOTE_BROWSER", "false").strip().lower() == "true"


def _max_browsers() -> int:
    try:
        return max(1, int(os.getenv("CONTENT_REMOTE_BROWSER_MAX", "1")))
    except ValueError:
        return 1


def owner() -> str | None:
    """The trial account whose browser applies here: the publish task's, else the request's."""
    named = task_owner.get()
    if named:
        return named
    account = trial_accounts.current.get()
    return account.username if account else None


def _ports_path() -> Path:
    return adapter._data_dir() / "private" / "remote-browser-ports.json"


def port_for(username: str) -> int:
    """A stable debugging port per account, assigned on first use."""
    with _lock:
        path = _ports_path()
        try:
            ports = json.loads(path.read_text(encoding="utf-8"))
            ports = ports if isinstance(ports, dict) else {}
        except (OSError, ValueError):
            ports = {}
        if username not in ports:
            used = set(ports.values())
            free = next((port for port in range(PORT_BASE, PORT_BASE + PORT_SPAN) if port not in used), None)
            if free is None:
                raise adapter.BrowserPublishError("browser_ports_full", "服务器发布浏览器数量已满，请联系管理员。")
            ports[username] = free
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(ports, ensure_ascii=False), encoding="utf-8")
        return int(ports[username])


def config_override() -> dict | None:
    """The browser configuration for the current trial account (None off the trial server)."""
    if not enabled():
        return None
    username = owner()
    if not username:
        raise adapter.BrowserPublishError("account_required", "请先用试用账号登录。")
    account = trial_accounts.current.get()
    label = (account.display_name if account and account.username == username else username)[:60]
    return {
        "version": 1,
        "accounts": {
            account_id(username): {
                "label": f"{label}的平台账号",
                "cdp_url": f"http://127.0.0.1:{port_for(username)}",
                "platforms": {
                    platform: {"adapter": "xianyu-web-v1" if platform == "xianyu" else "promotion-web-v1", "enabled": True}
                    for platform in PLATFORMS
                },
            }
        },
    }


def account_id(username: str) -> str:
    return "trial-" + re.sub(r"[^A-Za-z0-9_-]", "_", username)[:56]


# --------------------------------------------------------------------------- processes

def _executable() -> str:
    for name in (os.getenv("CONTENT_REMOTE_BROWSER_BIN", ""), "chromium-browser", "chromium", "google-chrome"):
        if name and (found := shutil.which(name)):
            return found
    raise adapter.BrowserPublishError("browser_missing", "服务器尚未安装 Chromium。")


def _processes() -> list[tuple[int, list[str]]]:
    """Main browser processes (not renderers) started with a remote debugging port."""
    found = []
    for entry in Path("/proc").iterdir() if Path("/proc").is_dir() else []:
        if not entry.name.isdigit():
            continue
        try:
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        text = [value.decode("utf-8", "replace") for value in arguments if value]
        if any(value.startswith("--remote-debugging-port=") for value in text) and not any(
            value.startswith("--type=") for value in text
        ):
            found.append((int(entry.name), text))
    return found


def _port_of(arguments: list[str]) -> int | None:
    for value in arguments:
        if value.startswith("--remote-debugging-port="):
            with suppress(ValueError):
                return int(value.split("=", 1)[1])
    return None


def start(cdp_url: str, profile: Path, url: str) -> None:
    """Start this account's browser on the virtual display, closing an idle one if needed."""
    port = urlsplit(cdp_url).port
    with _lock:
        running = [(pid, _port_of(arguments)) for pid, arguments in _processes()]
        others = [(pid, other) for pid, other in running if other != port]
        while len(others) >= _max_browsers():
            pid, other = others.pop(0)
            if not _stop_if_idle(other, pid):
                raise adapter.BrowserPublishError(
                    "browser_busy", "服务器的发布浏览器正在为其他试用账号发布，请过几分钟再试。"
                )
        profile.mkdir(parents=True, exist_ok=True)
        environment = {**os.environ, "DISPLAY": os.getenv("CONTENT_REMOTE_BROWSER_DISPLAY", ":99")}
        subprocess.Popen(
            [
                _executable(),
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={profile}",
                f"--window-size={WINDOW[0]},{WINDOW[1]}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--renderer-process-limit=3",
                "--disable-features=Translate,BackForwardCache,MediaRouter",
                "--lang=zh-CN",
                # Server-specific switches, e.g. --no-sandbox where the service may not
                # create the browser's own sandbox (systemd NoNewPrivileges).
                *os.getenv("CONTENT_REMOTE_BROWSER_FLAGS", "").split(),
                url,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
        _last_used[port] = time.monotonic()
        _ensure_reaper()


def verify(cdp_url: str, profile: Path) -> None:
    """The listener on this port must be the account's own browser bound to loopback."""
    port = urlsplit(cdp_url).port
    rows = [arguments for _pid, arguments in _processes() if _port_of(arguments) == port]
    if not rows:
        raise adapter.BrowserPublishError("browser_unreachable", "发布浏览器尚未打开。")
    for arguments in rows:
        profiles = [value.split("=", 1)[1] for value in arguments if value.startswith("--user-data-dir=")]
        if "--remote-debugging-address=127.0.0.1" not in arguments or len(profiles) != 1 or Path(profiles[0]).resolve() != profile:
            raise adapter.BrowserPublishError("browser_profile_conflict", "发布端口被其他浏览器占用，请联系管理员。")
    _last_used[port] = time.monotonic()


def _stop_if_idle(port: int | None, pid: int) -> bool:
    """Close a browser no publish task is using. Its profile (and logins) stays on disk."""
    if port is None:
        return False
    try:
        with adapter._browser_lock(f"http://127.0.0.1:{port}", wait=0):
            _stop(pid)
            _last_used.pop(port, None)
            return True
    except adapter.BrowserPublishError:
        return False


def _stop(pid: int) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
        time.sleep(0.2)
    with suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def _ensure_reaper() -> None:
    global _reaper
    if _reaper is not None and _reaper.is_alive():
        return

    def reap():
        while True:
            time.sleep(60)
            with suppress(Exception), _lock:
                now = time.monotonic()
                for pid, arguments in _processes():
                    port = _port_of(arguments)
                    if port and PORT_BASE <= port < PORT_BASE + PORT_SPAN and now - _last_used.get(port, 0) > IDLE_SECONDS:
                        _stop_if_idle(port, pid)

    _reaper = threading.Thread(target=reap, name="remote-browser-reaper", daemon=True)
    _reaper.start()


# --------------------------------------------------------------------------- screen and input

class _DevTools:
    """A minimal DevTools-protocol client for one tab."""

    def __init__(self, websocket_url: str):
        from websockets.sync.client import connect

        self.socket = connect(websocket_url, open_timeout=5, max_size=20_000_000)
        self.next_id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 10) -> dict:
        self.next_id += 1
        self.socket.send(json.dumps({"id": self.next_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            message = json.loads(self.socket.recv(timeout=max(0.1, deadline - time.monotonic())))
            if message.get("id") == self.next_id:
                if "error" in message:
                    raise RuntimeError(message["error"].get("message", "devtools error"))
                return message.get("result", {})

    def close(self) -> None:
        with suppress(Exception):
            self.socket.close()


def _targets(port: int) -> list[dict]:
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request(f"http://127.0.0.1:{port}/json/list"), timeout=5) as response:
        items = json.loads(response.read(2_000_000))
    return [item for item in items if item.get("type") == "page" and item.get("webSocketDebuggerUrl")]


def _platform_tab(port: int, platform: str) -> dict:
    """The newest tab on this platform's own sites (the login or task tab)."""
    from . import automation_browser_session as sessions

    hosts = {urlsplit(sessions.PAGES[platform]["login"]).hostname or "", *sessions.LOGIN_HOSTS.get(platform, ())}
    domains = {".".join(host.split(".")[-2:]) for host in hosts} | ({"taobao.com"} if platform == "xianyu" else set())
    tabs = _targets(port)
    for tab in tabs:  # DevTools lists the most recently active tab first
        host = urlsplit(tab.get("url", "")).hostname or ""
        if ".".join(host.split(".")[-2:]) in domains:
            return tab
    if tabs:
        return tabs[0]
    raise adapter.BrowserPublishError("browser_tab_missing", "发布浏览器里没有打开的页面，请重新点击扫码登录。")


def _connection(platform: str) -> tuple[int, dict]:
    from . import automation_browser_session as sessions

    _identifier, account, _recipe = sessions._connection(platform)
    port = urlsplit(account["cdp_url"]).port
    if not sessions._port_open(port):
        raise adapter.BrowserPublishError("browser_unreachable", "发布浏览器已关闭（闲置 10 分钟会自动关闭），请重新点击扫码登录。")
    sessions._verify_dedicated_process(account["cdp_url"], sessions._profile(account["cdp_url"]))
    return port, _platform_tab(port, platform)


def screen(platform: str) -> tuple[bytes, int, int]:
    port, tab = _connection(platform)
    client = _DevTools(tab["webSocketDebuggerUrl"])
    try:
        with suppress(Exception):
            client.call("Page.bringToFront", timeout=3)
        metrics = client.call("Page.getLayoutMetrics", timeout=5).get("cssVisualViewport", {})
        shot = client.call("Page.captureScreenshot", {"format": "jpeg", "quality": 72}, timeout=10)
    finally:
        client.close()
    _last_used[port] = time.monotonic()
    return base64.b64decode(shot["data"]), int(metrics.get("clientWidth") or WINDOW[0]), int(metrics.get("clientHeight") or WINDOW[1])


def click(platform: str, x: float, y: float) -> None:
    port, tab = _connection(platform)
    client = _DevTools(tab["webSocketDebuggerUrl"])
    try:
        metrics = client.call("Page.getLayoutMetrics", timeout=5).get("cssVisualViewport", {})
        left = round(x * float(metrics.get("clientWidth") or WINDOW[0]))
        top = round(y * float(metrics.get("clientHeight") or WINDOW[1]))
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            client.call("Input.dispatchMouseEvent", {"type": kind, "x": left, "y": top, "button": "left",
                                                     "clickCount": 0 if kind == "mouseMoved" else 1})
    finally:
        client.close()
    _last_used[port] = time.monotonic()


KEYS = {
    "Enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"},
    "Backspace": {"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
    "Tab": {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
}


def type_text(platform: str, text: str, key: str | None) -> None:
    port, tab = _connection(platform)
    client = _DevTools(tab["webSocketDebuggerUrl"])
    try:
        if text:
            client.call("Input.insertText", {"text": text})
        if key:
            spec = KEYS[key]
            client.call("Input.dispatchKeyEvent", {"type": "keyDown", **spec})
            client.call("Input.dispatchKeyEvent", {"type": "keyUp", **{k: v for k, v in spec.items() if k != "text"}})
    finally:
        client.close()
    _last_used[port] = time.monotonic()


def forget(username: str) -> None:
    """Close the account's browser and delete its profile: its platform logins are gone."""
    port = port_for(username)
    cdp_url = f"http://127.0.0.1:{port}"
    with _lock, adapter._browser_lock(cdp_url, wait=30):
        for pid, arguments in _processes():
            if _port_of(arguments) == port:
                _stop(pid)
        _last_used.pop(port, None)
        from . import automation_browser_session as sessions

        shutil.rmtree(sessions._profile(cdp_url), ignore_errors=True)
        bindings = adapter._data_dir() / "private" / "browser-login-bindings"
        for path in bindings.glob(f"{account_id(username)}-*.json") if bindings.is_dir() else []:
            path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- routes

def require_trial_account() -> str:
    username = owner() if enabled() else None
    if not username:
        raise HTTPException(404, "服务器扫码登录仅在在线试用中提供")
    return username


router = APIRouter(prefix="/api/v1/remote-login", tags=["remote login"])
Platform = Literal["xianyu", "douyin", "xiaohongshu", "kuaishou"]


class ClickBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class TypeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(default="", max_length=64)
    key: Literal["Enter", "Backspace", "Tab"] | None = None


def _translate(error: adapter.BrowserPublishError) -> HTTPException:
    return HTTPException(409, error.message)


@router.post("/{platform}/start")
def start_login(platform: Platform, _user: str = Depends(require_trial_account)) -> dict:
    """Open this platform's login page in the account's server browser."""
    from . import automation_browser_session as sessions

    return sessions.session(platform, open_page="login")


@router.get("/{platform}/screen")
def login_screen(platform: Platform, _user: str = Depends(require_trial_account)) -> Response:
    try:
        image, width, height = screen(platform)
    except adapter.BrowserPublishError as error:
        raise _translate(error) from None
    except Exception:
        raise HTTPException(503, "暂时取不到浏览器画面，请稍后重试") from None
    return Response(image, media_type="image/jpeg", headers={
        "Cache-Control": "no-store", "X-Page-Width": str(width), "X-Page-Height": str(height),
    })


@router.post("/{platform}/click")
def login_click(platform: Platform, body: ClickBody, _user: str = Depends(require_trial_account)) -> dict:
    try:
        click(platform, body.x, body.y)
    except adapter.BrowserPublishError as error:
        raise _translate(error) from None
    except Exception:
        raise HTTPException(503, "点击没有送达浏览器，请重试") from None
    return {"ok": True}


@router.post("/{platform}/type")
def login_type(platform: Platform, body: TypeBody, _user: str = Depends(require_trial_account)) -> dict:
    if not body.text and not body.key:
        raise HTTPException(422, "请输入内容")
    try:
        type_text(platform, body.text, body.key)
    except adapter.BrowserPublishError as error:
        raise _translate(error) from None
    except Exception:
        raise HTTPException(503, "输入没有送达浏览器，请重试") from None
    return {"ok": True}


@router.post("/forget", status_code=204)
def forget_logins(user: str = Depends(require_trial_account)) -> Response:
    try:
        forget(user)
    except adapter.BrowserPublishError as error:
        raise _translate(error) from None
    return Response(status_code=204)
