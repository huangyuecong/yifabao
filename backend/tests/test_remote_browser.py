"""Online trial: one server browser, one incognito context per trial account."""

import base64
import io
import json
import re

import pytest
from fastapi import HTTPException

from content_service import automation_browser as adapter
from content_service import automation_browser_session as sessions
from content_service import remote_browser, trial_accounts


def basic(username, password="secret-pass"):
    return {"Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()}


class FakeBrowser:
    """The DevTools target/context calls the module makes, against an in-memory browser."""

    def __init__(self):
        self.pages = []  # {"targetId", "url", "browserContextId", "type"}
        self.disposed = []
        self.count = 0

    def __call__(self, method, params=None):
        params = params or {}
        if method == "Target.getTargets":
            return {"targetInfos": [dict(page) for page in self.pages]}
        if method == "Target.createBrowserContext":
            self.count += 1
            return {"browserContextId": f"ctx{self.count}"}
        if method == "Target.createTarget":
            self.count += 1
            self.pages.append({"targetId": f"t{self.count}", "url": params["url"], "type": "page",
                               "browserContextId": params["browserContextId"]})
            return {"targetId": f"t{self.count}"}
        if method == "Target.disposeBrowserContext":
            self.disposed.append(params["browserContextId"])
            self.pages = [page for page in self.pages if page["browserContextId"] != params["browserContextId"]]
            return {}
        raise AssertionError(method)


@pytest.fixture
def remote(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTENT_TRIAL_ACCOUNTS", "true")
    monkeypatch.setenv("CONTENT_REMOTE_BROWSER", "true")
    monkeypatch.setattr(trial_accounts, "_private_dir", lambda: tmp_path)
    monkeypatch.setattr(adapter, "_data_dir", lambda: tmp_path)
    trial_accounts._verified.clear()
    remote_browser._last_used.clear()
    for name in ("alice", "bob"):
        monkeypatch.setattr("sys.stdin", io.StringIO("secret-pass\n"))
        trial_accounts.main(["set", name])
    browser = FakeBrowser()
    monkeypatch.setattr(remote_browser, "_browser_call", browser)
    monkeypatch.setattr(remote_browser, "_running", lambda: True)
    yield browser
    trial_accounts._verified.clear()


def as_user(name):
    return remote_browser.task_owner.set(name)


def test_off_by_default():
    assert remote_browser.config_override() is None
    assert remote_browser.lock_scope() == ""
    with pytest.raises(HTTPException):
        remote_browser.require_trial_account()


def test_accounts_share_one_browser_but_not_logins_or_locks(remote):
    token = trial_accounts.current.set(trial_accounts.Account("alice", "爱丽丝", False))
    try:
        alice = adapter._load_config()
        alice_lock = remote_browser.lock_scope()
    finally:
        trial_accounts.current.reset(token)
    token = as_user("bob")
    try:
        bob = adapter._load_config()
        bob_lock = remote_browser.lock_scope()
    finally:
        remote_browser.task_owner.reset(token)
    (alice_id, alice_account), = alice["accounts"].items()
    (bob_id, bob_account), = bob["accounts"].items()
    assert (alice_id, bob_id) == ("trial-alice", "trial-bob")
    assert alice_account["label"] == "爱丽丝的平台账号"
    assert alice_account["cdp_url"] == bob_account["cdp_url"] == remote_browser.CDP_URL
    assert alice_lock != bob_lock
    with pytest.raises(adapter.BrowserPublishError):
        adapter._load_config()  # no account → no browser


def test_each_account_gets_its_own_incognito_context(remote, tmp_path):
    bindings = tmp_path / "private" / "browser-login-bindings"
    bindings.mkdir(parents=True)
    (bindings / "trial-alice-douyin.json").write_text("{}")
    alice = remote_browser.context_for("alice", create=True)
    bob = remote_browser.context_for("bob", create=True)
    assert alice != bob
    assert remote_browser.context_for("alice", create=True) == alice  # reused, found by its marker tab
    assert remote_browser.context_for("carol", create=False) is None
    assert sorted(remote_browser.running_owners()) == ["alice", "bob"]
    # A fresh incognito context has no login, so an old account binding must not linger.
    assert not (bindings / "trial-alice-douyin.json").exists()


def test_full_browser_closes_the_most_idle_account(remote, monkeypatch):
    monkeypatch.setenv("CONTENT_REMOTE_BROWSER_MAX", "2")
    first = remote_browser.context_for("alice", create=True)
    remote_browser.context_for("bob", create=True)
    remote_browser._last_used["alice"] = 0  # idle longest
    remote_browser.context_for("carol", create=True)
    assert remote.disposed == [first]
    assert sorted(remote_browser.running_owners()) == ["bob", "carol"]


def test_a_publishing_account_is_never_closed_for_room(remote, monkeypatch):
    monkeypatch.setenv("CONTENT_REMOTE_BROWSER_MAX", "1")
    remote_browser.context_for("alice", create=True)
    with remote_browser._account_lock("alice", wait=0), pytest.raises(adapter.BrowserPublishError) as error:
        remote_browser.context_for("bob", create=True)
    assert error.value.code == "browser_busy" and remote.disposed == []


def test_account_context_keeps_tabs_and_cookies_inside_the_account(remote):
    alice = remote_browser.context_for("alice", create=True)
    bob = remote_browser.context_for("bob", create=True)
    calls = []

    class Page:
        def __init__(self, url, context):
            self.url, self.context_id = url, context

    class Session:
        def __init__(self, page=None):
            self.page = page

        def send(self, method, params=None):
            calls.append((method, params))
            if method == "Target.getTargetInfo":
                return {"targetInfo": {"browserContextId": self.page.context_id}}
            if method == "Target.createTarget":
                base.pages.append(Page(params["url"], params["browserContextId"]))
                return {"targetId": "new"}
            if method == "Storage.getCookies":
                return {"cookies": [{"name": "sid", "value": "1", "domain": ".douyin.com", "path": "/", "expires": -1},
                                    {"name": "o", "value": "2", "domain": ".kuaishou.com", "path": "/", "expires": 2e9}]}
            return {}

        def detach(self):
            pass

    class Base:
        pages = [Page("about:blank", "default"), Page(remote_browser.MARKER + "alice", alice),
                 Page("https://creator.douyin.com/", alice), Page("https://creator.douyin.com/", bob)]

        def new_cdp_session(self, page):
            return Session(page)

    class Browser:
        def new_browser_cdp_session(self):
            return Session()

    base = Base()
    context = remote_browser.AccountContext(Browser(), base, alice)
    assert [page.url for page in context.pages] == ["https://creator.douyin.com/"]
    page = context.new_page()
    assert page.context_id == alice and page.url.startswith("about:blank#tab-")
    context.clear_cookies(domain=re.compile(r"(^|\.)douyin\.com$"))
    restored = next(params for method, params in calls if method == "Storage.setCookies")
    assert restored["browserContextId"] == alice
    assert [cookie["name"] for cookie in restored["cookies"]] == ["o"] and restored["cookies"][0]["expires"] == 2e9


def test_publish_task_uses_the_submitters_context(remote, monkeypatch, content_client):
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


def test_screen_and_input_go_to_the_accounts_platform_tab(remote, monkeypatch):
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

    alice = remote_browser.context_for("alice", create=True)
    bob = remote_browser.context_for("bob", create=True)
    remote.pages += [
        {"targetId": "bob-dy", "url": "https://creator.douyin.com/", "type": "page", "browserContextId": bob},
        {"targetId": "alice-dy", "url": "https://creator.douyin.com/", "type": "page", "browserContextId": alice},
        {"targetId": "alice-other", "url": "https://www.baidu.com/", "type": "page", "browserContextId": alice},
    ]
    monkeypatch.setattr(remote_browser, "_DevTools", FakeTools)
    token = as_user("alice")
    try:
        assert remote_browser.screen("douyin") == (b"jpeg", 1000, 800)
        remote_browser.click("douyin", 0.5, 0.25)
        remote_browser.type_text("douyin", "1234", "Enter")
        with pytest.raises(adapter.BrowserPublishError):
            remote_browser.task_owner.set("carol")
            remote_browser.screen("douyin")  # no context: nothing to show
    finally:
        remote_browser.task_owner.reset(token)
    assert ("open", f"ws://127.0.0.1:{remote_browser.PORT}/devtools/page/alice-dy") in calls
    assert not any(url.endswith("bob-dy") for kind, url in calls if kind == "open")
    pressed = [params for method, params in calls if method == "Input.dispatchMouseEvent" and params["type"] == "mousePressed"]
    assert pressed and (pressed[0]["x"], pressed[0]["y"]) == (500, 200)
    assert ("Input.insertText", {"text": "1234"}) in calls


def test_status_without_a_context_is_not_opened(remote, monkeypatch):
    monkeypatch.setattr(sessions, "_port_open", lambda port: True)
    token = as_user("alice")
    try:
        result = sessions.sessions(("douyin",))
    finally:
        remote_browser.task_owner.reset(token)
    assert result["items"][0]["state"] == "not_opened"
    assert remote_browser.running_owners() == []  # a status check opens nothing


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


def test_forget_closes_the_context_and_bindings(remote, tmp_path):
    alice = remote_browser.context_for("alice", create=True)
    remote_browser.context_for("bob", create=True)
    bindings = tmp_path / "private" / "browser-login-bindings"
    bindings.mkdir(parents=True)
    (bindings / "trial-alice-douyin.json").write_text(json.dumps({"version": 1}))
    (bindings / "trial-bob-douyin.json").write_text(json.dumps({"version": 1}))
    remote_browser.forget("alice")
    assert remote.disposed == [alice]
    assert remote_browser.running_owners() == ["bob"]
    assert sorted(path.name for path in bindings.iterdir()) == ["trial-bob-douyin.json"]


def test_monitor_checks_do_not_keep_a_context_open(remote):
    remote_browser._last_used.pop("alice", None)
    token, quiet = as_user("alice"), remote_browser.monitoring.set(True)
    try:
        remote_browser._touch()
    finally:
        remote_browser.monitoring.reset(quiet)
        remote_browser.task_owner.reset(token)
    assert "alice" not in remote_browser._last_used


def test_clear_login_signs_one_platform_out(remote, monkeypatch, tmp_path):
    from contextlib import contextmanager

    cleared, closed, storage = [], [], []

    class Page:
        def __init__(self, url):
            self.url = url

        def close(self):
            closed.append(self.url)

    class Context:
        pages = [Page("https://creator.douyin.com/home"), Page("https://www.xiaohongshu.com/")]

        def clear_cookies(self, domain):
            cleared.append(domain)

        def new_page(self):
            return Page("about:blank")

        def new_cdp_session(self, page):
            class Session:
                def send(self, method, params):
                    storage.append(params["origin"])
            return Session()

    @contextmanager
    def connected(cdp):
        yield Context()

    remote_browser.context_for("alice", create=True)
    monkeypatch.setattr(sessions, "_port_open", lambda port: True)
    monkeypatch.setattr(sessions, "_verify_dedicated_process", lambda cdp, profile: None)
    monkeypatch.setattr(sessions, "_connected", connected)
    bindings = tmp_path / "private" / "browser-login-bindings"
    bindings.mkdir(parents=True)
    (bindings / "trial-alice-douyin.json").write_text("{}")
    (bindings / "trial-alice-kuaishou.json").write_text("{}")
    token = as_user("alice")
    try:
        result = sessions.clear_login("douyin")
    finally:
        remote_browser.task_owner.reset(token)
    assert result["state"] == "login_required"
    pattern = cleared[0]
    assert pattern.search("creator.douyin.com") and not pattern.search("xiaohongshu.com")
    assert closed == ["https://creator.douyin.com/home", "about:blank"]  # the platform tab, then its own helper tab
    assert "https://creator.douyin.com" in storage
    assert sorted(path.name for path in bindings.iterdir()) == ["trial-alice-kuaishou.json"]
