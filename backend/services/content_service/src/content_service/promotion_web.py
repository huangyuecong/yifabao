"""Creator web video forms observed on 2026-10-25; no private publishing API."""

import re
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from . import automation_browser as browser
from .automation_platforms import get_platform

ADAPTER = 'promotion-web-v1'
PLATFORMS = ('douyin', 'xiaohongshu', 'kuaishou')
EDITORS = {
    'douyin': '[contenteditable="true"].editor-kit-container',
    'xiaohongshu': '.tiptap[contenteditable="true"]',
    'kuaishou': '[contenteditable="true"][placeholder="作品描述不会写？试试智能文案"]',
}
TITLES = {'douyin': '填写作品标题，为作品获得更多流量', 'xiaohongshu': '填写标题会有更多赞哦'}
# Large videos upload and transcode for minutes; the publish button only becomes
# usable afterwards. Wait for that instead of giving up while it is still busy.
UPLOAD_TIMEOUT_SECONDS = 20 * 60
POLL_SECONDS = 2
BUSY_TEXT = re.compile(r'^\s*(上传中|正在上传|视频处理中|处理中|转码中)')
RECEIPT_TIMEOUT_SECONDS = 120
# Identity checks a platform shows after 发布 on a new device; the owner must answer them.
VERIFY_TEXT = re.compile(r'(接收短信验证码|短信验证码|安全验证|身份验证|验证身份|请完成验证)')
VERIFY_WAIT_SECONDS = 10 * 60
LABELS = {'douyin': '抖音', 'xiaohongshu': '小红书', 'kuaishou': '快手'}


def is_recipe(recipe):
    return isinstance(recipe, dict) and recipe.get('adapter') == ADAPTER


def recipe_issues(platform, recipe):
    return [] if platform in PLATFORMS and is_recipe(recipe) and recipe.get('enabled') is True else ['宣传网页适配器尚未启用。']


def block(code, message):
    raise browser.BrowserPublishError(code, message)


def validate_payload(platform, payload):
    result = dict(payload)
    spec = get_platform(platform)
    for key, limit in (('title', spec.title_limit), ('description', spec.description_limit)):
        value = result.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            block('invalid_copy', f'发布{key}内容为空或超过平台字数限制。')
        result[key] = value.strip()
    root = (browser._data_dir() / 'automation-media').resolve()
    path = Path(str(result.get('video_path') or '')).resolve()
    if (not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in {'.mp4', '.mov', '.m4v'}
            or not 0 < path.stat().st_size <= 500 * 1024 * 1024):
        block('invalid_video', '请选择当前草稿中的有效视频，最大 500 MB。')
    result['video_path'] = str(path)
    result['web_description'] = (result['title'] + '\n' if platform == 'kuaishou' else '') + result['description']
    if platform == 'kuaishou' and len(result['web_description']) > 1000:
        block('copy_too_long', '快手标题与正文合计不能超过 1000 字，请精简文案。')
    return result


def guard(page, platform):
    from .automation_browser_session import LOGIN_LABELS
    browser._validate_platform_url(platform, page.url)
    if urlsplit(page.url).path.startswith(('/login', '/passport')):
        block('login_required', '平台登录已失效，请重新登录。')
    for label in (*LOGIN_LABELS[platform], '请完成验证', '请完成安全验证', '上传失败', '视频上传失败'):
        if page.get_by_text(label, exact=True).filter(visible=True).count():
            block('platform_blocked', '平台显示登录、验证或上传失败提示，请点击“打开发布页”处理后重试。')


def dismiss_tips(page, platform):
    # Only dismiss observed introductory tips. Never dismiss verification or
    # rights/ownership declarations, nor select a promotional offer.
    if platform == 'kuaishou':
        tip = page.get_by_role('alertdialog').get_by_role('button', name='Skip', exact=True)
        if tip.is_visible():
            tip.click()
    elif platform == 'douyin':
        tips = page.get_by_text('我知道了', exact=True).filter(visible=True)
        for tip in tips.all():
            tip.click(timeout=3000)


def _xhs_submit_point(page):
    """Read the observed closed-shadow publish button, then use a real mouse click.

    The native site intentionally encapsulates its footer. CDP reads that UI;
    it neither disables the site's checks nor invokes a private submit function.
    """
    from playwright.sync_api import expect
    host = page.locator('xhs-publish-btn[is-publish="true"]')
    expect(host).to_have_count(1)
    expect(host).to_have_attribute('submit-disabled', 'false')
    expect(host).to_have_attribute('submit-loading', 'false')
    host.scroll_into_view_if_needed()
    client = page.context.new_cdp_session(page)
    try:
        root = client.send('DOM.getDocument', {'depth': 1})['root']['nodeId']
        node = client.send('DOM.querySelector', {'nodeId': root, 'selector': 'xhs-publish-btn[is-publish="true"]'})['nodeId']
        tree = client.send('DOM.describeNode', {'nodeId': node, 'depth': -1, 'pierce': True})['node']
        matches = []
        def visit(current):
            attrs = dict(zip(current.get('attributes', [])[::2], current.get('attributes', [])[1::2], strict=True))
            text = ''.join(child.get('nodeValue', '') for child in current.get('children', []))
            if (current['nodeName'] == 'BUTTON' and text.strip() == '发布'
                    and 'bg-red' in attrs.get('class', '').split()
                    and attrs.get('aria-disabled') == 'false' and attrs.get('aria-busy') == 'false'):
                matches.append(current)
            for child in current.get('children', []) + current.get('shadowRoots', []):
                visit(child)
        visit(tree)
        if len(matches) != 1:
            block('submit_not_ready', '小红书发布按钮尚未就绪。')
        obj = client.send('DOM.resolveNode', {'backendNodeId': matches[0]['backendNodeId']})['object']['objectId']
        box = client.send('Runtime.callFunctionOn', {'objectId': obj, 'returnByValue': True,
            'functionDeclaration': '''function() {
                const r = this.getBoundingClientRect(); const h = this.getRootNode().host;
                const x = r.x + r.width/2, y = r.y + r.height/2;
                const top = document.elementFromPoint(x, y);
                return {x,y,ok: r.width>0 && r.height>0 && !this.disabled && (top===h || h.contains(top))};
            }'''})['result'].get('value', {})
        if not box.get('ok'):
            block('submit_obscured', '小红书发布按钮被遮挡，请处理当前窗口提示。')
        return box['x'], box['y']
    finally:
        client.detach()


def submit_target(page, platform):
    if platform == 'xiaohongshu':
        return _xhs_submit_point(page)
    if platform == 'douyin':
        return page.get_by_role('button', name='发布', exact=True)
    return page.locator('div[class*="_button-primary_"]').filter(has=page.get_by_text('发布', exact=True))


def _submit_ready(page, platform):
    if platform == 'xiaohongshu':
        host = page.locator('xhs-publish-btn[is-publish="true"]')
        return (host.count() == 1 and host.get_attribute('submit-disabled') == 'false'
                and host.get_attribute('submit-loading') == 'false')
    target = submit_target(page, platform)
    if target.count() != 1 or not target.is_visible():
        return False
    if platform == 'douyin':
        return target.is_enabled()
    return 'disabled' not in (target.get_attribute('class') or '').lower() and target.get_attribute('aria-disabled') != 'true'


def _pending_step(page, platform):
    """What the page is still doing before the publish button can be used; '' when ready."""
    if not page.get_by_text('重新上传', exact=True).filter(visible=True).count():
        return '视频上传'
    if page.locator('[class*="uploading-container"]:visible').count():
        return '视频上传'
    if page.get_by_text(BUSY_TEXT).filter(visible=True).count():
        return '平台处理视频'
    if not _submit_ready(page, platform):
        return '发布按钮启用'
    return ''


def wait_until_ready(page, platform, *, timeout=None, sleep=time.sleep, clock=time.monotonic):
    """Poll until the upload is accepted and the publish button is usable, twice in a row."""
    timeout = UPLOAD_TIMEOUT_SECONDS if timeout is None else timeout
    deadline = clock() + timeout
    ready_polls = 0
    while True:
        guard(page, platform)
        pending = _pending_step(page, platform)
        ready_polls = 0 if pending else ready_polls + 1
        if ready_polls >= 2:
            return
        if clock() >= deadline:
            minutes = max(1, round(timeout / 60))
            block('upload_timeout', f'等待{pending}超过 {minutes} 分钟仍未完成，未点击发布，请重试。')
        sleep(POLL_SECONDS)


def check_form(page, platform, payload):
    from playwright.sync_api import expect
    guard(page, platform)
    wait_until_ready(page, platform)
    expect(page.locator(EDITORS[platform])).to_have_text(payload['web_description'], use_inner_text=True)
    if platform in TITLES:
        expect(page.get_by_placeholder(TITLES[platform], exact=True)).to_have_value(payload['title'])
    # Each observed editor changes from progress/cancel to re-upload only after
    # accepting the video. A local blob preview alone is not upload evidence.
    expect(page.get_by_text('重新上传', exact=True).filter(visible=True).first).to_be_visible(timeout=180_000)
    expect(page.locator('[class*="uploading-container"]:visible')).to_have_count(0, timeout=180_000)
    for label in ('上传失败', '视频上传失败', '重新上传失败'):
        expect(page.get_by_text(label, exact=True).filter(visible=True)).to_have_count(0)
    if platform == 'kuaishou':
        expect(page.get_by_role('radio', name='所有人可见', exact=True)).to_be_checked()
        expect(page.get_by_role('radio', name='立即发布', exact=True)).to_be_checked()
    target = submit_target(page, platform)
    if platform != 'xiaohongshu':
        expect(target).to_have_count(1)
        target.click(trial=True, timeout=10_000)
    return target


def prepare_page(page, platform, payload):
    from playwright.sync_api import expect
    guard(page, platform)
    page.bring_to_front()
    # A new editor must begin at its file picker, never replace a restored draft.
    if page.locator(EDITORS[platform]).count():
        block('editor_not_empty', '该页面已有草稿，请保留原稿并打开新的发布页。')
    picker = page.locator('input[type="file"]')
    expect(picker).to_have_count(1, timeout=30_000)
    picker.set_input_files(payload['video_path'])
    page.locator(EDITORS[platform]).wait_for(timeout=180_000)
    dismiss_tips(page, platform)
    if platform in TITLES:
        page.get_by_placeholder(TITLES[platform], exact=True).fill(payload['title'])
    page.locator(EDITORS[platform]).fill(payload['web_description'])
    return check_form(page, platform, payload)


def _listed_after_publish(page, platform, payload):
    """Kuaishou shows no success message: after 发布 it opens 作品管理 with
    ``from=publish`` and lists the new work (observed 2026-10-31)."""
    if platform != 'kuaishou':
        return False
    parts = urlsplit(page.url)
    return (parts.hostname == 'cp.kuaishou.com' and parts.path == '/article/manage/video'
            and 'from=publish' in parts.query.split('&')
            and page.get_by_text(payload['title'], exact=False).filter(visible=True).count() > 0)


def verification_visible(page):
    return page.get_by_text(VERIFY_TEXT).filter(visible=True).count() > 0


def _verification_hint(platform):
    from . import remote_browser
    name = LABELS[platform]
    minutes = VERIFY_WAIT_SECONDS // 60
    where = (f'在“发布记录”点“去验证”（或在“平台账号”打开{name}窗口）' if remote_browser.enabled()
             else f'在专用发布浏览器的{name}页面')
    return f'{name}要求短信验证本人操作：请{where}获取并输入验证码，{minutes} 分钟内完成后会继续发布。'


def receipt(page, platform, payload, *, timeout=RECEIPT_TIMEOUT_SECONDS,
            verify_timeout=VERIFY_WAIT_SECONDS, clock=time.monotonic):
    """Require explicit success (or Kuaishou's listing of the new work), never infer it.

    Douyin asks an account it sees on a new device (the trial server) for an SMS code
    after 发布 is clicked, and publishes only once it is entered (observed 2026-11-16).
    While that dialog shows, wait for the account owner; the tab must stay open.
    """
    success = page.get_by_text(re.compile(r'^(发布成功|笔记发布成功|作品发布成功|发布成功，审核中)$')).filter(visible=True)
    deadline = clock() + timeout
    verifying = False
    while not success.count():
        if _listed_after_publish(page, platform, payload):
            return {'status': 'published', 'url': '', 'external_id': ''}
        if verification_visible(page):
            if not verifying:
                verifying = True
                deadline = max(deadline, clock() + verify_timeout)
                browser.report_hint(_verification_hint(platform))
        elif verifying:
            # Verified (or the dialog went away): the platform goes on publishing.
            verifying = False
            deadline = max(deadline, clock() + timeout)
            browser.report_hint('')
        if clock() >= deadline:
            if verifying:
                # The dialog still blocks the publish, so nothing was published.
                raise browser.BrowserPublishError(
                    'verification_required',
                    f'{LABELS[platform]}要求短信验证，{verify_timeout // 60} 分钟内未完成验证，作品没有发布；完成验证后可重试。',
                    status='failed')
            raise TimeoutError('no publish receipt')
        page.wait_for_timeout(1000)
    if verifying:
        browser.report_hint('')
    patterns = {
        'douyin': r'https://www\.douyin\.com/video/([0-9]+)',
        'xiaohongshu': r'https://www\.xiaohongshu\.com/(?:explore|discovery/item)/([a-zA-Z0-9]+)',
        'kuaishou': r'https://www\.kuaishou\.com/short-video/([a-zA-Z0-9]+)',
    }
    links = page.locator('a[href]').evaluate_all('(els)=>els.map(e=>({url:e.href,text:e.innerText}))')
    found = [(link, re.fullmatch(patterns[platform], link['url'].split('?')[0])) for link in links
             if payload['title'] in link['text'] or link['text'].strip() in {'查看作品', '查看笔记'}]
    found = [(link, match) for link, match in found if match]
    if len(found) != 1:
        # The platform said 发布成功 explicitly; its result page just has no single work link.
        return {'status': 'published', 'url': '', 'external_id': ''}
    link, match = found[0]
    return {'status': 'published', 'url': link['url'], 'external_id': match.group(1)}


def publish(platform, payload, identifier, account, recipe, before_submit):
    from . import automation_browser_session as sessions
    prepared = validate_payload(platform, payload)
    submitted = False
    with browser._browser_lock(account['cdp_url'], wait=browser.LOCK_WAIT_SECONDS):
        # A closed publishing browser is reopened with its saved logins.
        started = sessions.ensure_browser(account['cdp_url'])
        with sessions._connected(account['cdp_url']) as context:
            page = context.new_page()
            if started:
                sessions.close_start_tab(context, page)
            try:
                def verify_identity():
                    state = sessions._inspect_identity(context, page, platform, identifier, account, recipe)
                    if state['state'] != 'logged_in' or state.get('account_id') != identifier:
                        block('account_unverified', state.get('message') or '请先核对发布账号。')
                # Signed out: the tab shows the login page and the task waits for the scan.
                sessions.ensure_logged_in(context, page, platform, identifier, account, recipe)
                page.goto(sessions.PAGES[platform]['publish'], wait_until='domcontentloaded', timeout=30_000)
                prepare_page(page, platform, prepared)
                verify_identity()
                page.bring_to_front()
                target = check_form(page, platform, prepared)
                before_submit()
                submitted = True
                if platform == 'xiaohongshu':
                    page.mouse.click(*target)
                else:
                    target.click(timeout=15_000)
                return {**receipt(page, platform, prepared), 'account_label': account['label']}
            except Exception as error:
                if submitted and isinstance(error, browser.BrowserPublishError) and error.status == 'failed':
                    raise  # known not published (e.g. verification never completed)
                if submitted:
                    raise browser.BrowserPublishError('publication_unknown', '已尝试提交，尚未取得完整作品回执；请在平台核查，本次不会自动重复发布。', status='unknown') from None
                if isinstance(error, browser.BrowserPublishError):
                    raise
                raise browser.BrowserPublishError('preparation_failed', '视频上传或表单校验尚未完成，未点击发布，请重试。') from None
            finally:
                # Close only the tab this task opened, whatever the outcome.
                with suppress(Exception):
                    page.close()
