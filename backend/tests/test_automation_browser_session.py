"""Dedicated profile isolation and honest live browser/account state."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from content_service import automation_browser_session as service
from content_service import automation_browser_session_routes as routes


class Locator:
    def __init__(self, text="", visible=False, count=0):
        self.text, self.visible, self.amount = text, visible, count

    def count(self):
        return self.amount

    def nth(self, _index):
        return self

    def is_visible(self):
        return self.visible

    def inner_text(self, **_kwargs):
        return self.text

    def wait_for(self, **_kwargs):
        pass

    def filter(self, **_kwargs):
        return self

    def or_(self, other):
        return self if self.visible else other

    @property
    def first(self):
        return self


class Page:
    def __init__(self, url="about:blank", login=None, identity=None):
        self.url, self.login, self.front = url, login, False
        self.navigated = []
        self.identity = identity
        self.context = None
        self.closed = False
        self.visible = False
        self.focused = False

    def goto(self, url, **_kwargs):
        redirect = self.context.fresh_login_url if self.context else None
        self.url = redirect(url) if callable(redirect) else redirect or url
        self.navigated.append(url)

    def bring_to_front(self):
        self.front = True

    def evaluate(self, _expression):
        return {"visible": self.visible, "focused": self.focused}

    def get_by_text(self, text, **_kwargs):
        return Locator(text, text == self.login, int(text == self.login))

    def locator(self, selector):
        return Locator(self.identity or "", bool(self.identity), int(bool(self.identity)))

    def close(self):
        self.closed = True
        if self.context is not None:
            self.context.pages.remove(self)


class Context:
    def __init__(self, pages=None):
        self.pages = pages or []
        self.fresh_identity = None
        self.fresh_login_url = None

    def new_page(self):
        page = Page(identity=self.fresh_identity)
        page.context = self
        self.pages.append(page)
        return page


@pytest.fixture
def browser(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONTENT_PUBLISH_BROWSER_CONFIG", raising=False)
    context = Context()

    @contextmanager
    def connected(_url):
        yield context

    monkeypatch.setattr(service, "_connected", connected)
    monkeypatch.setattr(service, "_port_open", lambda _port: True)
    monkeypatch.setattr(service, "_verify_dedicated_process", lambda *_: None)
    return context


def test_initial_config_enables_builtin_web_adapters_and_preserves_existing_config(browser):
    service._ensure_initial_config()
    path = service.adapter._config_path()
    original = path.read_bytes()
    config = json.loads(original)
    for platform, recipe in config["accounts"]["local-promotion"]["platforms"].items():
        assert platform in service.PAGES
        if platform == 'xianyu':
            assert recipe == {'adapter': 'xianyu-web-v1', 'enabled': True}
        else:
            assert recipe == {'adapter': 'promotion-web-v1', 'enabled': True}
    service._ensure_initial_config()
    assert path.read_bytes() == original


def test_failed_atomic_config_write_leaves_no_partial_config(browser, monkeypatch):
    monkeypatch.setattr(
        service.json, "dump", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError):
        service._ensure_initial_config()
    path = service.adapter._config_path()
    assert not path.exists()
    assert list(path.parent.glob(".browser-config-*.tmp")) == []


def test_missing_platform_recipe_reuses_existing_single_dedicated_account(browser):
    path = service.adapter._config_path()
    service._atomic_json(
        path,
        {
            "version": 1,
            "accounts": {
                "existing": {
                    "label": "Existing",
                    "cdp_url": "http://127.0.0.1:9333",
                    "platforms": {"douyin": {}},
                }
            },
        },
    )
    identifier, account, recipe = service._connection("xiaohongshu")
    assert identifier == "existing" and account["cdp_url"].endswith(":9333") and recipe == {}


def test_xianyu_login_and_repeated_publish_reuse_the_editor(browser):
    editor = Page(service.PAGES["xianyu"]["publish"], login="登录")
    browser.pages.append(editor)
    browser.fresh_login_url = "https://www.goofish.com/login"
    for action in ("login", "publish", "publish"):
        result = service.session("xianyu", open_page=action)
        assert result["state"] == "login_required" and not result["publish_ready"]
    assert browser.pages == [editor] and not editor.navigated


def test_xianyu_member_identity_is_fresh_bound_and_does_not_enable_publishing(browser):
    service._ensure_initial_config()
    path = service.adapter._config_path()
    config = json.loads(path.read_text(encoding='utf-8'))
    config['accounts']['local-promotion']['platforms']['xianyu']['enabled'] = False
    service._atomic_json(path, config)
    editor = Page(service.PAGES["xianyu"]["publish"])
    browser.pages.append(editor)
    browser.fresh_identity = "fixture_member"
    result = service.session("xianyu", open_page="login")
    assert result["state"] == "logged_in" and not result["publish_ready"]
    assert not any(result["operations_ready"].values())
    browser.fresh_identity = "another_member"
    assert service.session("xianyu")["state"] == "unknown"
    browser.fresh_identity = None
    browser.fresh_login_url = "https://www.goofish.com/login"
    assert service.session("xianyu")["state"] == "login_required"
    assert browser.pages == [editor] and not editor.navigated


def test_xianyu_access_error_does_not_claim_login(browser):
    blocked = Page(service.PAGES["xianyu"]["publish"], login="为了保障您的体验，请使用正常浏览器访问闲鱼~")
    result = service._observed("xianyu", blocked, "account", {"label": "Web"}, {})
    assert result["state"] == "unknown" and not result["publish_ready"]
    assert "暂未允许" in result["message"]


def test_xianyu_management_only_identity_uses_fresh_page_and_operation_readiness(browser, monkeypatch):
    recipe = {"management": {"delist": {
        "management_url": "https://www.goofish.com/test-only-manage",
        "account_locator": {"css": "#account"}, "expected_account": "Account A",
    }}}
    editor = Page(service.PAGES["xianyu"]["publish"], identity="Old account")
    browser.pages.append(editor)
    browser.fresh_identity = "Account A"
    monkeypatch.setattr(service.adapter, "_operation_issues", lambda _p, op, _r: [] if op == "delist" else ["not verified"])
    result = service._inspect_identity(browser, editor, "xianyu", "account", {"label": "Web"}, recipe)
    assert result["state"] == "logged_in" and not result["publish_ready"]
    assert result["operations_ready"] == {"publish": False, "relist": False, "delist": True}
    assert not editor.navigated and browser.pages == [editor]
    browser.fresh_identity = "Account B"
    assert service._inspect_identity(browser, editor, "xianyu", "account", {"label": "Web"}, recipe)["state"] == "unknown"


def test_open_preserves_existing_qr_or_editor_and_never_claims_login(browser):
    existing = Page("https://creator.douyin.com/creator-micro/home")
    browser.pages.append(existing)
    result = service.session("douyin", open_page="login")
    assert existing.front and existing.navigated == [] and len(browser.pages) == 1
    assert result["connected"] and result["state"] == "unknown"
    assert not result["publish_ready"] and "account_id" not in result


def test_publish_opens_new_fixed_page_and_keeps_existing_draft(browser):
    existing = Page("https://creator.douyin.com/creator-micro/home")
    browser.pages.append(existing)
    result = service.session("douyin", open_page="publish")
    assert existing.navigated == [] and len(browser.pages) == 2
    assert browser.pages[-1].url == service.PAGES["douyin"]["publish"]
    assert result["state"] == "unknown"


@pytest.mark.parametrize(
    "platform,public_url",
    [
        ("xianyu", "https://www.goofish.com/item?id=123"),
        ("douyin", "https://www.douyin.com/"),
        ("xiaohongshu", "https://www.xiaohongshu.com/explore"),
        ("kuaishou", "https://www.kuaishou.com/"),
    ],
)
def test_login_opens_creator_workspace_instead_of_public_feed(browser, platform, public_url):
    feed = Page(public_url)
    browser.pages.append(feed)
    # No creator tab: the check reads a fresh account page (no evidence here), never the feed.
    assert service.session(platform)["state"] == "unknown"
    assert feed.navigated == [] and not feed.front
    service.session(platform, open_page="login")
    assert feed.navigated == [] and not feed.front
    assert len(browser.pages) == 2
    assert browser.pages[-1].url == service.PAGES[platform]["login"]
    assert browser.pages[-1].front


def test_repeated_publish_entry_reuses_editor_even_with_later_home_tab(browser):
    editor = Page(service.PAGES["douyin"]["publish"])
    home = Page("https://creator.douyin.com/creator-micro/home")
    browser.pages.extend([editor, home])
    service.session("douyin", open_page="publish")
    service.session("douyin", open_page="publish")
    assert browser.pages == [editor, home]
    assert editor.front and editor.navigated == [] and home.navigated == []


@pytest.mark.parametrize(
    "platform,url,label",
    [
        ("xiaohongshu", "https://creator.xiaohongshu.com/login", None),
        ("kuaishou", "https://passport.kuaishou.com/pc/account/login/?sid=test", None),
        ("kuaishou", "https://cp.kuaishou.com/profile", "立即登录"),
    ],
)
def test_known_login_pages_report_login_required(browser, platform, url, label):
    browser.pages.append(Page(url, label))
    result = service.session(platform)
    assert result["connected"] and result["state"] == "login_required"
    assert "?sid=" not in result["url"] and not result["publish_ready"]


def test_live_matching_account_is_required_and_recipe_controls_publish_ready(browser, monkeypatch):
    page = Page("https://creator.douyin.com/creator-micro/home")
    recipe = {
        "verified": False,
        "expected_account": "Account A",
        "account_locator": {"css": ".account"},
        "blockers": [{"css": ".login"}],
    }
    monkeypatch.setattr(
        service.adapter,
        "_locator",
        lambda _p, spec: Locator("Account A", True, 1) if spec["css"] == ".account" else Locator(),
    )
    monkeypatch.setattr(service.adapter, "_recipe_issues", lambda *_: ["not enabled"])
    result = service._observed("douyin", page, "configured-id", {"label": "Main account"}, recipe)
    assert result["state"] == "logged_in" and result["account_id"] == "configured-id"
    assert not result["publish_ready"]
    recipe["expected_account"] = "Different account"
    assert (
        service._observed("douyin", page, "configured-id", {"label": "Main account"}, recipe)[
            "state"
        ]
        == "unknown"
    )


def test_fresh_douyin_login_binds_and_uses_builtin_adapter(browser):
    service._ensure_initial_config()
    original = service.adapter._config_path().read_bytes()
    browser.pages.append(
        Page("https://creator.douyin.com/creator-micro/home", identity="抖音号：sample_account")
    )
    browser.fresh_identity = "抖音号：sample_account"
    result = service.session("douyin")
    assert result["state"] == "logged_in" and result["account_id"] == "local-promotion"
    assert result["publish_ready"]
    assert service.adapter._config_path().read_bytes() == original
    path = (
        service.adapter._data_dir() / "private/browser-login-bindings/local-promotion-douyin.json"
    )
    assert path.is_file() and "sample_account" not in path.read_text()
    browser.fresh_identity = "抖音号：different_account"
    assert service.session("douyin")["state"] == "unknown"
    browser.fresh_identity = "抖音号：sample_account"
    assert service.session("douyin")["state"] == "logged_in"


def test_login_is_detected_without_any_platform_tab_open(browser):
    """The reported case: a platform could publish, yet showed 待打开 with no tab open."""
    service._ensure_initial_config()
    browser.fresh_identity = "抖音号：sample_account"
    result = service.session("douyin")
    assert result["state"] == "logged_in" and result["account_id"] == "local-promotion"
    assert browser.pages == []  # the account tab it opened is closed again


def test_status_check_is_not_blocked_by_a_running_publish(browser):
    service._ensure_initial_config()
    browser.fresh_identity = "抖音号：sample_account"
    with service.adapter._browser_lock(service.DEFAULT_CDP):
        assert service.session("douyin")["state"] == "logged_in"
        assert service.sessions(("douyin",))["items"][0]["state"] == "logged_in"


def test_publish_waits_in_its_own_tab_for_the_user_to_log_in(browser, monkeypatch):
    monkeypatch.setattr(service, "LOGIN_POLL_SECONDS", 0)
    task = Page("about:blank")
    task.context = browser
    states = iter(["login_required", "login_required", "logged_in"])
    monkeypatch.setattr(
        service, "_inspect_identity",
        lambda *_: {"state": next(states), "account_id": "acct"},
    )
    clock = iter(range(0, 1000, 5))
    state = service.ensure_logged_in(browser, task, "douyin", "acct", {"label": "a"}, {})
    assert state["state"] == "logged_in" and state["waited_for_login"]
    assert task.navigated == [service.PAGES["douyin"]["login"]] and task.front
    monkeypatch.setattr(service, "_inspect_identity", lambda *_: {"state": "login_required"})
    with pytest.raises(service.adapter.BrowserPublishError) as error:
        service.wait_for_login(
            browser, task, "douyin", "acct", {}, {}, timeout=20,
            sleep=lambda _: None, clock=lambda: next(clock),
        )
    assert error.value.code == "login_timeout"


def test_identity_check_fetches_fresh_home_without_replacing_existing_tabs(browser):
    service._ensure_initial_config()
    home = Page("https://creator.douyin.com/creator-micro/home", identity="抖音号：sample_account")
    editor = Page(service.PAGES["douyin"]["publish"])
    browser.pages.extend([home, editor])
    browser.fresh_identity = "抖音号：sample_account"
    result = service.session("douyin")
    assert result["state"] == "logged_in" and editor.navigated == []
    assert browser.pages == [home, editor]


def test_expired_login_never_uses_old_home_account_dom(browser):
    service._ensure_initial_config()
    stale_home = Page(
        "https://creator.douyin.com/creator-micro/home", identity="抖音号：stale_user"
    )
    browser.pages.append(stale_home)
    browser.fresh_login_url = "https://creator.douyin.com/login"
    result = service.session("douyin")
    assert result["state"] == "login_required" and not result["publish_ready"]
    assert not (service.adapter._data_dir() / "private/browser-login-bindings").exists()
    assert stale_home.navigated == [] and browser.pages == [stale_home]


def test_failed_fresh_check_cannot_fall_back_to_old_logged_in_dom(browser, monkeypatch):
    service._ensure_initial_config()
    browser.pages.append(
        Page("https://creator.douyin.com/creator-micro/home", identity="抖音号：stale_user")
    )
    monkeypatch.setattr(
        browser, "new_page", lambda: (_ for _ in ()).throw(RuntimeError("navigation failed"))
    )
    result = service.session("douyin")
    assert result["state"] == "unknown" and not result["publish_ready"]


def test_async_account_render_is_awaited_before_identity_read(browser, monkeypatch):
    service._ensure_initial_config()
    old = Page(service.ACCOUNT_PAGES["douyin"])
    browser.pages.append(old)
    waits = []

    class RenderingSignal(Locator):
        def or_(self, _other):
            return self

        def wait_for(self, **kwargs):
            waits.append(kwargs)
            fresh.identity = "抖音号：rendered_account"

    fresh = Page()
    fresh.context = browser
    monkeypatch.setattr(
        fresh,
        "locator",
        lambda _selector: Locator(fresh.identity, True, 1) if fresh.identity else RenderingSignal(),
    )

    def new_page():
        browser.pages.append(fresh)
        return fresh

    monkeypatch.setattr(browser, "new_page", new_page)
    result = service.session("douyin")
    assert result["state"] == "logged_in" and result["publish_ready"]
    assert waits == [{"state": "visible", "timeout": 15000}]
    assert fresh.closed and browser.pages == [old]


def test_status_check_restores_other_platform_current_login_tab(browser):
    service._ensure_initial_config()
    home = Page(service.ACCOUNT_PAGES["douyin"])
    scanning = Page("https://passport.kuaishou.com/pc/account/login/")
    scanning.visible = scanning.focused = True
    browser.pages.extend([home, scanning])
    browser.fresh_identity = "抖音号：sample_account"
    result = service.session("douyin")
    assert result["state"] == "logged_in"
    assert scanning.front and not home.front
    assert scanning.navigated == [] and browser.pages == [home, scanning]


def test_account_check_url_uses_and_closes_separate_tab(browser, monkeypatch):
    editor = Page(service.PAGES["douyin"]["publish"])
    browser.pages.append(editor)
    created = []

    def new_page():
        page = Page(identity="irrelevant")
        page.context = browser
        browser.pages.append(page)
        created.append(page)
        return page

    monkeypatch.setattr(browser, "new_page", new_page)
    monkeypatch.setattr(
        service.adapter,
        "_locator",
        lambda page, _spec: Locator("Account A", True, 1) if page is not editor else Locator(),
    )
    recipe = {
        "verified": False,
        "account_check_url": "https://creator.douyin.com/creator-micro/home",
        "account_locator": {"css": ".account"},
        "expected_account": "Account A",
    }
    result = service._inspect_identity(
        browser, editor, "douyin", "existing", {"label": "Main"}, recipe
    )
    assert result["state"] == "logged_in" and not result["publish_ready"]
    assert created[0].closed and editor.navigated == [] and browser.pages == [editor]


def test_session_list_shares_process_verification_and_connection(browser, monkeypatch):
    service._ensure_initial_config()
    browser.pages.extend(
        [
            Page("https://www.goofish.com/publish", login="登录"),
            Page("https://creator.douyin.com/login"),
            Page("https://creator.xiaohongshu.com/login"),
            Page("https://passport.kuaishou.com/pc/account/login/"),
        ]
    )
    browser.fresh_login_url = lambda url: "https://www.goofish.com/login" if "goofish.com" in url else "https://creator.douyin.com/login"
    verified, connected_calls = [], []
    monkeypatch.setattr(service, "_verify_dedicated_process", lambda *args: verified.append(args))

    @contextmanager
    def connected(url):
        connected_calls.append(url)
        yield browser

    monkeypatch.setattr(service, "_connected", connected)
    result = service.sessions()
    assert len(verified) == len(connected_calls) == 1
    assert {item["state"] for item in result["items"]} == {"login_required"}


def test_kuaishou_login_clicks_only_observed_public_login_entry(browser, monkeypatch):
    page = Page("https://cp.kuaishou.com/profile", login="立即登录")
    browser.pages.append(page)
    clicked = []
    login = Locator("立即登录", True, 1)

    def click(**_kwargs):
        clicked.append(True)
        page.url = "https://passport.kuaishou.com/pc/account/login/"

    login.click = click
    monkeypatch.setattr(
        page, "get_by_text", lambda text, **_: login if text == "立即登录" else Locator()
    )
    monkeypatch.setattr(page, "wait_for_url", lambda *_args, **_kwargs: None, raising=False)
    assert service.session("kuaishou")["state"] == "login_required" and clicked == []
    assert service.session("kuaishou", open_page="login")["state"] == "login_required"
    assert clicked == [True]


def test_dedicated_profile_conflict_stops_before_connect(browser, monkeypatch):
    monkeypatch.setattr(
        service,
        "_verify_dedicated_process",
        lambda *_: (_ for _ in ()).throw(
            service._error("browser_profile_conflict", "profile conflict")
        ),
    )
    monkeypatch.setattr(
        service, "_connected", lambda *_: pytest.fail("must not read unrelated browser")
    )
    result = service.session("douyin", open_page="login")
    assert result["state"] == "unavailable" and not result["connected"]


@pytest.mark.parametrize(
    "address,profile_value", [("0.0.0.0", "expected"), ("127.0.0.1", "normal-profile")]
)
def test_process_validation_rejects_public_bind_and_normal_profile(
    tmp_path, monkeypatch, address, profile_value
):
    expected = (tmp_path / "expected").resolve()
    monkeypatch.setattr(
        service, "_listener_processes", lambda _: [{"address": address, "command": "test"}]
    )
    monkeypatch.setattr(
        service,
        "_windows_arguments",
        lambda _: [
            "edge",
            "--remote-debugging-port=9222",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={tmp_path / profile_value}",
        ],
    )
    with pytest.raises(service.adapter.BrowserPublishError, match="占用"):
        service._verify_dedicated_process(service.DEFAULT_CDP, expected)


def test_read_does_not_start_browser(browser, monkeypatch):
    monkeypatch.setattr(service, "_port_open", lambda _: False)
    monkeypatch.setattr(service, "_start_browser", lambda *_: pytest.fail("GET must not launch"))
    assert service.session("douyin")["state"] == "not_opened"
    assert not service.adapter._config_path().exists()


def test_listing_status_does_not_probe_promotion_pages(browser, monkeypatch):
    probed = []
    original = service._connection
    monkeypatch.setattr(service, "_connection", lambda platform: (probed.append(platform), original(platform))[1])
    result = service.sessions(("xianyu",))
    assert probed == ["xianyu"]
    assert [item["platform"] for item in result["items"]] == ["xianyu"]


@pytest.fixture
def api(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.require_publisher] = lambda: SimpleNamespace(user_id="local")
    monkeypatch.setattr(service, "session", lambda platform, **_: service._base(platform))
    monkeypatch.setattr(service, "sessions", lambda: {"items": []})
    return app


def test_routes_require_local_origin_write_header_and_fixed_destinations(api):
    with TestClient(api, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
        base = "/api/v1/automation/browser"
        assert client.get(base + "/sessions").status_code == 200
        assert (
            client.post(base + "/platforms/douyin/open", json={"page": "login"}).status_code == 403
        )
        headers = {"X-Content-Config-Request": "1"}
        assert (
            client.post(
                base + "/platforms/douyin/open", json={"page": "login"}, headers=headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                base + "/platforms/xianyu/open", json={"page": "login"}, headers=headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                base + "/platforms/douyin/open",
                json={"page": "login", "url": "https://evil.test"},
                headers=headers,
            ).status_code
            == 422
        )
        denied = client.post(
            base + "/platforms/douyin/inspect", headers={**headers, "Origin": "https://evil.test"}
        )
        assert denied.status_code == 403 and denied.headers["cache-control"] == "private, no-store"
    with TestClient(api, base_url="http://127.0.0.1", client=("192.0.2.4", 1234)) as client:
        assert client.get(base + "/sessions").status_code == 403


@pytest.mark.parametrize(
    "purpose,expected",
    [("listing", ("xianyu",)), ("promotion", ("douyin", "xiaohongshu", "kuaishou"))],
)
def test_session_route_limits_browser_checks_to_current_business(api, monkeypatch, purpose, expected):
    calls = []

    def selected_sessions(platforms):
        calls.append(platforms)
        return {"items": [service._base(platform) for platform in platforms]}

    monkeypatch.setattr(service, "sessions", selected_sessions)
    with TestClient(api, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
        response = client.get("/api/v1/automation/browser/sessions", params={"purpose": purpose})
        assert response.status_code == 200
        assert calls == [expected]
        assert tuple(item["platform"] for item in response.json()["items"]) == expected
        assert client.get("/api/v1/automation/browser/sessions?purpose=unknown").status_code == 422


def _fake_lookup(monkeypatch, tcp_rows, process_commands, port_open=True):
    """Stub both PowerShell lookups: the TCP table and the process command lines."""
    def run(command, **_):
        script = command[-1]
        rows = tcp_rows if "Get-NetTCPConnection" in script else [{"command": value} for value in process_commands]
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows))

    monkeypatch.setattr(service.subprocess, "run", run)
    monkeypatch.setattr(service, "_port_open", lambda _: port_open)
    monkeypatch.setattr(service, "_windows_arguments", lambda command: command.split(" "))


def _edge(profile, address="127.0.0.1"):
    return (f"msedge --remote-debugging-port=9222 --remote-debugging-address={address} "
            f"--user-data-dir={profile}")


def test_hidden_listener_falls_back_to_the_dedicated_browser_process(tmp_path, monkeypatch):
    expected = (tmp_path / "expected").resolve()
    _fake_lookup(monkeypatch, [], [_edge(expected)])
    service._verify_dedicated_process(service.DEFAULT_CDP, expected)


@pytest.mark.parametrize("commands", [
    lambda tmp: [_edge(tmp / "normal-profile")],
    lambda tmp: [_edge(tmp / "expected", address="0.0.0.0")],
    lambda tmp: [_edge(tmp / "expected"), _edge(tmp / "normal-profile")],
])
def test_fallback_still_refuses_other_browsers_on_the_port(tmp_path, monkeypatch, commands):
    expected = (tmp_path / "expected").resolve()
    _fake_lookup(monkeypatch, [], commands(tmp_path))
    with pytest.raises(service.adapter.BrowserPublishError, match="占用"):
        service._verify_dedicated_process(service.DEFAULT_CDP, expected)


def test_closed_port_is_still_reported_as_not_open(tmp_path, monkeypatch):
    expected = (tmp_path / "expected").resolve()
    _fake_lookup(monkeypatch, [], [_edge(expected)], port_open=False)
    with pytest.raises(service.adapter.BrowserPublishError, match="尚未打开"):
        service._verify_dedicated_process(service.DEFAULT_CDP, expected)
