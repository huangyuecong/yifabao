"""Server-side publishing browser for the online trial: one incognito context per trial account.

The local workbench opens a visible browser on the user's own computer. On the trial
server there is no screen, so with CONTENT_REMOTE_BROWSER=true one Chromium runs on a
virtual display and every trial account gets its own incognito browser context in it
(like an incognito window: separate cookies and storage, nothing written to disk).
The web page shows that context's login tab as a picture: testers scan the platform's
QR code with their phone, and may click or type into the page (for a verification
code). Publish tasks run in the submitting account's own context, so one tester never
publishes with another tester's platform login.

A context closes after ``IDLE_SECONDS`` without use (never while it is publishing), or
when the tester clears it; its logins are gone with it. Each context keeps one marker
tab (``MARKER``) so it can be found again after the service restarts. At most
``CONTENT_REMOTE_BROWSER_MAX`` contexts are open at once.

Playwright, reconnecting for every task, sees all tabs as one context, so the publishers
get an ``AccountContext`` that opens tabs, lists tabs and clears cookies inside the
account's own context. The screen and input use the DevTools protocol over a local
websocket, so polling the picture does not start a Playwright driver each time.
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
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from . import automation_browser as adapter
from . import trial_accounts

PORT = 9300
CDP_URL = f"http://127.0.0.1:{PORT}"
MARKER = "about:blank#yifabao-account="
IDLE_SECONDS = 45 * 60
WINDOW = (1280, 860)
PLATFORMS = ("xianyu", "douyin", "xiaohongshu", "kuaishou")

# A publish task runs outside any request; it names the account whose context to use.
task_owner: ContextVar[str | None] = ContextVar("remote_browser_owner", default=None)
# Set by the login monitor: its checks must not keep an idle context open.
monitoring: ContextVar[bool] = ContextVar("remote_browser_monitoring", default=False)
_lock = threading.RLock()
_last_used: dict[str, float] = {}
_reaper: threading.Thread | None = None


def enabled() -> bool:
    return os.getenv("CONTENT_REMOTE_BROWSER", "false").strip().lower() == "true"


def _max_contexts() -> int:
    try:
        return max(1, int(os.getenv("CONTENT_REMOTE_BROWSER_MAX", "6")))
    except ValueError:
        return 6


def owner() -> str | None:
    """The trial account whose context applies here: the publish task's, else the request's."""
    named = task_owner.get()
    if named:
        return named
    account = trial_accounts.current.get()
    return account.username if account else None


def account_id(username: str) -> str:
    return "trial-" + re.sub(r"[^A-Za-z0-9_-]", "_", username)[:56]


def lock_scope() -> str:
    """Accounts share one browser but must not wait for each other's publish or status locks."""
    username = owner() if enabled() else None
    return "-" + account_id(username) if username else ""


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
                "cdp_url": CDP_URL,
                "platforms": {
                    platform: {"adapter": "xianyu-web-v1" if platform == "xianyu" else "promotion-web-v1", "enabled": True}
                    for platform in PLATFORMS
                },
            }
        },
    }


# --------------------------------------------------------------------------- the browser process

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


def _profile() -> Path:
    from . import automation_browser_session as sessions

    return sessions._profile(CDP_URL)


def start(cdp_url: str, profile: Path, url: str) -> None:
    """Start the shared browser on the virtual display. Accounts never use its default context."""
    with _lock:
        shutil.rmtree(profile, ignore_errors=True)
        profile.mkdir(parents=True, exist_ok=True)
        environment = {**os.environ, "DISPLAY": os.getenv("CONTENT_REMOTE_BROWSER_DISPLAY", ":99")}
        subprocess.Popen(
            [
                _executable(),
                f"--remote-debugging-port={PORT}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={profile}",
                f"--window-size={WINDOW[0]},{WINDOW[1]}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-features=Translate,BackForwardCache,MediaRouter",
                "--lang=zh-CN",
                # Server-specific switches, e.g. --no-sandbox where the service may not
                # create the browser's own sandbox (systemd NoNewPrivileges).
                *os.getenv("CONTENT_REMOTE_BROWSER_FLAGS", "").split(),
                "about:blank",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
        _ensure_reaper()


def verify(cdp_url: str, profile: Path) -> None:
    """The listener on the port must be our browser, bound to loopback, with our profile."""
    rows = [arguments for _pid, arguments in _processes() if _port_of(arguments) == PORT]
    if not rows:
        raise adapter.BrowserPublishError("browser_unreachable", "发布浏览器尚未打开。")
    for arguments in rows:
        profiles = [value.split("=", 1)[1] for value in arguments if value.startswith("--user-data-dir=")]
        if "--remote-debugging-address=127.0.0.1" not in arguments or len(profiles) != 1 or Path(profiles[0]).resolve() != profile:
            raise adapter.BrowserPublishError("browser_profile_conflict", "发布端口被其他浏览器占用，请联系管理员。")
    _touch()


def _running() -> bool:
    from . import automation_browser_session as sessions

    return sessions._port_open(PORT)


def _stop_browser() -> None:
    for pid, arguments in _processes():
        if _port_of(arguments) == PORT:
            with suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
                time.sleep(0.2)
            with suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    shutil.rmtree(_profile(), ignore_errors=True)


# --------------------------------------------------------------------------- incognito contexts

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



def _browser_call(method: str, params: dict | None = None) -> dict:
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request(f"{CDP_URL}/json/version"), timeout=5) as response:
        websocket = json.loads(response.read(64_000))["webSocketDebuggerUrl"]
    client = _DevTools(websocket)
    try:
        return client.call(method, params)
    finally:
        client.close()


def _pages() -> list[dict]:
    return [item for item in _browser_call("Target.getTargets").get("targetInfos", []) if item.get("type") == "page"]


def _contexts() -> dict[str, str]:
    """Open accounts -> their context id, read from the marker tabs (survives a service restart)."""
    if not _running():
        return {}
    return {
        page["url"][len(MARKER):]: page["browserContextId"]
        for page in _pages() if page.get("url", "").startswith(MARKER)
    }


def _touch(username: str | None = None) -> None:
    username = username or owner()
    if username and not monitoring.get():
        _last_used[username] = time.monotonic()


@contextmanager
def _account_lock(username: str, wait: float) -> Iterator[None]:
    """That account's publish lock: held while it publishes, so its context is not closed under it."""
    token = task_owner.set(username)
    try:
        with adapter._browser_lock(CDP_URL, wait=wait):
            yield
    finally:
        task_owner.reset(token)


def _close_context(username: str, context: str) -> None:
    with suppress(Exception):
        _browser_call("Target.disposeBrowserContext", {"browserContextId": context})
    _last_used.pop(username, None)
    _forget_bindings(username)


def _forget_bindings(username: str) -> None:
    bindings = adapter._data_dir() / "private" / "browser-login-bindings"
    if bindings.is_dir():
        for path in bindings.glob(f"{account_id(username)}-*.json"):
            path.unlink(missing_ok=True)


def context_for(username: str, *, create: bool) -> str | None:
    """The account's incognito context id; with ``create``, opens one (closing the most idle if full)."""
    with _lock:
        contexts = _contexts()
        if username in contexts:
            return contexts[username]
        if not create:
            return None
        others = sorted((name for name in contexts if name != username), key=lambda name: _last_used.get(name, 0))
        while len(contexts) >= _max_contexts() and others:
            name = others.pop(0)
            try:
                with _account_lock(name, wait=0):
                    _close_context(name, contexts.pop(name))
            except adapter.BrowserPublishError:
                continue
        if len(contexts) >= _max_contexts():
            raise adapter.BrowserPublishError("browser_busy", "服务器上同时登录的试用账号已满，请过几分钟再试。")
        context = _browser_call("Target.createBrowserContext", {"disposeOnDetach": False})["browserContextId"]
        _browser_call("Target.createTarget", {"url": MARKER + username, "browserContextId": context})
        _forget_bindings(username)  # a new incognito context has no login yet
        _touch(username)
        return context


def has_context(username: str | None = None) -> bool:
    username = username or owner()
    return bool(username) and _running() and context_for(username, create=False) is not None


def running_owners() -> list[str]:
    """Trial accounts with an open context now."""
    try:
        return list(_contexts())
    except Exception:
        return []


class AccountContext:
    """The part of a Playwright BrowserContext the publishers use, inside one account's context."""

    def __init__(self, browser, base, context_id: str):
        self._base = base
        self._id = context_id
        self._session = browser.new_browser_cdp_session()

    def _context_of(self, page) -> str | None:
        try:
            session = self._base.new_cdp_session(page)
        except Exception:
            return None
        try:
            return session.send("Target.getTargetInfo")["targetInfo"].get("browserContextId")
        except Exception:
            return None
        finally:
            with suppress(Exception):
                session.detach()

    @property
    def pages(self) -> list:
        return [page for page in self._base.pages
                if not page.url.startswith(MARKER) and self._context_of(page) == self._id]

    def new_page(self):
        tag = uuid4().hex
        self._session.send("Target.createTarget", {"url": f"about:blank#tab-{tag}", "browserContextId": self._id})
        found = next((page for page in self._base.pages if page.url.endswith(tag)), None)
        return found or self._base.wait_for_event("page", predicate=lambda page: page.url.endswith(tag), timeout=15_000)

    def new_cdp_session(self, page):
        return self._base.new_cdp_session(page)

    def clear_cookies(self, domain=None) -> None:
        if domain is None:
            self._session.send("Storage.clearCookies", {"browserContextId": self._id})
            return
        cookies = self._session.send("Storage.getCookies", {"browserContextId": self._id}).get("cookies", [])
        keep = [cookie for cookie in cookies if not domain.search(cookie.get("domain", "").lstrip("."))]
        self._session.send("Storage.clearCookies", {"browserContextId": self._id})
        fields = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "priority", "sourceScheme")
        restored = [{**{key: cookie[key] for key in fields if key in cookie},
                     **({"expires": cookie["expires"]} if cookie.get("expires", -1) > 0 else {})} for cookie in keep]
        if restored:
            self._session.send("Storage.setCookies", {"cookies": restored, "browserContextId": self._id})


def account_context(browser, base) -> AccountContext:
    username = owner()
    if not username:
        raise adapter.BrowserPublishError("account_required", "请先用试用账号登录。")
    return AccountContext(browser, base, context_for(username, create=True))


def _ensure_reaper() -> None:
    global _reaper
    if _reaper is not None and _reaper.is_alive():
        return

    def reap():
        idle_since = None
        while True:
            time.sleep(60)
            with suppress(Exception), _lock:
                now = time.monotonic()
                contexts = _contexts()
                for name, context in contexts.items():
                    if now - _last_used.get(name, 0) > IDLE_SECONDS:
                        try:
                            with _account_lock(name, wait=0):
                                _close_context(name, context)
                        except adapter.BrowserPublishError:
                            continue  # publishing right now
                # Nobody has used the browser for a while: free its memory.
                if _running() and not _contexts():
                    idle_since = idle_since or now
                    if now - idle_since > IDLE_SECONDS:
                        _stop_browser()
                        idle_since = None
                else:
                    idle_since = None

    _reaper = threading.Thread(target=reap, name="remote-browser-reaper", daemon=True)
    _reaper.start()


# --------------------------------------------------------------------------- screen and input

def _platform_tab(platform: str) -> str:
    """DevTools websocket of the account's tab on this platform's sites (the login or task tab)."""
    from . import automation_browser_session as sessions

    username = owner()
    context = context_for(username, create=False) if username and _running() else None
    if context is None:
        raise adapter.BrowserPublishError("browser_unreachable", "你的服务器浏览器已关闭（闲置 45 分钟会自动关闭），请重新点击扫码登录。")
    hosts = {urlsplit(sessions.PAGES[platform]["login"]).hostname or "", *sessions.LOGIN_HOSTS.get(platform, ())}
    domains = {".".join(host.split(".")[-2:]) for host in hosts} | ({"taobao.com"} if platform == "xianyu" else set())
    tabs = [page for page in _pages() if page.get("browserContextId") == context and not page.get("url", "").startswith(MARKER)]
    chosen = next((tab for tab in reversed(tabs)
                   if ".".join((urlsplit(tab.get("url", "")).hostname or "").split(".")[-2:]) in domains), None)
    chosen = chosen or (tabs[-1] if tabs else None)
    if chosen is None:
        raise adapter.BrowserPublishError("browser_tab_missing", "服务器浏览器里没有打开的页面，请重新点击扫码登录。")
    _touch()
    return f"ws://127.0.0.1:{PORT}/devtools/page/{chosen['targetId']}"


def screen(platform: str) -> tuple[bytes, int, int]:
    client = _DevTools(_platform_tab(platform))
    try:
        with suppress(Exception):
            client.call("Page.bringToFront", timeout=3)
        metrics = client.call("Page.getLayoutMetrics", timeout=5).get("cssVisualViewport", {})
        shot = client.call("Page.captureScreenshot", {"format": "jpeg", "quality": 72}, timeout=10)
    finally:
        client.close()
    return base64.b64decode(shot["data"]), int(metrics.get("clientWidth") or WINDOW[0]), int(metrics.get("clientHeight") or WINDOW[1])


def click(platform: str, x: float, y: float) -> None:
    client = _DevTools(_platform_tab(platform))
    try:
        metrics = client.call("Page.getLayoutMetrics", timeout=5).get("cssVisualViewport", {})
        left = round(x * float(metrics.get("clientWidth") or WINDOW[0]))
        top = round(y * float(metrics.get("clientHeight") or WINDOW[1]))
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            client.call("Input.dispatchMouseEvent", {"type": kind, "x": left, "y": top, "button": "left",
                                                     "clickCount": 0 if kind == "mouseMoved" else 1})
    finally:
        client.close()


KEYS = {
    "Enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"},
    "Backspace": {"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
    "Tab": {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
}


def type_text(platform: str, text: str, key: str | None) -> None:
    client = _DevTools(_platform_tab(platform))
    try:
        if text:
            client.call("Input.insertText", {"text": text})
        if key:
            spec = KEYS[key]
            client.call("Input.dispatchKeyEvent", {"type": "keyDown", **spec})
            client.call("Input.dispatchKeyEvent", {"type": "keyUp", **{k: v for k, v in spec.items() if k != "text"}})
    finally:
        client.close()


def forget(username: str) -> None:
    """Close the account's incognito context: all its platform logins are gone."""
    with _lock, _account_lock(username, wait=30):
        context = _contexts().get(username)
        if context:
            _close_context(username, context)
        _forget_bindings(username)
    from . import login_monitor

    login_monitor.forget(owner=username)


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
    from . import login_monitor

    result = sessions.session(platform, open_page="login")
    login_monitor.remember(result)
    return result


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
