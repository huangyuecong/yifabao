"""Product creation -> revision -> human approval -> library -> exact publishing input."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from agent_core import Identity
from content_service import automation_content as content
from content_service import automation_jobs as jobs
from content_service import automation_review as review
from content_service import automation_routes as routes
from content_service import content_packages as packages
from content_service import db, provider_dependencies
from content_service.automation_models import AutomationRun
from content_service.media_adapters import LocalAssetStore
from content_service.models import AssetRef, LibraryItem, ResourceEntry, ResourceVersion

BASE = '/api/v1/automation'
HEADERS = {'X-Content-Config-Request': '1'}
OWNER = Identity('owner', 'Owner', '', frozenset({'admin'}))
CONFIG = {'name': '木盒创作', 'purpose': 'creation', 'product_id': 'product', 'image_ids': ['photo'],
          'platforms': [], 'image_mode': 'existing'}


@pytest.fixture
def creation(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "db.sqlite"}', connect_args={'check_same_thread': False})
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    store = LocalAssetStore(tmp_path / 'objects')
    (tmp_path / 'objects').mkdir(exist_ok=True)
    Image.new('RGB', (180, 140), 'brown').save(tmp_path / 'objects' / 'photo.jpg')
    settings = SimpleNamespace(data_dir=tmp_path, llm_provider='openai', llm_model='test-vision')
    for module in (content, review, packages):
        monkeypatch.setattr(module, 'get_runtime_settings', lambda: settings)
    for module in (content, packages, provider_dependencies):
        monkeypatch.setattr(module, 'get_asset_store_port', lambda: store)
    monkeypatch.setattr(db, 'SessionLocal', factory)
    monkeypatch.setattr(content, 'SessionLocal', factory)
    monkeypatch.setattr(content, 'model_settings', lambda *_: settings)
    calls = []

    def model(*args):
        calls.append(args)
        facts = json.loads(args[2][1]['content'][0]['text'])
        return json.dumps({'title': '木盒收纳', 'description': '整理桌面的小木盒', 'category': '家居',
                           'listing_title': '木制收纳盒', 'listing_description': '木制收纳盒，用于桌面收纳。',
                           'classifications': [{'id': item['id'], 'role': 'main', 'reason': '商品原图', 'confidence': .9} for item in facts['images']]})

    monkeypatch.setattr(content, 'call_chat', model)

    def render(images, draft, directory, _):
        target = directory / 'video.mp4'
        target.write_bytes(b'test-encoded-video:' + draft['description'].encode())
        return target

    monkeypatch.setattr(content, 'render_slideshow', render)
    monkeypatch.setattr(routes, 'execute_run', lambda *_: None)
    monkeypatch.setattr(routes, 'execute_creation_batch', lambda *_: None)
    monkeypatch.setenv('CONTENT_IDENTITY_MODE', 'development')
    with factory() as session:
        session.add(ResourceEntry(id='product', owner_id='owner', resource_kind='product', title='木制收纳盒', category='product'))
        session.add(LibraryItem(id='product', owner_id='owner', resource_entry_id='product', name='木制收纳盒', category='product', text_content='木制，用于桌面收纳'))
        session.add(ResourceEntry(id='photo', owner_id='owner', resource_kind='image', title='原图', category='image', parent_id='product'))
        session.add(AssetRef(id='photo-asset', entry_id='photo', storage_key='photo.jpg', media_type='image/jpeg', size_bytes=10, checksum='source'))
        session.commit()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_identity] = lambda: OWNER
    with TestClient(app) as client:
        yield client, factory, calls, settings, app
    engine.dispose()


def start(client, config=None):
    response = client.post(BASE + '/workflows', headers=HEADERS, json=config or CONFIG)
    assert response.status_code == 201, response.text
    workflow = response.json()
    assert not workflow['config']['auto_publish']
    response = client.post(f'{BASE}/workflows/{workflow["id"]}/runs', headers={**HEADERS, 'Idempotency-Key': str(uuid4())})
    assert response.status_code == 202, response.text
    identifier = response.json()['id']
    jobs.execute_run(identifier, OWNER)
    run = client.get(f'{BASE}/runs/{identifier}').json()
    assert run['status'] == 'ready', run
    return run


def approve(client, run):
    response = client.post(f'{BASE}/runs/{run["id"]}/approve', headers=HEADERS,
                           json={'expected_revision': run['draft']['review']['revision']})
    assert response.status_code == 200, response.text
    return response.json()


def test_independent_creation_approval_archives_atomically_and_is_idempotent(creation):
    client, factory, calls, _, _ = creation
    run = start(client)
    assert run['config']['platforms'] == [] and run['platform_results'] == {}
    assert run['draft']['listing_description'] and run['draft']['has_video']
    assert len(calls) == 1
    assert client.get(BASE + '/approved-contents').json()['items'] == []
    assert client.post(f'{BASE}/runs/{run["id"]}/publish', headers=HEADERS).status_code == 409
    approved = approve(client, run)
    identifier = approved['draft']['approved_content_id']
    assert identifier
    package = client.get(BASE + '/approved-contents').json()['items'][0]
    assert package['product_id'] == 'product'
    assert package['image_ids'] and package['original_image_ids'] and package['video_id']
    assert len(approved['draft']['library_ids']) == 4
    assert approve(client, approved)['draft']['approved_content_id'] == identifier
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ResourceEntry).where(ResourceEntry.category == 'approved_content')) == 1
        assert session.get(ResourceEntry, package['original_image_ids'][0]).parent_id == 'product'
        assert session.get(ResourceEntry, package['image_ids'][0]).parent_id == identifier


def test_revision_requires_new_human_approval_and_old_package_stays_immutable(creation):
    client, factory, _, _, _ = creation
    run = approve(client, start(client))
    first = client.get(BASE + '/approved-contents').json()['items'][0]
    response = client.put(f'{BASE}/runs/{run["id"]}/draft', headers=HEADERS, json={
        'expected_revision': 1, 'title': '新版标题', 'description': run['draft']['description'],
        'listing_title': '修改商品名称', 'listing_description': '新的事实描述', 'category': '家居',
    })
    assert response.status_code == 200, response.text
    revised = response.json()
    assert revised['draft']['review']['state'] == 'pending' and revised['draft']['video_stale']
    assert revised['draft']['approved_content_id'] is None
    assert client.post(f'{BASE}/runs/{run["id"]}/approve', headers=HEADERS, json={'expected_revision': 2}).status_code == 409
    assert client.get(BASE + '/approved-contents').json()['items'] == [first]
    response = client.post(f'{BASE}/runs/{run["id"]}/revise', headers=HEADERS,
        json={'expected_revision': 2, 'scope': 'video', 'feedback': '同步新版标题'})
    assert response.status_code == 202
    jobs.execute_run(run['id'], OWNER)
    revised = client.get(f'{BASE}/runs/{run["id"]}').json()
    assert revised['draft']['review']['revision'] == 3 and not revised['draft']['video_stale']
    assert revised['draft']['review']['state'] == 'pending'
    approved = approve(client, revised)
    assert approved['draft']['approved_content_id'] != first['id']
    assert len(client.get(BASE + '/approved-contents').json()['items']) == 2
    with factory() as session:
        row = session.get(AutomationRun, run['id'])
        assert json.loads(row.payload_json)['_history'][0]['title'] == first['title']


def test_publish_reads_approved_bytes_and_copy_without_model_calls(creation):
    client, factory, calls, _, _ = creation
    run = approve(client, start(client))
    identifier = run['draft']['approved_content_id']
    promotion = routes.WorkflowConfig(name='宣传', platforms=['douyin', 'xiaohongshu', 'kuaishou'],
        approved_content_id=identifier).model_dump(mode='json')
    listing = routes.WorkflowConfig(name='上架', platforms=['xianyu'], approved_content_id=identifier,
        price='19.50', location='上海', condition='全新').model_dump(mode='json')
    # Later library edits must never silently change the reviewed copy.
    with factory() as session:
        entry = session.get(ResourceEntry, identifier)
        newer = ResourceVersion(entry_id=entry.id, version=2, text_content='unreviewed', metadata_json='{}', created_by='owner')
        session.add(newer)
        session.flush()
        entry.current_version_id = newer.id
        session.commit()
    p = content.prepare_content(promotion, OWNER)
    s = content.prepare_content(listing, OWNER)
    assert p['title'] == run['draft']['title'] and p['video_path']
    assert s['title'] == run['draft']['listing_title'] and not s['video_path']
    assert s['price'] == '19.50' and 'price' not in p
    assert len(calls) == 1


def test_package_ownership_and_media_integrity(creation):
    client, factory, _, settings, app = creation
    identifier = approve(client, start(client))['draft']['approved_content_id']
    other = Identity('other', 'Other', '', frozenset({'admin'}))
    app.dependency_overrides[routes.get_identity] = lambda: other
    assert client.get(BASE + '/approved-contents').json()['items'] == []
    assert client.get(f'{BASE}/approved-contents/{identifier}').status_code == 404
    cfg = routes.WorkflowConfig(name='宣传', platforms=['douyin'], approved_content_id=identifier).model_dump(mode='json')
    with pytest.raises(HTTPException) as caught:
        content.prepare_content(cfg, other)
    assert caught.value.status_code == 404
    with factory() as session:
        _, data = packages.package_entry(session, identifier, 'owner')
        asset = session.get(AssetRef, data['assets'][data['image_ids'][0]]['asset_id'])
        (settings.data_dir / 'objects' / asset.storage_key).write_bytes(b'tampered')
    with pytest.raises(HTTPException) as caught:
        content.prepare_content(cfg, OWNER)
    assert caught.value.status_code == 409


def test_failed_archive_rolls_back_approval(creation, monkeypatch):
    client, factory, _, _, _ = creation
    run = start(client)
    def fail(*_):
        raise HTTPException(503, '存储暂时不可用')
    monkeypatch.setattr(packages, 'archive_creation', fail)
    response = client.post(f'{BASE}/runs/{run["id"]}/approve', headers=HEADERS, json={'expected_revision': 1})
    assert response.status_code == 503
    with factory() as session:
        row = session.get(AutomationRun, run['id'])
        assert json.loads(row.payload_json)['_review']['state'] == 'pending'
    assert client.get(BASE + '/approved-contents').json()['items'] == []


def test_creation_cannot_bind_platform_and_custom_product_enters_library_on_approval(creation):
    client, factory, _, _, _ = creation
    assert client.post(BASE + '/workflows', headers=HEADERS, json=CONFIG | {'platforms': ['douyin']}).status_code == 422
    run = start(client, CONFIG | {'source_mode': 'custom', 'custom_product_name': '自定义木盒', 'custom_product_description': '木制收纳盒'})
    assert client.get(BASE + '/approved-contents').json()['items'] == []
    approve(client, run)
    package = client.get(BASE + '/approved-contents').json()['items'][0]
    with factory() as session:
        assert session.get(ResourceEntry, package['product_id']).title == '自定义木盒'
        assert session.get(LibraryItem, package['product_id']).text_content == '木制收纳盒'


def test_image_failure_keeps_copy_and_requires_explicit_repair_before_approval(creation, monkeypatch):
    from content_service.llm_adapters import ProviderCallError
    client, _, calls, _, _ = creation
    def unavailable(*_):
        raise ProviderCallError('生图服务暂时不可用')
    monkeypatch.setattr(content, 'generate_promotion_images', unavailable)
    run = start(client, CONFIG | {'image_mode': 'generate'})
    assert run['draft']['title'] and run['draft']['listing_title']
    assert run['draft']['generation_error']['stage'] == 'images'
    assert run['draft']['image_count'] == 0 and not run['draft']['has_video']
    assert client.post(f'{BASE}/runs/{run["id"]}/approve', headers=HEADERS, json={'expected_revision': 1}).status_code == 409
    response = client.post(f'{BASE}/runs/{run["id"]}/revise', headers=HEADERS,
        json={'expected_revision': 1, 'scope': 'originals', 'feedback': '先使用商品原图'})
    assert response.status_code == 202
    jobs.execute_run(run['id'], OWNER)
    repaired = client.get(f'{BASE}/runs/{run["id"]}').json()
    assert not repaired['draft']['generation_error'] and repaired['draft']['has_video']
    assert repaired['draft']['review']['revision'] == 2 and repaired['draft']['review']['state'] == 'pending'
    assert len(calls) == 1
    approve(client, repaired)


def test_batch_creates_independent_products_once_and_a_failure_does_not_block_siblings(creation):
    client, factory, calls, settings, _ = creation
    Image.new('RGB', (180, 140), 'blue').save(settings.data_dir / 'objects' / 'second.jpg')
    with factory() as session:
        session.add(ResourceEntry(id='product-2', owner_id='owner', resource_kind='product', title='蓝色收纳盒', category='product'))
        session.add(LibraryItem(id='product-2', owner_id='owner', resource_entry_id='product-2', name='蓝色收纳盒', category='product', text_content='蓝色，用于桌面收纳'))
        session.add(ResourceEntry(id='photo-2', owner_id='owner', resource_kind='image', title='蓝色原图', parent_id='product-2'))
        session.add(AssetRef(entry_id='photo-2', storage_key='second.jpg', media_type='image/jpeg', size_bytes=10, checksum='source'))
        session.commit()
    cfg2 = CONFIG | {'name': '蓝盒创作', 'product_id': 'product-2', 'image_ids': ['photo-2']}
    bad = CONFIG | {'name': '无效产品', 'product_id': 'missing-product', 'image_ids': ['missing-image']}
    body = {'items': [CONFIG, cfg2, bad]}
    headers = HEADERS | {'Idempotency-Key': 'multi-product-request'}
    response = client.post(BASE + '/creation-batches', headers=headers, json=body)
    assert response.status_code == 202, response.text
    ids = [run['id'] for run in response.json()['items']]
    duplicate = client.post(BASE + '/creation-batches', headers=headers, json=body)
    assert [run['id'] for run in duplicate.json()['items']] == ids
    assert client.post(BASE + '/creation-batches', headers=headers, json={'items': [CONFIG]}).status_code == 409
    jobs.execute_creation_batch(ids, OWNER)
    runs = [client.get(f'{BASE}/runs/{identifier}').json() for identifier in ids]
    assert [run['status'] for run in runs] == ['ready', 'ready', 'failed']
    assert all(run['draft']['review']['state'] == 'pending' for run in runs[:2])
    with factory() as session:
        snapshots = [json.loads(session.get(AutomationRun, identifier).payload_json)['_sources'] for identifier in ids[:2]]
        assert [source['product']['entry_id'] for source in snapshots] == ['product', 'product-2']
        assert [source['images'][0]['id'] for source in snapshots] == ['photo', 'photo-2']
    assert {json.loads(call[2][1]['content'][0]['text'])['product']['name'] for call in calls} == {'木制收纳盒', '蓝色收纳盒'}
    assert client.get(BASE + '/approved-contents').json()['items'] == []
    approve(client, runs[0])
    assert len(client.get(BASE + '/approved-contents').json()['items']) == 1
    assert client.get(f'{BASE}/runs/{ids[1]}').json()['draft']['review']['state'] == 'pending'


def test_batch_rejects_duplicate_products_and_insufficient_capacity_without_partial_creation(creation):
    client, factory, _, _, _ = creation
    headers = HEADERS | {'Idempotency-Key': 'bounded-batch'}
    assert client.post(BASE + '/creation-batches', headers=headers, json={'items': [CONFIG, CONFIG]}).status_code == 422
    assert client.post(BASE + '/creation-batches', headers=headers, json={'items': [CONFIG | {'purpose': 'promotion', 'platforms': ['douyin']}]}).status_code == 422
    full = [CONFIG | {'product_id': f'product-{i}'} for i in range(20)]
    assert client.post(BASE + '/creation-batches', headers=headers, json={'items': full}).status_code == 202
    response = client.post(BASE + '/creation-batches', headers=HEADERS | {'Idempotency-Key': 'overflow'}, json={'items': [CONFIG]})
    assert response.status_code == 429
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AutomationRun)) == 20
