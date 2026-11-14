"""Xianyu's observed web form. Every listing uses 其他闲置; never guess an address."""

import re
import time
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PIL import Image

from . import automation_browser as browser

ADAPTER = "xianyu-web-v1"
PUBLISH_URL = "https://www.goofish.com/publish"
EDITOR = '[contenteditable="true"]'
IMAGES = '[aria-roledescription="sortable"]'
ADDRESS = '.ant-form-item:has(label[title="宝贝所在地"]) [class^="address--"]'
UNSUPPORTED = "网页版暂不支持发布此分类"
# Xianyu rebuilds the category list as it recognises the photos; until then it only
# offers guesses from the text, which may not include 其他闲置.
CATEGORY_WAIT_SECONDS = 45
# Xianyu suggests categories from the photos (e.g. 国产白酒), many of which the
# web form refuses. 其他闲置 is always offered and accepted, so every listing uses it.
CATEGORY = "其他闲置"


def is_recipe(recipe):
    return isinstance(recipe, dict) and recipe.get("adapter") == ADAPTER


def recipe_issues(recipe):
    if set(recipe) != {"adapter", "enabled"} or recipe.get("enabled") is not True:
        return ["闲鱼网页上架适配器未启用或配置无效。"]
    return []


def _block(code, message):
    raise browser.BrowserPublishError(code, message)


def validate_payload(payload):
    result = dict(payload, category=CATEGORY)
    labels = {'title': '名称', 'description': '文案', 'condition': '成色', 'location': '所在地'}
    for key in ("title", "description", "condition", "location"):
        if not isinstance(result.get(key), str) or not result[key].strip():
            _block("required_field_missing", f"请填写商品{labels[key]}。")
        result[key] = result[key].strip()
    if len(result["title"]) > 30 or len(result["description"]) > 1000:
        _block("copy_too_long", "商品名称最多 30 字，描述最多 1000 字。")
    if len(result["location"]) > 200 or len(result["condition"]) > 80:
        _block("field_too_long", "地点或成色内容过长。")
    if result['location'] in {'请选择地址', '请选择', '省 / 市 / 区'}:
        _block('location_missing', '请填写实际商品所在地。')
    result["web_description"] = f'{result["title"]}\n{result["description"]}\n成色：{result["condition"]}'
    for key in ("price", "shipping_fee"):
        try:
            value = Decimal(str(result[key]))
            if not value.is_finite() or value < 0 or (key == "price" and value == 0) or value.as_tuple().exponent < -2:
                raise ValueError()
            result[key] = format(value, ".2f")
        except (KeyError, ValueError, InvalidOperation):
            _block("price_invalid", "售价必须大于 0，运费不能为负数，金额最多两位小数。")
    paths = result.get("image_paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 9:
        _block("media_invalid", "请添加 1–9 张商品图片。")
    root = (browser._data_dir() / "automation-media").resolve()
    checked = []
    for value in paths:
        if not isinstance(value, str):
            _block("media_invalid", "商品图片无效。")
        path = Path(value).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            _block("media_invalid", "商品图片必须来自本次任务的素材目录。")
        if not 0 < path.stat().st_size <= 20 * 1024 * 1024:
            _block("media_invalid", "每张商品图片不能超过 20 MB。")
        try:
            with Image.open(path) as photo:
                if photo.width * photo.height > 40_000_000:
                    raise ValueError()
                photo.verify()
        except (OSError, ValueError, Image.DecompressionBombError):
            _block("media_invalid", "商品图片文件损坏或尺寸过大。")
        checked.append(str(path))
    if len(set(checked)) != len(checked):
        _block("media_invalid", "商品图片不能重复。")
    result["image_paths"] = checked
    return result


def _field(page, label):
    return page.locator('.ant-form-item').filter(has=page.locator(f'label[title="{label}"]'))


def guard(page, *, category=True):
    from .automation_browser_session import LOGIN_LABELS

    browser._validate_platform_url("xianyu", page.url)
    if any(page.get_by_text(label, exact=True).filter(visible=True).count() for label in LOGIN_LABELS["xianyu"]):
        _block("login_required", "闲鱼登录已失效，请重新登录。")
    if page.get_by_text("为了保障您的体验，请使用正常浏览器访问闲鱼~", exact=True).is_visible():
        _block("browser_blocked", "闲鱼页面要求验证浏览器，请先处理页面提示。")
    if category and page.get_by_text(UNSUPPORTED, exact=False).filter(visible=True).count():
        _block("category_unsupported", "当前商品分类不支持闲鱼网页发布，请选择符合商品实际情况且支持网页的分类。")
    for text in ("请完成验证", "请先完成验证", "拖动滑块", "身份核对"):
        if page.get_by_text(text, exact=False).filter(visible=True).count():
            _block("verification_required", "闲鱼要求验证身份，请点击“打开发布页”完成验证后重试。")


def _category_option(page, field, category):
    """Open the category list and scroll it until `category` is rendered (or the list ends)."""
    field.locator('.ant-select-selector').click()
    dropdown = page.locator('.ant-select-dropdown:visible').last
    option = dropdown.locator('.ant-select-item-option-content').filter(has_text=re.compile('^' + re.escape(category) + '$'))
    holder = dropdown.locator('.rc-virtual-list-holder').first
    with suppress(Exception):
        # A reopened list keeps its old scroll position, where a shorter rebuilt
        # list renders no rows at all; start again from the top.
        holder.wait_for(timeout=5000)
        holder.evaluate('e => { e.scrollTop = 0 }')
    with suppress(Exception):
        dropdown.locator('.ant-select-item-option-content').first.wait_for(timeout=5000)
        # The list is virtual: only ~10 rows are rendered, and 其他闲置 is
        # usually last. Scroll until it is rendered or the list ends.
        while not option.count() and holder.count():
            moved = holder.evaluate('e => { const top = e.scrollTop; e.scrollTop += e.clientHeight; return e.scrollTop !== top }')
            if not moved:
                break
            page.wait_for_timeout(150)
    return option


def _select_category(page, category):
    from playwright.sync_api import expect

    field = _field(page, "分类")
    field.locator('.ant-select-selection-item').wait_for(timeout=30_000)
    selected = field.locator('.ant-select-selection-item')
    if selected.inner_text().strip() != category:
        deadline = time.monotonic() + CATEGORY_WAIT_SECONDS
        while True:
            option = _category_option(page, field, category)
            if option.count() == 1:
                break
            # Not offered yet: close the list and look again once Xianyu has
            # finished recognising the photos.
            with suppress(Exception):
                page.keyboard.press('Escape')
            if time.monotonic() >= deadline:
                _block("category_unavailable", f"闲鱼当前可选分类中没有“{category}”，未提交商品。")
            page.wait_for_timeout(2000)
        option.click()
    try:
        # The page only shows the new category after its h5api request answers.
        expect(selected).to_have_text(category, timeout=15_000)
    except AssertionError:
        _block("category_not_applied", f"闲鱼没有确认“{category}”分类（闲鱼接口未响应，请检查网络或代理），未提交商品，请稍后重试。")
    # Changing a category loads an async schema; wait for the known stable form.
    expect(page.locator('.ant-spin-spinning:visible')).to_have_count(0, timeout=15_000)
    with suppress(Exception):
        expect(page.get_by_text(UNSUPPORTED, exact=False)).not_to_be_visible(timeout=5000)
    guard(page)


def _place_name(location):
    """Drop 省/市/区 from a full address: AMap and the form show only the place name."""
    text = re.sub(r'[\s/]+', '', location)
    rest = re.sub(r'^(?:[^省市]{2,7}省|[^区市]{2,10}(?:自治区|特别行政区))', '', text)
    city = re.sub(r'^[^市]{2,8}市', '', rest)
    if city != text:
        city = re.sub(r'^[^区县]{1,8}[区县]', '', city)
    return city if len(city) >= 2 else text


def _related_places(name, places):
    # AMap also answers with unrelated places, so a suggestion must share the name.
    # A more specific place of that name wins over a broader one it contains.
    related = [place for place in dict.fromkeys(places) if name in place or (len(place) >= 2 and place in name)]
    return sorted(related, key=lambda place: (name not in place, not place.startswith(name)))


def _exact(names):
    return re.compile('^(?:' + '|'.join(re.escape(name) for name in names) + ')$')


def _texts(items):
    return [text.strip() for text in items.all_inner_texts()]


def _offered_places(page, box, items, query, previous):
    box.fill(query)
    with suppress(Exception):
        items.first.wait_for(timeout=10_000)
    # The list keeps the previous rows until AMap answers the new text.
    places = previous
    for _ in range(12):
        page.wait_for_timeout(250)
        places = _texts(items)
        if places != previous:
            break
    return places


def _reopen_suggestions(page, box, items, query):
    # Choosing a row collapses the list, and AMap ignores a repeat of its last
    # text, so step away from that text before asking again.
    box.fill(query[:-1])
    page.wait_for_timeout(1000)
    _offered_places(page, box, items, query, _texts(items))


def _choose_offered(dialog, items, place):
    """Click a suggestion. Area names like 东软小镇 are offered but select nothing."""
    def closed():
        with suppress(Exception):
            dialog.wait_for(state='hidden', timeout=3000)
        return not dialog.is_visible()

    items.filter(has_text=_exact([place])).first.click(timeout=5000)
    if closed():
        return True
    # AMap selection can also just move the map, leaving the place to be
    # chosen from the refreshed address list before the form updates.
    row = dialog.locator('[class^="addressName--"]').filter(has_text=_exact([place]))
    if row.count():
        row.first.click()
    return closed()


def _search_location(page, dialog, location, name):
    box = dialog.get_by_placeholder('搜索地点')
    items = page.locator('.auto-item:visible')
    places = []
    query = name
    tried = set()
    try:
        # A full address can bury the place itself: 东软小镇 is only offered when
        # searched by its own name, so that search follows the full address.
        for query in dict.fromkeys((location, name)):
            places = _offered_places(page, box, items, query, places)
            exact = next((place for place in places if place in {location, name}), None)
            if exact:
                tried.add(exact)
                if _choose_offered(dialog, items, exact):
                    return exact
        for place in [place for place in _related_places(name, places) if place not in tried][:3]:
            if not items.count():
                _reopen_suggestions(page, box, items, query)
            if _choose_offered(dialog, items, place):
                return place
    except Exception:
        _block("location_unavailable", f"无法在闲鱼选中“{name}”，未提交商品，请重试。")
    offered = '、'.join(dict.fromkeys(places[:5]))
    _block("location_unavailable", f"闲鱼没有可选中的“{name}”" + (f"，可选地点：{offered}。" if offered else "，请填写更具体的地点名称。"))


def _set_location(page, location):
    """Choose the place and return the name the form confirmed, which is what later checks compare."""
    from playwright.sync_api import expect

    name = _place_name(location)
    current = page.locator(ADDRESS)
    shown = current.inner_text().strip()
    if shown in {location, name}:
        return shown
    current.click()
    dialog = page.get_by_role('dialog', name='宝贝所在地')
    dialog.wait_for()
    # The map installs autocomplete asynchronously after the dialog appears.
    dialog.locator('.amap-container').wait_for(state='attached', timeout=15_000)
    with suppress(Exception):
        dialog.locator('[class^="addressName--"]').first.wait_for(timeout=5000)
    # Existing saved/nearby address labels are exact matches, not city substrings.
    saved = dialog.locator('[class^="addressName--"]').filter(has_text=_exact([location, name]))
    if saved.count():
        picked = saved.first.inner_text().strip()
        saved.first.click()
    else:
        picked = _search_location(page, dialog, location, name)
    try:
        expect(current).to_have_text(picked, timeout=10_000)
        expect(dialog).not_to_be_visible(timeout=10_000)
    except Exception:
        _block("location_mismatch", f"闲鱼显示的所在地不是“{picked}”，未提交商品，请重试。")
    return picked


def _images_ready(page, count, timeout=60_000):
    # A placeholder/thumbnail slot is not an uploaded image. Observed successful
    # uploads expose an actual fleamarket image and a delete control per slot.
    page.wait_for_function("""count => {
      const slots = [...document.querySelectorAll('[aria-roledescription="sortable"]')];
      return slots.length === count && slots.every(slot =>
        slot.querySelector('[class^="delete-btn--"]') &&
        [...slot.querySelectorAll('img')].some(img => img.complete && img.naturalWidth > 2 &&
          getComputedStyle(img).display !== 'none' && img.src.includes('fleamarket') && !img.src.includes('.gif')));
    }""", arg=count, timeout=timeout)


def check_form(page, payload):
    from playwright.sync_api import expect

    guard(page)
    expect(page.locator(EDITOR)).to_have_text(payload['web_description'], use_inner_text=True)
    expect(_field(page, '分类').locator('.ant-select-selection-item')).to_have_text(payload['category'])
    for label, key in [('价格', 'price'), ('邮费', 'shipping_fee')]:
        if key == 'shipping_fee' and Decimal(payload[key]) == 0:
            continue
        if Decimal(_field(page, label).locator('input').input_value()) != Decimal(payload[key]):
            _block('price_mismatch', '闲鱼页面的售价或运费与本次商品不一致，未提交。')
    expect(page.get_by_role('radio', name='包邮' if Decimal(payload['shipping_fee']) == 0 else '一口价', exact=True)).to_be_checked()
    expect(page.locator(ADDRESS)).to_have_text(payload['location'])
    _images_ready(page, len(payload['image_paths']), timeout=5000)
    errors = page.locator('.ant-form-item-explain-error:visible')
    if errors.count():
        _block('form_incomplete', '闲鱼表单尚未通过校验：' + '；'.join(errors.all_text_contents())[:300])
    handled = {'宝贝图片', '宝贝描述', '分类', '价格', '邮费', '宝贝所在地'}
    for label in page.locator('label.ant-form-item-required:visible').all():
        name = label.get_attribute('title') or label.inner_text().strip('* \n')
        if name not in handled:
            _block('additional_fields_required', f'闲鱼要求额外填写“{name}”，自动上架暂不支持，当前未提交。')


def prepare_page(page, payload):
    """Upload and populate a fresh editor; this function never submits."""
    guard(page, category=False)
    page.locator(EDITOR).wait_for(timeout=20_000)
    if page.locator(EDITOR).inner_text().strip() or page.locator(IMAGES).count():
        _block('editor_not_empty', '发布页已有内容，为避免覆盖，请使用新的商品发布页。')
    page.locator('input[type="file"]').set_input_files(payload['image_paths'])
    page.locator(EDITOR).fill(payload['web_description'])
    # Choose the category only after the photos are in: Xianyu builds the list from them.
    _images_ready(page, len(payload['image_paths']))
    _select_category(page, payload['category'])
    _field(page, '价格').locator('input').fill(payload['price'])
    page.get_by_role('radio', name='包邮' if Decimal(payload['shipping_fee']) == 0 else '一口价', exact=True).check()
    if Decimal(payload['shipping_fee']) > 0:
        _field(page, '邮费').locator('input').fill(payload['shipping_fee'])
    payload['location'] = _set_location(page, payload['location'])
    check_form(page, payload)


def _receipt(page, payload):
    # A new item URL is required. A toast, button click or login page is never a receipt.
    # Reaching that URL is the proof: item pages can take over 30s to fire "load".
    try:
        page.wait_for_url(re.compile(r'https://(?:www\.)?goofish\.com/item\?'), timeout=30_000, wait_until='commit')
        url, item_id = browser._listing_url(page.url)
        return {'status': 'published', 'url': url, 'external_id': item_id}
    except Exception:
        raise browser.BrowserPublishError('publication_unknown', '已尝试提交商品，但未取得商品详情链接。请在闲鱼核查，本次不会自动重复发布。', status='unknown') from None


def publish(payload, identifier, account, recipe, before_submit):
    from . import automation_browser_session as sessions

    if not callable(before_submit):
        _block('submission_marker_required', '发布必须记录提交状态。')
    prepared = validate_payload(payload)
    submitted = False
    with browser._browser_lock(account['cdp_url'], wait=browser.LOCK_WAIT_SECONDS):
        # A closed publishing browser is reopened with its saved logins.
        started = sessions.ensure_browser(account['cdp_url'])
        with sessions._connected(account['cdp_url']) as context:
            page = context.new_page()
            if started:
                sessions.close_start_tab(context, page)
            try:
                page.goto(PUBLISH_URL, wait_until='domcontentloaded', timeout=30_000)
                def verify_identity():
                    state = sessions._inspect_identity(context, page, 'xianyu', identifier, account, recipe)
                    if state['state'] != 'logged_in' or state.get('account_id') != identifier:
                        _block('account_unverified', state.get('message') or '闲鱼账号尚未确认。')
                # Signed out: the tab shows the login page and the task waits for the
                # scan, then reopens a fresh form under the new session.
                if sessions.ensure_logged_in(context, page, 'xianyu', identifier, account, recipe).get('waited_for_login'):
                    page.goto(PUBLISH_URL, wait_until='domcontentloaded', timeout=30_000)
                prepare_page(page, prepared)
                submit = page.get_by_role('button', name='发布', exact=True)
                submit.click(trial=True, timeout=10_000)
                verify_identity()
                check_form(page, prepared)
                before_submit()
                submitted = True
                submit.click(timeout=15_000)
                return {**_receipt(page, prepared), 'account_id': identifier, 'account_label': account['label']}
            except Exception as error:
                if submitted:
                    raise browser.BrowserPublishError('publication_unknown', '已尝试发布，结果尚未确认；请在闲鱼核查，禁止自动重复提交。', status='unknown') from None
                if isinstance(error, browser.BrowserPublishError):
                    raise
                raise browser.BrowserPublishError('preparation_failed', '商品图片或表单尚未准备完成，未点击发布，请重试。') from None
            finally:
                # Close only the tab this task opened, whatever the outcome; the
                # browser and its other tabs stay as they were.
                with suppress(Exception):
                    page.close()
