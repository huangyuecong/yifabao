"""Offline browser contracts for the observed creator forms and submit boundary."""

from contextlib import contextmanager
from pathlib import Path

import pytest

from content_service import automation_browser as browser
from content_service import automation_browser_session as sessions
from content_service import promotion_web as web


@pytest.fixture(scope='module')
def chromium():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        edge = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
        instance = p.chromium.launch(headless=True, **({'executable_path': str(edge)} if edge.exists() else {}))
        yield instance
        instance.close()


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(web, 'POLL_SECONDS', 0.2)


@pytest.fixture
def payload(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTENT_DATA_DIR', str(tmp_path))
    folder = tmp_path / 'automation-media'
    folder.mkdir()
    video = folder / 'video.mp4'
    video.write_bytes(b'video fixture')
    return {'title': '商品标题', 'description': '真实商品介绍', 'video_path': str(video)}


def html(platform, delay_ms=0):
    editor = {
        'douyin': '<input placeholder="填写作品标题，为作品获得更多流量"><div contenteditable="true" class="editor-kit-container"></div>',
        'xiaohongshu': '<input placeholder="填写标题会有更多赞哦"><div class="tiptap" contenteditable="true"></div>',
        'kuaishou': '<div contenteditable="true" placeholder="作品描述不会写？试试智能文案"></div><label><input type="radio" checked>所有人可见</label><label><input type="radio" checked>立即发布</label>',
    }[platform]
    busy = 'true' if delay_ms else 'false'
    button = {
        'douyin': f'<button onclick="attempts++" {"disabled" if delay_ms else ""}>发布</button>',
        'xiaohongshu': f'<xhs-publish-btn is-publish="true" submit-disabled="{busy}" submit-loading="false"></xhs-publish-btn>',
        'kuaishou': f'<div class="_button-primary_test{" _disabled_x" if delay_ms else ""}" onclick="attempts++"><div>发布</div></div>',
    }[platform]
    progress = '<span id="progress">上传中 37%</span>' if delay_ms else ''
    # Simulate a slow upload: the page already offers 重新上传 while the video is
    # still uploading, and only enables 发布 when it is done.
    finish = f"""setTimeout(()=>{{document.querySelector('#progress')?.remove();
      document.querySelector('button')?.removeAttribute('disabled');
      document.querySelector('xhs-publish-btn')?.setAttribute('submit-disabled','false');
      document.querySelector('._button-primary_test')?.classList.remove('_disabled_x');}}, {delay_ms});""" if delay_ms else ''

    return f'''<!doctype html><meta charset="utf-8"><input type="file"><div id="form"></div>
    <script>window.attempts=0;
    customElements.define('xhs-publish-btn', class extends HTMLElement {{ connectedCallback() {{
      this.attachShadow({{mode:'closed'}}).innerHTML='<button class="ce-btn bg-red" aria-disabled="false" aria-busy="false" onclick="window.attempts++">发布</button>';
    }} }});
    document.querySelector('input').onchange=()=>{{document.querySelector('#form').innerHTML=`{editor}<span>重新上传</span>{progress}{button}`;{finish}}};
    </script>'''


def attempts_when_closed(context):
    """Record each task tab's submit count at the moment the task closes it."""
    attempts = []
    new_page = context.new_page
    def opened():
        page = new_page()
        close = page.close
        def closing(**options):
            attempts.append(page.evaluate('window.attempts'))
            close(**options)
        page.close = closing
        return page
    context.new_page = opened
    return attempts


@pytest.mark.parametrize('platform', web.PLATFORMS)
def test_upload_fills_exact_copy_and_does_not_publish(chromium, payload, platform):
    context = chromium.new_context()
    context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html(platform)))
    page = context.new_page()
    page.goto(sessions.PAGES[platform]['publish'])
    prepared = web.validate_payload(platform, payload)
    web.prepare_page(page, platform, prepared)
    assert page.locator(web.EDITORS[platform]).inner_text() == prepared['web_description']
    assert page.evaluate('window.attempts') == 0
    # Refuse to overwrite this occupied editor on another attempt.
    with pytest.raises(browser.BrowserPublishError, match='已有草稿'):
        web.prepare_page(page, platform, prepared)
    context.close()


@pytest.mark.parametrize('platform', web.PLATFORMS)
@pytest.mark.parametrize('failure', ['identity', 'receipt', 'none'])
def test_only_submit_after_durable_marker_and_no_retry_on_uncertain_result(chromium, payload, monkeypatch, platform, failure):
    context = chromium.new_context()
    context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html(platform)))
    @contextmanager
    def connection(_):
        yield context
    monkeypatch.setattr(sessions, '_connected', connection)
    monkeypatch.setattr(sessions, 'ensure_browser', lambda *_: False)
    monkeypatch.setattr(sessions, '_inspect_identity', lambda *_: {'state': 'unknown' if failure == 'identity' else 'logged_in', 'account_id': 'test'})
    def receipt(*_):
        if failure == 'receipt':
            raise RuntimeError('connection lost')
        return {'status': 'published', 'external_id': 'fixture'}
    monkeypatch.setattr(web, 'receipt', receipt)
    marks = []
    attempts = attempts_when_closed(context)
    account = {'cdp_url': 'http://127.0.0.1:9222', 'label': 'fixture'}
    if failure == 'none':
        assert web.publish(platform, payload, 'test', account, {}, lambda: marks.append(True))['status'] == 'published'
    else:
        with pytest.raises(browser.BrowserPublishError) as error:
            web.publish(platform, payload, 'test', account, {}, lambda: marks.append(True))
        assert error.value.status == ('unknown' if failure == 'receipt' else 'blocked')
    assert marks == ([] if failure == 'identity' else [True])
    # The task's tab is closed whether it succeeded or failed.
    # An unverified account stops before the form is even loaded.
    assert attempts == [None if failure == 'identity' else 1] and context.pages == []
    context.close()


def test_private_path_and_long_merged_copy_are_rejected(payload, tmp_path):
    outside = tmp_path / 'private.mp4'
    outside.write_bytes(b'private')
    with pytest.raises(browser.BrowserPublishError):
        web.validate_payload('douyin', payload | {'video_path': str(outside)})
    with pytest.raises(browser.BrowserPublishError):
        web.validate_payload('kuaishou', payload | {'description': '文' * 1000})


def publish_with(context, monkeypatch, platform, payload, marks):
    @contextmanager
    def connection(_):
        yield context
    monkeypatch.setattr(sessions, '_connected', connection)
    monkeypatch.setattr(sessions, 'ensure_browser', lambda *_: False)
    monkeypatch.setattr(sessions, '_inspect_identity', lambda *_: {'state': 'logged_in', 'account_id': 'test'})
    monkeypatch.setattr(web, 'receipt', lambda *_: {'status': 'published', 'external_id': 'fixture'})
    account = {'cdp_url': 'http://127.0.0.1:9222', 'label': 'fixture'}
    return web.publish(platform, payload, 'test', account, {}, lambda: marks.append(True))


@pytest.mark.parametrize('platform', web.PLATFORMS)
def test_slow_upload_is_awaited_then_published_once(chromium, payload, monkeypatch, platform):
    import time

    context = chromium.new_context()
    context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html(platform, delay_ms=1500)))
    marks = []
    attempts = attempts_when_closed(context)
    started = time.monotonic()
    assert publish_with(context, monkeypatch, platform, payload, marks)['status'] == 'published'
    assert time.monotonic() - started >= 1.4
    assert marks == [True] and attempts == [1] and context.pages == []
    context.close()


@pytest.mark.parametrize('platform', web.PLATFORMS)
def test_upload_that_never_finishes_times_out_without_clicking(chromium, payload, monkeypatch, platform):
    context = chromium.new_context()
    context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html(platform, delay_ms=600_000)))
    monkeypatch.setattr(web, 'UPLOAD_TIMEOUT_SECONDS', 1)
    marks = []
    attempts = attempts_when_closed(context)
    with pytest.raises(browser.BrowserPublishError) as error:
        publish_with(context, monkeypatch, platform, payload, marks)
    assert error.value.code == 'upload_timeout' and error.value.status != 'unknown'
    assert marks == [] and attempts == [0] and context.pages == []
    context.close()


MANAGE_HTML = '''<!doctype html><meta charset="utf-8"><h1>视频管理</h1>
<div class="item"><span>商品标题 真实商品介绍</span><span>已发布 2026-10-31 01:00</span></div>'''


@pytest.mark.parametrize('query,published', [('status=2&from=publish', True), ('status=2', False)])
def test_kuaishou_manage_list_after_publish_counts_as_success(chromium, payload, query, published):
    """The reported case: 快手 published, but showed 待核查 because it never says 发布成功."""
    context = chromium.new_context()
    context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=MANAGE_HTML))
    page = context.new_page()
    page.goto(f'https://cp.kuaishou.com/article/manage/video?{query}')
    prepared = web.validate_payload('kuaishou', payload)
    try:
        if published:
            assert web.receipt(page, 'kuaishou', prepared, timeout=5) == {'status': 'published', 'url': '', 'external_id': ''}
        else:
            # Without from=publish this is just a management page, not proof of a new work.
            with pytest.raises(TimeoutError):
                web.receipt(page, 'kuaishou', prepared, timeout=1)
    finally:
        context.close()


def test_explicit_success_without_a_work_link_counts_as_published(chromium, payload):
    context = chromium.new_context()
    page = context.new_page()
    page.set_content('<!doctype html><meta charset="utf-8"><div>发布成功</div><a href="https://creator.example/manage">作品管理</a>')
    result = web.receipt(page, 'douyin', web.validate_payload('douyin', payload))
    assert result == {'status': 'published', 'url': '', 'external_id': ''}
    context.close()


def test_xiaohongshu_upload_longer_than_the_old_five_second_check(chromium, payload, monkeypatch):
    """The reported failure: 小红书 still uploading after the old 5 s button check, so 发布 was never clicked."""
    context = chromium.new_context()
    context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html('xiaohongshu', delay_ms=12_000)))
    marks = []
    attempts = attempts_when_closed(context)
    assert publish_with(context, monkeypatch, 'xiaohongshu', payload, marks)['status'] == 'published'
    assert marks == [True] and attempts == [1] and context.pages == []
    context.close()


SMS_HTML = '''<!doctype html><meta charset="utf-8"><div id="dialog"><h3>接收短信验证码</h3>
<p>为确保是本人操作抖音账号，请输入当前手机号收到的短信验证码</p></div>
<script>setTimeout(() => {{ document.querySelector('#dialog').remove(); {after} }}, {delay});</script>'''


def test_douyin_waits_while_the_owner_enters_the_sms_code(chromium, payload):
    """Observed on the trial server: 抖音 asks for an SMS code after 发布 and publishes once it is entered."""
    context = chromium.new_context()
    page = context.new_page()
    page.set_content(SMS_HTML.format(delay=2500, after="document.body.insertAdjacentHTML('beforeend', '<div>发布成功</div>')"))
    hints = []
    token = browser.publish_hint.set(hints.append)
    try:
        # The plain receipt wait (1 s) would give up; the dialog extends it for the owner.
        result = web.receipt(page, 'douyin', web.validate_payload('douyin', payload), timeout=1, verify_timeout=30)
    finally:
        browser.publish_hint.reset(token)
        context.close()
    assert result['status'] == 'published'
    assert '抖音要求短信验证' in hints[0] and hints[-1] == ''


def test_an_unanswered_sms_check_is_a_known_failure_not_unknown(chromium, payload):
    context = chromium.new_context()
    page = context.new_page()
    page.set_content(SMS_HTML.format(delay=600_000, after=''))
    try:
        with pytest.raises(browser.BrowserPublishError) as error:
            web.receipt(page, 'douyin', web.validate_payload('douyin', payload), timeout=1, verify_timeout=2)
    finally:
        context.close()
    # The dialog still blocked 发布, so nothing was published and a retry is safe.
    assert error.value.code == 'verification_required' and error.value.status == 'failed'
