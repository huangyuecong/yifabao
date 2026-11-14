import base64
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

from content_service import automation_browser as browser
from content_service import automation_browser_session as sessions
from content_service import xianyu_web as web


@pytest.fixture
def payload(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTENT_DATA_DIR', str(tmp_path))
    folder = tmp_path / 'automation-media' / 'task'
    folder.mkdir(parents=True)
    path = folder / 'product.png'
    Image.new('RGB', (50, 50), '#afbdcf').save(path)
    return {'title': '收纳盒', 'description': '木制收纳盒，有轻微使用痕迹。',
            'condition': '轻微使用痕迹', 'category': '其他闲置', 'location': '测试地点',
            'price': '25', 'shipping_fee': '6', 'image_paths': [str(path)]}


@pytest.mark.parametrize('field,value', [
    ('price', 'NaN'), ('price', '0'), ('shipping_fee', '-1'),
    ('location', '请选择地址'), ('image_paths', []),
])
def test_bad_product_cannot_reach_browser(payload, field, value):
    payload[field] = value
    with pytest.raises(browser.BrowserPublishError):
        web.validate_payload(payload)


@pytest.mark.parametrize('category', ['酒类/白酒/酱香型白酒', '', None])
def test_every_listing_uses_other_idle_category(payload, category):
    assert web.validate_payload(payload | {'category': category})['category'] == '其他闲置'


def test_rejects_paths_outside_snapshot_and_duplicate_images(payload, tmp_path):
    outside = tmp_path / 'outside.png'
    Image.new('RGB', (10, 10)).save(outside)
    with pytest.raises(browser.BrowserPublishError):
        web.validate_payload(payload | {'image_paths': [str(outside)]})
    with pytest.raises(browser.BrowserPublishError):
        web.validate_payload(payload | {'image_paths': payload['image_paths'] * 2})


HTML = '''<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<div class="ant-form-item"><label title="宝贝图片" class="ant-form-item-required">宝贝图片</label>
<input type="file" multiple id="files"><div id="pictures"></div></div>
<div class="ant-form-item"><label title="宝贝描述" class="ant-form-item-required">宝贝描述</label><div contenteditable="true"></div></div>
<div class="ant-form-item"><label title="分类" class="ant-form-item-required">分类</label>
<div class="ant-select-selector" onclick="document.querySelector('#options').hidden=false"><span class="ant-select-selection-item">其他服务</span></div></div>
<div id="unsupported">网页版暂不支持发布此分类，请使用闲鱼APP扫码继续发布</div>
<div class="ant-select-dropdown" id="options" hidden><div class="ant-select-item-option-content" onclick="setTimeout(()=>{document.querySelector('.ant-select-selection-item').textContent='其他闲置';document.querySelector('#unsupported').hidden=true;document.querySelector('#options').hidden=true},80)">其他闲置</div></div>
<div class="ant-form-item"><label title="价格" class="ant-form-item-required">价格</label><input></div>
<div class="ant-form-item"><label title="邮费" class="ant-form-item-required">邮费</label><input></div>
<label><input type="radio" name="shipping" value="0" checked>包邮</label>
<label><input type="radio" name="shipping" value="6">一口价</label>
<div class="ant-form-item"><label title="宝贝所在地">宝贝所在地</label><div class="address--fixture">测试地点</div></div>
<button onclick="document.querySelector('#attempts').textContent++">发布</button><output id="attempts">0</output>
<script>document.querySelector('#files').onchange=e=>{
document.querySelector('#pictures').innerHTML=[...e.target.files].map(()=>'<div aria-roledescription="sortable"><span class="delete-btn--fixture"></span><img src="https://www.goofish.com/fixture-fleamarket.png"></div>').join('');
};</script></body></html>'''


@pytest.fixture(scope='module')
def chromium():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        edge = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
        instance = p.chromium.launch(headless=True, **({'executable_path': str(edge)} if edge.exists() else {}))
        yield instance
        instance.close()


@pytest.fixture
def page(chromium, payload):
    context = chromium.new_context(service_workers='block')
    image = Path(payload['image_paths'][0]).read_bytes()
    def serve(route):
        if route.request.url.endswith('fixture-fleamarket.png'):
            route.fulfill(content_type='image/png', body=image)
        else:
            route.fulfill(content_type='text/html', body=HTML)
    context.route('**/*', serve)
    page = context.new_page()
    page.goto(web.PUBLISH_URL)
    yield page
    context.close()


def test_upload_and_exact_category_clear_app_only_warning_without_submitting(page, payload):
    prepared = web.validate_payload(payload)
    web.prepare_page(page, prepared)
    assert not page.locator('#unsupported').is_visible()
    assert page.locator(web.EDITOR).inner_text() == prepared['web_description']
    assert page.locator('#files').evaluate('(e)=>e.files.length') == 1
    assert page.locator('#attempts').inner_text() == '0'


VIRTUAL_HTML = '''<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<div class="ant-form-item"><label title="分类">分类</label>
<div class="ant-select-selector" onclick="document.querySelector('#options').hidden=false"><span class="ant-select-selection-item">国产白酒</span></div></div>
<div id="unsupported">网页版暂不支持发布此分类，请使用闲鱼APP扫码继续发布</div>
<div class="ant-select-dropdown" id="options" hidden><div class="rc-virtual-list-holder" style="height:100px;overflow-y:hidden">
<div id="rows" style="height:400px"></div></div></div>
<script>
const names = ['国产白酒', '其他酒类', '收藏-酒瓶', '养生配制酒', '露酒', '家居摆件', '酒具套件', '其他闲置'];
const holder = document.querySelector('.rc-virtual-list-holder');
const pick = name => { document.querySelector('.ant-select-selection-item').textContent = name;
  document.querySelector('#unsupported').hidden = name === '其他闲置'; document.querySelector('#options').hidden = true; };
// Like AntD's virtual list, render only the rows inside the visible window.
const render = () => { const first = Math.floor(holder.scrollTop / 50);
  document.querySelector('#rows').innerHTML = names.slice(first, first + 2).map(name =>
    `<div class="ant-select-item-option-content" style="height:50px" onclick="pick('${name}')">${name}</div>`).join(''); };
holder.onscroll = render; render();
</script></body></html>'''


def test_other_idle_is_found_below_the_rendered_part_of_the_virtual_list(chromium):
    context = chromium.new_context()
    context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=VIRTUAL_HTML))
    page = context.new_page()
    page.goto(web.PUBLISH_URL)
    try:
        web._select_category(page, '其他闲置')
        assert page.locator('.ant-select-selection-item').inner_text() == '其他闲置'
        assert not page.locator('#unsupported').is_visible()
    finally:
        context.close()


def late_list_html(delay_ms):
    """The category list first holds text-only guesses; 其他闲置 arrives once the photos are recognised."""
    return VIRTUAL_HTML.replace(
        "const names = ['国产白酒', '其他酒类', '收藏-酒瓶', '养生配制酒', '露酒', '家居摆件', '酒具套件', '其他闲置'];",
        "let names = ['其他服务', '电子资料', '社科心理'];"
        f"setTimeout(() => {{ names = ['国产白酒', '其他酒类', '露酒', '其他闲置']; render(); }}, {delay_ms});",
    )


def test_category_waits_for_the_list_built_from_the_photos(chromium):
    context = chromium.new_context()
    context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=late_list_html(2500)))
    page = context.new_page()
    page.goto(web.PUBLISH_URL)
    try:
        web._select_category(page, '其他闲置')
        assert page.locator('.ant-select-selection-item').inner_text() == '其他闲置'
    finally:
        context.close()


def test_category_gives_up_when_other_idle_never_appears(chromium, monkeypatch):
    monkeypatch.setattr(web, 'CATEGORY_WAIT_SECONDS', 1)
    context = chromium.new_context()
    context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=late_list_html(600_000)))
    page = context.new_page()
    page.goto(web.PUBLISH_URL)
    try:
        with pytest.raises(browser.BrowserPublishError) as error:
            web._select_category(page, '其他闲置')
        assert error.value.code == 'category_unavailable'
    finally:
        context.close()


LOCATION_HTML = '''<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<div class="ant-form-item"><label title="宝贝所在地">宝贝所在地</label><div class="address--fixture" onclick="document.querySelector('[role=dialog]').hidden=false">广东东软学院</div></div>
<div role="dialog" aria-label="宝贝所在地" hidden><div class="amap-container"></div>
<div id="saved"><div class="addressName--fixture" onclick="pick(this.textContent)">骏富商贸城</div></div>
<input placeholder="搜索地点"><div id="suggestions"></div></div>
<script>
const SUGGESTIONS = {
  '广东省佛山市南海区东软小镇': ['广东东软学院小镇', '东软小镇26号楼'],
  '东软小镇': ['东软软件园', '东软小镇', '东软小镇26号楼'],
  '广东省佛山市南海区东软小镇北区': ['东软小镇', '东软小镇北区(公交站)'],
  '东软小镇北区': ['东软小镇', '东软小镇北区(公交站)'],
  '广东省佛山市南海区不存在的地点': ['瑞幸咖啡(绿地店)', '瑞幸咖啡(绿地店)'],
  '不存在的地点': ['瑞幸咖啡(绿地店)', '瑞幸咖啡(绿地店)'],
  '广东省佛山市南海区东软新城': ['东软新城3号楼'],
  '东软新城': ['东软新城', '东软软件园', '东软新城3号楼'],
};
// An area name is offered, but clicking it only collapses the list.
const AREAS = ['东软新城'];
const pick = name => { document.querySelector('#suggestions').innerHTML = '';
  if (AREAS.includes(name)) return;
  document.querySelector('.address--fixture').textContent = name;
  document.querySelector('[role=dialog]').hidden = true; };
// Like AMap, the previous rows stay until the answer for the latest text arrives,
// and repeating the last searched text gets no answer at all.
let searched = '';
document.querySelector('input').oninput = e => { const query = e.target.value;
  if (query === searched) return;
  searched = query;
  setTimeout(() => { if (e.target.value === query) document.querySelector('#suggestions').innerHTML =
    (SUGGESTIONS[query] || []).map(name => `<div class="auto-item" onclick="pick(this.textContent)">${name}</div>`).join(''); }, 900); };
</script></body></html>'''


@pytest.fixture
def location_page(chromium):
    context = chromium.new_context()
    context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=LOCATION_HTML))
    page = context.new_page()
    page.goto(web.PUBLISH_URL)
    yield page
    context.close()


@pytest.mark.parametrize('location,name', [
    ('广东省佛山市南海区广东东软学院', '广东东软学院'),
    ('佛山市南海区东软学院', '东软学院'),
    ('广东省 佛山市 南海区 东软小镇', '东软小镇'),
    ('北京市朝阳区望京', '望京'),
    ('东软小区东门', '东软小区东门'),
    ('广东省佛山市', '广东省佛山市'),
])
def test_place_name_drops_only_the_administrative_prefix(location, name):
    assert web._place_name(location) == name


@pytest.mark.parametrize('location,shown', [
    ('广东省佛山市南海区东软小镇', '东软小镇'),
    ('广东省佛山市南海区东软小镇北区', '东软小镇北区(公交站)'),
    ('广东省佛山市南海区东软新城', '东软新城3号楼'),
    ('骏富商贸城', '骏富商贸城'),
])
def test_location_resolves_to_the_place_the_form_confirms(location_page, location, shown):
    assert web._set_location(location_page, location) == shown
    assert location_page.locator(web.ADDRESS).inner_text() == shown


def test_full_address_of_the_place_already_shown_leaves_the_form_alone(location_page):
    assert web._set_location(location_page, '广东省佛山市南海区广东东软学院') == '广东东软学院'
    assert not location_page.get_by_role('dialog', name='宝贝所在地').is_visible()


def test_unrelated_suggestions_are_offered_but_never_picked(location_page):
    with pytest.raises(browser.BrowserPublishError) as caught:
        web._set_location(location_page, '广东省佛山市南海区不存在的地点')
    assert '瑞幸咖啡(绿地店)' in str(caught.value)
    assert location_page.locator(web.ADDRESS).inner_text() == '广东东软学院'


def test_receipt_accepts_the_item_page_xianyu_redirects_to_after_publishing(chromium):
    context = chromium.new_context()
    def serve(route):
        # Like the real page, one resource never finishes, so "load" never fires.
        if route.request.resource_type == 'document':
            route.fulfill(content_type='text/html', body='<!DOCTYPE html><meta charset="utf-8"><img src="/never.png"><main>收纳盒</main>')
    context.route('**/*', serve)
    page = context.new_page()
    page.goto('https://www.goofish.com/item?id=1095497898449&categoryId=&spm=a21ybx.publish.0.0', wait_until='commit')
    try:
        assert web._receipt(page, {'title': '收纳盒'}) == {
            'status': 'published', 'url': 'https://www.goofish.com/item?id=1095497898449',
            'external_id': '1095497898449'}
    finally:
        context.close()


def test_unsupported_category_is_not_replaced_with_other_category(page):
    with pytest.raises(browser.BrowserPublishError) as error:
        web._select_category(page, '其他服务')
    assert error.value.code == 'category_unsupported'
    assert page.locator('.ant-select-selection-item').inner_text() == '其他服务'
    assert page.locator('#attempts').inner_text() == '0'


def test_extra_category_fields_and_changed_prices_block_submit(page, payload):
    prepared = web.validate_payload(payload)
    web.prepare_page(page, prepared)
    page.locator('body').evaluate("e=>e.insertAdjacentHTML('beforeend','<label class=\"ant-form-item-required\" title=\"品牌\">品牌</label>')")
    with pytest.raises(browser.BrowserPublishError, match='品牌'):
        web.check_form(page, prepared)
    page.locator('label[title="品牌"]').evaluate('(e)=>e.remove()')
    web._field(page, '价格').locator('input').fill('99')
    with pytest.raises(browser.BrowserPublishError) as error:
        web.check_form(page, prepared)
    assert error.value.code == 'price_mismatch'
    assert page.locator('#attempts').inner_text() == '0'


def test_placeholders_do_not_count_as_uploaded_images(page):
    from playwright.sync_api import TimeoutError as BrowserTimeoutError

    page.locator('#pictures').evaluate('(e)=>e.innerHTML=\'<div aria-roledescription="sortable"><img src="data:image/png;base64,' + base64.b64encode(b'broken').decode() + '\"></div>\'')
    with pytest.raises(BrowserTimeoutError):
        web._images_ready(page, 1, timeout=100)


@pytest.mark.parametrize('failure', ['identity', 'preparation', 'receipt', 'none'])
def test_submission_boundary_and_uncertain_receipt_never_retry(chromium, payload, monkeypatch, failure):
    context = chromium.new_context()
    context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=HTML))
    @contextmanager
    def connected(_):
        yield context
    monkeypatch.setattr(sessions, '_connected', connected)
    monkeypatch.setattr(sessions, 'ensure_browser', lambda *_: False)
    monkeypatch.setattr(sessions, '_inspect_identity', lambda *_: {'state': 'unknown' if failure == 'identity' else 'logged_in', 'account_id': 'fixture'})
    calls = []
    def prepare(*_):
        if failure == 'preparation':
            raise browser.BrowserPublishError('category_unsupported', '不支持此分类')
    monkeypatch.setattr(web, 'prepare_page', prepare)
    monkeypatch.setattr(web, 'check_form', lambda *_: None)
    def receipt(*_):
        if failure == 'receipt':
            raise TimeoutError()
        return {'status': 'published', 'url': 'https://www.goofish.com/item?id=123'}
    monkeypatch.setattr(web, '_receipt', receipt)
    attempts = attempts_when_closed(context)
    try:
        if failure == 'none':
            assert web.publish(payload, 'fixture', {'label': 'test', 'cdp_url': 'http://127.0.0.1:9222'}, {}, lambda: calls.append('submit'))['status'] == 'published'
        else:
            with pytest.raises(browser.BrowserPublishError) as error:
                web.publish(payload, 'fixture', {'label': 'test', 'cdp_url': 'http://127.0.0.1:9222'}, {}, lambda: calls.append('submit'))
            assert error.value.status == ('unknown' if failure == 'receipt' else 'blocked')
        assert calls == (['submit'] if failure in {'receipt', 'none'} else [])
        # The task's tab is closed whether it succeeded or failed.
        assert attempts == ['1' if calls else '0'] and context.pages == []
    finally:
        context.close()


def attempts_when_closed(context):
    """Record each task tab's submit counter at the moment the task closes it."""
    attempts = []
    new_page = context.new_page
    def opened():
        page = new_page()
        close = page.close
        def closing(**options):
            attempts.append(page.locator('#attempts').inner_text())
            close(**options)
        page.close = closing
        return page
    context.new_page = opened
    return attempts


def test_builtin_adapter_only_enables_new_product_publish():
    recipe = {'adapter': web.ADAPTER, 'enabled': True}
    assert browser._recipe_issues('xianyu', recipe) == []
    assert browser._recipe_issues('douyin', recipe)
    assert browser._operation_issues('xianyu', 'delist', recipe)
    assert browser._recipe_issues('xianyu', recipe | {'enabled': False})
    assert browser._recipe_issues('xianyu', recipe | {'publish_url': 'https://evil.test'})
