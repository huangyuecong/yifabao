"""Execution destinations are explicit; blocked Android work never uses a browser."""

from unittest.mock import Mock

import pytest

from content_service import automation_browser, automation_executor
from content_service.automation_browser import BrowserPublishError
from content_service.automation_platforms import catalog_document, get_platform


def test_catalog_describes_capabilities_without_claiming_adapter_readiness():
    catalog = {item["id"]: item for item in catalog_document()}
    assert set(catalog) == {"xianyu", "douyin", "xiaohongshu", "kuaishou"}
    assert catalog["xianyu"]["purpose"] == "listing"
    assert all(
        catalog[key]["purpose"] == "promotion" for key in ("douyin", "xiaohongshu", "kuaishou")
    )
    assert catalog["xiaohongshu"]["execution_modes"] == ("browser",)
    assert catalog["xianyu"]["execution_modes"] == ("browser",)
    assert catalog["xiaohongshu"]["title_limit"] == 20
    assert all("ready" not in item for item in catalog.values())
    with pytest.raises(ValueError):
        get_platform("unregistered")


def test_legacy_execution_defaults_to_browser_and_preserves_submit_callback(monkeypatch):
    callback = Mock()
    browser = Mock(return_value={"status": "published"})
    monkeypatch.setattr(automation_browser, "publish", browser)
    automation_executor.preflight({"platforms": ["douyin"]})
    receipt = automation_executor.execute(
        "douyin", "publish", {"title": "test"}, {}, before_submit=callback
    )
    assert receipt["status"] == "published"
    browser.assert_called_once_with("douyin", {"title": "test"}, {}, before_submit=callback)
    callback.assert_not_called()  # Only the platform adapter may cross the submit boundary.


@pytest.mark.parametrize(
    "platform,mode,operation",
    [
        ("unknown", "android", "publish"),
        ("xiaohongshu", "android", "publish"),
        ("xianyu", "android", "publish"),
        ("kuaishou", "android", "delist"),
        ("douyin", "guess", "publish"),
    ],
)
def test_unsupported_destinations_never_reach_the_browser(monkeypatch, platform, mode, operation):
    browser = Mock()
    callback = Mock()
    monkeypatch.setattr(automation_browser, "publish", browser)
    monkeypatch.setattr(automation_browser, "manage_listing", browser)
    with pytest.raises(BrowserPublishError):
        automation_executor.execute(
            platform, operation, {}, {}, execution_mode=mode, before_submit=callback
        )
    browser.assert_not_called()
    callback.assert_not_called()


def test_android_failure_has_no_browser_fallback(monkeypatch):
    from content_service import automation_android

    browser = Mock()
    callback = Mock()
    monkeypatch.setattr(automation_browser, "publish", browser)
    monkeypatch.setattr(
        automation_android,
        "execute",
        Mock(side_effect=BrowserPublishError("android_unavailable", "待连接")),
    )
    with pytest.raises(BrowserPublishError, match="不支持所选执行方式"):
        automation_executor.execute(
            "xianyu",
            "publish",
            {},
            {},
            execution_mode="android",
            device_id="emulator-main",
            before_submit=callback,
        )
    browser.assert_not_called()
    callback.assert_not_called()


def test_android_preflight_is_explicit_and_keeps_snapshot(monkeypatch):
    from content_service import automation_android

    config = {
        "execution_mode": "android",
        "device_id": "phone-one",
        "platforms": ["xianyu"],
    }
    preflight = Mock(side_effect=BrowserPublishError("adapter_unverified", "待适配"))
    monkeypatch.setattr(automation_android, "preflight", preflight)
    with pytest.raises(BrowserPublishError, match="不支持所选执行方式"):
        automation_executor.preflight(config)
    preflight.assert_not_called()
    assert config["device_id"] == "phone-one"


@pytest.mark.parametrize("operation", ["publish", "relist", "delist"])
def test_xianyu_web_dispatch_keeps_account_and_submission_marker(monkeypatch, operation):
    callback = Mock()
    dispatch = Mock(return_value={"status": "verified-fixture"})
    monkeypatch.setattr(automation_browser, "publish" if operation == "publish" else "manage_listing", dispatch)
    connection = {"account_id": "web-account"}
    automation_executor.preflight({"platforms": ["xianyu"], "operation": operation, "accounts": {"xianyu": "web-account"}})
    automation_executor.execute("xianyu", operation, {"title": "test"}, connection, before_submit=callback)
    args = ("xianyu", {"title": "test"}, connection) if operation == "publish" else ("xianyu", operation, {"title": "test"}, connection)
    dispatch.assert_called_once_with(*args, before_submit=callback)
    callback.assert_not_called()


def test_unbound_management_cannot_start():
    with pytest.raises(BrowserPublishError, match="明确选择"):
        automation_executor.preflight({"platforms": ["xianyu"], "operation": "delist"})
