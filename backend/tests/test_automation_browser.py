import json
from pathlib import Path

import pytest

from content_service import automation_browser as browser


@pytest.fixture
def recipe():
    # These selectors belong ONLY to the synthetic fixture below, never to Douyin.
    return {
        "enabled": True,
        "verified": True,
        "publish_url": "https://creator.douyin.com/test-only-local-fixture",
        "account_locator": {"css": "#account"},
        "expected_account": "Fixture account",
        "blockers": [{"css": "#challenge"}],
        "steps": [
            {"action": "fill", "locator": {"css": "#title"}, "field": "title"},
            {"action": "fill", "locator": {"css": "#description"}, "field": "description"},
            {"action": "upload", "locator": {"css": "#video"}, "field": "video_path"},
        ],
        "uploads_complete": [{"locator": {"css": "#uploaded"}, "text": "Upload complete"}],
        "submit": {"role": "button", "name": "Publish"},
        "success": {"locator": {"css": "#success"}, "text": "Published"},
        "receipt": {"locator": {"css": "#receipt"}, "attribute": "href"},
    }


@pytest.fixture
def configured(tmp_path, monkeypatch, recipe):
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONTENT_PUBLISH_BROWSER_CONFIG", raising=False)
    directory = tmp_path / "private"
    directory.mkdir()
    config = {
        "version": 1,
        "accounts": {
            "fixture": {
                "label": "本机测试账号",
                "cdp_url": "http://127.0.0.1:9222",
                "platforms": {"douyin": recipe},
            }
        },
    }
    path = directory / "publishing-browser.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return config


@pytest.fixture
def payload(tmp_path):
    directory = tmp_path / "automation-media"
    directory.mkdir(exist_ok=True)
    video = directory / "sample.mp4"
    video.write_bytes(b"synthetic-local-upload-fixture")
    return {
        "title": "Fixture title",
        "description": "Fixture description",
        "video_path": str(video),
    }


def test_status_missing_config_never_claims_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONTENT_PUBLISH_BROWSER_CONFIG", raising=False)
    result = browser.status()
    assert result["configured"] is False
    assert result["platforms"]["douyin"]["configured"] is False
    assert result["platforms"]["douyin"]["missing"]


def test_status_only_reports_config_not_login_or_account_secret(configured, monkeypatch):
    monkeypatch.setattr(browser, "_playwright_available", lambda: True)
    result = browser.status()
    assert result["platforms"]["douyin"]["configured"] is True
    assert not result["platforms"]["xianyu"]["configured"]
    assert set(result["platforms"]) == {"xianyu", "douyin", "xiaohongshu", "kuaishou"}
    assert "Fixture account" not in json.dumps(result)
    assert "cdp_url" not in json.dumps(result)
    assert "logged_in" not in result


@pytest.mark.parametrize(
    "platform,creator,receipt_url",
    [
        (
            "douyin",
            "https://creator.douyin.com/test-only",
            "https://www.douyin.com/video/1234567890",
        ),
        (
            "xiaohongshu",
            "https://creator.xiaohongshu.com/test-only",
            "https://www.xiaohongshu.com/explore/1234567890abcdef12345678",
        ),
        (
            "kuaishou",
            "https://cp.kuaishou.com/test-only",
            "https://www.kuaishou.com/short-video/fixture123",
        ),
    ],
)
def test_promotion_platform_support_requires_verified_recipe_and_exact_media(
    configured, recipe, payload, monkeypatch, tmp_path, platform, creator, receipt_url
):
    recipe["publish_url"] = creator
    configured["accounts"]["fixture"]["platforms"] = {platform: recipe}
    path = tmp_path / "private" / "publishing-browser.json"
    path.write_text(json.dumps(configured), encoding="utf-8")
    assert browser._load_config()["accounts"]["fixture"]["platforms"][platform] == recipe
    assert browser._recipe_issues(platform, recipe) == []
    assert (
        browser._validated_payload(platform, payload, recipe)["video_path"] == payload["video_path"]
    )
    assert browser._validate_publication_receipt(platform, receipt_url) == receipt_url
    recipe.update(enabled=False, verified=False)
    path.write_text(json.dumps(configured), encoding="utf-8")
    monkeypatch.setattr(browser, "_playwright_available", lambda: True)
    assert browser.status()["platforms"][platform]["configured"] is False
    with pytest.raises(browser.BrowserPublishError):
        browser._select_account(browser._load_config(), platform, {"account_id": "fixture"})


@pytest.mark.parametrize(
    "platform,url",
    [
        ("xiaohongshu", "https://creator.xiaohongshu.com/publish/publish"),
        ("xiaohongshu", "https://www.xiaohongshu.com/user/profile/1234567890abcdef12345678"),
        ("xiaohongshu", "https://www.xiaohongshu.com.evil.test/explore/1234567890abcdef12345678"),
        ("kuaishou", "https://cp.kuaishou.com/article/manage/video"),
        ("kuaishou", "https://passport.kuaishou.com/pc/account/login"),
        ("kuaishou", "https://www.kuaishou.com/profile/fixture123"),
        ("kuaishou", "https://www.kuaishou.com.evil.test/short-video/fixture123"),
    ],
)
def test_dashboard_login_profile_and_lookalike_urls_are_not_publishing_receipts(platform, url):
    with pytest.raises(browser.BrowserPublishError):
        browser._validate_publication_receipt(platform, url)


def test_kuaishou_passport_redirect_is_reported_as_login_required(recipe):
    from types import SimpleNamespace

    with pytest.raises(browser.BrowserPublishError) as exc:
        browser._guard_page(
            SimpleNamespace(url="https://passport.kuaishou.com/pc/account/login"),
            "kuaishou",
            recipe,
        )
    assert exc.value.code == "login_required"


def test_xianyu_browser_publisher_requires_a_verified_recipe(configured):
    with pytest.raises(browser.BrowserPublishError) as exc:
        browser.publish("xianyu", {}, before_submit=lambda: pytest.fail("no submission"))
    assert exc.value.code == "account_required"


@pytest.mark.integration
@pytest.mark.parametrize(
    "platform,creator,receipt_url",
    [
        (
            "xiaohongshu",
            "https://creator.xiaohongshu.com/test-only",
            "https://www.xiaohongshu.com/explore/1234567890abcdef12345678",
        ),
        (
            "kuaishou",
            "https://cp.kuaishou.com/test-only",
            "https://www.kuaishou.com/short-video/fixture123",
        ),
    ],
)
@pytest.mark.parametrize("valid_receipt", [True, False])
def test_offline_promotion_upload_and_single_submit_requires_work_receipt(
    chromium, recipe, payload, monkeypatch, platform, creator, receipt_url, valid_receipt
):
    recipe["publish_url"] = creator
    html = HTML.replace(
        "https://www.douyin.com/video/1234567890", receipt_url if valid_receipt else creator
    )
    context = chromium.new_context(service_workers="block")
    try:
        page = context.new_page()
        page.route(
            "**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html)
        )
        page.goto(creator)
        monkeypatch.setattr(browser, "TIMEOUT_MS", 1500)
        monkeypatch.setattr(browser, "UPLOAD_TIMEOUT_MS", 500)
        marks = []

        def before_submit():
            assert page.locator("#uploaded").is_visible()
            assert page.locator("#title").input_value() == payload["title"]
            assert page.locator("#description").input_value() == payload["description"]
            assert page.locator("#attempts").inner_text() == "0"
            marks.append("persisted")

        if valid_receipt:
            result = browser._publish_on_page(page, platform, payload, recipe, before_submit)
            assert result == {"status": "published", "url": receipt_url}
        else:
            with pytest.raises(browser.BrowserPublishError) as exc:
                browser._publish_on_page(page, platform, payload, recipe, before_submit)
            assert exc.value.status == "unknown"
        assert marks == ["persisted"]
        assert page.locator("#attempts").inner_text() == "1"
    finally:
        context.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:9222",
        "http://127.0.0.1.example.com:9222",
        "http://127.0.0.1:9222/json/version",
        "https://127.0.0.1:9222",
        "http://name:password@127.0.0.1:9222",
        "http://127.0.0.1:9222?url=elsewhere",
        "http://169.254.169.254:80",
        "ws://127.0.0.1:9222/devtools/page/test",
    ],
)
def test_cdp_rejects_nonlocal_or_unsafe_addresses(url):
    with pytest.raises(browser.BrowserPublishError, match="loopback"):
        browser._validate_cdp_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://creator.douyin.com/upload",
        "https://creator.douyin.com.example.com/upload",
        "https://user:pass@creator.douyin.com/upload",
        "https://creator.douyin.com:444/upload",
        "https://www.goofish.com/upload",
        "javascript:alert(1)",
    ],
)
def test_platform_url_requires_exact_platform_host(url):
    with pytest.raises(browser.BrowserPublishError):
        browser._validate_platform_url("douyin", url)


def test_empty_and_unverified_recipe_are_blocked(recipe):
    assert browser._recipe_issues("douyin", {})
    recipe["verified"] = False
    assert browser._recipe_issues("douyin", recipe)


@pytest.mark.parametrize(
    "field,value",
    [
        ("steps", [{"action": [], "locator": {"css": "button"}}]),
        ("required_fields", [{}]),
        ("success", []),
        ("uploads_complete", [None]),
    ],
)
def test_malformed_recipe_is_reported_instead_of_crashing(recipe, field, value):
    recipe[field] = value
    assert browser._recipe_issues("douyin", recipe)


def test_recipe_requires_all_fields_upload_check_and_success(recipe):
    recipe["steps"].pop(0)
    recipe["uploads_complete"] = []
    recipe["success"] = None
    issues = browser._recipe_issues("douyin", recipe)
    assert any("title" in value for value in issues)
    assert any("上传完成" in value for value in issues)
    assert any("成功" in value for value in issues)


def test_separate_account_page_must_use_platform_allowlist(recipe):
    recipe["account_check_url"] = "https://example.com/account"
    assert browser._recipe_issues("douyin", recipe)
    recipe["account_check_url"] = "https://creator.douyin.com/test-only-account"
    assert browser._recipe_issues("douyin", recipe) == []


def test_recipe_rejects_early_final_submit_and_arbitrary_code(recipe):
    recipe["steps"].append({"action": "click", "locator": recipe["submit"]})
    assert any("最终发布" in value for value in browser._recipe_issues("douyin", recipe))
    recipe["steps"][-1] = {
        "action": "evaluate",
        "locator": {"css": "body"},
        "value": "fetch('elsewhere')",
    }
    assert browser._recipe_issues("douyin", recipe)


def test_upload_checks_require_positive_completion_evidence(recipe):
    recipe["uploads_complete"] = [{"locator": {"css": "#spinner"}, "state": "hidden"}]
    assert any("可见" in issue for issue in browser._recipe_issues("douyin", recipe))


def test_connection_cannot_override_cdp_or_selectors(configured):
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._select_account(configured, "douyin", {"cdp_url": "http://127.0.0.1:9222"})
    assert error.value.code == "connection_invalid"


def test_missing_submission_marker_is_blocked():
    with pytest.raises(browser.BrowserPublishError) as error:
        browser.publish("douyin", {})
    assert error.value.code == "submission_marker_required"


def test_media_must_be_inside_data_directory(configured, recipe, payload, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.mp4")
    outside.write_bytes(b"test")
    try:
        payload["video_path"] = str(outside)
        with pytest.raises(browser.BrowserPublishError) as error:
            browser._validated_payload("douyin", payload, recipe)
        assert error.value.code == "media_invalid"
    finally:
        outside.unlink()


def test_private_file_is_never_uploaded_even_with_video_extension(
    configured, recipe, payload, tmp_path
):
    private_file = tmp_path / "private" / "credential.mp4"
    private_file.write_bytes(b"private-test-data")
    payload["video_path"] = str(private_file)
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._validated_payload("douyin", payload, recipe)
    assert error.value.code == "media_invalid"


def test_config_cannot_be_loaded_outside_private_directory(configured, tmp_path, monkeypatch):
    path = tmp_path / "external-config.json"
    path.write_text(json.dumps(configured), encoding="utf-8")
    monkeypatch.setenv("CONTENT_PUBLISH_BROWSER_CONFIG", str(path))
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._load_config()
    assert error.value.code == "config_path_invalid"


def test_cdp_discovery_cannot_redirect_to_external_websocket(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return json.dumps(
                {"webSocketDebuggerUrl": "ws://example.com:9222/devtools/browser/a"}
            ).encode()

    class Opener:
        def open(self, request, timeout):
            return Response()

    monkeypatch.setattr(browser, "build_opener", lambda *args: Opener())
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._cdp_websocket("http://127.0.0.1:9222")
    assert error.value.code == "cdp_invalid"


def test_missing_payload_field_is_blocked(configured, recipe, payload):
    payload["description"] = "  "
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._validated_payload("douyin", payload, recipe)
    assert error.value.code == "required_field_missing"


def test_browser_port_is_locked_across_attempts(configured):
    with browser._browser_lock("http://127.0.0.1:9222"):
        with (
            pytest.raises(browser.BrowserPublishError) as error,
            browser._browser_lock("http://localhost:9222"),
        ):
            pass
        assert error.value.code == "browser_busy"
    with browser._browser_lock("http://127.0.0.1:9222"):
        pass


def test_publish_waits_for_a_login_check_to_release_the_browser(configured):
    import threading
    import time

    held, release = threading.Event(), threading.Event()

    def login_check():
        with browser._browser_lock("http://127.0.0.1:9222"):
            held.set()
            release.wait(5)

    checker = threading.Thread(target=login_check)
    checker.start()
    held.wait(5)
    threading.Timer(0.6, release.set).start()
    started = time.monotonic()
    with browser._browser_lock("http://127.0.0.1:9222", wait=5):
        assert time.monotonic() - started >= 0.5
    checker.join(5)


HTML = """<!doctype html><html><body>
<span id="account">Fixture account</span>
<div id="challenge" hidden>Login required</div>
<input id="title"><textarea id="description"></textarea>
<input id="video" type="file" onchange="document.querySelector('#uploaded').hidden=false">
<div id="uploaded" hidden>Upload complete</div>
<button onclick="document.querySelector('#attempts').textContent=String(Number(document.querySelector('#attempts').textContent)+1);document.querySelector('#success').hidden=false;document.querySelector('#receipt').hidden=false"><span>Publish</span></button>
<div id="attempts">0</div><div id="success" hidden>Published</div>
<a id="receipt" hidden href="https://www.douyin.com/video/1234567890">View result</a>
</body></html>"""


@pytest.fixture(scope="module")
def chromium():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        candidates = [
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        ]
        executable = next((str(path) for path in candidates if path.is_file()), None)
        try:
            instance = runtime.chromium.launch(
                headless=True, **({"executable_path": executable} if executable else {})
            )
        except playwright.Error:
            pytest.skip(
                "A local Chromium installation is required for the offline browser fixture."
            )
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def page(chromium, monkeypatch, recipe):
    context = chromium.new_context(service_workers="block")
    page = context.new_page()
    # All requests are fulfilled in memory; no platform or external network is used.
    page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=HTML))
    page.goto(recipe["publish_url"])
    monkeypatch.setattr(browser, "TIMEOUT_MS", 1500)
    monkeypatch.setattr(browser, "UPLOAD_TIMEOUT_MS", 500)
    yield page
    context.close()


@pytest.mark.integration
def test_offline_form_uploads_fills_commits_once_and_returns_evidence(page, recipe, payload):
    marks = []

    def before_submit():
        assert page.locator("#attempts").inner_text() == "0"
        assert page.locator("#title").input_value() == payload["title"]
        assert page.locator("#description").input_value() == payload["description"]
        assert page.locator("#uploaded").is_visible()
        marks.append("persisted")

    receipt = browser._publish_on_page(page, "douyin", payload, recipe, before_submit)
    assert marks == ["persisted"]
    assert page.locator("#attempts").inner_text() == "1"
    assert receipt == {"status": "published", "url": "https://www.douyin.com/video/1234567890"}


@pytest.mark.integration
def test_offline_form_callback_failure_never_clicks(page, recipe, payload):
    def cancelled():
        raise RuntimeError("workflow cancelled")

    with pytest.raises(browser.BrowserPublishError) as error:
        browser._publish_on_page(page, "douyin", payload, recipe, cancelled)
    assert error.value.status == "blocked"
    assert page.locator("#attempts").inner_text() == "0"


@pytest.mark.integration
@pytest.mark.parametrize(
    "submit,preparation",
    [
        ({"role": "button", "name": "Publish"}, {"css": "button"}),
        ({"css": "button"}, {"role": "button", "name": "Publish"}),
        ({"role": "button", "name": "Publish"}, {"css": "button > span"}),
        ({"role": "button", "name": "Publish"}, {"css": "body"}),
    ],
)
def test_offline_form_equivalent_submit_selector_cannot_click_during_preparation(
    page, recipe, payload, submit, preparation
):
    recipe["submit"] = submit
    recipe["steps"].append({"action": "click", "locator": preparation})
    assert browser._recipe_issues("douyin", recipe) == []
    marks = []
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._publish_on_page(page, "douyin", payload, recipe, lambda: marks.append(True))
    assert error.value.code == "early_submission_blocked"
    assert error.value.status == "blocked"
    assert marks == []
    assert page.locator("#attempts").inner_text() == "0"


@pytest.mark.integration
def test_offline_form_account_mismatch_never_marks_or_clicks(page, recipe, payload):
    recipe["expected_account"] = "Another account"
    marks = []
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._publish_on_page(page, "douyin", payload, recipe, lambda: marks.append(True))
    assert error.value.code == "account_mismatch"
    assert error.value.status == "blocked"
    assert marks == []
    assert page.locator("#attempts").inner_text() == "0"


@pytest.mark.integration
def test_offline_form_absent_receipt_after_click_is_unknown_not_retryable(page, recipe, payload):
    recipe["success"] = {"locator": {"css": "#never-exists"}, "text": "Published"}
    marks = []
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._publish_on_page(page, "douyin", payload, recipe, lambda: marks.append(True))
    assert error.value.status == "unknown"
    assert marks == [True]
    assert page.locator("#attempts").inner_text() == "1"


@pytest.mark.integration
def test_offline_form_stale_success_does_not_count_as_new_publication(page, recipe, payload):
    recipe["success"] = {"locator": {"css": "#account"}, "text": "Fixture account"}
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._publish_on_page(page, "douyin", payload, recipe, lambda: None)
    assert error.value.code == "stale_success"
    assert page.locator("#attempts").inner_text() == "0"


@pytest.fixture
def cross_route_pages(chromium, monkeypatch, recipe):
    context = chromium.new_context(service_workers="block")
    recipe["account_check_url"] = "https://creator.douyin.com/test-only-account"
    recipe["publish_url"] = "https://creator.douyin.com/test-only-upload"
    recipe["steps"] = [recipe["steps"][2], *recipe["steps"][:2]]
    state = {"account_checks": 0, "second_account": "Fixture account"}
    editor = HTML.replace('<span id="account">Fixture account</span>', "").replace(
        '<div id="uploaded" hidden>', '<div id="uploaded">'
    )
    upload = """<!doctype html><html><body>
<div id="challenge" hidden>Login required</div>
<input id="video" type="file" onchange="setTimeout(function(){location.href='/test-only-editor'},150)">
</body></html>"""

    def serve(route):
        if route.request.url.endswith("/test-only-account"):
            state["account_checks"] += 1
            account = "Fixture account" if state["account_checks"] == 1 else state["second_account"]
            body = f'<span id="account">{account}</span><div id="challenge" hidden>Login required</div>'
        elif route.request.url.endswith("/test-only-upload"):
            body = upload
        else:
            body = editor
        route.fulfill(status=200, content_type="text/html", body=body)

    # Both tabs are the same browser context; every request is served offline.
    context.route("**/*", serve)
    page = context.new_page()
    account_page = context.new_page()
    page.goto(recipe["publish_url"])
    monkeypatch.setattr(browser, "TIMEOUT_MS", 1500)
    monkeypatch.setattr(browser, "UPLOAD_TIMEOUT_MS", 1000)
    yield page, account_page, state
    context.close()


@pytest.mark.integration
def test_offline_separate_identity_page_and_upload_route_transition(
    cross_route_pages, recipe, payload
):
    page, account_page, state = cross_route_pages
    assert browser._recipe_issues("douyin", recipe) == []
    marks = []

    def before_submit():
        assert state["account_checks"] == 2
        assert page.url.endswith("/test-only-editor")
        assert page.locator("#title").input_value() == payload["title"]
        assert page.locator("#description").input_value() == payload["description"]
        marks.append(True)

    try:
        receipt = browser._publish_on_page(
            page, "douyin", payload, recipe, before_submit, account_page=account_page
        )
    except browser.BrowserPublishError as error:
        # Keep actual offline Playwright diagnostics visible when this fixture fails.
        if error.__context__ is not None:
            raise error.__context__ from error
        raise
    assert receipt["status"] == "published"
    assert page.locator("#attempts").inner_text() == "1"
    assert marks == [True]


@pytest.mark.integration
def test_offline_account_changed_after_upload_blocks_final_submit(
    cross_route_pages, recipe, payload
):
    page, account_page, state = cross_route_pages
    state["second_account"] = "Different account"
    marks = []
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._publish_on_page(
            page, "douyin", payload, recipe, lambda: marks.append(True), account_page=account_page
        )
    assert error.value.code == "account_mismatch"
    assert error.value.status == "blocked"
    assert state["account_checks"] == 2
    assert page.locator("#attempts").inner_text() == "0"
    assert marks == []


@pytest.mark.integration
def test_offline_separate_account_configuration_cannot_skip_identity_page(page, recipe, payload):
    recipe["account_check_url"] = "https://creator.douyin.com/test-only-account"
    marks = []
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._publish_on_page(page, "douyin", payload, recipe, lambda: marks.append(True))
    assert error.value.code == "account_check_missing"
    assert page.locator("#attempts").inner_text() == "0"
    assert marks == []


@pytest.fixture
def management_recipe():
    return {
        "enabled": True,
        "verified": True,
        "management_url": "https://www.goofish.com/test-only-manage",
        "account_locator": {"css": "#account"},
        "expected_account": "Fixture account",
        "blockers": [{"css": "#challenge"}],
        "item_locator": {"css": "#item"},
        "item_link": {"css": "a"},
        "title_locator": {"css": "h2"},
        "source_state": {
            "status": "listed",
            "check": {"locator": {"css": ".state"}, "text": "Listed"},
        },
        "target_state": {
            "status": "delisted",
            "check": {"locator": {"css": ".state"}, "text": "Delisted"},
        },
        "steps": [],
        "submit": {"role": "button", "name": "Delist item"},
    }


@pytest.fixture
def listing_payload():
    return {
        "target_url": "https://www.goofish.com/item?id=12345",
        "item_id": "12345",
        "title": "Exact fixture item",
    }


LISTING_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<span id="account">Fixture account</span><div id="challenge" hidden>Login required</div>
<article id="item"><a href="/item?id=12345">Item link</a><h2>Exact fixture item</h2>
<div class="state">Listed</div><span class="attempts">0</span>
<button onclick="document.querySelector('.attempts').textContent='1';document.querySelector('.state').textContent='Delisted'">Delist item</button>
<button class="delete" onclick="document.querySelector('.attempts').textContent='deleted'">删除商品</button>
</article></body></html>"""


@pytest.fixture
def listing_page(chromium, management_recipe, monkeypatch):
    context = chromium.new_context(service_workers="block")
    page = context.new_page()
    page.route(
        "**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=LISTING_HTML)
    )
    page.goto(management_recipe["management_url"])
    monkeypatch.setattr(browser, "TIMEOUT_MS", 500)
    monkeypatch.setattr(browser, "UPLOAD_TIMEOUT_MS", 500)
    yield page
    context.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://www.goofish.com/item?id=abc",
        "https://www.goofish.com/item?id=123&id=456",
        "https://www.goofish.com/item?id=123&other=1",
        "https://h5.m.goofish.com/item?id=123",
        "https://www.goofish.com/item?id=123#other",
        "https://www.goofish.com/item/123",
        "http://www.goofish.com/item?id=123",
        "https://www.goofish.com.example.com/item?id=123",
    ],
)
def test_listing_identity_url_is_exact_and_strict(url):
    with pytest.raises(browser.BrowserPublishError):
        browser._listing_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.goofish.com/item?id=12345&categoryId=50023914&spm=a.12-b",
        "https://www.goofish.com/item?id=12345&categoryId=&spm=a21ybx.publish.0.0",
    ],
)
def test_listing_url_normalizes_observed_category_and_tracking_parameters(url):
    assert browser._listing_url(url) == ("https://www.goofish.com/item?id=12345", "12345")


@pytest.mark.parametrize(
    "suffix", ["categoryId=abc", "spm=bad%20value", "categoryId=1&categoryId=2", "spm=a&spm=b"]
)
def test_listing_url_rejects_invalid_category_and_tracking_parameters(suffix):
    with pytest.raises(browser.BrowserPublishError):
        browser._listing_url("https://www.goofish.com/item?id=12345&" + suffix)


def test_management_can_be_configured_without_publish(
    configured, management_recipe, tmp_path, monkeypatch
):
    configured["accounts"]["fixture"]["platforms"] = {
        "xianyu": {"management": {"delist": management_recipe}}
    }
    (tmp_path / "private" / "publishing-browser.json").write_text(
        json.dumps(configured), encoding="utf-8"
    )
    monkeypatch.setattr(browser, "_playwright_available", lambda: True)
    result = browser.status()
    assert result["platforms"]["xianyu"]["operations"]["delist"]["configured"]
    assert not result["platforms"]["xianyu"]["configured"]
    assert result["configured"] is False
    selected = browser._select_account(
        configured, "xianyu", {"account_id": "fixture"}, operation="delist"
    )
    assert selected[2] == management_recipe


def test_management_requires_correct_operation_source_target_evidence(management_recipe):
    assert browser._management_recipe_issues("xianyu", "delist", management_recipe) == []
    assert browser._management_recipe_issues("xianyu", "relist", management_recipe)
    management_recipe["target_state"]["check"] = management_recipe["source_state"]["check"]
    assert browser._management_recipe_issues("xianyu", "delist", management_recipe)


def test_management_cross_page_result_needs_completion_confirmation(management_recipe):
    management_recipe["result_url"] = "https://www.goofish.com/test-only-results"
    assert browser._management_recipe_issues("xianyu", "delist", management_recipe)


@pytest.mark.parametrize("text", ["卖掉了", "已售出", "Sold", "删除"])
def test_management_recipe_cannot_treat_terminal_states_as_delisted(management_recipe, text):
    management_recipe["source_state"]["check"]["text"] = text
    assert browser._management_recipe_issues("xianyu", "delist", management_recipe)


@pytest.mark.integration
def test_offline_delist_verifies_same_item_target_state(
    listing_page, listing_payload, management_recipe
):
    marks = []

    def before_submit():
        assert listing_page.locator(".attempts").inner_text() == "0"
        marks.append(True)

    result = browser._manage_on_page(
        listing_page, "delist", listing_payload, management_recipe, before_submit
    )
    assert result == {
        "status": "delisted",
        "url": listing_payload["target_url"],
        "external_id": "12345",
    }
    assert marks == [True]
    assert listing_page.locator(".attempts").inner_text() == "1"


@pytest.mark.integration
def test_offline_listing_waits_for_async_state_text_update(
    listing_page, listing_payload, management_recipe
):
    listing_page.locator("button:first-of-type").evaluate("""button => button.onclick = () => {
        document.querySelector('.attempts').textContent = '1';
        setTimeout(() => document.querySelector('.state').textContent = 'Delisted', 120);
    }""")
    result = browser._manage_on_page(
        listing_page, "delist", listing_payload, management_recipe, lambda: None
    )
    assert result["status"] == "delisted"


@pytest.mark.integration
@pytest.mark.parametrize("field,value", [("item_id", "888"), ("title", "Another item")])
def test_offline_listing_identity_mismatch_never_submits(
    listing_page, listing_payload, management_recipe, field, value
):
    listing_payload[field] = value
    marks = []
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._manage_on_page(
            listing_page, "delist", listing_payload, management_recipe, lambda: marks.append(True)
        )
    assert error.value.code == "listing_identity_mismatch"
    assert marks == []
    assert listing_page.locator(".attempts").inner_text() == "0"


@pytest.mark.integration
def test_offline_listing_wrong_source_state_never_submits(
    listing_page, listing_payload, management_recipe
):
    management_recipe["source_state"]["check"]["text"] = "Delisted"
    marks = []
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._manage_on_page(
            listing_page, "relist", listing_payload, management_recipe, lambda: marks.append(True)
        )
    assert error.value.code == "listing_source_state_mismatch"
    assert marks == []
    assert listing_page.locator(".attempts").inner_text() == "0"


@pytest.mark.integration
def test_offline_sold_listing_is_never_relisted(listing_page, listing_payload, management_recipe):
    listing_page.locator(".state").evaluate("element => element.textContent = '卖掉了'")
    management_recipe["source_state"]["check"]["text"] = "卖掉了"
    marks = []
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._manage_on_page(
            listing_page, "relist", listing_payload, management_recipe, lambda: marks.append(True)
        )
    assert error.value.code == "listing_terminal_state"
    assert marks == []
    assert listing_page.locator(".attempts").inner_text() == "0"


@pytest.mark.integration
def test_offline_delete_button_never_becomes_delist_action(
    listing_page, listing_payload, management_recipe
):
    management_recipe["submit"] = {"css": ".delete"}
    marks = []
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._manage_on_page(
            listing_page, "delist", listing_payload, management_recipe, lambda: marks.append(True)
        )
    assert error.value.code == "unsafe_management_action"
    assert marks == []
    assert listing_page.locator(".attempts").inner_text() == "0"


@pytest.mark.integration
def test_offline_listing_callback_failure_never_submits(
    listing_page, listing_payload, management_recipe
):
    def cancelled():
        raise RuntimeError("cancelled")

    with pytest.raises(browser.BrowserPublishError) as error:
        browser._manage_on_page(
            listing_page, "delist", listing_payload, management_recipe, cancelled
        )
    assert error.value.status == "blocked"
    assert listing_page.locator(".attempts").inner_text() == "0"


@pytest.mark.integration
@pytest.mark.parametrize(
    "locator,code",
    [
        ({"css": "button:first-of-type"}, "early_submission_blocked"),
        ({"css": ".delete"}, "unsafe_management_step"),
    ],
)
def test_offline_listing_preparation_cannot_commit_or_delete(
    listing_page, listing_payload, management_recipe, locator, code
):
    management_recipe["steps"] = [{"action": "click", "locator": locator}]
    marks = []
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._manage_on_page(
            listing_page, "delist", listing_payload, management_recipe, lambda: marks.append(True)
        )
    assert error.value.code == code
    assert marks == []
    assert listing_page.locator(".attempts").inner_text() == "0"


@pytest.mark.integration
def test_offline_listing_missing_target_evidence_is_unknown(
    listing_page, listing_payload, management_recipe
):
    management_recipe["target_state"]["check"]["text"] = "Expected but absent"
    marks = []
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._manage_on_page(
            listing_page, "delist", listing_payload, management_recipe, lambda: marks.append(True)
        )
    assert error.value.status == "unknown"
    assert marks == [True]
    assert listing_page.locator(".attempts").inner_text() == "1"


@pytest.mark.integration
def test_offline_relist_can_verify_on_different_result_page(
    listing_page, listing_payload, management_recipe
):
    source = LISTING_HTML.replace(">Listed</div>", ">Delisted</div>").replace(
        ">Delist item</button>", ">Relist item</button>"
    )
    source = source.replace(
        "document.querySelector('.state').textContent='Delisted'",
        "document.querySelector('#confirmed').hidden=false",
    )
    source = source.replace("</body>", '<div id="confirmed" hidden>Action complete</div></body>')
    target = LISTING_HTML.replace('id="item"', 'id="result-item"')
    management_recipe.update(
        {
            "submit": {"role": "button", "name": "Relist item"},
            "source_state": {
                "status": "delisted",
                "check": {"locator": {"css": ".state"}, "text": "Delisted"},
            },
            "target_state": {
                "status": "listed",
                "check": {"locator": {"css": ".state"}, "text": "Listed"},
            },
            "transition_check": {"locator": {"css": "#confirmed"}, "text": "Action complete"},
            "result_url": "https://www.goofish.com/test-only-results",
            "target_item_locator": {"css": "#result-item"},
        }
    )
    assert browser._management_recipe_issues("xianyu", "relist", management_recipe) == []
    listing_page.unroute("**/*")
    listing_page.route(
        "**/*",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body=target if route.request.url.endswith("results") else source,
        ),
    )
    listing_page.goto(management_recipe["management_url"])
    marks = []
    result = browser._manage_on_page(
        listing_page, "relist", listing_payload, management_recipe, lambda: marks.append(True)
    )
    assert result["status"] == "listed"
    assert listing_page.url == management_recipe["result_url"]
    assert marks == [True]


@pytest.fixture
def xianyu_publish_recipe(recipe):
    recipe["publish_url"] = "https://www.goofish.com/test-only-publish"
    recipe.pop("receipt")
    recipe["pre_submit_checks"] = [
        {"locator": {"css": "#selected-location"}, "text_field": "location"},
        {"locator": {"css": "#selected-category"}, "text_field": "category"},
        {
            "locator": {"css": 'input[name="shipping"][value="free"]:checked'},
            "when": {"field": "shipping_fee", "op": "eq", "value": 0},
        },
        {
            "locator": {"css": 'input[name="shipping"][value="fixed"]:checked'},
            "when": {"field": "shipping_fee", "op": "gt", "value": 0},
        },
    ]
    recipe["steps"] = [
        {
            "action": "fill",
            "locator": {"css": "#description"},
            "fields": ["title", "description"],
            "separator": "\n",
            "max_length": 1500,
        },
        *[
            {"action": "fill", "locator": {"css": "#" + field}, "field": field}
            for field in ("price", "condition", "location", "category")
        ],
        {
            "action": "click",
            "locator": {"role": "radio", "name": "Free shipping"},
            "when": {"field": "shipping_fee", "op": "eq", "value": 0},
        },
        {
            "action": "click",
            "locator": {"role": "radio", "name": "Fixed shipping"},
            "when": {"field": "shipping_fee", "op": "gt", "value": 0},
        },
        {
            "action": "fill",
            "locator": {"css": "#shipping_fee"},
            "field": "shipping_fee",
            "when": {"field": "shipping_fee", "op": "gt", "value": 0},
        },
        {"action": "upload", "locator": {"css": "#images"}, "field": "image_paths"},
    ]
    return recipe


@pytest.fixture
def xianyu_publish_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path))
    media = tmp_path / "automation-media"
    media.mkdir(exist_ok=True)
    image = media / "sample.png"
    image.write_bytes(b"fixture-image")
    return {
        "title": "Fixture title",
        "description": "Fixture description",
        "price": 20,
        "condition": "New",
        "location": "Fixture city",
        "category": "Fixture category",
        "shipping_fee": 0,
        "image_paths": [str(image)],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"fields": ["title", "video_path"]},
        {"fields": ["title", "title"]},
        {"fields": ["title", "price"]},
        {"fields": ["title", "location"]},
        {"fields": ["title", "category"]},
        {"max_length": 0},
        {"max_length": True},
        {"separator": "<script>"},
        {"field": "description"},
        {"value": "fixed"},
    ],
)
def test_combined_fill_rejects_media_numbers_ambiguous_sources_and_unsafe_limits(
    xianyu_publish_recipe, change
):
    xianyu_publish_recipe["steps"][0].update(change)
    assert browser._recipe_issues("xianyu", xianyu_publish_recipe)


@pytest.mark.parametrize(
    "condition",
    [
        {"field": "shipping_fee", "op": "eval", "value": 0},
        {"field": "shipping_fee", "op": "gt", "value": "0"},
        {"field": "shipping_fee", "op": "gt", "value": True},
        {"field": "shipping_fee", "op": "gt", "value": float("nan")},
        {"field": "video_path", "op": "eq", "value": "/private"},
        {"field": "description", "op": "gt", "value": "text"},
        {"field": "shipping_fee", "op": "eq", "value": 0, "script": "anything"},
    ],
)
def test_step_conditions_reject_arbitrary_expression_or_unsupported_values(
    xianyu_publish_recipe, condition
):
    xianyu_publish_recipe["steps"][5]["when"] = condition
    assert browser._recipe_issues("xianyu", xianyu_publish_recipe)


def test_positive_shipping_condition_does_not_replace_amount_entry(
    xianyu_publish_recipe, xianyu_publish_payload
):
    xianyu_publish_recipe["steps"] = [
        step for step in xianyu_publish_recipe["steps"] if step.get("field") != "shipping_fee"
    ]
    xianyu_publish_payload["shipping_fee"] = 8
    # Static coverage includes the free-shipping branch; runtime coverage must
    # still reject the positive branch when its amount field is absent.
    assert browser._recipe_issues("xianyu", xianyu_publish_recipe) == []
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._validated_payload("xianyu", xianyu_publish_payload, xianyu_publish_recipe)
    assert error.value.code == "conditional_fields_missing"


def test_combined_fill_enforces_limit_without_truncating(
    xianyu_publish_recipe, xianyu_publish_payload
):
    xianyu_publish_payload["title"] = "A"
    xianyu_publish_payload["description"] = "中" * 1498
    result = browser._validated_payload("xianyu", xianyu_publish_payload, xianyu_publish_recipe)
    assert len(browser._step_value(xianyu_publish_recipe["steps"][0], result)) == 1500
    xianyu_publish_payload["description"] += "中"
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._validated_payload("xianyu", xianyu_publish_payload, xianyu_publish_recipe)
    assert error.value.code == "field_length_exceeded"


XIANYU_PUBLISH_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<span id="account">Fixture account</span><div id="challenge" hidden>Login required</div>
<div id="description" contenteditable="true"></div>
<input id="price"><input id="condition">
<input id="location" oninput="document.querySelector('#selected-location').textContent=this.value">
<input id="category" oninput="document.querySelector('#selected-category').textContent=this.value">
<div id="selected-location">Default city</div><div id="selected-category">Default category</div>
<label><input type="radio" name="shipping" value="free">Free shipping</label>
<label><input type="radio" name="shipping" value="fixed">Fixed shipping</label>
<input id="shipping_fee" value="untouched">
<input id="images" type="file" multiple onchange="document.querySelector('#uploaded').hidden=false">
<div id="uploaded" hidden>Upload complete</div>
<button onclick="document.querySelector('#attempts').textContent='1';document.querySelector('#success').hidden=false">Publish</button>
<div id="attempts">0</div><div id="success" hidden>Published</div>
</body></html>"""


@pytest.fixture
def xianyu_publish_page(chromium, xianyu_publish_recipe, monkeypatch):
    context = chromium.new_context(service_workers="block")
    page = context.new_page()
    page.route(
        "**/*",
        lambda route: route.fulfill(status=200, content_type="text/html", body=XIANYU_PUBLISH_HTML),
    )
    page.goto(xianyu_publish_recipe["publish_url"])
    monkeypatch.setattr(browser, "TIMEOUT_MS", 1500)
    monkeypatch.setattr(browser, "UPLOAD_TIMEOUT_MS", 500)
    yield page
    context.close()


@pytest.mark.integration
@pytest.mark.parametrize("shipping_fee,selected", [(0, "free"), (8.5, "fixed"), ("0.00", "free")])
def test_offline_xianyu_combines_copy_and_selects_shipping_branch(
    xianyu_publish_page, xianyu_publish_recipe, xianyu_publish_payload, shipping_fee, selected
):
    page = xianyu_publish_page
    xianyu_publish_payload["shipping_fee"] = shipping_fee
    assert browser._recipe_issues("xianyu", xianyu_publish_recipe) == []
    marks = []

    def before_submit():
        assert page.locator("#description").inner_text() == "Fixture title\nFixture description"
        assert page.locator(f'input[name="shipping"][value="{selected}"]').is_checked()
        assert page.locator("#shipping_fee").input_value() == (
            str(shipping_fee) if selected == "fixed" else "untouched"
        )
        marks.append(True)

    result = browser._publish_on_page(
        page, "xianyu", xianyu_publish_payload, xianyu_publish_recipe, before_submit
    )
    assert result["status"] == "published"
    assert marks == [True]
    assert page.locator("#attempts").inner_text() == "1"


@pytest.mark.integration
def test_offline_overlong_combined_copy_stops_before_any_form_edit(
    xianyu_publish_page, xianyu_publish_recipe, xianyu_publish_payload
):
    xianyu_publish_payload["description"] = "中" * 1500
    marks = []
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._publish_on_page(
            xianyu_publish_page,
            "xianyu",
            xianyu_publish_payload,
            xianyu_publish_recipe,
            lambda: marks.append(True),
        )
    assert error.value.code == "field_length_exceeded"
    assert marks == []
    assert xianyu_publish_page.locator("#attempts").inner_text() == "0"
    assert xianyu_publish_page.locator("#description").inner_text() == ""


@pytest.mark.parametrize("field", ["location", "category"])
def test_xianyu_requires_unconditional_selection_evidence(xianyu_publish_recipe, field):
    xianyu_publish_recipe["pre_submit_checks"] = [
        check
        for check in xianyu_publish_recipe["pre_submit_checks"]
        if check.get("text_field") != field
    ]
    assert any(
        "地点与类目" in issue for issue in browser._recipe_issues("xianyu", xianyu_publish_recipe)
    )


def test_conditional_evidence_cannot_skip_required_location_check(xianyu_publish_recipe):
    xianyu_publish_recipe["pre_submit_checks"][0]["when"] = {
        "field": "shipping_fee",
        "op": "gt",
        "value": 0,
    }
    assert any(
        "地点与类目" in issue for issue in browser._recipe_issues("xianyu", xianyu_publish_recipe)
    )


@pytest.mark.parametrize("field", ["location", "category"])
def test_wait_only_does_not_count_as_selecting_location_or_category(xianyu_publish_recipe, field):
    for step in xianyu_publish_recipe["steps"]:
        if step.get("field") == field:
            step.clear()
            step.update({"action": "wait", "locator": {"role": "option", "name_field": field}})
    assert any(field in issue for issue in browser._recipe_issues("xianyu", xianyu_publish_recipe))


@pytest.mark.integration
@pytest.mark.parametrize("field", ["location", "category"])
def test_offline_wrong_selected_field_blocks_before_submit(
    xianyu_publish_page, xianyu_publish_recipe, xianyu_publish_payload, field
):
    page = xianyu_publish_page
    page.locator("#" + field).evaluate("element => element.oninput = null")
    marks = []
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._publish_on_page(
            page,
            "xianyu",
            xianyu_publish_payload,
            xianyu_publish_recipe,
            lambda: marks.append(True),
        )
    assert error.value.status == "blocked"
    assert marks == []
    assert page.locator("#attempts").inner_text() == "0"


@pytest.mark.integration
def test_offline_free_shipping_radio_must_still_be_selected_at_submit(
    xianyu_publish_page, xianyu_publish_recipe, xianyu_publish_payload
):
    page = xianyu_publish_page
    page.locator("#images").evaluate("""element => element.onchange = () => {
        document.querySelector('#uploaded').hidden = false;
        document.querySelector('input[value="fixed"]').checked = true;
    }""")
    marks = []
    with pytest.raises(browser.BrowserPublishError) as error:
        browser._publish_on_page(
            page,
            "xianyu",
            xianyu_publish_payload,
            xianyu_publish_recipe,
            lambda: marks.append(True),
        )
    assert error.value.status == "blocked"
    assert marks == []
    assert page.locator("#attempts").inner_text() == "0"
