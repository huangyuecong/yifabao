"""Online trial: each trial account gets its own server browser for platform logins."""

import base64
import io
import json

import pytest

from content_service import automation_browser as adapter
from content_service import automation_browser_session as sessions
from content_service import remote_browser, trial_accounts


def basic(username, password="secret-pass"):
    return {"Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()}


@pytest.fixture
def remote(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTENT_TRIAL_ACCOUNTS", "true")
    monkeypatch.setenv("CONTENT_REMOTE_BROWSER", "true")
    monkeypatch.setattr(trial_accounts, "_private_dir", lambda: tmp_path)
    monkeypatch.setattr(adapter, "_data_dir", lambda: tmp_path)
    trial_accounts._verified.clear()
    for name in ("alice", "bob"):
        monkeypatch.setattr("sys.stdin", io.StringIO("secret-pass\n"))
        trial_accounts.main(["set", name])
    yield tmp_path
    trial_accounts._verified.clear()


def test_off_by_default():
    assert remote_browser.config_override() is None
    with pytest.raises(Exception):
        remote_browser.require_trial_account()


def test_each_account_has_its_own_browser(remote):
    token = trial_accounts.current.set(trial_accounts.Account("alice", "爱丽丝", False))
    try:
        alice = adapter._load_config()
    finally:
        trial_accounts.current.reset(token)
    token = remote_browser.task_owner.set("bob")
    try:
        bob = adapter._load_config()
    finally:
        remote_browser.task_owner.reset(token)
    (alice_id, alice_account), = alice["accounts"].items()
    (bob_id, bob_account), = bob["accounts"].items()
    assert (alice_id, bob_id) == ("trial-alice", "trial-bob")
    assert alice_account["label"] == "爱丽丝的平台账号"
    assert alice_account["cdp_url"] != bob_account["cdp_url"]
    # Separate ports mean separate profiles, so separate platform logins.
    assert sessions._profile(alice_account["cdp_url"]) != sessions._profile(bob_account["cdp_url"])
    # The port stays the same the next time.
    assert remote_browser.port_for("alice") == int(alice_account["cdp_url"].rsplit(":", 1)[1])
    with pytest.raises(adapter.BrowserPublishError):
        adapter._load_config()  # no account → no browser


def test_publish_task_uses_the_submitters_browser(remote, monkeypatch, content_client):
    from content_service import catalog_publishing

    seen = {}

    class Record:
        id, workflow, content_id, platform, submitted_by = "r1", "promotion", "c1", "douyin", "bob"

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args, **kwargs):
            class Result:
                rowcount = 1
            return Result()

        def commit(self):
            pass

        def get(self, model, identifier):
            return Record() if identifier == "r1" else object()

    def fake_execute(platform, operation, payload, connection, **kwargs):
        seen["config"] = adapter._load_config()
        raise RuntimeError("stop here")

    monkeypatch.setattr(catalog_publishing.db, "SessionLocal", FakeSession)
    monkeypatch.setattr(catalog_publishing, "_promotion_payload", lambda session, content: {})
    monkeypatch.setattr("content_service.automation_executor.execute", fake_execute)
    catalog_publishing.run_record("r1")
    assert list(seen["config"]["accounts"]) == ["trial-bob"]


def test_screen_and_input_go_to_the_platform_tab(remote, monkeypatch):
    calls = []

    class FakeTools:
        def __init__(self, url):
            calls.append(("open", url))

        def call(self, method, params=None, timeout=10):
            calls.append((method, params))
            if method == "Page.getLayoutMetrics":
                return {"cssVisualViewport": {"clientWidth": 1000, "clientHeight": 800}}
            if method == "Page.captureScreenshot":
                return {"data": base64.b64encode(b"jpeg").decode()}
            return {}

        def close(self):
            pass

    monkeypatch.setattr(remote_browser, "_DevTools", FakeTools)
    monkeypatch.setattr(sessions, "_port_open", lambda port: True)
    monkeypatch.setattr(remote_browser, "verify", lambda cdp, profile: None)
    monkeypatch.setattr(remote_browser, "_targets", lambda port: [
        {"url": "https://www.baidu.com/", "webSocketDebuggerUrl": "ws://other"},
        {"url": "https://creator.douyin.com/", "webSocketDebuggerUrl": "ws://douyin"},
    ])
    token = remote_browser.task_owner.set("alice")
    try:
        assert remote_browser.screen("douyin") == (b"jpeg", 1000, 800)
        remote_browser.click("douyin", 0.5, 0.25)
        remote_browser.type_text("douyin", "1234", "Enter")
    finally:
        remote_browser.task_owner.reset(token)
    assert ("open", "ws://douyin") in calls
    pressed = [params for method, params in calls if method == "Input.dispatchMouseEvent" and params["type"] == "mousePressed"]
    assert pressed and (pressed[0]["x"], pressed[0]["y"]) == (500, 200)
    assert ("Input.insertText", {"text": "1234"}) in calls


def test_routes_need_a_trial_account(remote, content_client, monkeypatch):
    monkeypatch.setattr(remote_browser, "screen", lambda platform: (b"img", 10, 20))
    assert content_client.get("/api/v1/remote-login/douyin/screen").status_code == 401
    response = content_client.get("/api/v1/remote-login/douyin/screen", headers=basic("alice"))
    assert response.status_code == 200
    assert response.headers["x-page-width"] == "10"
    # The browser-session routes accept a trial account instead of a local request.
    monkeypatch.setattr(sessions, "sessions", lambda platforms=None: {"items": []})
    listed = content_client.get("/api/v1/automation/browser/sessions", headers=basic("alice"))
    assert listed.status_code == 200, listed.text


def test_forget_removes_profile_and_bindings(remote, monkeypatch):
    monkeypatch.setattr(remote_browser, "_processes", lambda: [])
    port = remote_browser.port_for("alice")
    profile = sessions._profile(f"http://127.0.0.1:{port}")
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("x")
    bindings = remote / "private" / "browser-login-bindings"
    bindings.mkdir(parents=True)
    (bindings / "trial-alice-douyin.json").write_text(json.dumps({"version": 1}))
    (bindings / "trial-bob-douyin.json").write_text(json.dumps({"version": 1}))
    remote_browser.forget("alice")
    assert not profile.exists()
    assert sorted(path.name for path in bindings.iterdir()) == ["trial-bob-douyin.json"]
