"""Device-independent checks for account isolation and the irreversible submit boundary."""

import json
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from content_service import android_control as control
from content_service import android_publishing as publishing
from content_service import automation_android as android
from content_service.automation_browser import BrowserPublishError


@pytest.fixture
def android_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path))
    config = {
        "devices": {
            "main": {"serial": "emulator-5554", "platforms": ["douyin"], "label": "Android"}
        }
    }
    state = {
        "apps": {
            "douyin": {
                "owner": "alice",
                "state": "logged_in",
                "identity": "alice123",
                "account_id": "account",
                "account_label": "Douyin Alice",
                "version": "1",
            }
        },
        "session": {
            "id": "a" * 32,
            "owner": "alice",
            "platform": "douyin",
            "expires_at": time.time() + 90,
            "frame_id": "b" * 32,
            "frame_at": time.time(),
            "width": 720,
            "height": 1280,
        },
    }
    control.save_state(config, "main", state)
    device = SimpleNamespace(
        screenshot=lambda: Image.new("RGB", (720, 1280)),
        app_info=lambda _: {"versionName": "1"},
        clipboard="",
    )
    calls = []
    monkeypatch.setattr(control, "connect", lambda *_: device)
    monkeypatch.setattr(publishing, "connect", lambda *_: device)
    monkeypatch.setattr(android, "_load_config", lambda: config)
    monkeypatch.setattr(control, "nodes", lambda _: [])
    monkeypatch.setattr(control, "adb", lambda *args, **kwargs: calls.append(args))
    return config, state, device, calls


def test_session_owner_token_expiry_and_replay(android_state):
    config, state, _, calls = android_state
    for token, owner in [("x" * 32, "alice"), ("a" * 32, "bob")]:
        with pytest.raises(BrowserPublishError, match="会话"):
            control.screen(config, "main", token, owner)
    action = {"kind": "tap", "frame_id": "b" * 32, "x": 10, "y": 10}
    response = control.control(config, "main", "a" * 32, "alice", action)
    assert response["frame_id"] != "b" * 32 and len(calls) == 1
    with pytest.raises(BrowserPublishError, match="屏幕"):
        control.control(config, "main", "a" * 32, "alice", action)
    assert len(calls) == 1
    state["session"]["expires_at"] = 0
    control.save_state(config, "main", state)
    with pytest.raises(BrowserPublishError):
        control.screen(config, "main", "a" * 32, "alice")


def test_login_session_can_switch_platform_without_waiting_for_expiry(android_state, monkeypatch):
    config, _, device, _ = android_state
    config["devices"]["main"]["platforms"].append("xianyu")
    launched = []
    monkeypatch.setattr(control, "launch", lambda _, platform: launched.append(platform))
    device.app_current = lambda: {"package": control.PACKAGES["xianyu"]}

    response = control.open_session(config, "main", "xianyu", "alice")

    assert response["platform"] == "xianyu"
    assert response["session_id"] != "a" * 32
    assert launched == ["xianyu"]
    with pytest.raises(BrowserPublishError):
        control.close_session(config, "main", "a" * 32, "alice")
    assert control.read_state(config, "main")["session"]["id"] == response["session_id"]
    with pytest.raises(BrowserPublishError):
        control.screen(config, "main", "a" * 32, "alice")


@pytest.mark.parametrize("protected", ["other_owner", "draft", "prepared"])
def test_open_login_cannot_take_over_protected_device(android_state, monkeypatch, protected):
    config, state, _, _ = android_state
    config["devices"]["main"]["platforms"].append("xianyu")
    if protected == "other_owner":
        state["session"]["owner"] = "bob"
    elif protected == "draft":
        state["session"]["draft"] = {"run_id": "editing"}
    else:
        state.pop("session")
        state["prepared"] = {"run_id": "submitting", "expires_at": time.time() + 90}
    control.save_state(config, "main", state)
    monkeypatch.setattr(control, "connect", lambda *_: pytest.fail("must not touch the device"))

    with pytest.raises(BrowserPublishError) as exc:
        control.open_session(config, "main", "xianyu", "alice")

    assert exc.value.code in {"android_session_busy", "android_publication_busy"}
    assert control.read_state(config, "main") == state


@pytest.mark.parametrize(
    ("foreground", "expected_launches"),
    [
        ("com.android.launcher3", ["douyin"]),
        (control.PACKAGES["douyin"], []),
        ("com.android.permissioncontroller", []),
    ],
)
def test_reopening_login_recovers_exited_app_only(
    android_state, monkeypatch, foreground, expected_launches
):
    config, _, device, _ = android_state
    launched = []
    monkeypatch.setattr(control, "launch", lambda _, platform: launched.append(platform))
    device.app_current = lambda: {"package": foreground}
    device.shell = lambda *args, **kwargs: SimpleNamespace(output="123", exit_code=0)

    response = control.open_session(config, "main", "douyin", "alice")

    assert response["session_id"] == "a" * 32
    assert launched == expected_launches


def test_reopening_draft_never_relaunches_app(android_state, monkeypatch):
    config, state, device, _ = android_state
    state["session"]["draft"] = {
        "run_id": "editing",
        "note": "draft",
        "imported": [],
        "payload": {},
    }
    control.save_state(config, "main", state)
    device.app_current = lambda: {"package": "com.android.launcher3"}
    monkeypatch.setattr(control, "launch", lambda *_: pytest.fail("must preserve draft screen"))

    response = control.open_session(config, "main", "douyin", "alice")

    assert response["draft"]["run_id"] == "editing"


def test_resumed_session_refreshes_app_version_and_invalidates_publish_verification(android_state):
    config, state, device, _ = android_state
    state["apps"]["douyin"]["publish_verified"] = True
    control.save_state(config, "main", state)
    device.app_info = lambda _: {"versionName": "2"}
    device.app_current = lambda: {"package": control.PACKAGES["douyin"]}

    result = control.open_session(config, "main", "douyin", "alice")

    assert result["app"]["version"] == "2"
    assert "publish_verified" not in result["app"]
    assert result["app"]["account_id"] == "account"
    assert result["session_id"] == "a" * 32


def test_expired_session_close_is_idempotent_and_keeps_account(android_state, monkeypatch):
    config, state, _, _ = android_state
    state["session"]["expires_at"] = 0
    control.save_state(config, "main", state)
    monkeypatch.setattr(
        control, "connect", lambda *_: pytest.fail("closing requires no connection")
    )

    assert control.close_session(config, "main", "a" * 32, "alice") == {"closed": True}
    assert control.close_session(config, "main", "a" * 32, "alice") == {"closed": True}
    stored = control.read_state(config, "main")
    assert "session" not in stored
    assert stored["apps"] == state["apps"]


def test_other_owner_cannot_close_even_an_expired_session(android_state):
    config, state, _, _ = android_state
    state["session"]["expires_at"] = 0
    control.save_state(config, "main", state)

    with pytest.raises(BrowserPublishError):
        control.close_session(config, "main", "a" * 32, "bob")

    assert control.read_state(config, "main") == state


def test_failed_device_action_cannot_replay_frame(android_state, monkeypatch):
    config, _, _, calls = android_state

    def failing(*args):
        calls.append(args)
        raise OSError("lost connection after a tap")

    monkeypatch.setattr(control, "adb", failing)
    action = {"kind": "tap", "frame_id": "b" * 32, "x": 10, "y": 10}
    with pytest.raises(OSError):
        control.control(config, "main", "a" * 32, "alice", action)
    with pytest.raises(BrowserPublishError):
        control.control(config, "main", "a" * 32, "alice", action)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("foreground", "pid", "expected"),
    [
        ("com.android.permissioncontroller", "", "exited"),
        ("com.android.permissioncontroller", "123", "permission_required"),
        ("com.android.launcher3", "", "exited"),
        (control.PACKAGES["douyin"], "123", "running"),
    ],
)
def test_frame_distinguishes_crash_from_permission_dialog(android_state, foreground, pid, expected):
    config, _, device, _ = android_state
    device.app_current = lambda: {"package": foreground}
    device.shell = lambda *args, **kwargs: SimpleNamespace(output=pid, exit_code=0)
    frame = control.screen(config, "main", "a" * 32, "alice")
    assert frame["app"]["runtime_state"] == expected
    assert bool(frame["app"].get("error")) == (expected == "exited")


def test_publish_tap_requires_tracked_workflow(android_state, monkeypatch):
    config, _, _, calls = android_state
    monkeypatch.setattr(
        control,
        "nodes",
        lambda _: [
            {
                "package": control.PACKAGES["douyin"],
                "enabled": "true",
                "text": "发布",
                "bounds": "[0,0][80,80]",
            }
        ],
    )
    with pytest.raises(BrowserPublishError) as exc:
        control.control(
            config,
            "main",
            "a" * 32,
            "alice",
            {"kind": "tap", "frame_id": "b" * 32, "x": 10, "y": 10},
        )
    assert exc.value.code == "android_use_publish_action" and not calls


def test_cross_process_lock_and_account_isolation(android_state):
    config, _, _, _ = android_state
    with (
        control.device_lock(config, "main"),
        pytest.raises(BrowserPublishError),
        control.device_lock(config, "main"),
    ):
        pytest.fail("same device lock must be exclusive")
    with pytest.raises(BrowserPublishError):
        publishing.bound_account(config, "main", "douyin", "bob")
    with pytest.raises(BrowserPublishError):
        publishing.bound_account(config, "main", "douyin", "alice", "another")


def test_other_user_profile_cannot_bind_account(android_state, monkeypatch):
    config, _, _, _ = android_state
    items = [{"package": control.PACKAGES["douyin"], "text": "抖音号：someoneelse"}]
    assert control.account_identity("douyin", items) == "someoneelse"
    monkeypatch.setattr(control, "nodes", lambda _: items)
    with pytest.raises(BrowserPublishError):
        control.verify_account(config, "main", "a" * 32, "alice")
    assert not control.read_state(config, "main")["apps"]["douyin"].get("account_id")


def test_duplicate_identifiers_are_not_a_verified_account():
    package = control.PACKAGES["douyin"]
    items = [
        {"package": package, "text": "抖音号: alice123"},
        {"package": package, "text": "抖音号: bob123"},
    ]
    assert control.account_identity("douyin", items) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyin.com/user/123456789",
        "https://v.douyin.com/abcdef/",
        "https://www.douyin.com.evil.test/video/123456789",
        "https://user@www.douyin.com/video/123456789",
        "https://www.douyin.com:bad/video/123456789",
    ],
)
def test_profile_short_and_untrusted_links_are_not_receipts(url):
    assert publishing.receipt_from_url("douyin", url) is None


def test_content_receipt():
    assert (
        publishing.receipt_from_url("douyin", "https://www.douyin.com/video/123456789")[
            "external_id"
        ]
        == "123456789"
    )
    assert (
        publishing.receipt_from_url("xianyu", "https://www.goofish.com/item?id=123456789")[
            "external_id"
        ]
        == "123456789"
    )


def test_media_must_stay_in_private_generated_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path))
    path = tmp_path / "outside.mp4"
    path.write_bytes(b"media")
    with pytest.raises(BrowserPublishError):
        publishing.validated_media("douyin", {"video_path": str(path)})
    directory = tmp_path / "automation-media" / "run"
    directory.mkdir(parents=True)
    inside = directory / "video.mp4"
    inside.write_bytes(b"media")
    assert publishing.validated_media("douyin", {"video_path": str(inside)}) == [inside]


def prepared(android_state, monkeypatch):
    config, state, device, calls = android_state
    state.pop("session")
    payload = {"title": "Title", "description": "Text"}
    state["prepared"] = {
        "run_id": "run",
        "platform": "douyin",
        "owner": "alice",
        "account_id": "account",
        "expires_at": time.time() + 90,
        "digest": publishing.payload_digest(payload),
        "imported": ["media"],
        "version": "1",
        "form_digest": publishing.form_digest([]),
    }
    control.save_state(config, "main", state)
    monkeypatch.setattr(publishing, "verify_form", lambda *_: {"bounds": "[0,0][80,80]"})
    monkeypatch.setattr(publishing, "guard", lambda *_: [])
    monkeypatch.setattr(publishing, "click", lambda *_: calls.append("tap"))
    return payload


def test_durable_marker_failure_prevents_all_taps(android_state, monkeypatch):
    payload = prepared(android_state, monkeypatch)

    def marker():
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError):
        publishing.execute(
            "douyin",
            "publish",
            payload,
            {"run_id": "run", "owner": "alice"},
            device_id="main",
            before_submit=marker,
        )
    assert not android_state[3]
    assert not control.read_state(android_state[0], "main").get("prepared")


def test_failure_after_single_tap_is_unknown_and_cannot_replay(android_state, monkeypatch):
    payload = prepared(android_state, monkeypatch)
    monkeypatch.setattr(
        publishing, "nodes", lambda *_: (_ for _ in ()).throw(OSError("device lost"))
    )
    marker = []
    with pytest.raises(BrowserPublishError) as exc:
        publishing.execute(
            "douyin",
            "publish",
            payload,
            {"run_id": "run", "owner": "alice"},
            device_id="main",
            before_submit=lambda: marker.append(True),
        )
    assert exc.value.status == "unknown" and marker == [True] and android_state[3] == ["tap"]
    with pytest.raises(BrowserPublishError):
        publishing.execute(
            "douyin",
            "publish",
            payload,
            {"run_id": "run", "owner": "alice"},
            device_id="main",
            before_submit=lambda: marker.append(True),
        )
    assert marker == [True] and android_state[3] == ["tap"]


@pytest.mark.parametrize("change", ["payload", "owner", "version", "screen"])
def test_changed_submission_context_stops_before_marker(android_state, monkeypatch, change):
    payload = prepared(android_state, monkeypatch)
    owner = "bob" if change == "owner" else "alice"
    if change == "payload":
        payload["title"] = "Changed"
    if change == "version":
        android_state[2].app_info = lambda _: {"versionName": "2"}
    if change == "screen":
        monkeypatch.setattr(publishing, "guard", lambda *_: [{"text": "changed"}])
    with pytest.raises(BrowserPublishError):
        publishing.execute(
            "douyin",
            "publish",
            payload,
            {"run_id": "run", "owner": owner},
            device_id="main",
            before_submit=lambda: pytest.fail("must stop before submission"),
        )
    assert not android_state[3]


def test_status_does_not_leak_other_owners_or_claim_auto_ready(android_state):
    config, state, _, _ = android_state
    state.update(connected=True, checked_at=time.time())
    state["apps"]["douyin"]["publish_verified"] = True
    control.save_state(config, "main", state)
    document = {
        "devices": [{"device_id": "main"}],
        "adb_configured": True,
        "platforms": {"douyin": {}},
    }
    result = control.enrich_status(config, document, "bob")
    assert "alice" not in json.dumps(result).lower() and not result["platforms"]["douyin"]["ready"]
