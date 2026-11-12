"""Prepare App forms and submit once, only after account and form verification.

App text selectors are deliberately strict. Unknown screens stop for inspection;
no coordinate recipe, guessed account, blind retry or browser fallback is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .android_connection import adb, connect
from .android_control import (
    PACKAGES,
    _frame,
    _session,
    account_identity,
    device_lock,
    fail,
    launch,
    nodes,
    open_session,
    own_profile,
    read_state,
    save_state,
    session_active,
)
from .automation_browser import BrowserPublishError

# These are installed, exported Android share activities, resolved from the
# target APKs on the server, not third-party publishing APIs.
SHARE_ACTIVITIES = {
    "douyin": "com.ss.android.ugc.aweme/.share.ui.SystemShareNewActivity",
    "xiaohongshu": "com.xingin.xhs/.routers.RouterPageActivity",
    "kuaishou": "com.smile.gifmaker/com.yxcorp.gifshow.activity.UriRouterActivity.alias",
    "xianyu": "com.taobao.idlefish/com.taobao.fleamarket.home.activity.InitActivity",
}
SUBMIT_LABELS = {"发布", "确认发布", "立即发布", "发布笔记", "确认上架", "确认下架", "重新上架"}
_SUCCESS = ("发布成功", "发布完成", "笔记已发布", "作品发布成功", "上架成功", "下架成功")
_BLOCKERS = (
    "获取验证码",
    "验证码登录",
    "手机号登录",
    "请完成验证",
    "拖动滑块",
    "安全验证",
    "实名认证",
    "网络异常",
    "上传失败",
)


def bounds(node):
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if not match:
        fail("android_ui_invalid", "页面控件位置无效。")
    x1, y1, x2, y2 = map(int, match.groups())
    if x2 <= x1 or y2 <= y1:
        fail("android_ui_invalid", "页面控件不可见。")
    return x1, y1, x2, y2


def click(config, device_id, node):
    x1, y1, x2, y2 = bounds(node)
    adb(
        config,
        [
            "-s",
            config["devices"][device_id]["serial"],
            "shell",
            "input",
            "tap",
            str((x1 + x2) // 2),
            str((y1 + y2) // 2),
        ],
    )


def visible(items, platform):
    return [
        item
        for item in items
        if item.get("package") == PACKAGES[platform]
        and item.get("enabled") == "true"
        and item.get("visible-to-user", "true") == "true"
    ]


def unique_text(items, labels, *, optional=False):
    matches = [
        item
        for item in items
        if item.get("text", "").strip() in labels or item.get("content-desc", "").strip() in labels
    ]
    # Nested accessibility descriptions may refer to the same visible control.
    matches = list({item.get("bounds"): item for item in matches}.values())
    if len(matches) != 1:
        if optional and not matches:
            return None
        fail("android_form_changed", "无法唯一定位 App 控件，请打开安卓屏幕检查当前页面。")
    return matches[0]


def guard(device, platform):
    if device.app_current().get("package") != PACKAGES[platform]:
        fail("android_app_not_foreground", "App 已退出或弹出系统页面，请打开屏幕处理后重试。")
    items = visible(nodes(device), platform)
    text = "\n".join(item.get("text", "") for item in items)
    if any(word in text for word in _BLOCKERS):
        fail(
            "android_app_requires_action", "App 要求登录、验证或处理上传错误，请打开安卓屏幕完成。"
        )
    return items


def check_account(device, config, device_id, platform, binding):
    launch(device, platform)
    items = guard(device, platform)
    identity = account_identity(platform, items) if own_profile(platform, items) else None
    if not identity:
        profile = unique_text(items, {"我", "我的", "我，按钮", "我的，按钮"}, optional=True)
        if profile:
            click(config, device_id, profile)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                items = guard(device, platform)
                identity = (
                    account_identity(platform, items) if own_profile(platform, items) else None
                )
                if identity:
                    break
                time.sleep(0.5)
    if not identity or identity != binding.get("identity"):
        fail("android_account_changed", "当前 App 账号无法确认或与绑定账号不一致，请重新检测登录。")


def validated_media(platform, payload):
    values = payload.get("image_paths", []) if platform == "xianyu" else [payload.get("video_path")]
    if not isinstance(values, list) or not 1 <= len(values) <= (9 if platform == "xianyu" else 1):
        fail("android_media_invalid", "请先准备商品图片或宣传视频。")
    root = Path(os.getenv("CONTENT_DATA_DIR", ".data/content")).resolve() / "automation-media"
    result = []
    for value in values:
        if not isinstance(value, str):
            fail("android_media_invalid", "发布素材无效。")
        path = Path(value).resolve()
        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or not 0 < path.stat().st_size <= 500 * 1024 * 1024
        ):
            fail("android_media_invalid", "素材不存在、过大或不属于此工作台。")
        allowed = (
            {".jpg", ".jpeg", ".png", ".webp"} if platform == "xianyu" else {".mp4", ".mov", ".m4v"}
        )
        if path.suffix.lower() not in allowed:
            fail("android_media_invalid", "素材格式不支持。")
        result.append(path)
    return result


def import_media(device, platform, payload, *, config, device_id, state):
    from .android_media import stage

    return stage(device, config, device_id, state, platform, validated_media(platform, payload))


def _set_field(device, config, device_id, item, value):
    if item.get("text") == value:
        return
    click(config, device_id, item)
    device.send_keys(value, clear=True)


def fill_text(device, config, device_id, platform, payload):
    items = guard(device, platform)
    unique_text(items, {"发布", "确认发布", "立即发布", "发布笔记"})
    fields = [item for item in items if item.get("class") == "android.widget.EditText"]
    title = payload.get("title", "").strip()
    description = payload.get("description", "").strip()
    if not title or not description:
        fail("android_copy_missing", "发布标题和文案不能为空。")
    titles = [
        item
        for item in fields
        if "标题" in (item.get("text", "") + item.get("hint", "") + item.get("content-desc", ""))
    ]
    bodies = [
        item
        for item in fields
        if any(
            word in item.get("text", "") + item.get("hint", "") + item.get("content-desc", "")
            for word in ("描述", "分享", "文案", "正文", "介绍", "这一刻")
        )
        and item not in titles
    ]
    if len(titles) == 1 and len(bodies) == 1:
        _set_field(device, config, device_id, titles[0], title)
        _set_field(device, config, device_id, bodies[0], description)
    elif len(fields) == 1:
        _set_field(device, config, device_id, fields[0], title + "\n" + description)
    else:
        fail("android_copy_field_missing", "请在安卓发布页显示标题或文案输入框，再继续检查。")
    if "mInputShown=true" in device.shell(["dumpsys", "input_method"]).output:
        device.press("back")


def verify_form(device, platform, payload, imported):
    items = guard(device, platform)
    texts = [item.get("text", "").strip() for item in items]
    if any(message in text for message in _SUCCESS for text in texts):
        fail("android_stale_receipt", "页面已有发布结果，请先核查，不能再次提交。")
    title, description = payload.get("title", "").strip(), payload.get("description", "").strip()
    if not ((title in texts and description in texts) or title + "\n" + description in texts):
        fail("android_copy_not_verified", "App 页面上的标题或文案与草稿不一致。")
    if not imported:
        fail("android_media_not_verified", "缺少本次素材传输记录。")
    if platform == "xianyu":
        # Every commercial field must be visible and exact, rather than taking
        # defaults from the previously published item or the logged-in account.
        for key, label in (
            ("price", "售价"),
            ("condition", "成色"),
            ("shipping_fee", "运费"),
            ("location", "发货城市"),
        ):
            value = str(payload.get(key, "")).strip()
            expected = {value, f"¥{value}", f"￥{value}", f"{label} {value}", f"{label}：{value}"}
            if key == "shipping_fee" and value in {"0", "0.0", "0.00"}:
                expected |= {"包邮", "运费：包邮"}
            if not value or not expected.intersection(texts):
                fail("android_product_field_not_verified", f"请在闲鱼发布页核对并填写{label}。")
        count = len(imported)
        if not any(
            text in {f"{count}/9", f"{count}/18", f"已选{count}张", f"已选 {count} 张"}
            for text in texts
        ):
            fail("android_images_not_verified", "请在闲鱼确认本次商品图片已全部选中。")
    else:
        descriptions = [item.get("content-desc", "") for item in items]
        if not any(
            "封面" in text or "视频预览" in text or "视频时长" in text
            for text in texts + descriptions
        ):
            fail("android_video_not_verified", "请完成视频选择与处理，进入可见视频封面的发布页。")
    submit = unique_text(items, {"发布", "确认发布", "立即发布", "发布笔记"})
    return submit


def payload_digest(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def form_digest(items):
    # Ignore animation/focus state, but bind all visible text, controls and geometry.
    return hashlib.sha256(
        json.dumps(
            [
                {
                    key: item.get(key, "")
                    for key in (
                        "text",
                        "content-desc",
                        "resource-id",
                        "bounds",
                        "class",
                        "enabled",
                        "selected",
                        "checked",
                    )
                }
                for item in items
            ],
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def bound_account(config, device_id, platform, owner, account_id=None):
    app = read_state(config, device_id)["apps"].get(platform, {})
    if app.get("owner") != owner or app.get("state") != "logged_in" or not app.get("account_id"):
        fail("android_login_required", "请先登录并检测绑定该平台账号。")
    if account_id and account_id != app["account_id"]:
        fail("android_account_changed", "所选发布账号与 App 当前绑定账号不一致，请核对执行规则。")
    return app


def open_draft(config, device_id, platform, owner, run_id, payload, account_id):
    validated_media(platform, payload)
    binding = bound_account(config, device_id, platform, owner, account_id)
    frame = open_session(config, device_id, platform, owner)
    if frame.get("draft") and frame["draft"]["run_id"] != run_id:
        fail("android_session_busy", "设备正在编辑其他草稿，请先关闭原会话。")
    with device_lock(config, device_id):
        state = read_state(config, device_id)
        session = _session(state, frame["session_id"], owner)
        current_draft = session.get("draft", {})
        if current_draft and current_draft.get("run_id") != run_id:
            fail("android_session_busy", "设备正在编辑其他草稿，请先关闭原会话。")
        device = connect(config, device_id)
        version = device.app_info(PACKAGES[platform]).get("versionName", "")
        if (
            current_draft.get("run_id") == run_id
            and current_draft.get("digest") == payload_digest(payload)
            and current_draft.get("account_id") == binding["account_id"]
            and current_draft.get("version") == version
        ):
            # Resume an unchanged editor in place. A saved copy/media change
            # must be imported again instead of returning a stale App draft.
            return _frame(config, device_id, device, state)
        try:
            check_account(device, config, device_id, platform, binding)
            imported = import_media(
                device, platform, payload, config=config, device_id=device_id, state=state
            )
            mime = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
                ".mp4": "video/mp4",
                ".mov": "video/quicktime",
                ".m4v": "video/x-m4v",
            }[Path(imported[0]["target"]).suffix.lower()]
            note = "素材已导入 AIWorkbench 相册，请在 App 选择本次素材并进入发布页。"
            shared = False
            try:
                result = device.shell(
                    [
                        "am",
                        "start",
                        "-a",
                        "android.intent.action.SEND",
                        "-t",
                        mime,
                        "-n",
                        SHARE_ACTIVITIES[platform],
                        "--eu",
                        "android.intent.extra.STREAM",
                        imported[0]["uri"],
                        "--grant-read-uri-permission",
                    ],
                    timeout=25,
                )
                shared = result.exit_code == 0 and "Error" not in result.output
            except Exception:
                # App versions may reject their exported share activity. The
                # imported media is still usable through the App's own album.
                pass
            if shared:
                # No guessed 'next' coordinates: let the operator navigate the App's editor.
                try:
                    fill_text(device, config, device_id, platform, payload)
                    note = "文案已填写，请核对素材与发布信息。"
                except BrowserPublishError:
                    pass
            session["draft"] = {
                "run_id": run_id,
                "payload": payload,
                "digest": payload_digest(payload),
                "imported": imported,
                "account_id": binding["account_id"],
                "version": version,
                "note": note,
            }
            return _frame(config, device_id, device, state)
        except Exception:
            # Keep the login screen recoverable after an account/storage error.
            # An old draft is not eligible for submission after preparation failed.
            session.pop("draft", None)
            save_state(config, device_id, state)
            raise


def fill_draft(config, device_id, token, owner):
    with device_lock(config, device_id):
        state = read_state(config, device_id)
        session = _session(state, token, owner)
        draft = session.get("draft")
        if not draft:
            fail("android_draft_missing", "请从待发布草稿打开 App。")
        device = connect(config, device_id)
        fill_text(device, config, device_id, session["platform"], draft["payload"])
        draft["note"] = "文案已填写，请核对素材与发布信息。"
        return _frame(config, device_id, device, state)


def reserve_submission(config, device_id, token, owner, run_id, payload, frame_id):
    """Caller holds device lock and has an explicit per-draft media confirmation."""
    state = read_state(config, device_id)
    session = _session(state, token, owner)
    draft = session.get("draft", {})
    if draft.get("run_id") != run_id or draft.get("digest") != payload_digest(payload):
        fail("android_draft_changed", "草稿已变化，请重新导入 App 并检查。")
    if session.get("frame_id") != frame_id or time.time() - session.get("frame_at", 0) > 20:
        fail("android_stale_frame", "请刷新安卓画面后再发布。")
    platform = session["platform"]
    app = bound_account(config, device_id, platform, owner, draft["account_id"])
    device = connect(config, device_id)
    version = device.app_info(PACKAGES[platform]).get("versionName", "")
    if version != draft["version"]:
        fail("android_app_updated", "App 版本已变化，请重新检查草稿。")
    verify_form(device, platform, payload, draft["imported"])
    state["prepared"] = {
        "run_id": run_id,
        "platform": platform,
        "owner": owner,
        "digest": draft["digest"],
        "imported": draft["imported"],
        "account_id": app["account_id"],
        "version": version,
        "expires_at": time.time() + 120,
        "form_digest": form_digest(guard(device, platform)),
    }
    state.pop("session", None)
    save_state(config, device_id, state)
    return platform, app["account_id"]


def preflight(workflow):
    from .automation_android import _load_config

    config = _load_config()
    device_id = workflow.get("device_id")
    if device_id not in config["devices"]:
        fail("android_device_missing", "请选择已配置的安卓设备。")
    if workflow.get("operation", "publish") != "publish":
        fail("android_management_unverified", "闲鱼 App 重新上架和下架尚待账号登录后适配。")
    if not workflow.get("_android_run_id") or len(workflow["platforms"]) != 1:
        fail(
            "android_form_unverified",
            "请先生成草稿，从草稿打开 App 核对后发布；无人值守发布尚待实机验证。",
        )
    state = read_state(config, device_id)
    prepared = state.get("prepared", {})
    if session_active(state) or prepared.get("expires_at", 0) < time.time():
        fail("android_form_unverified", "发布表单已过期或设备正在使用，请重新检查草稿。")
    if (
        prepared.get("owner") != workflow.get("_android_owner")
        or prepared.get("run_id") != workflow["_android_run_id"]
        or prepared.get("platform") != workflow["platforms"][0]
    ):
        fail("android_form_unverified", "没有此任务已核对的 App 发布表单。")
    bound_account(
        config, device_id, prepared["platform"], prepared["owner"], prepared["account_id"]
    )


def receipt_from_url(platform, url):
    if not isinstance(url, str) or len(url) > 1000:
        return None
    match = re.search(r"https://[^\s，。]+", url)
    if not match:
        return None
    url = match[0]
    parts = urlsplit(url)
    domains = {
        "douyin": {"www.douyin.com", "v.douyin.com"},
        "xiaohongshu": {"www.xiaohongshu.com", "xhslink.com"},
        "kuaishou": {"www.kuaishou.com", "v.kuaishou.com"},
        "xianyu": {"www.goofish.com", "m.tb.cn"},
    }
    try:
        valid_port = parts.port in (None, 443)
    except ValueError:
        return None
    if (
        parts.hostname not in domains[platform]
        or parts.username
        or parts.password
        or not valid_port
    ):
        return None
    # Short URLs and profile URLs cannot establish an item-specific receipt.
    patterns = {
        "douyin": r"/video/(\d{8,30})/?",
        "xiaohongshu": r"/(?:explore|discovery/item)/([a-fA-F0-9]{24})/?",
        "kuaishou": r"/short-video/([A-Za-z0-9_-]{5,100})/?",
    }
    if platform == "xianyu":
        identifier = parse_qs(parts.query).get("id", [""])[0]
        if (
            parts.hostname != "www.goofish.com"
            or parts.path != "/item"
            or not re.fullmatch(r"[0-9]{5,30}", identifier)
        ):
            return None
    else:
        match = re.fullmatch(patterns[platform], parts.path)
        if (
            not match
            or parts.hostname
            != {
                "douyin": "www.douyin.com",
                "xiaohongshu": "www.xiaohongshu.com",
                "kuaishou": "www.kuaishou.com",
            }[platform]
        ):
            return None
        identifier = match[1]
    return {"url": url, "external_id": identifier}


def execute(platform, operation, payload, connection, *, device_id, before_submit):
    from .automation_android import _load_config

    config = _load_config()
    runtime = {
        "device_id": device_id,
        "operation": operation,
        "platforms": [platform],
        "_android_run_id": connection.get("run_id"),
        "_android_owner": connection.get("owner"),
    }
    preflight(runtime)
    with device_lock(config, device_id):
        preflight(runtime)
        state = read_state(config, device_id)
        prepared = state["prepared"]
        if prepared["digest"] != payload_digest(payload):
            fail("android_draft_changed", "草稿已修改，请重新检查 App 发布表单。")
        binding = bound_account(
            config, device_id, platform, connection.get("owner"), prepared["account_id"]
        )
        device = connect(config, device_id)
        if device.app_info(PACKAGES[platform]).get("versionName") != prepared["version"]:
            fail("android_app_updated", "App 已更新，请重新检查表单。")
        submit = verify_form(device, platform, payload, prepared["imported"])
        if form_digest(guard(device, platform)) != prepared["form_digest"]:
            fail("android_form_changed", "App 页面已变化，请重新检查草稿。")
        from .android_media import mark_submission, release_published

        # Protect staged files across worker crashes and unknown publication outcomes.
        mark_submission(state, prepared["imported"], connection["run_id"], "pending")
        # Consume the form before the submit boundary; it can never be replayed.
        state.pop("prepared", None)
        save_state(config, device_id, state)
        # Remove stale clipboard URLs before the durable, single-submit boundary.
        device.clipboard = ""
        try:
            before_submit()
        except Exception:
            mark_submission(state, prepared["imported"], connection["run_id"], "not_submitted")
            save_state(config, device_id, state)
            raise
        try:
            click(config, device_id, submit)
            deadline = time.monotonic() + 90
            confirmed = False
            while time.monotonic() < deadline:
                items = visible(nodes(device), platform)
                text = "\n".join(item.get("text", "") for item in items)
                if any(word in text for word in _SUCCESS):
                    confirmed = True
                    break
                time.sleep(1)
            if not confirmed:
                raise ValueError("no fresh receipt")
            # An explicit success prompt plus a newly copied own-content link
            # supplies a receipt. Absence of either is UNKNOWN, never success.
            copy = unique_text(items, {"复制链接"}, optional=True)
            if not copy:
                share = unique_text(items, {"分享", "分享作品", "分享笔记"}, optional=True)
                if share:
                    click(config, device_id, share)
                    copy = unique_text(
                        visible(nodes(device), platform), {"复制链接"}, optional=True
                    )
            if not copy:
                raise ValueError("no receipt link")
            click(config, device_id, copy)
            receipt = receipt_from_url(platform, device.clipboard)
            if not receipt:
                raise ValueError("invalid receipt")
            # Cleanup failure cannot turn a confirmed publication into an unknown one.
            with suppress(Exception):
                release_published(
                    device, config, device_id, state, prepared["imported"], connection["run_id"]
                )
            return {"status": "published", **receipt, "account_label": binding["account_label"]}
        except Exception:
            mark_submission(state, prepared["imported"], connection["run_id"], "unknown")
            save_state(config, device_id, state)
            raise BrowserPublishError(
                "android_publication_unknown",
                "已提交，但尚未取得本次发布链接，请在平台核查；不会自动重复发布。",
                status="unknown",
            ) from None
