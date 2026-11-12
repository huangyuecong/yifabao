"""Private Android screen sessions, account binding and exclusive device access."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from PIL import Image

from .android_connection import adb, connect, ensure_connection
from .automation_browser import BrowserPublishError
from .automation_platforms import get_platform

PACKAGES = {
    "xianyu": "com.taobao.idlefish",
    "xiaohongshu": "com.xingin.xhs",
    "douyin": "com.ss.android.ugc.aweme",
    "kuaishou": "com.smile.gifmaker",
}
SESSION_SECONDS = 900
_ACCOUNT_PREFIX = {
    "douyin": "抖音号",
    "xiaohongshu": "小红书号",
    "kuaishou": "快手号",
    "xianyu": "闲鱼号|鱼号|会员名",
}


def fail(code, message):
    raise BrowserPublishError(code, message)


def _directory() -> Path:
    path = (
        Path(os.getenv("CONTENT_DATA_DIR", ".data/content")).resolve() / "private" / "android-state"
    )
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _key(config, device_id):
    device = config["devices"].get(device_id)
    if device is None:
        fail("android_device_missing", "请选择已配置的安卓设备。")
    tunnel = config.get("ssh_tunnel")
    endpoint = (
        f"{tunnel['user']}@{tunnel['host']}:{tunnel['remote_port']}" if tunnel else device["serial"]
    )
    return hashlib.sha256(endpoint.encode()).hexdigest()[:32]


@contextmanager
def device_lock(config, device_id):
    path = _directory() / (_key(config, device_id) + ".lock")
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fail("android_device_busy", "这台安卓正在执行操作，请稍后重试。")
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def read_state(config, device_id):
    path = _directory() / (_key(config, device_id) + ".json")
    if not path.exists():
        return {"apps": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("apps"), dict):
            raise ValueError()
        return value
    except (ValueError, OSError):
        fail("android_state_invalid", "安卓账号状态无法读取，请检查服务存储。")


def save_state(config, device_id, value):
    path = _directory() / (_key(config, device_id) + ".json")
    descriptor, temp = tempfile.mkstemp(dir=path.parent, prefix=".android-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def session_active(state):
    return state.get("session", {}).get("expires_at", 0) > time.time()


def _session(state, token, owner):
    session = state.get("session", {})
    if not session_active(state) or session.get("id") != token or session.get("owner") != owner:
        fail("android_session_expired", "屏幕会话已结束或不属于当前用户，请重新打开。")
    session["expires_at"] = time.time() + SESSION_SECONDS
    return session


def nodes(device):
    xml = device.dump_hierarchy(compressed=False, max_depth=40)
    if len(xml) > 2_000_000:
        fail("android_ui_invalid", "App 页面过大，请返回后重试。")
    return [dict(node.attrib) for node in ET.fromstring(xml).iter("node")]


def account_identity(platform, items):
    """Only the App's own visible account identifier can establish a binding."""
    texts = [
        item.get("text", "").strip()
        for item in items
        if item.get("package") == PACKAGES[platform] and item.get("password") != "true"
    ]
    pattern = re.compile(
        r"(?:" + _ACCOUNT_PREFIX[platform] + r")\s*[:：]?\s*([A-Za-z0-9_.-]{3,64})\Z"
    )
    identifiers = set()
    for index, text in enumerate(texts):
        match = pattern.search(text)
        if match:
            identifiers.add(match[1])
        elif (
            re.fullmatch(r"(?:" + _ACCOUNT_PREFIX[platform] + r")\s*[:：]?", text)
            and index + 1 < len(texts)
            and re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", texts[index + 1])
        ):
            identifiers.add(texts[index + 1])
    return next(iter(identifiers)) if len(identifiers) == 1 else None


def own_profile(platform, items):
    return any(
        item.get("package") == PACKAGES[platform]
        and any(
            word in item.get("text", "") + item.get("content-desc", "")
            for word in ("编辑资料", "编辑个人资料", "编辑主页", "个人资料编辑")
        )
        for item in items
    )


def _state_of(platform, items):
    if account_identity(platform, items):
        return "logged_in"
    texts = "\n".join(
        item.get("text", "") for item in items if item.get("package") == PACKAGES[platform]
    )
    if any(
        text in texts
        for text in (
            "手机号登录",
            "验证码登录",
            "获取验证码",
            "登录/注册",
            "登录后",
            "登录即可",
            "立即登录",
        )
    ):
        return "login_required"
    if any(text in texts for text in ("个人信息保护", "隐私政策", "用户协议")) and "同意" in texts:
        return "needs_consent"
    return "unverified"


def launch(device, platform):
    package = PACKAGES.get(platform)
    if not package:
        fail("platform_unsupported", "平台不支持安卓登录。")
    if not device.app_info(package):
        fail("android_app_missing", "该平台 App 尚未安装。")
    # This shared, small server runs one foreground platform at a time. Force-stop
    # releases other Apps' background memory without clearing their account data.
    for other in PACKAGES.values():
        if other != package:
            device.shell(["am", "force-stop", other], timeout=15)
    # Resolve the installed launcher; no hard-coded Activity names or user-supplied intents.
    activity = (
        device.shell(["cmd", "package", "resolve-activity", "--brief", package])
        .output.strip()
        .splitlines()[-1]
    )
    if not activity.startswith(package + "/") or not re.fullmatch(r"[A-Za-z0-9_.$/]+", activity):
        fail("android_app_launch_failed", "无法找到 App 的启动入口。")
    result = device.shell(["am", "start", "-W", "-n", activity], timeout=25)
    if result.exit_code or "Error:" in result.output:
        fail("android_app_launch_failed", "App 启动失败，请重新打开并检查运行状态。")


def _frame(config, device_id, device, state):
    state.update(connected=True, checked_at=time.time())
    session = state["session"]
    try:
        screenshot = device.screenshot()
    except Exception:
        # Read-only fallback after Android restarts its accessibility connection.
        raw = adb(
            config,
            ["-s", config["devices"][device_id]["serial"], "exec-out", "screencap", "-p"],
            timeout=15,
        )
        screenshot = Image.open(io.BytesIO(raw))
    output = io.BytesIO()
    screenshot.convert("RGB").save(output, "JPEG", quality=78)
    session.update(
        frame_id=uuid4().hex, frame_at=time.time(), width=screenshot.width, height=screenshot.height
    )
    app = state["apps"].setdefault(session["platform"], {})
    try:
        foreground = device.app_current().get("package", "")
        if foreground in {"com.android.launcher3", "com.android.launcher"}:
            app["runtime_state"] = "exited"
            app["error"] = "App 已返回安卓桌面，请重新打开；若再次退出，需处理此版本的运行兼容性。"
        elif foreground == "com.android.permissioncontroller":
            running = device.shell(["pidof", PACKAGES[session["platform"]]], timeout=8)
            if not running.output.strip():
                app["runtime_state"] = "exited"
                app["error"] = "App 已退出，屏幕上残留的权限弹窗不代表 App 仍在运行。"
            else:
                app["runtime_state"] = "permission_required"
                app.pop("error", None)
        elif foreground == PACKAGES[session["platform"]]:
            app["runtime_state"] = "running"
            app.pop("error", None)
    except Exception:
        pass
    save_state(config, device_id, state)
    return {
        "session_id": session["id"],
        "platform": session["platform"],
        "frame_id": session["frame_id"],
        "width": screenshot.width,
        "height": screenshot.height,
        "image": "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode(),
        "app": public_app(state["apps"].get(session["platform"], {}))
        if state["apps"].get(session["platform"], {}).get("owner") in (None, session["owner"])
        else {"state": "unverified"},
        "draft": (
            {
                "run_id": session["draft"]["run_id"],
                "note": session["draft"]["note"],
                "media_count": len(session["draft"]["imported"]),
                "files": [
                    item["target"].rsplit("/", 1)[-1] for item in session["draft"]["imported"]
                ],
                "fields": {
                    key: value
                    for key, value in session["draft"]["payload"].items()
                    if key
                    in {"title", "description", "price", "condition", "shipping_fee", "location"}
                },
            }
            if session.get("draft")
            else None
        ),
    }


def public_app(app):
    return {
        key: app[key]
        for key in (
            "installed",
            "version",
            "state",
            "account_id",
            "account_label",
            "checked_at",
            "publish_verified",
            "error",
            "runtime_state",
        )
        if key in app
    }


def open_session(config, device_id, platform, owner):
    if platform not in config["devices"].get(device_id, {}).get("platforms", []):
        fail("android_platform_missing", "该设备未绑定此平台。")
    with device_lock(config, device_id):
        state = read_state(config, device_id)
        if state.get("prepared", {}).get("expires_at", 0) > time.time():
            fail("android_publication_busy", "当前草稿正在等待发布，请等待发布结果后再打开登录。")
        if session_active(state):
            current = state["session"]
            if current.get("owner") != owner:
                fail("android_session_busy", "安卓屏幕正在使用中，请先关闭原会话。")
            if current.get("platform") == platform:
                _session(state, current["id"], owner)
                device = connect(config, device_id)
                info = device.app_info(PACKAGES[platform]) or {}
                app = state["apps"].setdefault(platform, {})
                version = info.get("versionName", "")
                if app.get("version") != version:
                    app.pop("publish_verified", None)
                app.update(installed=bool(info), version=version)
                # A page refresh can lose the screen token while the App has
                # exited. Reopening login should recover that App, but must not
                # navigate away from an in-progress draft or a system dialog.
                if not current.get("draft"):
                    try:
                        foreground = device.app_current().get("package", "")
                    except Exception:
                        foreground = ""
                    if foreground in {"com.android.launcher3", "com.android.launcher"}:
                        launch(device, platform)
                return _frame(config, device_id, device, state)
            if current.get("draft"):
                fail("android_session_busy", "安卓正在编辑草稿，请先关闭草稿画面后再切换平台。")
            # The same user's login-only screen can be replaced. A stale tab's
            # token can no longer operate on, or close, the new platform session.
        device = connect(config, device_id)
        launch(device, platform)
        info = device.app_info(PACKAGES[platform])
        app = state["apps"].setdefault(platform, {})
        if app.get("version") != info.get("versionName", ""):
            app.pop("publish_verified", None)
        app.update(installed=True, version=info.get("versionName", ""))
        state.update(
            connected=True,
            checked_at=time.time(),
            session={
                "id": uuid4().hex,
                "owner": owner,
                "platform": platform,
                "expires_at": time.time() + SESSION_SECONDS,
            },
        )
        state.pop("prepared", None)
        return _frame(config, device_id, device, state)


def screen(config, device_id, token, owner):
    with device_lock(config, device_id):
        state = read_state(config, device_id)
        _session(state, token, owner)
        return _frame(config, device_id, connect(config, device_id), state)


def control(config, device_id, token, owner, action):
    with device_lock(config, device_id):
        state = read_state(config, device_id)
        session = _session(state, token, owner)
        if (
            action.get("frame_id") != session.get("frame_id")
            or time.time() - session.get("frame_at", 0) > 20
        ):
            fail("android_stale_frame", "屏幕已变化，请刷新画面后操作。")
        device = connect(config, device_id)
        kind = action["kind"]
        session["frame_id"] = None
        save_state(config, device_id, state)
        if kind in {"tap", "swipe"}:
            for x, y in [(action.get("x"), action.get("y"))] + (
                [(action.get("end_x"), action.get("end_y"))] if kind == "swipe" else []
            ):
                if (
                    type(x) is not int
                    or type(y) is not int
                    or not 0 <= x < session["width"]
                    or not 0 <= y < session["height"]
                ):
                    fail("android_coordinates_invalid", "屏幕坐标无效。")
            from .android_publishing import SUBMIT_LABELS, bounds, visible

            for item in visible(nodes(device), session["platform"]):
                if (
                    item.get("text", "").strip() in SUBMIT_LABELS
                    or item.get("content-desc", "").strip() in SUBMIT_LABELS
                ):
                    x1, y1, x2, y2 = bounds(item)
                    if (x1 <= action["x"] <= x2 and y1 <= action["y"] <= y2) or (
                        kind == "swipe"
                        and x1 <= action["end_x"] <= x2
                        and y1 <= action["end_y"] <= y2
                    ):
                        fail(
                            "android_use_publish_action",
                            "请使用工作台草稿的发布按钮，以便记录发布结果。",
                        )
            args = (
                ["input", "tap", str(action["x"]), str(action["y"])]
                if kind == "tap"
                else [
                    "input",
                    "swipe",
                    str(action["x"]),
                    str(action["y"]),
                    str(action["end_x"]),
                    str(action["end_y"]),
                    "350",
                ]
            )
            # ADB executes exactly once. UI RPC libraries may retry a failed click.
            adb(config, ["-s", config["devices"][device_id]["serial"], "shell", *args])
        elif kind == "text":
            text = action.get("text", "")
            if not isinstance(text, str) or not 1 <= len(text) <= 2000 or "\x00" in text:
                fail("android_text_invalid", "请输入 1–2000 字的内容。")
            device.send_keys(text, clear=True)
        elif kind == "reopen":
            launch(device, session["platform"])
        elif kind in {"back", "home"}:
            adb(
                config,
                [
                    "-s",
                    config["devices"][device_id]["serial"],
                    "shell",
                    "input",
                    "keyevent",
                    "4" if kind == "back" else "3",
                ],
            )
        else:
            fail("android_action_invalid", "不支持此安卓操作。")
        return _frame(config, device_id, device, state)


def verify_account(config, device_id, token, owner):
    with device_lock(config, device_id):
        state = read_state(config, device_id)
        session = _session(state, token, owner)
        platform = session["platform"]
        device = connect(config, device_id)
        items = nodes(device)
        identity = account_identity(platform, items) if own_profile(platform, items) else None
        app = state["apps"].setdefault(platform, {})
        app.update(state=_state_of(platform, items), checked_at=time.time())
        if not identity:
            app.pop("account_id", None)
            app.pop("identity", None)
            app.pop("publish_verified", None)
            save_state(config, device_id, state)
            fail(
                "android_login_not_verified",
                "请完成登录，进入“我”的个人主页或账号设置，显示平台账号 ID 后再检测。",
            )
        if app.get("identity") != identity:
            app.pop("publish_verified", None)
        app.update(
            identity=identity,
            account_label=f"{get_platform(platform).label} · {identity}",
            owner=owner,
            account_id=hashlib.sha256(
                f"{_key(config, device_id)}:{platform}:{identity}".encode()
            ).hexdigest()[:24],
        )
        return _frame(config, device_id, device, state)


def close_session(config, device_id, token, owner):
    with device_lock(config, device_id):
        state = read_state(config, device_id)
        session = state.get("session")
        if not session:
            return {"closed": True}
        if session.get("id") != token or session.get("owner") != owner:
            fail("android_session_expired", "原屏幕会话已结束，当前屏幕不会被关闭。")
        # Closing one's own expired session is safe and needs no ADB round trip.
        # Repeated DELETE calls after a page/dialog unmount are idempotent.
        state.pop("session", None)
        save_state(config, device_id, state)
    return {"closed": True}


def enrich_status(config, document, owner=None):
    any_connected = False
    for item in document["devices"]:
        state = read_state(config, item["device_id"])
        connected = bool(state.get("connected") and time.time() - state.get("checked_at", 0) < 120)
        any_connected |= connected
        item.update(
            state="device" if connected else "not_checked",
            busy=session_active(state),
            storage=state.get("storage"),
            apps={
                key: public_app(value)
                if owner is None or value.get("owner") in (None, owner)
                else {
                    "installed": value.get("installed"),
                    "version": value.get("version"),
                    "state": "unverified",
                }
                for key, value in state["apps"].items()
            },
        )
        if state.get("checked_at"):
            item["connected"] = connected
    document.update(
        available=document["adb_configured"],
        ready=any_connected,
        missing=[] if any_connected else ["请先检测并连接安卓设备。"],
    )
    for platform, item in document["platforms"].items():
        accounts = []
        for device in document["devices"]:
            app = device.get("apps", {}).get(platform, {})
            if app.get("state") == "logged_in" and app.get("account_id"):
                accounts.append(
                    {
                        "account_id": app["account_id"],
                        "label": app["account_label"],
                        "device_id": device["device_id"],
                        "configured": True,
                    }
                )
        verified = False
        item.update(
            configured=bool(accounts),
            ready=bool(accounts) and verified,
            accounts=accounts,
            missing=["请先登录并检测平台账号。"]
            if not accounts
            else ([] if verified else ["可从草稿进入 App 核对发布；无人值守发布待实机验证。"]),
        )
    return document


def inspect_configured_devices(config, document, owner=None):
    from .automation_android import _device_states

    try:
        ensure_connection(config)
        states = _device_states(adb(config, ["devices", "-l"]).decode("utf-8", "replace"))
        for device_id, spec in config["devices"].items():
            with device_lock(config, device_id):
                state = read_state(config, device_id)
                connected = states.get(spec["serial"]) == "device"
                state.update(connected=connected, checked_at=time.time())
                if connected:
                    # Package metadata can be read while a user logs in; no app is launched.
                    for platform in spec["platforms"]:
                        info = adb(
                            config,
                            [
                                "-s",
                                spec["serial"],
                                "shell",
                                "dumpsys",
                                "package",
                                PACKAGES[platform],
                            ],
                        ).decode("utf-8", "replace")
                        match = re.search(r"\bversionName=([^\s]+)", info)
                        app = state["apps"].setdefault(platform, {})
                        version = match[1] if match else ""
                        if app.get("version") != version:
                            app.pop("publish_verified", None)
                        app.update(installed=bool(match), version=version)
                        app.setdefault("state", "unverified" if match else "not_installed")
                if connected:
                    from types import SimpleNamespace

                    from .android_media import storage_snapshot

                    def storage_shell(args, timeout=20, serial=spec["serial"]):
                        output = adb(
                            config, ["-s", serial, "shell", *args], timeout=timeout
                        ).decode("utf-8", "replace")
                        return SimpleNamespace(output=output, exit_code=0)

                    try:
                        storage_snapshot(SimpleNamespace(shell=storage_shell), state)
                    except BrowserPublishError:
                        state.pop("storage", None)
                save_state(config, device_id, state)
        return enrich_status(config, document, owner)
    except BrowserPublishError as exc:
        document.update(ready=False, missing=[exc.message])
        return document
