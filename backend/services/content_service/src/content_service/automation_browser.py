"""Publish through an operator-configured, already logged-in Chromium browser.

Recipes are local administrator configuration, not API input. No platform selectors
ship as "verified": platform pages must be inspected and the recipe checked for the
actual account before enabling it. A successful click alone is never a receipt.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import math
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PLATFORM_HOSTS = {
    "xianyu": frozenset({"goofish.com", "www.goofish.com", "h5.m.goofish.com"}),
    "douyin": frozenset({"douyin.com", "www.douyin.com", "creator.douyin.com"}),
    "xiaohongshu": frozenset({"xiaohongshu.com", "www.xiaohongshu.com", "creator.xiaohongshu.com"}),
    "kuaishou": frozenset({"kuaishou.com", "www.kuaishou.com", "cp.kuaishou.com"}),
}
BROWSER_PLATFORMS = ("xianyu", "douyin", "xiaohongshu", "kuaishou")
REQUIRED_FIELDS = {
    "xianyu": frozenset(
        {
            "title",
            "description",
            "price",
            "condition",
            "shipping_fee",
            "location",
            "category",
            "image_paths",
        }
    ),
    "douyin": frozenset({"title", "description", "video_path"}),
    "xiaohongshu": frozenset({"title", "description", "video_path"}),
    "kuaishou": frozenset({"title", "description", "video_path"}),
}
PAYLOAD_FIELDS = frozenset().union(*REQUIRED_FIELDS.values())
MEDIA_FIELDS = frozenset({"image_paths", "video_path"})
TEXT_FIELDS = frozenset({"title", "description", "condition", "location", "category"})
COMBINED_TEXT_FIELDS = frozenset({"title", "description", "condition"})
NUMERIC_FIELDS = frozenset({"price", "shipping_fee"})
STEP_ACTIONS = frozenset({"fill", "click", "select", "upload", "wait"})
TIMEOUT_MS = 30_000
# Another publish may still be finishing; wait for it instead of failing at once.
LOCK_WAIT_SECONDS = 180
# Login checks queue behind each other briefly (the page loads all platforms at once).
STATUS_LOCK_WAIT_SECONDS = 60
UPLOAD_TIMEOUT_MS = 180_000
_FORBIDDEN_LISTING_STATE = re.compile(
    r"卖掉了|已卖出|已售出|已成交|已售罄|已删除|已交易|sold|deleted", re.IGNORECASE
)
_FORBIDDEN_FINAL_ACTION = re.compile(
    r"删除|永久|交易|购买|下单|支付|收款|退款|转账|确认收货|发货|成交|售出|卖出|批量|delete|remove|purchase|checkout|payment|pay\b|buy\b",
    re.IGNORECASE,
)


class BrowserPublishError(RuntimeError):
    """A known block, or an uncertain result that must not be sent again automatically."""

    def __init__(self, code: str, message: str, *, status: str = "blocked") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _blocked(code: str, message: str) -> BrowserPublishError:
    return BrowserPublishError(code, message)


def _data_dir() -> Path:
    return Path(os.getenv("CONTENT_DATA_DIR", ".data/content")).resolve()


def _config_path() -> Path:
    private = (_data_dir() / "private").resolve()
    path = Path(
        os.getenv("CONTENT_PUBLISH_BROWSER_CONFIG", str(private / "publishing-browser.json"))
    ).resolve()
    if not path.is_relative_to(private):
        raise _blocked("config_path_invalid", "浏览器发布配置必须位于数据目录的 private 子目录。")
    return path


def _load_config() -> dict[str, Any]:
    path = _config_path()
    if not path.is_file():
        raise _blocked("config_missing", "尚未设置本机浏览器发布配置。")
    try:
        if path.stat().st_size > 256_000:
            raise ValueError("large config")
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict) or config.get("version") != 1:
            raise ValueError("version")
        if set(config) - {"version", "accounts"}:
            raise ValueError("unknown config keys")
        accounts = config.get("accounts")
        if not isinstance(accounts, dict) or not accounts or len(accounts) > 30:
            raise ValueError("accounts")
        for account_id, account in accounts.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id):
                raise ValueError("account id")
            if not isinstance(account, dict) or set(account) - {"label", "cdp_url", "platforms"}:
                raise ValueError("account")
            if not isinstance(account.get("label"), str) or not 1 <= len(account["label"]) <= 80:
                raise ValueError("account label")
            _validate_cdp_url(account.get("cdp_url", ""))
            if not isinstance(account.get("platforms"), dict):
                raise ValueError("platforms")
            if set(account["platforms"]) - set(PLATFORM_HOSTS):
                raise ValueError("unknown platform")
        return config
    except BrowserPublishError:
        raise
    except (ValueError, TypeError, OSError):
        raise _blocked(
            "config_invalid", "浏览器发布配置格式无效，请由管理员检查 version、accounts 和字段。"
        ) from None


def _validate_cdp_url(value: Any, *, websocket: bool = False) -> str:
    try:
        if not isinstance(value, str) or len(value) > 500:
            raise ValueError("url")
        url = urlsplit(value)
        if url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("non-local")
        if url.username or url.password or url.query or url.fragment or not url.port:
            raise ValueError("components")
        if url.scheme == "http" and not websocket:
            if url.path not in {"", "/"}:
                raise ValueError("path")
        elif url.scheme == "ws":
            if not re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", url.path):
                raise ValueError("websocket path")
        else:
            raise ValueError("scheme")
    except (ValueError, TypeError):
        raise _blocked(
            "cdp_invalid", "浏览器调试地址仅支持本机 loopback HTTP 或浏览器 WebSocket 地址。"
        ) from None
    return value


def _validate_platform_url(platform: str, value: Any) -> str:
    try:
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError("url")
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.hostname not in PLATFORM_HOSTS[platform]
            or url.port not in {None, 443}
            or url.username
            or url.password
        ):
            raise ValueError("platform URL")
    except (ValueError, TypeError, KeyError):
        raise _blocked(
            "platform_url_invalid", "发布页面或结果链接不属于该平台允许的 HTTPS 域名。"
        ) from None
    return value


def _validate_publication_receipt(platform: str, value: str) -> str:
    """A creator dashboard/login/profile URL is not a receipt for a new work."""
    _validate_platform_url(platform, value)
    parts = urlsplit(value)
    paths = {
        "douyin": ("www.douyin.com", r"/video/[0-9]{8,30}/?"),
        "xiaohongshu": ("www.xiaohongshu.com", r"/(?:explore|discovery/item)/[a-fA-F0-9]{24}/?"),
        "kuaishou": ("www.kuaishou.com", r"/short-video/[A-Za-z0-9_-]{5,100}/?"),
    }
    if platform in paths:
        host, pattern = paths[platform]
        if parts.hostname != host or not re.fullmatch(pattern, parts.path):
            raise _blocked("receipt_invalid", "未取得本次作品的详情链接，请到平台核查发布结果。")
    return value


def _valid_locator(locator: Any, *, dynamic: bool = False) -> bool:
    if not isinstance(locator, dict):
        return False
    if set(locator) == {"css"}:
        return isinstance(locator["css"], str) and 0 < len(locator["css"]) <= 1000
    allowed = {"role", "name", "exact", "name_field"}
    if set(locator) - allowed or not isinstance(locator.get("role"), str):
        return False
    if locator.get("exact", True) is not True:
        return False
    if "name_field" in locator:
        return (
            dynamic
            and locator["name_field"] in PAYLOAD_FIELDS - MEDIA_FIELDS
            and "name" not in locator
        )
    return isinstance(locator.get("name"), str) and bool(locator["name"])


def _valid_check(check: Any, *, dynamic: bool = False) -> bool:
    allowed = {"locator", "state", "text"} | ({"when", "text_field"} if dynamic else set())
    return (
        isinstance(check, dict)
        and not set(check) - allowed
        and _valid_locator(check.get("locator"))
        and check.get("state", "visible") in {"visible", "hidden"}
        and ("text" not in check or isinstance(check["text"], str) and bool(check["text"]))
        and (
            "text_field" not in check or check["text_field"] in TEXT_FIELDS and "text" not in check
        )
        and not (check.get("state") == "hidden" and ("text" in check or "text_field" in check))
        and ("when" not in check or _valid_condition(check["when"]))
    )


def _valid_condition(condition: Any) -> bool:
    if not isinstance(condition, dict) or set(condition) != {"field", "op", "value"}:
        return False
    field, op, value = condition["field"], condition["op"], condition["value"]
    if field not in TEXT_FIELDS | NUMERIC_FIELDS or op not in {
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
    }:
        return False
    if field in TEXT_FIELDS:
        return op in {"eq", "ne"} and isinstance(value, str) and len(value) <= 500
    try:
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        )
    except OverflowError:
        return False


def _step_matches(step: dict, payload: dict) -> bool:
    condition = step.get("when")
    if condition is None:
        return True
    if not _valid_condition(condition):
        raise _blocked("condition_invalid", "准备步骤的条件配置无效。")
    field, op, expected = condition["field"], condition["op"], condition["value"]
    actual = payload.get(field)
    if field in NUMERIC_FIELDS:
        try:
            if isinstance(actual, bool):
                raise ValueError("boolean")
            actual = float(actual)
            if not math.isfinite(actual):
                raise ValueError("non-finite")
        except (TypeError, ValueError, OverflowError):
            raise _blocked("condition_value_invalid", f"条件字段不是有效数值：{field}。") from None
    elif not isinstance(actual, str):
        raise _blocked("condition_value_invalid", f"条件字段不是有效文本：{field}。")
    return {
        "eq": lambda: actual == expected,
        "ne": lambda: actual != expected,
        "gt": lambda: actual > expected,
        "gte": lambda: actual >= expected,
        "lt": lambda: actual < expected,
        "lte": lambda: actual <= expected,
    }[op]()


def _consumed_fields(step: dict) -> set[str]:
    fields = set(step.get("fields", []))
    if step.get("field") and step["action"] in {"fill", "select", "upload"}:
        fields.add(step["field"])
    if step.get("locator", {}).get("name_field") and step["action"] in {"fill", "select", "click"}:
        fields.add(step["locator"]["name_field"])
    # The free-shipping radio fully represents exactly zero shipping_fee. A
    # positive-fee branch must still fill the actual amount; a threshold alone
    # must not claim that a price or other business value was entered.
    if (
        step.get("when") == {"field": "shipping_fee", "op": "eq", "value": 0}
        and step["action"] == "click"
    ):
        fields.add("shipping_fee")
    return fields


def _step_value(step: dict, payload: dict) -> Any:
    if "fields" in step:
        values = []
        for field in step["fields"]:
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                raise _blocked("combined_field_invalid", f"组合文案字段必须为非空文本：{field}。")
            values.append(value)
        value = step.get("separator", "\n").join(values)
    elif "field" in step:
        value = payload[step["field"]]
    else:
        value = step.get("value")
    if step["action"] == "fill":
        limit = step.get("max_length", 10_000)
        # DOM maxlength implementations commonly count UTF-16 code units. Use the
        # same conservative limit so emoji cannot overflow a platform field.
        if len(str(value).encode("utf-16-le")) // 2 > limit:
            raise _blocked("field_length_exceeded", f"填写文案超过页面配置的 {limit} 字符上限。")
    return value


def _inspect_recipe(platform: str, recipe: Any) -> list[str]:
    issues: list[str] = []
    if not isinstance(recipe, dict):
        return ["未配置该平台的发布配方。"]
    allowed = {
        "enabled",
        "verified",
        "publish_url",
        "account_check_url",
        "account_locator",
        "expected_account",
        "blockers",
        "steps",
        "uploads_complete",
        "pre_submit_checks",
        "submit",
        "success",
        "receipt",
        "required_fields",
        "management",
    }
    if set(recipe) - allowed:
        issues.append("配方包含未知字段。")
    if recipe.get("enabled") is not True:
        issues.append("该平台配方尚未启用。")
    if recipe.get("verified") is not True:
        issues.append("尚未核验实际发布页面和全部定位器。")
    try:
        _validate_platform_url(platform, recipe.get("publish_url"))
        if recipe.get("account_check_url") is not None:
            _validate_platform_url(platform, recipe["account_check_url"])
    except BrowserPublishError as error:
        issues.append(error.message)
    if not _valid_locator(recipe.get("account_locator")):
        issues.append("缺少有效的登录账号定位器。")
    if (
        not isinstance(recipe.get("expected_account"), str)
        or not recipe["expected_account"].strip()
    ):
        issues.append("缺少预期账号标识。")
    blockers = recipe.get("blockers")
    if (
        not isinstance(blockers, list)
        or not blockers
        or any(not _valid_locator(item) for item in blockers)
    ):
        issues.append("缺少登录失效、验证或平台错误的阻塞定位器。")
    if not _valid_locator(recipe.get("submit")):
        issues.append("缺少最终发布按钮定位器。")
    success = recipe.get("success")
    if not _valid_check(success) or success.get("state", "visible") != "visible":
        issues.append("缺少可确认发布成功的页面证据。")
    uploads = recipe.get("uploads_complete")
    if (
        not isinstance(uploads, list)
        or not uploads
        or any(not _valid_check(check) for check in uploads)
    ):
        issues.append("缺少上传完成的检查条件。")
    elif not any(check.get("state", "visible") == "visible" for check in uploads):
        issues.append("上传完成检查至少需要一个可见的完成信号，不能只等待进度消失。")
    checks = recipe.get("pre_submit_checks", [])
    if not isinstance(checks, list) or any(
        not _valid_check(check, dynamic=True) for check in checks
    ):
        issues.append("发布前检查格式无效。")
    elif platform == "xianyu":
        verified_fields = {
            check["text_field"]
            for check in checks
            if check.get("text_field") in {"location", "category"}
            and check.get("state", "visible") == "visible"
            and "when" not in check
        }
        if {"location", "category"} - verified_fields:
            issues.append("闲鱼必须配置地点与类目选中结果的无条件 text_field 发布前检查。")
    receipt = recipe.get("receipt")
    if receipt is not None and (
        not isinstance(receipt, dict)
        or set(receipt) - {"locator", "attribute"}
        or not _valid_locator(receipt.get("locator"))
        or receipt.get("attribute", "href") != "href"
    ):
        issues.append("发布结果链接定位器无效。")
    required = recipe.get("required_fields", [])
    if not isinstance(required, list) or any(item not in PAYLOAD_FIELDS for item in required):
        issues.append("必填字段只能使用支持的发布字段。")
        required = []
    consumed: set[str] = set()
    uploaded: set[str] = set()
    steps = recipe.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > 100:
        issues.append("缺少完整的填写与上传步骤。")
        steps = []
    for step in steps:
        if not isinstance(step, dict) or set(step) - {
            "action",
            "locator",
            "field",
            "value",
            "state",
            "fields",
            "separator",
            "max_length",
            "when",
        }:
            issues.append("填写步骤格式无效。")
            continue
        action = step.get("action")
        locator = step.get("locator")
        if action not in STEP_ACTIONS or not _valid_locator(locator, dynamic=True):
            issues.append("步骤仅支持 fill、click、select、upload、wait 及 CSS/role 定位器。")
            continue
        if action == "click" and locator == recipe.get("submit"):
            issues.append("最终发布按钮不能出现在准备步骤中。")
        if "when" in step and not _valid_condition(step["when"]):
            issues.append("步骤条件仅支持受限字段与 eq/ne/gt/gte/lt/lte 比较。")
            continue
        combined = step.get("fields")
        if "fields" in step and (
            action != "fill"
            or not isinstance(combined, list)
            or not 2 <= len(combined) <= 3
            or any(field not in COMBINED_TEXT_FIELDS for field in combined)
            or len(set(combined)) != len(combined)
            or "max_length" not in step
        ):
            issues.append(
                "组合填写仅支持 title/description/condition 中 2–3 个不重复字段，且必须设置 max_length。"
            )
            continue
        if "separator" in step and (
            "fields" not in step or step["separator"] not in {"", " ", "\n", "\n\n"}
        ):
            issues.append("组合字段分隔符仅支持空格或换行。")
        if "max_length" in step and (
            action != "fill"
            or not isinstance(step["max_length"], int)
            or isinstance(step["max_length"], bool)
            or not 1 <= step["max_length"] <= 10_000
        ):
            issues.append("max_length 仅适用于 fill，且必须为 1–10000 的整数。")
        field = step.get("field")
        if action in {"fill", "select", "upload"}:
            if sum(key in step for key in ("field", "fields", "value")) != 1:
                issues.append("填写步骤必须且只能指定 field、fields 或固定 value。")
                continue
            if field is not None and field not in PAYLOAD_FIELDS:
                issues.append("步骤引用了不支持的字段。")
                continue
            if action == "upload":
                if field not in MEDIA_FIELDS:
                    issues.append("上传步骤只能引用 image_paths 或 video_path。")
                else:
                    uploaded.add(field)
            elif (
                field in MEDIA_FIELDS
                or "value" in step
                and not isinstance(step["value"], (str, int, float))
            ):
                issues.append("文本填写步骤值类型无效。")
        elif "field" in step or "fields" in step or "value" in step:
            issues.append("click/wait 步骤不支持 field/fields/value；动态文字请用 name_field。")
        if "state" in step and (action != "wait" or step["state"] not in {"visible", "hidden"}):
            issues.append("仅 wait 步骤支持 visible/hidden 状态。")
        consumed.update(_consumed_fields(step))
    fields = REQUIRED_FIELDS[platform] | set(required)
    if fields - consumed:
        issues.append("步骤尚未使用全部必填字段：" + "、".join(sorted(fields - consumed)))
    media_field = "image_paths" if platform == "xianyu" else "video_path"
    if media_field not in uploaded:
        issues.append("缺少平台要求的素材上传步骤。")
    return list(dict.fromkeys(issues))


def _recipe_issues(platform: str, recipe: Any) -> list[str]:
    from . import promotion_web
    if promotion_web.is_recipe(recipe):
        return promotion_web.recipe_issues(platform, recipe)
    if platform == "xianyu":
        from .xianyu_web import is_recipe, recipe_issues

        if is_recipe(recipe):
            return recipe_issues(recipe)
    try:
        return _inspect_recipe(platform, recipe)
    except (TypeError, ValueError, KeyError, AttributeError):
        return ["平台配方字段类型无效，请按照本机配置说明检查。"]


def _listing_url(value: Any) -> tuple[str, str]:
    """Only an exact public item URL can identify the item requested by the workflow."""
    _validate_platform_url("xianyu", value)
    url = urlsplit(value)
    try:
        query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise _blocked("listing_url_invalid", "商品链接查询参数格式无效。") from None
    # Xianyu's redirect after publishing carries an empty categoryId.
    if (
        url.hostname not in {"goofish.com", "www.goofish.com"}
        or url.path != "/item"
        or url.fragment
        or "id" not in query
        or set(query) - {"id", "categoryId", "spm"}
        or any(len(values) != 1 for values in query.values())
        or not re.fullmatch(r"[0-9]{1,30}", query["id"][0])
        or "categoryId" in query
        and not re.fullmatch(r"[0-9]{0,30}", query["categoryId"][0])
        or "spm" in query
        and not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", query["spm"][0])
    ):
        raise _blocked(
            "listing_url_invalid", "商品链接必须为 https://www.goofish.com/item?id=数字。"
        )
    item_id = query["id"][0]
    return f"https://www.goofish.com/item?id={item_id}", item_id


def _management_recipe_issues(platform: str, operation: str, recipe: Any) -> list[str]:
    if platform != "xianyu" or operation not in {"relist", "delist"}:
        return ["该平台尚不支持此商品管理操作。"]
    try:
        if not isinstance(recipe, dict):
            return ["尚未配置该商品管理操作。"]
        issues = []
        allowed = {
            "enabled",
            "verified",
            "management_url",
            "account_check_url",
            "account_locator",
            "expected_account",
            "blockers",
            "item_locator",
            "item_link",
            "title_locator",
            "source_state",
            "target_state",
            "target_item_locator",
            "target_item_link",
            "target_title_locator",
            "steps",
            "submit",
            "transition_check",
            "result_url",
        }
        if set(recipe) - allowed:
            issues.append("商品管理配方包含未知字段。")
        if recipe.get("enabled") is not True or recipe.get("verified") is not True:
            issues.append("商品管理配方尚未启用或未完成真实页面核验。")
        for field in ("management_url", "account_check_url", "result_url"):
            if recipe.get(field) is not None:
                try:
                    _validate_platform_url("xianyu", recipe[field])
                except BrowserPublishError:
                    issues.append(f"商品管理地址 {field} 不属于闲鱼允许的 HTTPS 域名。")
        for field in ("account_locator", "item_locator", "item_link", "title_locator", "submit"):
            if not _valid_locator(recipe.get(field), dynamic=field == "item_locator"):
                issues.append(f"缺少商品管理定位器：{field}。")
        for field in ("target_item_locator", "target_item_link", "target_title_locator"):
            if field in recipe and not _valid_locator(
                recipe[field], dynamic=field == "target_item_locator"
            ):
                issues.append(f"商品管理定位器无效：{field}。")
        if (
            not isinstance(recipe.get("expected_account"), str)
            or not recipe["expected_account"].strip()
        ):
            issues.append("缺少预期商品管理账号标识。")
        blockers = recipe.get("blockers")
        if (
            not isinstance(blockers, list)
            or not blockers
            or any(not _valid_locator(value) for value in blockers)
        ):
            issues.append("缺少登录、验证码或平台错误阻塞定位器。")
        states = {"relist": ("delisted", "listed"), "delist": ("listed", "delisted")}[operation]
        for field, expected in zip(("source_state", "target_state"), states, strict=True):
            state = recipe.get(field)
            if (
                not isinstance(state, dict)
                or set(state) != {"status", "check"}
                or state["status"] != expected
                or not _valid_check(state["check"])
                or state["check"].get("state", "visible") != "visible"
                or not state["check"].get("text")
                or _FORBIDDEN_LISTING_STATE.search(state["check"].get("text", ""))
                or _FORBIDDEN_FINAL_ACTION.search(state["check"].get("text", ""))
            ):
                issues.append(f"{field} 必须提供 {expected} 的可见状态证据。")
        if _FORBIDDEN_FINAL_ACTION.search(recipe.get("submit", {}).get("name", "")):
            issues.append("上下架最终动作不能是删除或交易操作。")
        if recipe.get("source_state", {}).get("check") == recipe.get("target_state", {}).get(
            "check"
        ):
            issues.append("商品源状态与目标状态不能使用相同证据。")
        if recipe.get("result_url") is not None and (
            not _valid_check(recipe.get("transition_check"))
            or recipe["transition_check"].get("state", "visible") != "visible"
        ):
            issues.append("跨页面核查结果前必须有可见的动作完成提示。")
        if "transition_check" in recipe and not _valid_check(recipe["transition_check"]):
            issues.append("动作完成提示格式无效。")
        steps = recipe.get("steps", [])
        if not isinstance(steps, list) or len(steps) > 30:
            issues.append("商品管理准备步骤必须是最多 30 项的数组。")
        else:
            for step in steps:
                if (
                    not isinstance(step, dict)
                    or set(step) - {"action", "locator", "state"}
                    or step.get("action") not in {"click", "wait"}
                    or not _valid_locator(step.get("locator"))
                    or "state" in step
                    and (step["action"] != "wait" or step["state"] not in {"visible", "hidden"})
                    or step.get("action") == "click"
                    and step.get("locator") == recipe.get("submit")
                ):
                    issues.append("商品管理准备步骤仅支持安全 click/wait，不能包含最终动作。")
        return list(dict.fromkeys(issues))
    except (TypeError, ValueError, KeyError, AttributeError):
        return ["商品管理配方字段类型无效。"]


def _operation_recipe(platform_recipe: Any, operation: str) -> Any:
    if operation == "publish":
        return platform_recipe
    if not isinstance(platform_recipe, dict) or not isinstance(
        platform_recipe.get("management"), dict
    ):
        return None
    return platform_recipe["management"].get(operation)


def _operation_issues(platform: str, operation: str, recipe: Any) -> list[str]:
    if operation == "publish":
        return _recipe_issues(platform, recipe)
    return _management_recipe_issues(platform, operation, recipe)


def _playwright_available() -> bool:
    try:
        return importlib.util.find_spec("playwright.sync_api") is not None
    except (ModuleNotFoundError, ValueError):
        return False


def status() -> dict[str, Any]:
    """Inspect local configuration only; does not connect or claim the browser is logged in."""
    available = _playwright_available()
    missing = [] if available else ["尚未安装浏览器发布依赖 requirements-browser.txt。"]
    try:
        config = _load_config()
    except BrowserPublishError as error:
        missing.append(error.message)
        config = {"accounts": {}}
    platforms: dict[str, Any] = {}
    accounts = []
    for platform in BROWSER_PLATFORMS:
        operations = {}
        for operation in ("publish", "relist", "delist") if platform == "xianyu" else ("publish",):
            entries = []
            for account_id, account in config["accounts"].items():
                recipe = _operation_recipe(account["platforms"].get(platform), operation)
                issues = _operation_issues(platform, operation, recipe)
                entries.append(
                    {
                        "account_id": account_id,
                        "label": account["label"],
                        "configured": available and not issues,
                        "missing": missing + issues,
                    }
                )
            configured = any(item["configured"] for item in entries)
            reasons = (
                []
                if configured
                else list(
                    dict.fromkeys(
                        missing + [reason for item in entries for reason in item["missing"]]
                    )
                )
            )
            operations[operation] = {
                "configured": configured,
                "accounts": entries,
                "missing": reasons or ([] if configured else ["未配置该平台操作的浏览器账号。"]),
            }
        # Preserve existing callers' meaning: platform.configured means publish,
        # never claim a complete publishing recipe from a management-only account.
        platforms[platform] = {**operations["publish"], "operations": operations}
    for account_id, account in config["accounts"].items():
        accounts.append(
            {
                "account_id": account_id,
                "label": account["label"],
                "platforms": [
                    name
                    for name, value in platforms.items()
                    if any(
                        item["account_id"] == account_id and item["configured"]
                        for item in value["accounts"]
                    )
                ],
            }
        )
    return {
        "available": available,
        "configured": any(item["configured"] for item in platforms.values()),
        "missing": missing,
        "platforms": platforms,
        "accounts": accounts,
    }


def _select_account(
    config: dict, platform: str, connection: dict | None, *, operation: str = "publish"
) -> tuple[str, dict, dict]:
    if connection is not None and (
        not isinstance(connection, dict) or set(connection) - {"account_id"}
    ):
        raise _blocked(
            "connection_invalid", "工作流只能选择本机配置的 account_id，不能传入地址或页面配方。"
        )
    account_id = (connection or {}).get("account_id")
    if not account_id:
        candidates = [
            key
            for key, value in config["accounts"].items()
            if not _operation_issues(
                platform, operation, _operation_recipe(value["platforms"].get(platform), operation)
            )
        ]
        if len(candidates) != 1:
            raise _blocked("account_required", "请为平台选择唯一且已配置的发布账号。")
        account_id = candidates[0]
    account = config["accounts"].get(account_id) if isinstance(account_id, str) else None
    if account is None:
        raise _blocked("account_missing", "选择的发布账号不存在于本机配置。")
    recipe = _operation_recipe(account["platforms"].get(platform), operation)
    issues = _operation_issues(platform, operation, recipe)
    if issues:
        raise _blocked("recipe_incomplete", " ".join(issues))
    return account_id, account, recipe


def _validated_payload(platform: str, payload: dict, recipe: dict) -> dict:
    if not isinstance(payload, dict):
        raise _blocked("payload_invalid", "发布数据格式无效。")
    result = dict(payload)
    fields = REQUIRED_FIELDS[platform] | set(recipe.get("required_fields", []))
    for field in fields:
        value = result.get(field)
        if value is None or isinstance(value, str) and not value.strip() or value == []:
            raise _blocked("required_field_missing", f"发布前缺少必填字段：{field}。")
    for field in PAYLOAD_FIELDS - MEDIA_FIELDS:
        value = result.get(field)
        if value is not None and (
            not isinstance(value, (str, int, float)) or isinstance(value, bool)
        ):
            raise _blocked("payload_invalid", f"发布字段类型无效：{field}。")
        if value is not None and len(str(value)) > (10_000 if field == "description" else 500):
            raise _blocked("payload_invalid", f"发布字段过长：{field}。")
    for field in {"price", "shipping_fee"} & fields:
        try:
            number = float(result[field])
            if not math.isfinite(number) or number < 0 or field == "price" and number == 0:
                raise ValueError("price")
        except (ValueError, TypeError, OverflowError):
            raise _blocked("price_invalid", "商品价格必须大于零，运费必须为非负数。") from None
    selected_steps = _selected_steps(platform, result, recipe)
    media_fields = {step["field"] for step in selected_steps if step["action"] == "upload"}
    for field in media_fields:
        values = result.get(field)
        if field == "video_path":
            values = [values]
        if not isinstance(values, list) or not values or len(values) > 9:
            raise _blocked("media_invalid", "图片需要 1–9 个本地文件，视频需要一个本地文件。")
        paths = []
        for value in values:
            if not isinstance(value, str) or not value:
                raise _blocked("media_invalid", "上传素材必须使用数据目录内的本地文件。")
            path = Path(value).resolve()
            if not path.is_relative_to(_data_dir() / "automation-media") or not path.is_file():
                raise _blocked(
                    "media_invalid", "上传素材不存在或不在应用的 automation-media 目录内。"
                )
            allowed = (
                {".jpg", ".jpeg", ".png", ".webp"}
                if field == "image_paths"
                else {".mp4", ".mov", ".m4v"}
            )
            if path.suffix.lower() not in allowed or path.stat().st_size == 0:
                raise _blocked("media_invalid", "上传素材扩展名不支持或文件为空。")
            paths.append(str(path))
        result[field] = paths if field == "image_paths" else paths[0]
    return result


def _selected_steps(platform: str, payload: dict, recipe: dict) -> list[dict]:
    steps = [step for step in recipe["steps"] if _step_matches(step, payload)]
    consumed = set().union(*(_consumed_fields(step) for step in steps)) if steps else set()
    missing = (REQUIRED_FIELDS[platform] | set(recipe.get("required_fields", []))) - consumed
    if missing:
        raise _blocked(
            "conditional_fields_missing",
            "当前条件分支没有填写全部必填字段：" + "、".join(sorted(missing)),
        )
    for step in steps:
        if step["action"] in {"fill", "select", "upload"}:
            _step_value(step, payload)
    return steps


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _blocked("cdp_redirect", "浏览器调试服务不允许 HTTP 重定向。")


def _cdp_websocket(cdp_url: str) -> str:
    _validate_cdp_url(cdp_url)
    if urlsplit(cdp_url).scheme == "ws":
        return cdp_url
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        request = Request(
            cdp_url.rstrip("/") + "/json/version", headers={"Accept": "application/json"}
        )
        with opener.open(request, timeout=8) as response:
            raw = response.read(64_001)
        if len(raw) > 64_000:
            raise ValueError("response size")
        discovered = json.loads(raw)["webSocketDebuggerUrl"]
        _validate_cdp_url(discovered, websocket=True)
        if urlsplit(discovered).port != urlsplit(cdp_url).port:
            raise _blocked("cdp_invalid", "浏览器调试发现结果端口与本机配置不一致。")
        return discovered
    except BrowserPublishError:
        raise
    except Exception:
        raise _blocked(
            "browser_unreachable", "无法连接本机发布浏览器，请启动调试浏览器并登录平台。"
        ) from None


@contextmanager
def _browser_lock(
    cdp_url: str, *, wait: float = 0, purpose: str = "publish"
) -> Iterator[None]:
    """A process-wide OS lock per local debugging port, shared across worker processes.

    Publishing and login-status checks hold separate locks: a check only opens
    its own account tab, so it may run while a publish works in its own tab.
    ``wait`` seconds are spent retrying while another holder of the same lock finishes.
    """
    directory = _data_dir() / "private" / "publishing-locks"
    directory.mkdir(parents=True, exist_ok=True)
    suffix = "" if purpose == "publish" else f"-{purpose}"
    path = directory / f"browser-{urlsplit(cdp_url).port}{suffix}.lock"
    handle = path.open("a+b")
    acquired = False
    deadline = time.monotonic() + wait
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        while not acquired:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise _blocked(
                        "browser_busy",
                        "该浏览器正在执行另一项发布任务，请稍后运行。"
                        if purpose == "publish"
                        else "正在检测其他平台的登录状态，请稍后再检测。",
                    ) from None
                time.sleep(0.5)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _locator(page: Any, spec: dict, payload: dict | None = None) -> Any:
    if "css" in spec:
        return page.locator(spec["css"])
    name = str((payload or {})[spec["name_field"]]) if "name_field" in spec else spec["name"]
    return page.get_by_role(spec["role"], name=name, exact=True)


def _unique(locator: Any) -> Any:
    # SPAs can replace the upload route with an editor after file selection.
    # Let the new target attach before deciding it is missing or ambiguous.
    locator.wait_for(state="attached", timeout=TIMEOUT_MS)
    if locator.count() != 1:
        raise _blocked("locator_ambiguous", "页面定位结果不唯一，已停止以避免操作错误元素。")
    return locator


def _check(
    page: Any, check: dict, *, timeout: int = TIMEOUT_MS, payload: dict | None = None
) -> None:
    locator = _locator(page, check["locator"])
    state = check.get("state", "visible")
    locator.wait_for(state=state, timeout=timeout)
    if state == "visible":
        _unique(locator)
        if "text" in check or "text_field" in check:
            expected = (
                check.get("text")
                if "text_field" not in check
                else (payload or {}).get(check["text_field"])
            )
            if not isinstance(expected, str) or not expected.strip():
                raise _blocked("page_check_invalid", "发布前文字检查缺少对应业务字段。")
            # State badges often stay mounted while their text updates after the
            # network response. Wait for the expected text rather than failing on
            # the first frame that still shows the source state.
            expect = importlib.import_module("playwright.sync_api").expect
            expect(locator).to_have_text(
                re.compile(r"^\s*" + re.escape(expected.strip()) + r"\s*$"),
                timeout=timeout,
                use_inner_text=True,
            )


def _guard_page(page: Any, platform: str, recipe: dict) -> None:
    if platform == "kuaishou" and urlsplit(page.url).hostname == "passport.kuaishou.com":
        raise _blocked("login_required", "快手网页登录已失效，请重新登录创作者中心。")
    _validate_platform_url(platform, page.url)
    for spec in recipe["blockers"]:
        locator = _locator(page, spec)
        # A hidden login panel is harmless; any visible matching blocker stops execution.
        for index in range(locator.count()):
            if locator.nth(index).is_visible():
                raise _blocked(
                    "platform_blocked", "平台显示登录、验证或错误提示，请人工处理后重新运行。"
                )
    if not recipe.get("account_check_url"):
        _verify_account_text(page, recipe)


def _verify_account_text(page: Any, recipe: dict) -> None:
    account = _locator(page, recipe["account_locator"])
    account.wait_for(state="visible", timeout=TIMEOUT_MS)
    _unique(account)
    if account.inner_text().strip() != recipe["expected_account"].strip():
        raise _blocked("account_mismatch", "当前登录账号与工作流绑定账号不一致。")


def _refresh_account_check(
    page: Any, platform: str, recipe: dict, account_page: Any | None
) -> None:
    if not recipe.get("account_check_url"):
        return
    if account_page is None or account_page is page or account_page.context is not page.context:
        raise _blocked("account_check_missing", "独立账号检查页必须属于当前发布浏览器会话。")
    account_page.goto(
        recipe["account_check_url"], wait_until="domcontentloaded", timeout=TIMEOUT_MS
    )
    _guard_page(account_page, platform, recipe)
    _verify_account_text(account_page, recipe)
    # Chromium can throttle rendering in a background tab after the identity page
    # navigates. Restore the editor before Playwright checks element stability.
    page.bring_to_front()


def _touches(locator: Any, other: Any) -> bool:
    return bool(
        locator.and_(other).count()
        or locator.locator("xpath=ancestor::*").and_(other).count()
        or locator.locator("xpath=descendant::*").and_(other).count()
    )


def _prepare(page: Any, platform: str, payload: dict, recipe: dict) -> None:
    for step in _selected_steps(platform, payload, recipe):
        _guard_page(page, platform, recipe)
        locator = _locator(page, step["locator"], payload)
        action = step["action"]
        if action == "wait":
            locator.wait_for(state=step.get("state", "visible"), timeout=UPLOAD_TIMEOUT_MS)
            continue
        _unique(locator)
        value = _step_value(step, payload)
        if action == "fill":
            locator.fill(str(value), timeout=TIMEOUT_MS)
        elif action == "select":
            locator.select_option(label=str(value), timeout=TIMEOUT_MS)
        elif action == "upload":
            locator.set_input_files(value, timeout=TIMEOUT_MS)
        elif action == "click":
            # Different selectors (for example CSS vs role) can identify the same
            # final button. Compare live DOM nodes, not just the recipe dictionaries.
            final_button = _locator(page, recipe["submit"])
            if _touches(locator, final_button):
                raise _blocked(
                    "early_submission_blocked", "准备步骤命中了最终发布按钮，已停止且未提交。"
                )
            locator.click(timeout=TIMEOUT_MS)
    for check in recipe["uploads_complete"]:
        _check(page, check, timeout=UPLOAD_TIMEOUT_MS)


def _check_pre_submit(page: Any, payload: dict, recipe: dict) -> None:
    for check in recipe.get("pre_submit_checks", []):
        if _step_matches(check, payload):
            _check(page, check, timeout=TIMEOUT_MS, payload=payload)


def _publish_on_page(
    page: Any,
    platform: str,
    payload: dict,
    recipe: dict,
    before_submit: Callable[[], Any],
    *,
    account_page: Any | None = None,
) -> dict:
    """Run once. Kept separate so tests can exercise the commit boundary locally."""
    submitted = False
    try:
        _refresh_account_check(page, platform, recipe, account_page)
        _guard_page(page, platform, recipe)
        _prepare(page, platform, payload, recipe)
        _guard_page(page, platform, recipe)
        # Stale success UI must not be mistaken for evidence from this attempt.
        success_locator = _locator(page, recipe["success"]["locator"])
        if any(success_locator.nth(index).is_visible() for index in range(success_locator.count())):
            raise _blocked("stale_success", "准备阶段已存在成功提示，无法确认新发布结果。")
        submit = _unique(_locator(page, recipe["submit"]))
        # Playwright trial checks visibility, stability and enabled state without clicking.
        submit.click(trial=True, timeout=TIMEOUT_MS)
        _refresh_account_check(page, platform, recipe, account_page)
        _guard_page(page, platform, recipe)
        _check_pre_submit(page, payload, recipe)
        # The persistence callback is deliberately adjacent to the ONLY final click.
        before_submit()
        submitted = True
        submit.click(timeout=TIMEOUT_MS)
        _check(page, recipe["success"], timeout=UPLOAD_TIMEOUT_MS)
        _validate_platform_url(platform, page.url)
        receipt_url = page.url
        if recipe.get("receipt"):
            link = _unique(_locator(page, recipe["receipt"]["locator"]))
            href = link.get_attribute("href")
            if not href:
                raise ValueError("receipt absent")
            receipt_url = _validate_platform_url(platform, urljoin(page.url, href))
        return {"status": "published", "url": _validate_publication_receipt(platform, receipt_url)}
    except Exception as error:
        if submitted:
            raise BrowserPublishError(
                "publication_unknown",
                "已尝试点击发布，但未取得可信成功回执；请在平台核查，禁止自动重试。",
                status="unknown",
            ) from None
        if isinstance(error, BrowserPublishError):
            raise
        raise _blocked(
            "browser_preparation_failed", "发布前页面检查或素材准备失败，未点击最终发布按钮。"
        ) from None


def publish(
    platform: str,
    payload: dict,
    connection: dict | None = None,
    before_submit: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Publish a prepared payload using a configured local account, without retries.

    The caller must durably mark the attempt as submitting in before_submit. Any
    ``unknown`` outcome requires platform reconciliation before another attempt.
    """
    if platform not in BROWSER_PLATFORMS:
        raise _blocked(
            "platform_unsupported", "网页发布支持闲鱼、抖音、小红书和快手。"
        )
    if not callable(before_submit):
        raise _blocked("submission_marker_required", "发布必须提供持久化提交状态的回调。")
    config = _load_config()
    account_id, account, recipe = _select_account(config, platform, connection)
    from . import promotion_web
    if promotion_web.is_recipe(recipe):
        return promotion_web.publish(platform, payload, account_id, account, recipe, before_submit)
    if platform == "xianyu":
        from . import xianyu_web

        if xianyu_web.is_recipe(recipe):
            return xianyu_web.publish(payload, account_id, account, recipe, before_submit)
    prepared = _validated_payload(platform, payload, recipe)
    if not _playwright_available():
        raise _blocked(
            "browser_dependency_missing", "请安装 requirements-browser.txt 后启用浏览器发布。"
        )
    with _browser_lock(account["cdp_url"]):
        websocket = _cdp_websocket(account["cdp_url"])
        playwright = None
        page = None
        account_page = None
        try:
            playwright = importlib.import_module("playwright.sync_api").sync_playwright().start()
            browser = playwright.chromium.connect_over_cdp(websocket, timeout=15_000)
            if not browser.contexts:
                raise _blocked("browser_context_missing", "调试浏览器没有可复用的登录会话。")
            # A dedicated new tab avoids overwriting the operator's existing tabs.
            page = browser.contexts[0].new_page()
            page.set_default_timeout(TIMEOUT_MS)
            if recipe.get("account_check_url"):
                account_page = browser.contexts[0].new_page()
                account_page.set_default_timeout(TIMEOUT_MS)
            page.goto(recipe["publish_url"], wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            receipt = _publish_on_page(
                page, platform, prepared, recipe, before_submit, account_page=account_page
            )
            return {**receipt, "account_id": account_id, "account_label": account["label"]}
        except BrowserPublishError:
            raise
        except Exception:
            raise _blocked(
                "browser_connection_failed", "浏览器连接或发布页面打开失败，未点击最终发布按钮。"
            ) from None
        finally:
            if account_page is not None:
                with suppress(Exception):
                    account_page.close()
            # Stopping the driver disconnects CDP. Do not close the user's browser,
            # context or result tab; an unknown attempt must remain reviewable.
            if playwright is not None:
                with suppress(Exception):
                    playwright.stop()


_UNSAFE_PREPARATION = re.compile(
    r"删除|永久|交易|购买|下单|支付|收款|退款|转账|确认收货|发货|成交|售出|卖出|批量|"
    r"上架|下架|重新发布|发布|delete|remove|purchase|checkout|relist|delist|payment|pay\b|buy\b",
    re.IGNORECASE,
)
_SAFE_MANAGEMENT_PREPARATION = frozenset(
    {
        "更多",
        "更多操作",
        "操作",
        "管理",
        "管理商品",
        "商品管理",
        "展开",
        "关闭",
        "返回",
        "取消",
        "More",
        "More actions",
        "Options",
        "Manage",
        "Expand",
        "Close",
        "Back",
        "Cancel",
    }
)


def _listing_identity(page: Any, payload: dict, recipe: dict, *, target: bool = False) -> Any:
    prefix = "target_" if target else ""
    item_spec = recipe.get(prefix + "item_locator", recipe["item_locator"])
    link_spec = recipe.get(prefix + "item_link", recipe["item_link"])
    title_spec = recipe.get(prefix + "title_locator", recipe["title_locator"])
    item = _unique(_locator(page, item_spec, payload))
    item.wait_for(state="visible", timeout=TIMEOUT_MS)
    link = _unique(_locator(item, link_spec))
    href = link.get_attribute("href")
    if not href:
        raise _blocked("listing_identity_mismatch", "商品区域缺少可核验的商品链接。")
    try:
        _, observed_id = _listing_url(urljoin(page.url, href))
    except BrowserPublishError:
        raise _blocked(
            "listing_identity_mismatch", "页面商品链接不是允许的闲鱼商品详情地址。"
        ) from None
    if observed_id != payload["item_id"]:
        raise _blocked("listing_identity_mismatch", "页面商品 ID 与工作流指定商品不一致。")
    title = _unique(_locator(item, title_spec))
    if title.inner_text().strip() != payload["title"]:
        raise _blocked("listing_identity_mismatch", "页面商品标题与工作流指定标题不完全一致。")
    return item


def _listing_source(page: Any, payload: dict, recipe: dict) -> Any:
    item = _listing_identity(page, payload, recipe)
    if _FORBIDDEN_LISTING_STATE.search(item.inner_text()):
        raise _blocked(
            "listing_terminal_state", "商品显示已售出、已成交或已删除，不能作为上下架源商品。"
        )
    try:
        _check(item, recipe["source_state"]["check"], timeout=TIMEOUT_MS)
    except Exception:
        raise _blocked(
            "listing_source_state_mismatch", "商品当前状态不符合本次上下架操作的源状态。"
        ) from None
    if _has_visible_check(item, recipe["target_state"]["check"]):
        raise _blocked(
            "listing_target_state_present", "商品区域已存在目标状态，无法确认新操作的状态变化。"
        )
    return item


def _has_visible_check(scope: Any, check: dict) -> bool:
    locator = _locator(scope, check["locator"])
    for index in range(locator.count()):
        element = locator.nth(index)
        if element.is_visible() and (
            "text" not in check or element.inner_text().strip() == check["text"].strip()
        ):
            return True
    return False


def _action_labels(locator: Any) -> str:
    return " ".join(
        [
            locator.inner_text(),
            locator.get_attribute("aria-label") or "",
            locator.get_attribute("title") or "",
            locator.get_attribute("value") or "",
            locator.get_attribute("href") or "",
        ]
    )


def _manage_on_page(
    page: Any,
    operation: str,
    payload: dict,
    recipe: dict,
    before_submit: Callable[[], Any],
    *,
    account_page: Any | None = None,
) -> dict:
    submitted = False
    try:
        _refresh_account_check(page, "xianyu", recipe, account_page)
        _guard_page(page, "xianyu", recipe)
        _listing_source(page, payload, recipe)
        for step in recipe.get("steps", []):
            _guard_page(page, "xianyu", recipe)
            item = _listing_source(page, payload, recipe)
            locator = _locator(item, step["locator"])
            if step["action"] == "wait":
                locator.wait_for(state=step.get("state", "visible"), timeout=TIMEOUT_MS)
                continue
            _unique(locator)
            if _touches(locator, _locator(page, recipe["submit"])):
                raise _blocked("early_submission_blocked", "准备步骤命中了最终上下架动作，已停止。")
            labels = _action_labels(locator) + " " + step["locator"].get("name", "")
            if _UNSAFE_PREPARATION.search(labels):
                raise _blocked(
                    "unsafe_management_step", "准备步骤涉及上下架、交易或删除操作，已停止。"
                )
            names = {
                locator.inner_text().strip(),
                (locator.get_attribute("aria-label") or "").strip(),
                (locator.get_attribute("title") or "").strip(),
            }
            if not names & _SAFE_MANAGEMENT_PREPARATION:
                raise _blocked(
                    "unsafe_management_step", "准备步骤不是已支持的菜单展开或取消操作，已停止。"
                )
            locator.click(timeout=TIMEOUT_MS)
        _refresh_account_check(page, "xianyu", recipe, account_page)
        _guard_page(page, "xianyu", recipe)
        item = _listing_source(page, payload, recipe)
        submit = _unique(_locator(item, recipe["submit"]))
        if _FORBIDDEN_FINAL_ACTION.search(_action_labels(submit)):
            raise _blocked(
                "unsafe_management_action", "最终按钮属于删除或交易操作，不能用于上下架。"
            )
        submit.click(trial=True, timeout=TIMEOUT_MS)
        _guard_page(page, "xianyu", recipe)
        _listing_source(page, payload, recipe)
        before_submit()
        submitted = True
        submit.click(timeout=TIMEOUT_MS)
        if recipe.get("transition_check"):
            _check(page, recipe["transition_check"], timeout=UPLOAD_TIMEOUT_MS)
        if recipe.get("result_url"):
            page.goto(recipe["result_url"], wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        _guard_page(page, "xianyu", recipe)
        item = _listing_identity(page, payload, recipe, target=True)
        if _FORBIDDEN_LISTING_STATE.search(item.inner_text()):
            raise ValueError("terminal listing state after action")
        _check(item, recipe["target_state"]["check"], timeout=UPLOAD_TIMEOUT_MS)
        if _has_visible_check(item, recipe["source_state"]["check"]):
            raise ValueError("conflicting listing state evidence")
        return {
            "status": "listed" if operation == "relist" else "delisted",
            "url": payload["target_url"],
            "external_id": payload["item_id"],
        }
    except Exception as error:
        if submitted:
            raise BrowserPublishError(
                "listing_result_unknown",
                "已尝试执行上下架，但未取得同一商品的目标状态证据；请到平台核查，禁止自动重试。",
                status="unknown",
            ) from None
        if isinstance(error, BrowserPublishError):
            raise
        raise _blocked(
            "listing_preparation_failed", "上下架前的页面或商品检查失败，未点击最终操作按钮。"
        ) from None


def manage_listing(
    platform: str,
    operation: str,
    payload: dict,
    connection: dict | None = None,
    before_submit: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Change one existing listing state exactly once, then verify that same item."""
    if platform != "xianyu" or operation not in {"relist", "delist"}:
        raise _blocked("operation_unsupported", "目前仅支持闲鱼现有商品的上架与下架操作。")
    if not callable(before_submit):
        raise _blocked("submission_marker_required", "上下架必须提供持久化提交状态的回调。")
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("title"), str)
        or not 1 <= len(payload["title"].strip()) <= 500
    ):
        raise _blocked(
            "listing_payload_invalid", "上下架必须提供商品详情链接和完整准确的商品标题。"
        )
    target_url, item_id = _listing_url(payload.get("target_url"))
    prepared = {"target_url": target_url, "item_id": item_id, "title": payload["title"].strip()}
    config = _load_config()
    account_id, account, recipe = _select_account(config, platform, connection, operation=operation)
    if not _playwright_available():
        raise _blocked(
            "browser_dependency_missing", "请安装 requirements-browser.txt 后启用浏览器操作。"
        )
    with _browser_lock(account["cdp_url"]):
        websocket = _cdp_websocket(account["cdp_url"])
        playwright = None
        account_page = None
        try:
            playwright = importlib.import_module("playwright.sync_api").sync_playwright().start()
            browser = playwright.chromium.connect_over_cdp(websocket, timeout=15_000)
            if not browser.contexts:
                raise _blocked("browser_context_missing", "调试浏览器没有可复用的登录会话。")
            page = browser.contexts[0].new_page()
            page.set_default_timeout(TIMEOUT_MS)
            if recipe.get("account_check_url"):
                account_page = browser.contexts[0].new_page()
                account_page.set_default_timeout(TIMEOUT_MS)
            page.goto(
                recipe.get("management_url") or target_url,
                wait_until="domcontentloaded",
                timeout=TIMEOUT_MS,
            )
            receipt = _manage_on_page(
                page, operation, prepared, recipe, before_submit, account_page=account_page
            )
            return {**receipt, "account_id": account_id, "account_label": account["label"]}
        except BrowserPublishError:
            raise
        except Exception:
            raise _blocked(
                "browser_connection_failed", "浏览器连接或商品管理页面打开失败，未点击最终操作。"
            ) from None
        finally:
            if account_page is not None:
                with suppress(Exception):
                    account_page.close()
            if playwright is not None:
                with suppress(Exception):
                    playwright.stop()
