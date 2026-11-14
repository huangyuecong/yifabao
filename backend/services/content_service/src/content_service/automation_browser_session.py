"""Visible, dedicated Chromium sessions for listing and promotion platforms.

Opening an official page is separate from proving account identity or enabling an
automatic publishing recipe. This module never reads a normal browser profile.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from . import automation_browser as adapter

PAGES = {
    "xianyu": {
        "login": "https://www.goofish.com/publish",
        "publish": "https://www.goofish.com/publish",
    },
    "douyin": {
        "login": "https://creator.douyin.com/",
        "publish": "https://creator.douyin.com/creator-micro/content/upload",
    },
    "xiaohongshu": {
        "login": "https://creator.xiaohongshu.com/",
        "publish": "https://creator.xiaohongshu.com/publish/publish",
    },
    "kuaishou": {
        "login": "https://cp.kuaishou.com/",
        "publish": "https://cp.kuaishou.com/article/publish/video",
    },
}
DEFAULT_CDP = "http://127.0.0.1:9222"
LOGIN_HOSTS = {"kuaishou": frozenset({"passport.kuaishou.com"})}
LOGIN_LABELS = {
    "xianyu": ("登录", "立即登录", "扫码登录", "密码登录", "短信登录", "登录后发布"),
    "douyin": ("扫码登录", "短信登录"),
    "xiaohongshu": ("短信登录", "发送验证码"),
    "kuaishou": ("立即登录", "扫码登录", "短信登录"),
}
# Observed in the signed-in creator home on 2026-10-23. This identifies an
# account; it does not validate any publishing action or upload selector.
ACCOUNT_PAGES = {
    "douyin": "https://creator.douyin.com/creator-micro/home",
    "xianyu": "https://www.goofish.com/account",
    "xiaohongshu": "https://creator.xiaohongshu.com/new/home",
    "kuaishou": "https://cp.kuaishou.com/profile",
}
# A publish task that finds the platform signed out waits this long for a QR login.
LOGIN_WAIT_SECONDS = 5 * 60
LOGIN_POLL_SECONDS = 5
# Observed on the authenticated account page on 2026-10-24. Use the member
# identifier, not a mutable/non-unique nickname from a product or public profile.
XIANYU_ACCOUNT_SELECTOR = 'div.row--KKR0VClL:has(> div.col1--YZ589g04:text-is("会员名")) > div.col2--ytB8sy2a'


def _error(code: str, message: str):
    return adapter.BrowserPublishError(code, message)


def _atomic_json(path: Path, document: dict, *, create_only: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".browser-config-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if create_only:
            # Linking a fully written file is atomic and fails if a configuration
            # was created concurrently. Never replace another process's settings.
            with suppress(FileExistsError):
                os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if temporary is not None:
            with suppress(FileNotFoundError):
                temporary.unlink()


def _ensure_initial_config() -> None:
    """Only create a missing config; never replace existing account settings."""
    path = adapter._config_path()
    if path.exists():
        return
    document = {
        "version": 1,
        "accounts": {
            "local-promotion": {
                "label": "本机发布账号",
                "cdp_url": DEFAULT_CDP,
                "platforms": {
                    platform: {"adapter": "xianyu-web-v1" if platform == "xianyu" else "promotion-web-v1", "enabled": True}
                    for platform in PAGES
                },
            }
        },
    }
    _atomic_json(path, document, create_only=True)


def _connection(platform: str) -> tuple[str | None, dict, dict]:
    if platform not in PAGES:
        raise _error("platform_unsupported", "网页登录支持闲鱼、抖音、小红书和快手。")
    try:
        config = adapter._load_config()
    except adapter.BrowserPublishError as error:
        if error.code != "config_missing":
            raise
        config = {"accounts": {}}
    candidates = [
        (identifier, account, account["platforms"][platform])
        for identifier, account in config["accounts"].items()
        if platform in account["platforms"]
    ]
    if len(candidates) > 1:
        raise _error(
            "account_ambiguous", "该平台配置了多个浏览器账号，请先在本机配置中指定使用的账号。"
        )
    if candidates:
        identifier, account, recipe = candidates[0]
        return identifier, account, recipe if isinstance(recipe, dict) else {}
    if len(config["accounts"]) == 1:
        identifier, account = next(iter(config["accounts"].items()))
        return identifier, account, {}
    return None, {"cdp_url": DEFAULT_CDP, "label": "本机发布账号"}, {}


def _profile(cdp_url: str) -> Path:
    adapter._validate_cdp_url(cdp_url)
    port = urlsplit(cdp_url).port
    name = "publishing-browser-profile" + (f"-{port}" if port != 9222 else "")
    private = (adapter._data_dir() / "private").resolve()
    profile = (private / name).resolve()
    if not profile.is_relative_to(private):
        raise _error("profile_invalid", "发布浏览器资料目录必须位于本项目的 private 目录。")
    return profile


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.4)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def _listener_processes(port: int) -> list[dict]:
    if os.name != "nt":
        raise _error(
            "browser_host_unsupported", "请在运行工作台的 Windows 电脑上打开专用发布浏览器。"
        )
    # Port is an integer from the locally configured, validated CDP URL. No URL,
    # profile path, or request-controlled string is interpolated into this script.
    script = (
        "$ErrorActionPreference='Stop'; "
        f"$listeners=@(Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue); "
        "$rows=@(foreach($listener in $listeners){ "
        "$process=Get-CimInstance Win32_Process -Filter ('ProcessId='+$listener.OwningProcess); "
        "[pscustomobject]@{address=$listener.LocalAddress;command=$process.CommandLine}}); "
        "ConvertTo-Json -InputObject $rows -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=12,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise _error(
            "browser_process_unverified", "无法确认专用浏览器进程，请关闭专用窗口后重新打开。"
        )
    rows = json.loads(result.stdout or "[]")
    if not rows and _port_open(port):
        # Some Windows setups (VPN/security drivers) hide listening sockets from
        # Get-NetTCPConnection. Fall back to the browser processes that were started
        # with this debugging port; _verify_dedicated_process still requires every
        # one of them to be the dedicated profile bound to 127.0.0.1.
        rows = _debugging_processes(port)
    return rows


def _debugging_processes(port: int) -> list[dict]:
    script = (
        "$ErrorActionPreference='Stop'; "
        f"$rows=@(Get-CimInstance Win32_Process -Filter \"CommandLine LIKE '%--remote-debugging-port={port}%'\" "
        "| Where-Object { $_.CommandLine -notlike '*--type=*' } "
        "| ForEach-Object { [pscustomobject]@{command=$_.CommandLine} }); "
        "ConvertTo-Json -InputObject $rows -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=12,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise _error(
            "browser_process_unverified", "无法确认专用浏览器进程，请关闭专用窗口后重新打开。"
        )
    rows = []
    for row in json.loads(result.stdout or "[]"):
        arguments = _windows_arguments(row.get("command") or "")
        addresses = [
            value.split("=", 1)[1]
            for value in arguments
            if value.startswith("--remote-debugging-address=")
        ]
        rows.append({"address": addresses[0] if len(addresses) == 1 else None, "command": row.get("command")})
    return rows


def _windows_arguments(command: str) -> list[str]:
    import ctypes
    from ctypes import wintypes

    count = ctypes.c_int()
    shell = ctypes.windll.shell32
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    pointer = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not pointer:
        return []
    try:
        return [pointer[index] for index in range(count.value)]
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(pointer)


def _verify_dedicated_process(cdp_url: str, profile: Path) -> None:
    from . import remote_browser

    if remote_browser.enabled():
        remote_browser.verify(cdp_url, profile)
        return
    port = urlsplit(cdp_url).port
    rows = _listener_processes(port)
    if not rows:
        raise _error("browser_unreachable", "专用发布浏览器尚未打开。")
    for row in rows:
        arguments = _windows_arguments(row.get("command") or "")
        profiles = [
            value.split("=", 1)[1] for value in arguments if value.startswith("--user-data-dir=")
        ]
        if (
            row.get("address") not in {"127.0.0.1", "::1"}
            or f"--remote-debugging-port={port}" not in arguments
            or "--remote-debugging-address=127.0.0.1" not in arguments
            or len(profiles) != 1
            or Path(profiles[0]).resolve() != profile
        ):
            raise _error(
                "browser_profile_conflict",
                "发布端口被其他浏览器占用，请关闭冲突窗口；工作台不会访问普通浏览器资料。",
            )


def _browser_executable() -> Path:
    candidates = [
        Path(os.getenv("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.getenv("PROGRAMFILES", "C:/Program Files"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.getenv("PROGRAMFILES", "C:/Program Files"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.getenv("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise _error("browser_missing", "未找到 Edge 或 Chrome，请先在本机安装浏览器。")


def _start_browser(cdp_url: str, profile: Path, url: str) -> None:
    from . import remote_browser

    if remote_browser.enabled():
        remote_browser.start(cdp_url, profile, url)
        return
    if os.name != "nt":
        raise _error(
            "browser_host_unsupported", "请在运行工作台的 Windows 电脑上打开专用发布浏览器。"
        )
    profile.mkdir(parents=True, exist_ok=True)
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 1  # This is the requested visible login window.
    subprocess.Popen(
        [
            str(_browser_executable()),
            f"--remote-debugging-port={urlsplit(cdp_url).port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        startupinfo=startup,
        close_fds=True,
    )


BROWSER_START_SECONDS = 20


def ensure_browser(cdp_url: str) -> bool:
    """Open the dedicated publishing browser if it is closed; True when this call started it.

    A publish task should not fail just because the window was closed: the profile keeps
    the platform logins, so starting it again is all that is needed.
    """
    profile = _profile(cdp_url)
    port = urlsplit(cdp_url).port
    started = False
    if not _port_open(port):
        _start_browser(cdp_url, profile, "about:blank")
        started = True
        deadline = time.monotonic() + BROWSER_START_SECONDS
        while not _port_open(port):
            if time.monotonic() >= deadline:
                raise _error(
                    "browser_start_timeout", "专用发布浏览器启动超时，请点击“打开发布页”后重试。"
                )
            time.sleep(0.3)
    _verify_dedicated_process(cdp_url, profile)
    return started


def close_start_tab(context, keep) -> None:
    """Close the blank tab a freshly started browser opens, keeping the task's own tab."""
    for page in list(context.pages):
        if page is not keep and page.url in {"about:blank", "edge://newtab/", "chrome://newtab/"}:
            with suppress(Exception):
                page.close()


@contextmanager
def _connected(cdp_url: str):
    if not adapter._playwright_available():
        raise _error(
            "browser_dependency_missing", "浏览器发布组件尚未安装，请运行项目启动器完成依赖检查。"
        )
    runtime = None
    try:
        websocket = adapter._cdp_websocket(cdp_url)
        runtime = importlib.import_module("playwright.sync_api").sync_playwright().start()
        browser = runtime.chromium.connect_over_cdp(websocket, timeout=10_000)
        if not browser.contexts:
            raise _error("browser_context_missing", "专用浏览器尚未建立可用会话，请稍后重试。")
        yield browser.contexts[0]
    finally:
        # Disconnect the automation client, retaining the visible browser and login.
        if runtime is not None:
            with suppress(Exception):
                runtime.stop()


def _official_page(page, platform: str) -> bool:
    try:
        parsed = urlsplit(page.url)
        if (
            parsed.hostname in LOGIN_HOSTS.get(platform, ())
            and parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 443}
        ):
            return True
        adapter._validate_platform_url(platform, page.url)
        return True
    except adapter.BrowserPublishError:
        return False


def _base(platform: str, state="not_opened", message="尚未打开网页登录页面。") -> dict:
    return {
        "platform": platform,
        "connected": False,
        "state": state,
        "publish_ready": False,
        "message": message,
        "url": PAGES[platform]["login"],
    }


def _bound_identity(platform: str, identifier: str, identity: str) -> bool:
    """Bind the observed identity separately; never enable a publishing recipe."""
    path = (
        adapter._data_dir() / "private" / "browser-login-bindings" / f"{identifier}-{platform}.json"
    )
    if platform == "douyin":
        identity = re.sub(r"^抖音号\s*[:：]\s*", "抖音号：", identity)
    digest = hashlib.sha256((platform + "\0" + identity).encode()).hexdigest()
    if path.exists():
        try:
            if path.stat().st_size > 4000:
                raise ValueError("large binding file")
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("version") != 1 or not re.fullmatch(
                r"[0-9a-f]{64}", document.get("identity_sha256", "")
            ):
                raise ValueError("invalid bindings")
        except (ValueError, TypeError, OSError):
            raise _error(
                "browser_binding_invalid", "账号绑定记录无法读取，请检查本机浏览器配置。"
            ) from None
        return document["identity_sha256"] == digest
    _atomic_json(path, {"version": 1, "identity_sha256": digest}, create_only=True)
    # A different local worker may have created the binding first.
    return json.loads(path.read_text(encoding="utf-8"))["identity_sha256"] == digest


def _observed(platform, page, identifier, account, recipe, *, fresh_identity=None) -> dict:
    result = _base(platform, "unknown", "网页已打开，请完成登录；当前账号尚未核验。")
    result["connected"] = True
    parsed = urlsplit(page.url)
    if platform == "xianyu":
        blocked = page.get_by_text("为了保障您的体验，请使用正常浏览器访问闲鱼~", exact=True)
        if any(blocked.nth(index).is_visible() for index in range(blocked.count())):
            result["message"] = "闲鱼暂未允许此浏览器访问，请在专用浏览器中处理页面提示后重试。"
            return result
    login_visible = any(
        any(matches.nth(index).is_visible() for index in range(matches.count()))
        for label in LOGIN_LABELS[platform]
        for matches in [page.get_by_text(label, exact=True)]
    )
    if (
        parsed.path.startswith(("/login", "/passport"))
        or parsed.hostname in LOGIN_HOSTS.get(platform, ())
        or login_visible
    ):
        result.update(state="login_required", message="请在专用浏览器中扫码或完成平台登录。")
        return result
    identity_recipe = _identity_recipe(recipe)
    for spec in identity_recipe.get("blockers", []):
        if not adapter._valid_locator(spec):
            continue
        locator = adapter._locator(page, spec)
        if any(locator.nth(index).is_visible() for index in range(locator.count())):
            result.update(
                state="login_required", message="页面显示登录、验证或错误提示，请在浏览器中处理。"
            )
            return result
    observed = None
    explicit_identity = (
        adapter._valid_locator(identity_recipe.get("account_locator"))
        and isinstance(identity_recipe.get("expected_account"), str)
        and identity_recipe["expected_account"].strip()
    )
    if not explicit_identity and platform in {"xiaohongshu", "kuaishou"}:
        result["message"] = "网页已打开，当前平台的账号自动识别尚待核验；可先在官方网页完成登录。"
    if explicit_identity:
        locator = adapter._locator(page, identity_recipe["account_locator"])
        if locator.count() == 1 and locator.is_visible():
            actual = locator.inner_text(timeout=2000).strip()
            if actual != identity_recipe["expected_account"].strip():
                result["message"] = "当前网页账号与配置的账号不一致，请切换账号后重新检测。"
                return result
            observed = actual
    elif (
        platform == "douyin"
        and parsed.hostname == "creator.douyin.com"
        and parsed.path == "/creator-micro/home"
    ):
        locator = page.locator("div.unique_id-EuH8eA")
        if locator.count() == 1 and locator.is_visible():
            match = re.fullmatch(
                r"抖音号\s*[:：]\s*([^\r\n\t]{1,100})", locator.inner_text(timeout=2000).strip()
            )
            if match and match.group(1).strip():
                observed = "抖音号：" + match.group(1).strip()
    elif platform == "xianyu" and parsed.hostname == "www.goofish.com" and parsed.path == "/account":
        locator = page.locator(XIANYU_ACCOUNT_SELECTOR)
        if locator.count() == 1 and locator.is_visible():
            member = locator.inner_text(timeout=2000).strip()
            if re.fullmatch(r"[^\r\n\t]{1,100}", member):
                observed = "闲鱼会员名：" + member
    elif platform == "xiaohongshu" and parsed.hostname == "creator.xiaohongshu.com" and parsed.path == "/new/home":
        locator = page.get_by_text(re.compile(r'^小红书账号\s*[:：]\s*[A-Za-z0-9_-]+$'))
        if locator.count() == 1 and locator.is_visible():
            observed = re.sub(r'\s+', '', locator.inner_text()).replace(':', '：')
    elif platform == "kuaishou" and parsed.hostname == "cp.kuaishou.com" and parsed.path == "/profile":
        # Some accounts have no public Kwai ID. The page's own fresh account
        # response provides a stable numeric UID; never bind a shared nickname.
        if fresh_identity and page.locator('.user-info-name').is_visible():
            observed = fresh_identity
    if observed and identifier:
        if not _bound_identity(platform, identifier, observed):
            result["message"] = "当前网页账号与此前绑定的账号不同，请切换回已绑定账号。"
            return result
        result.update(
            state="logged_in",
            account_id=identifier,
            account_label=account["label"],
            publish_ready=not adapter._recipe_issues(platform, recipe),
            operations_ready={
                operation: not adapter._operation_issues(
                    platform, operation, adapter._operation_recipe(recipe, operation)
                )
                for operation in (("publish", "relist", "delist") if platform == "xianyu" else ("publish",))
            },
            message="已核对并绑定当前网页账号。",
        )
        if not result["publish_ready"]:
            result["message"] += "可打开平台发布页；自动发布流程尚未完成核验。"
    return result


def _identity_recipe(recipe: dict) -> dict:
    if adapter._valid_locator(recipe.get("account_locator")):
        return recipe
    # Management can be configured without a new-listing recipe. Use its own
    # identity page only when all configured operations agree on the account.
    management = recipe.get("management", {})
    candidates = [
        value for value in management.values()
        if isinstance(value, dict) and adapter._valid_locator(value.get("account_locator"))
        and isinstance(value.get("expected_account"), str) and value["expected_account"].strip()
    ] if isinstance(management, dict) else []
    if candidates and len({value["expected_account"].strip() for value in candidates}) == 1:
        return candidates[0]
    return recipe


def _inspect_identity(context, page, platform, identifier, account, recipe) -> dict:
    """Load fresh account evidence; an existing tab may retain a signed-out user.

    ``page`` may be None when no platform tab is open; the account page alone decides.
    """
    if page is not None:
        current = _observed(platform, page, None, account, recipe)
        if current['state'] == 'login_required':
            return current
    identity_recipe = _identity_recipe(recipe)
    target = identity_recipe.get("account_check_url")
    if not target and adapter._valid_locator(identity_recipe.get("account_locator")):
        target = identity_recipe.get("management_url") or identity_recipe.get("publish_url") or PAGES[platform]["login"]
    target = target or ACCOUNT_PAGES.get(platform)
    if not target:
        # No configured or observed identity selector exists for these platforms.
        # They cannot become logged_in from this existing-page branch.
        if page is None:
            return {**_base(platform), "connected": True}
        return _observed(platform, page, identifier, account, recipe)
    adapter._validate_platform_url(platform, target)
    unknown = {
        **_base(platform, "unknown", "暂未取得当前账号的有效页面证据，请稍后重新检测。"),
        "connected": True,
    }
    restore_page = None
    # Reading UI focus avoids a status refresh pulling the user away from a QR
    # code on another platform. Do not inspect storage or authentication tokens.
    for candidate in context.pages:
        # Edge exposes native permission dialogs as CDP pages. Evaluating their
        # document can stall forever; they are not user-editable platform tabs.
        if urlsplit(candidate.url).scheme not in {'https', 'http'}:
            continue
        try:
            state = candidate.evaluate(
                "({visible: document.visibilityState === 'visible', focused: document.hasFocus()})"
            )
            if state.get("focused"):
                restore_page = candidate
                break
            if state.get("visible") and restore_page is None:
                restore_page = candidate
        except Exception:
            continue
    account_page = None
    account_identity = []
    try:
        # Never verify or bind from a previously opened home/editor DOM. Another
        # tab may have changed the shared session while that DOM retained user A.
        account_page = context.new_page()
        if platform == "kuaishou":
            def observe_account(response):
                parts = urlsplit(response.url)
                if (parts.scheme == 'https' and parts.hostname == 'cp.kuaishou.com'
                        and parts.path == '/rest/v2/creator/pc/authority/account/current'
                        and response.request.method == 'POST' and response.status == 200):
                    with suppress(Exception):
                        document = response.json()
                        data = document.get('data', {})
                        uid = data.get('userId')
                        if document.get('result') == 1 and data.get('logined') is True and type(uid) is int and uid > 0:
                            account_identity.append('快手UID：' + str(uid))
            account_page.on('response', observe_account)
        account_page.goto(target, wait_until="domcontentloaded", timeout=30_000)
        if not _official_page(account_page, platform):
            return unknown
        if urlsplit(account_page.url).path not in {"/login", "/passport"}:
            with suppress(Exception):
                if adapter._valid_locator(identity_recipe.get("account_locator")):
                    signal = adapter._locator(account_page, identity_recipe["account_locator"])
                elif platform == "douyin":
                    signal = account_page.locator("div.unique_id-EuH8eA")
                elif platform == "xianyu":
                    signal = account_page.locator(XIANYU_ACCOUNT_SELECTOR)
                elif platform == "xiaohongshu":
                    signal = account_page.get_by_text(re.compile(r'^小红书账号\s*[:：]\s*[A-Za-z0-9_-]+$'))
                elif platform == "kuaishou":
                    signal = account_page.locator('.user-info-name')
                else:
                    signal = account_page.get_by_text(LOGIN_LABELS[platform][0], exact=True)
                # Locator unions remain live as the SPA renders. Filtering each
                # branch to visible avoids an attached hidden account shell
                # masking a later login prompt. No fixed sleep or unbounded wait.
                signal = signal.filter(visible=True)
                for label in LOGIN_LABELS[platform]:
                    signal = signal.or_(
                        account_page.get_by_text(label, exact=True).filter(visible=True)
                    )
                signal.first.wait_for(state="visible", timeout=15_000)
        return _observed(platform, account_page, identifier, account, recipe,
                         fresh_identity=account_identity[-1] if account_identity else None)
    except Exception:
        return unknown
    finally:
        if account_page is not None:
            with suppress(Exception):
                account_page.close()
            if restore_page is not None:
                with suppress(Exception):
                    restore_page.bring_to_front()


def _kuaishou_login_entry(page) -> None:
    # This exact public-page entry was observed in the live creator UI.
    # Inspect never clicks it, and existing editor routes are preserved.
    parts = urlsplit(page.url)
    if parts.hostname != "cp.kuaishou.com" or parts.path != "/profile":
        return
    login = page.get_by_text("立即登录", exact=True)
    if login.count() == 1 and login.is_visible():
        with suppress(Exception):
            login.click(timeout=5000)
            page.wait_for_url(
                lambda url: urlsplit(url).hostname == "passport.kuaishou.com", timeout=5000
            )


def wait_for_login(
    context, page, platform, identifier, account, recipe, *, timeout=None,
    sleep=time.sleep, clock=time.monotonic,
) -> dict:
    """Show the login page in the task's own tab until the configured account signs in.

    Used by a publish task that found the platform signed out: instead of
    failing, it waits for the user to scan the code, then carries on.
    """
    timeout = LOGIN_WAIT_SECONDS if timeout is None else timeout
    with suppress(Exception):
        page.goto(PAGES[platform]["login"], wait_until="domcontentloaded", timeout=30_000)
    with suppress(Exception):
        page.bring_to_front()
        if platform == "kuaishou":
            _kuaishou_login_entry(page)
    deadline = clock() + timeout
    while clock() < deadline:
        sleep(LOGIN_POLL_SECONDS)
        # While the login form is visible this returns at once without
        # opening a tab; only a signed-in page triggers the account check.
        with suppress(Exception):
            state = _inspect_identity(context, page, platform, identifier, account, recipe)
            if state["state"] == "logged_in" and state.get("account_id") == identifier:
                return state
    minutes = max(1, round(timeout / 60))
    raise _error("login_timeout", f"等待登录超过 {minutes} 分钟，未发布。请在专用浏览器登录后重试。")


def ensure_logged_in(context, page, platform, identifier, account, recipe) -> dict:
    """A publish task's first account check: logged in → go on; signed out → log in first."""
    state = _inspect_identity(context, page, platform, identifier, account, recipe)
    if state["state"] == "unknown":
        # Account pages sometimes render slowly; one more look before giving up.
        state = _inspect_identity(context, page, platform, identifier, account, recipe)
    if state["state"] == "login_required":
        state = {**wait_for_login(context, page, platform, identifier, account, recipe), "waited_for_login": True}
    if state["state"] != "logged_in" or state.get("account_id") != identifier:
        raise _error("account_unverified", state.get("message") or "平台账号尚未确认。")
    return state


def _session_on_context(context, platform, identifier, account, recipe, open_page=None):
    # A platform's public feed is not its creator/login workspace. Bringing a
    # feed tab forward made the login button appear to do nothing useful.
    creator_host = urlsplit(PAGES[platform]["login"]).hostname
    pages = [
        page
        for page in context.pages
        if _official_page(page, platform)
        and urlsplit(page.url).hostname in {creator_host, *LOGIN_HOSTS.get(platform, ())}
        and (platform != "xianyu" or urlsplit(page.url).path == "/publish")
    ]
    page = pages[-1] if pages else None
    if open_page is not None:
        target = PAGES[platform][open_page]
        if open_page == "publish":
            target_parts = urlsplit(target)
            matching = [
                candidate
                for candidate in pages
                if urlsplit(candidate.url).hostname == target_parts.hostname
                and urlsplit(candidate.url).path == target_parts.path
            ]
            if matching:
                page = matching[-1]
        # Login reveals the existing page, preserving QR codes and unsaved edits.
        # A different publishing route gets a new tab instead of replacing it.
        if page is None or (
            open_page == "publish" and urlsplit(page.url).path != urlsplit(target).path
        ):
            page = context.new_page()
        if not _official_page(page, platform):
            with suppress(Exception):
                page.goto(target, wait_until="domcontentloaded", timeout=15_000)
        page.bring_to_front()
        if platform == "kuaishou" and open_page == "login":
            _kuaishou_login_entry(page)
    if page is None:
        # No platform tab is open; the login cookie may still be valid.
        return _inspect_identity(context, None, platform, identifier, account, recipe)
    if not _official_page(page, platform):
        return {
            **_base(platform, "unknown", "浏览器已打开，平台页面尚在加载或跳转。"),
            "connected": True,
        }
    return _inspect_identity(context, page, platform, identifier, account, recipe)


def session(platform: str, *, open_page: str | None = None) -> dict:
    if platform not in PAGES or open_page not in {None, "login", "publish"}:
        raise _error("platform_unsupported", "请选择受支持的发布平台和页面。")
    try:
        identifier, account, recipe = _connection(platform)
        cdp_url = account["cdp_url"]
        profile = _profile(cdp_url)
        with adapter._browser_lock(cdp_url, wait=adapter.STATUS_LOCK_WAIT_SECONDS, purpose="status"):
            if open_page is not None:
                _ensure_initial_config()
                identifier, account, recipe = _connection(platform)
                if account["cdp_url"] != cdp_url:
                    raise _error("browser_config_changed", "浏览器配置已变化，请重新打开登录页面。")
            if not _port_open(urlsplit(cdp_url).port):
                if open_page is None:
                    return _base(platform)
                _start_browser(cdp_url, profile, PAGES[platform][open_page])
                deadline = time.monotonic() + 12
                while not _port_open(urlsplit(cdp_url).port):
                    if time.monotonic() >= deadline:
                        raise _error(
                            "browser_start_timeout", "专用浏览器启动较慢，请稍后点击检测登录。"
                        )
                    time.sleep(0.2)
            _verify_dedicated_process(cdp_url, profile)
            with _connected(cdp_url) as context:
                return _session_on_context(
                    context, platform, identifier, account, recipe, open_page
                )
    except adapter.BrowserPublishError as error:
        return _base(platform, "unavailable", error.message)
    except Exception:
        return _base(platform, "unavailable", "无法连接专用发布浏览器，请重试打开网页登录。")


def sessions(platforms: tuple[str, ...] | None = None) -> dict:
    # Platform cards normally share one profile. Verify that process and
    # connect once per request; account DOM evidence is still read live each time.
    groups = {}
    result = {}
    selected = platforms if platforms is not None else tuple(PAGES)
    if any(platform not in PAGES for platform in selected):
        raise _error("platform_unsupported", "请选择受支持的发布平台。")
    for platform in selected:
        try:
            identifier, account, recipe = _connection(platform)
            groups.setdefault(account["cdp_url"], []).append(
                (platform, identifier, account, recipe)
            )
        except adapter.BrowserPublishError as error:
            result[platform] = _base(platform, "unavailable", error.message)
    for cdp_url, entries in groups.items():
        try:
            profile = _profile(cdp_url)
            if not _port_open(urlsplit(cdp_url).port):
                for platform, *_ in entries:
                    result[platform] = _base(platform)
                continue
            with adapter._browser_lock(cdp_url, wait=adapter.STATUS_LOCK_WAIT_SECONDS, purpose="status"):
                _verify_dedicated_process(cdp_url, profile)
                with _connected(cdp_url) as context:
                    for platform, identifier, account, recipe in entries:
                        try:
                            result[platform] = _session_on_context(
                                context, platform, identifier, account, recipe
                            )
                        except Exception:
                            result[platform] = {
                                **_base(
                                    platform, "unknown", "页面状态暂时无法读取，请稍后重新检测。"
                                ),
                                "connected": True,
                            }
        except adapter.BrowserPublishError as error:
            for platform, *_ in entries:
                result[platform] = _base(platform, "unavailable", error.message)
        except Exception:
            for platform, *_ in entries:
                result[platform] = _base(
                    platform, "unavailable", "无法连接专用发布浏览器，请重试打开网页登录。"
                )
    return {"items": [result[platform] for platform in selected]}
