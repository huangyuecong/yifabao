"""Explicit dispatch; Android errors must never fall back to browser publishing."""

from .automation_browser import BrowserPublishError
from .automation_platforms import get_platform


def _validate(platform: str, operation: str, mode: str) -> None:
    try:
        spec = get_platform(platform)
    except ValueError as exc:
        raise BrowserPublishError("platform_unsupported", str(exc)) from None
    if mode not in {"browser", "android"} or mode not in spec.execution_modes:
        raise BrowserPublishError("executor_unsupported", "该平台不支持所选执行方式")
    if operation not in spec.operations:
        raise BrowserPublishError("operation_unsupported", "该平台不支持所选操作")


def preflight(config: dict) -> None:
    mode = config.get("execution_mode", "browser")
    platforms = config.get("platforms", [])
    if not platforms:
        raise BrowserPublishError("platform_missing", "请先选择发布平台")
    for platform in platforms:
        _validate(platform, config.get("operation", "publish"), mode)
    if config.get("operation", "publish") != "publish" and not config.get("accounts", {}).get("xianyu"):
        raise BrowserPublishError("account_required", "商品上下架必须明确选择已登录的闲鱼网页账号")
    if mode == "android":
        from .automation_android import preflight as android_preflight

        android_preflight(config)
    # Browser validation remains in its existing publisher: this preserves old
    # workflows and the durable submit boundary. No connection is inferred here.


def execute(
    platform: str,
    operation: str,
    payload: dict,
    connection: dict,
    *,
    execution_mode: str = "browser",
    device_id: str | None = None,
    before_submit,
):
    _validate(platform, operation, execution_mode)
    if execution_mode == "android":
        from .automation_android import execute as android_execute

        return android_execute(
            platform,
            operation,
            payload,
            connection,
            device_id=device_id,
            before_submit=before_submit,
        )
    if device_id:
        raise BrowserPublishError("device_invalid", "浏览器工作流不能绑定安卓设备")
    if operation == "publish":
        from .automation_browser import publish

        return publish(platform, payload, connection, before_submit=before_submit)
    from .automation_browser import manage_listing

    return manage_listing(platform, operation, payload, connection, before_submit=before_submit)
