"""Real database state-machine tests, with external generation and publishing replaced."""

import json
from datetime import timedelta
from types import ModuleType
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from agent_core import Identity
from content_service import automation_jobs as jobs
from content_service import automation_routes as routes
from content_service import db
from content_service.automation_browser import BrowserPublishError
from content_service.automation_models import AutomationEvent, AutomationRun, AutomationWorkflow
from content_service.models import AuditLog, SensitiveRule, utcnow

BASE = "/api/v1/automation"
HEADERS = {"X-Content-Config-Request": "1"}
ALICE = Identity("alice", "Alice", "marketing", frozenset({"admin"}))
BOB = Identity("bob", "Bob", "marketing", frozenset({"admin"}))
CONFIG = {
    "name": "商品自动发布", "image_ids": ["image-1"], "require_review": False,
    "platforms": ["xianyu"], "execution_mode": "browser", "price": "99.00", "location": "示例城市",
}
PAYLOAD = {
    "title": "商品标题", "description": "根据真实商品资料生成的介绍", "price": "99.00",
    "condition": "全新", "shipping_fee": "0", "location": "示例城市", "category": "家居",
    "image_paths": ["C:/private/assets/example.png"], "video_path": "C:/private/assets/video.mp4",
    "classifications": [{"id": "image-1", "role": "main", "label": "商品主图", "secret_path": "SECRET"}],
    "source_summary": {"product_name": "商品", "image_count": 1, "storage_key": "SECRET"},
}


@pytest.fixture
def automation(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'automation.db'}", connect_args={"check_same_thread": False})
    for model in (AuditLog, SensitiveRule, AutomationWorkflow, AutomationRun, AutomationEvent):
        model.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(routes, "execute_run", lambda *_: None)
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "development")
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_identity] = lambda: ALICE
    content = ModuleType("content_service.automation_content")
    content.prepare_content = lambda config, identity: dict(PAYLOAD)
    browser = ModuleType("content_service.automation_browser")
    calls = []

    def publish(platform, payload, connection=None, before_submit=None):
        calls.append(platform)
        before_submit()
        return {"status": "published", "url": "https://example.com/receipt", "account_label": "测试账号"}

    browser.publish = publish
    browser.status = lambda: {"available": True}
    browser.BrowserPublishError = BrowserPublishError
    monkeypatch.setitem(__import__("sys").modules, "content_service.automation_content", content)
    monkeypatch.setitem(__import__("sys").modules, "content_service.automation_browser", browser)
    # These database state-machine tests replace the transport for either
    # destination. Adapter isolation and preflight are tested separately.
    from content_service import automation_android
    monkeypatch.setattr(automation_android, "preflight", lambda _: None)
    def android_execute(platform, operation, payload, connection, *, device_id, before_submit):
        account = {"account_id": connection.get("account_id", "")}
        if operation == "publish":
            return browser.publish(platform, payload, account, before_submit=before_submit)
        return browser.manage_listing(platform, operation, payload, account, before_submit=before_submit)
    monkeypatch.setattr(automation_android, "execute", android_execute)
    with TestClient(app) as client:
        yield client, factory, app, browser, calls
    engine.dispose()


def create(client, config=None):
    result = client.post(BASE + "/workflows", headers=HEADERS, json=config or CONFIG)
    assert result.status_code == 201, result.text
    return result.json()["id"]


def start(client, workflow_id, key=None):
    result = client.post(f"{BASE}/workflows/{workflow_id}/runs", headers={**HEADERS, "Idempotency-Key": key or str(uuid4())})
    assert result.status_code == 202, result.text
    return result.json()["id"]


def test_review_required_by_default_and_rechecked_at_submission(automation, monkeypatch, tmp_path):
    from content_service import automation_review as review
    client, factory, _, browser, calls = automation
    media = tmp_path / 'media.jpg'
    media.write_bytes(b'original')
    monkeypatch.setattr(review, 'media_path', lambda _: media)
    config = {key: value for key, value in CONFIG.items() if key != 'require_review'}
    run_id = start(client, create(client, config))
    jobs.execute_run(run_id, ALICE)
    result = client.get(f'{BASE}/runs/{run_id}').json()
    assert result['status'] == 'ready' and result['draft']['review']['state'] == 'pending'
    assert result['config']['auto_publish'] is False and not calls
    assert client.post(f'{BASE}/runs/{run_id}/publish', headers=HEADERS).status_code == 409
    assert client.post(f'{BASE}/runs/{run_id}/approve', headers=HEADERS, json={'expected_revision': 2}).status_code == 409
    assert client.post(f'{BASE}/runs/{run_id}/approve', headers=HEADERS, json={'expected_revision': 1}).status_code == 200
    assert client.post(f'{BASE}/runs/{run_id}/publish', headers=HEADERS).status_code == 202
    # Change the file after approval but before the worker resumes.
    media.write_bytes(b'tampered')
    jobs.execute_run(run_id, ALICE)
    assert not calls
    assert client.get(f'{BASE}/runs/{run_id}').json()['status'] == 'failed'


def test_edit_invalidates_approval_and_stale_revision_is_rejected(automation, monkeypatch, tmp_path):
    from content_service import automation_review as review
    client, _, _, _, calls = automation
    media = tmp_path / 'media.jpg'
    media.write_bytes(b'original')
    monkeypatch.setattr(review, 'media_path', lambda _: media)
    run_id = start(client, create(client, CONFIG | {'require_review': True}))
    jobs.execute_run(run_id, ALICE)
    assert client.post(f'{BASE}/runs/{run_id}/approve', headers=HEADERS, json={'expected_revision': 1}).status_code == 200
    body = {'title': '修改标题', 'description': PAYLOAD['description'], 'category': PAYLOAD['category'], 'price': '99.00', 'expected_revision': 1}
    result = client.put(f'{BASE}/runs/{run_id}/draft', headers=HEADERS, json=body)
    assert result.status_code == 200
    assert result.json()['draft']['review'] == result.json()['draft']['review'] | {'revision': 2, 'state': 'pending'}
    assert result.json()['draft']['history'][0]['title'] == PAYLOAD['title']
    assert client.put(f'{BASE}/runs/{run_id}/draft', headers=HEADERS, json=body).status_code == 409
    assert client.post(f'{BASE}/runs/{run_id}/publish', headers=HEADERS).status_code == 409 and not calls


def test_failed_revision_preserves_previous_draft(automation, monkeypatch):
    import sys
    client, _, _, _, calls = automation
    run_id = start(client, create(client, CONFIG | {'require_review': True}))
    jobs.execute_run(run_id, ALICE)
    def failure(*args):
        raise RuntimeError('provider failed')
    monkeypatch.setattr(sys.modules['content_service.automation_content'], 'revise_content', failure, raising=False)
    body = {'expected_revision': 1, 'feedback': '请精简文案', 'scope': 'copy'}
    assert client.post(f'{BASE}/runs/{run_id}/revise', headers=HEADERS, json=body).status_code == 202
    jobs.execute_run(run_id, ALICE)
    result = client.get(f'{BASE}/runs/{run_id}').json()
    assert result['status'] == 'ready' and result['draft']['title'] == PAYLOAD['title']
    assert result['draft']['review']['revision'] == 1 and result['error'] and not calls


def test_approved_draft_publishes_once(automation, monkeypatch, tmp_path):
    from content_service import automation_review as review
    client, _, _, _, calls = automation
    media = tmp_path / 'image.jpg'
    media.write_bytes(b'original')
    monkeypatch.setattr(review, 'media_path', lambda _: media)
    run_id = start(client, create(client, CONFIG | {'require_review': True}))
    jobs.execute_run(run_id, ALICE)
    assert client.post(f'{BASE}/runs/{run_id}/approve', headers=HEADERS, json={'expected_revision': 1}).status_code == 200
    assert client.post(f'{BASE}/runs/{run_id}/publish', headers=HEADERS).status_code == 202
    jobs.execute_run(run_id, ALICE)
    jobs.execute_run(run_id, ALICE)
    assert calls == ['xianyu']
    assert client.get(f'{BASE}/runs/{run_id}').json()['status'] == 'succeeded'


def test_owner_isolation_and_write_permissions(automation):
    client, _, app, _, _ = automation
    workflow_id = create(client)
    run_id = start(client, workflow_id)
    app.dependency_overrides[routes.get_identity] = lambda: BOB
    assert client.get(BASE + "/workflows").json()["items"] == []
    assert client.get(f"{BASE}/workflows/{workflow_id}").status_code == 404
    assert client.get(f"{BASE}/runs/{run_id}").status_code == 404
    assert client.post(f"{BASE}/runs/{run_id}/retry", headers=HEADERS).status_code == 404
    app.dependency_overrides[routes.get_identity] = lambda: Identity("user", "User", "", frozenset({"user"}))
    assert client.post(BASE + "/workflows", json=CONFIG, headers=HEADERS).status_code == 403


def test_required_fields_headers_and_request_deduplication(automation):
    client, _, _, _, _ = automation
    assert client.post(BASE + "/workflows", json=CONFIG).status_code == 403
    for updates in ({"price": "-1"}, {"price": None}, {"location": ""}, {"platforms": ["unsupported"]},
                    {"image_ids": ["same", "same"]}, {"platforms": ["douyin"], "video_mode": "existing"}, {"accounts": {"douyin": "http://evil"}}):
        assert client.post(BASE + "/workflows", json=CONFIG | updates, headers=HEADERS).status_code == 422
    workflow_id = create(client)
    assert client.post(f"{BASE}/workflows/{workflow_id}/runs", headers=HEADERS).status_code == 422
    first = start(client, workflow_id, "same-request")
    assert start(client, workflow_id, "same-request") == first
    assert len(client.get(BASE + "/runs").json()["items"]) == 1
    other = create(client)
    assert client.post(f"{BASE}/workflows/{other}/runs", headers={**HEADERS, "Idempotency-Key": "same-request"}).status_code == 409


def test_manual_listing_rules_validate_copy_and_keep_mode_in_run_snapshot(automation):
    client, _, _, _, _ = automation
    manual = CONFIG | {'copy_mode': 'manual', 'source_mode': 'custom',
                       'custom_product_name': '收纳盒', 'custom_product_description': '有使用痕迹。', 'category': '其他闲置'}
    for changes in ({'category': ''}, {'custom_product_name': '字' * 31}, {'custom_product_description': '字' * 1001},
                    {'source_mode': 'library'}, {'platforms': ['douyin']}):
        assert client.post(BASE + '/workflows', json=manual | changes, headers=HEADERS).status_code == 422
    run_id = start(client, create(client, manual))
    result = client.get(f'{BASE}/runs/{run_id}').json()
    assert result['config']['copy_mode'] == 'manual'
    assert result['config']['custom_product_description'] == '有使用痕迹。'


def test_claim_once_and_hide_private_paths(automation):
    client, _, _, _, calls = automation
    run_id = start(client, create(client))
    jobs.execute_run(run_id, ALICE)
    jobs.execute_run(run_id, ALICE)
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert result["status"] == "succeeded"
    assert calls == ["xianyu"]
    assert "C:/private" not in json.dumps(result)
    assert "SECRET" not in json.dumps(result)
    assert result["draft"]["has_video"]
    assert len([event for event in result["events"] if event["kind"] == "submitting"]) == 1


def test_retry_skips_platform_with_retained_successful_receipt(automation):
    client, factory, _, _, calls = automation
    run_id = start(client, create(client))
    with factory() as session:
        row = session.get(AutomationRun, run_id)
        row.status = "partial"
        row.payload_json = jobs.encoded(PAYLOAD)
        row.platform_results_json = jobs.encoded({"xianyu": {"status": "succeeded", "receipt": {"url": "https://example.com/existing"}}})
        session.commit()
    assert client.post(f"{BASE}/runs/{run_id}/retry", headers=HEADERS).status_code == 202
    jobs.execute_run(run_id, ALICE)
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert result["status"] == "succeeded"
    assert calls == []
    assert result["platform_results"]["xianyu"]["receipt"]["url"] == "https://example.com/existing"


def test_post_submit_failure_never_retries(automation):
    client, _, _, browser, _ = automation

    def publish(platform, payload, connection, before_submit):
        before_submit()
        raise RuntimeError("connection lost after click")

    browser.publish = publish
    run_id = start(client, create(client))
    jobs.execute_run(run_id, ALICE)
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert result["status"] == "unknown"
    assert result["platform_results"]["xianyu"]["status"] == "unknown"
    assert client.post(f"{BASE}/runs/{run_id}/retry", headers=HEADERS).status_code == 409
    assert client.post(f"{BASE}/runs/{run_id}/publish", headers=HEADERS).status_code == 409


def test_recover_expired_lease_and_preserve_success(automation):
    client, factory, _, _, _ = automation
    run_id = start(client, create(client))
    with factory() as session:
        row = session.get(AutomationRun, run_id)
        row.status = "publishing"
        row.lease_token = "lost-worker"
        row.lease_expires_at = utcnow() - timedelta(minutes=1)
        row.platform_results_json = jobs.encoded({"xianyu": {"status": "succeeded", "receipt": {"url": "receipt"}}, "douyin": {"status": "submitting"}})
        session.commit()
    assert jobs.recover_interrupted() == 1
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert result["status"] == "unknown"
    assert result["platform_results"]["xianyu"]["status"] == "succeeded"
    assert result["platform_results"]["douyin"]["status"] == "unknown"
    assert client.post(f"{BASE}/runs/{run_id}/retry", headers=HEADERS).status_code == 409


def test_manual_draft_then_explicit_publish(automation):
    client, _, _, _, calls = automation
    run_id = start(client, create(client, CONFIG | {"auto_publish": False}))
    jobs.execute_run(run_id, ALICE)
    assert calls == []
    assert client.get(f"{BASE}/runs/{run_id}").json()["status"] == "ready"
    result = client.put(f"{BASE}/runs/{run_id}/draft", headers=HEADERS, json={
        "title": "修订标题", "description": "修订文案", "category": "家居", "price": "89",
    })
    assert result.status_code == 200
    assert result.json()["draft"]["title"] == "修订标题"
    assert client.post(f"{BASE}/runs/{run_id}/publish", headers=HEADERS).status_code == 202
    jobs.execute_run(run_id, ALICE)
    assert calls == ["xianyu"]
    assert client.get(f"{BASE}/runs/{run_id}").json()["status"] == "succeeded"


@pytest.mark.parametrize("platforms,mode", [
    (["xianyu"], "browser"),
    (["douyin"], "browser"),
    (["xiaohongshu"], "browser"),
    (["kuaishou"], "browser"),
    (["douyin", "xiaohongshu", "kuaishou"], "browser"),
])
def test_unbound_offline_workflow_can_save_generate_and_edit_draft(
    automation, monkeypatch, tmp_path, platforms, mode
):
    from content_service import automation_android, automation_executor

    client, factory, _, browser, calls = automation
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path / "unconfigured-content"))

    def unexpected_platform_call(*args, **kwargs):
        pytest.fail("Draft preparation must not inspect accounts, connect, or publish")

    monkeypatch.setattr(automation_executor, "preflight", unexpected_platform_call)
    monkeypatch.setattr(automation_executor, "execute", unexpected_platform_call)
    monkeypatch.setattr(automation_android, "_load_config", unexpected_platform_call)
    monkeypatch.setattr(browser, "status", unexpected_platform_call)
    monkeypatch.setattr(browser, "publish", unexpected_platform_call)
    content = __import__("sys").modules["content_service.automation_content"]
    generated = []

    def prepare(config, identity):
        generated.append(config)
        assert config["accounts"] == {} and config["device_id"] is None
        assert config["auto_publish"] is False
        assert identity.user_id == "alice"
        return dict(PAYLOAD)

    monkeypatch.setattr(content, "prepare_content", prepare)
    selected = CONFIG | {
        "platforms": platforms, "execution_mode": mode, "accounts": {},
        "device_id": None, "auto_publish": False,
    }
    workflow_id = create(client, selected)
    saved = client.put(f"{BASE}/workflows/{workflow_id}", headers=HEADERS,
        json=selected | {"name": "Prepare before login"})
    assert saved.status_code == 200
    run_id = start(client, workflow_id)
    jobs.execute_run(run_id, ALICE)
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert result["status"] == "ready" and result["draft"]["title"] == PAYLOAD["title"]
    assert len(generated) == 1 and not calls
    assert all(value["status"] == "pending" for value in result["platform_results"].values())
    assert not any(event["kind"] in {"publishing", "submitting"} for event in result["events"])
    edited = client.put(f"{BASE}/runs/{run_id}/draft", headers=HEADERS, json={
        "title": "Prepared copy", "description": "Reviewed before logging in", "category": "家居",
        **({"price": "99.00"} if platforms == ["xianyu"] else {}),
    })
    assert edited.status_code == 200 and edited.json()["status"] == "ready"
    with factory() as session:
        row = session.get(AutomationRun, run_id)
        assert not row.publish_requested
        assert json.loads(row.config_json)["accounts"] == {}


def test_cooperative_cancel_prevents_submit_boundary(automation):
    client, factory, _, browser, calls = automation
    run_id = start(client, create(client))

    def publish(platform, payload, connection, before_submit):
        with factory() as session:
            row = session.get(AutomationRun, run_id)
            row.cancel_requested = True
            session.commit()
        before_submit()
        calls.append(platform)

    browser.publish = publish
    jobs.execute_run(run_id, ALICE)
    assert calls == []
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert result["status"] == "cancelled"
    with factory() as session:
        assert not session.scalars(select(AutomationEvent).where(AutomationEvent.run_id == run_id, AutomationEvent.kind == "submitting")).all()


def test_concurrent_workers_only_one_claim(automation, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    client, _, _, _, calls = automation
    content = __import__("sys").modules["content_service.automation_content"]
    entered, release = Event(), Event()
    preparations = []

    def prepare(config, identity):
        preparations.append(identity.user_id)
        entered.set()
        assert release.wait(10)
        return dict(PAYLOAD)

    monkeypatch.setattr(content, "prepare_content", prepare)
    run_id = start(client, create(client))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(jobs.execute_run, run_id, ALICE)
        assert entered.wait(10)
        second = pool.submit(jobs.execute_run, run_id, ALICE)
        second.result(timeout=10)
        release.set()
        first.result(timeout=10)
    assert preparations == ["alice"]
    assert calls == ["xianyu"]


def test_adapter_missing_submission_marker_is_not_safe_to_retry(automation):
    client, _, _, browser, _ = automation
    browser.publish = lambda *args, **kwargs: {"status": "published"}
    run_id = start(client, create(client))
    jobs.execute_run(run_id, ALICE)
    assert client.get(f"{BASE}/runs/{run_id}").json()["status"] == "unknown"
    assert client.post(f"{BASE}/runs/{run_id}/retry", headers=HEADERS).status_code == 409


def test_content_failure_can_retry_without_publishing_twice(automation, monkeypatch):
    client, _, _, _, calls = automation
    content = __import__("sys").modules["content_service.automation_content"]

    def fail(config, identity):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(content, "prepare_content", fail)
    run_id = start(client, create(client))
    jobs.execute_run(run_id, ALICE)
    assert calls == []
    assert client.get(f"{BASE}/runs/{run_id}").json()["status"] == "failed"
    monkeypatch.setattr(content, "prepare_content", lambda config, identity: dict(PAYLOAD))
    assert client.post(f"{BASE}/runs/{run_id}/retry", headers=HEADERS).status_code == 202
    jobs.execute_run(run_id, ALICE)
    assert client.get(f"{BASE}/runs/{run_id}").json()["status"] == "succeeded"
    assert calls == ["xianyu"]


def test_migration_creates_tables_and_idempotency_constraint(tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    service = Path(__file__).resolve().parents[1] / "services" / "content_service"
    config = Config(str(service / "alembic.ini"))
    database_url = f"sqlite:///{tmp_path / 'migrations.db'}"
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert {"automation_workflows", "automation_runs", "automation_events"} <= set(inspector.get_table_names())
        assert any(value["column_names"] == ["owner_id", "idempotency_key"] for value in inspector.get_unique_constraints("automation_runs"))
    finally:
        engine.dispose()


def test_expired_worker_cannot_submit_after_recovery_and_retry(automation, monkeypatch):
    """Expire the lease after the boundary SELECT, before its durable UPDATE."""
    client, factory, _, browser, _ = automation
    run_id = start(client, create(client))
    original_write = jobs._write_active
    interrupted = False
    clicked = []

    def publish(platform, payload, connection, before_submit):
        before_submit()
        clicked.append(platform)
        return {"status": "published", "url": "https://example.com/receipt"}

    def racing_write(session, row, token, values, *, submitting=False):
        nonlocal interrupted
        if submitting and not interrupted:
            interrupted = True
            with factory() as other:
                current = other.get(AutomationRun, run_id)
                current.lease_expires_at = utcnow() - timedelta(seconds=1)
                other.commit()
            assert jobs.recover_interrupted() == 1
            assert client.post(f"{BASE}/runs/{run_id}/retry", headers=HEADERS).status_code == 202
            jobs.execute_run(run_id, ALICE)
        return original_write(session, row, token, values, submitting=submitting)

    monkeypatch.setattr(jobs, "_write_active", racing_write)
    browser.publish = publish
    jobs.execute_run(run_id, ALICE)
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert interrupted
    assert clicked == ["xianyu"]
    assert result["status"] == "succeeded"
    assert result["attempt"] == 2
    assert len([event for event in result["events"] if event["kind"] == "submitting"]) == 1


def test_stale_final_write_cannot_overwrite_recovered_unknown(automation, monkeypatch):
    client, factory, _, _, _ = automation
    run_id = start(client, create(client))
    original_write = jobs._write_active
    interrupted = False

    def racing_write(session, row, token, values, *, submitting=False):
        nonlocal interrupted
        results = json.loads(values.get("platform_results_json", "{}"))
        if not interrupted and any(value.get("status") == "succeeded" for value in results.values()):
            interrupted = True
            with factory() as other:
                current = other.get(AutomationRun, run_id)
                current.lease_expires_at = utcnow() - timedelta(seconds=1)
                other.commit()
            assert jobs.recover_interrupted() == 1
        return original_write(session, row, token, values, submitting=submitting)

    monkeypatch.setattr(jobs, "_write_active", racing_write)
    jobs.execute_run(run_id, ALICE)
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert interrupted
    assert result["status"] == "unknown"
    assert result["platform_results"]["xianyu"]["status"] == "unknown"
    assert client.post(f"{BASE}/runs/{run_id}/retry", headers=HEADERS).status_code == 409


MANAGEMENT_CONFIG = {
    "name": "闲鱼商品下架", "operation": "delist", "platforms": ["xianyu"], "execution_mode": "browser",
    "accounts": {"xianyu": "my-account"},
    "target_url": "https://www.goofish.com/item?id=123456789", "target_title": "已有商品的精确标题",
}


@pytest.mark.parametrize("operation,receipt_status", [("relist", "listed"), ("delist", "delisted")])
def test_manage_listing_uses_exact_target_without_model(automation, monkeypatch, operation, receipt_status):
    client, _, _, browser, _ = automation
    content = __import__("sys").modules["content_service.automation_content"]
    monkeypatch.setattr(content, "prepare_content", lambda *_: pytest.fail("management must not call model"))
    calls = []

    def manage_listing(platform, selected_operation, payload, connection, before_submit):
        assert platform == "xianyu"
        assert selected_operation == operation
        assert payload == {"target_url": MANAGEMENT_CONFIG["target_url"], "title": MANAGEMENT_CONFIG["target_title"]}
        assert connection == {"account_id": "my-account"}
        before_submit()
        calls.append(selected_operation)
        return {"status": receipt_status, "url": payload["target_url"], "account_label": "我的闲鱼"}

    browser.manage_listing = manage_listing
    workflow_id = create(client, MANAGEMENT_CONFIG | {"operation": operation})
    run_id = start(client, workflow_id, "management-request")
    jobs.execute_run(run_id, ALICE)
    jobs.execute_run(run_id, ALICE)
    assert start(client, workflow_id, "management-request") == run_id
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert result["status"] == "succeeded"
    assert result["platform_results"]["xianyu"]["operation"] == operation
    assert result["platform_results"]["xianyu"]["receipt"]["status"] == receipt_status
    assert calls == [operation]
    assert result["draft"]["title"] == MANAGEMENT_CONFIG["target_title"]


@pytest.mark.parametrize("updates", [
    {"target_title": ""}, {"target_title": "多行\n标题"},
    {"target_url": "http://www.goofish.com/item?id=123"},
    {"target_url": "https://evil.example/item?id=123"},
    {"target_url": "https://www.goofish.com@evil.example/item?id=123"},
    {"target_url": "https://www.goofish.com/item?id=123&id=456"},
    {"target_url": "https://www.goofish.com/item?id=123&redirect=https://evil.example"},
    {"target_url": "https://www.goofish.com/item?id=123#other"},
    {"target_url": "https://www.goofish.com/item?id=not-a-number"},
    {"target_url": "https://www.goofish.com:8000/item?id=123"},
    {"target_url": "https://user:pass@www.goofish.com/item?id=123"},
    {"platforms": ["douyin"], "accounts": {"douyin": "my-account"}},
    {"platforms": ["xianyu", "douyin"]}, {"auto_publish": False},
])
def test_management_rejects_ambiguous_target_and_scope(automation, updates):
    client, _, _, _, _ = automation
    result = client.post(BASE + "/workflows", headers=HEADERS, json=MANAGEMENT_CONFIG | updates)
    assert result.status_code == 422


def test_management_cannot_be_edited_or_manually_published_as_draft(automation):
    client, factory, _, _, _ = automation
    run_id = start(client, create(client, MANAGEMENT_CONFIG))
    with factory() as session:
        row = session.get(AutomationRun, run_id)
        row.status = "ready"
        session.commit()
    assert client.put(f"{BASE}/runs/{run_id}/draft", headers=HEADERS, json={
        "title": "另一个商品", "description": "不要修改目标", "category": "商品", "price": "88",
    }).status_code == 409
    assert client.post(f"{BASE}/runs/{run_id}/publish", headers=HEADERS).status_code == 409


def test_management_unknown_cannot_retry_and_wrong_receipt_is_unknown(automation):
    client, _, _, browser, _ = automation

    def wrong_receipt(platform, operation, payload, connection, before_submit):
        before_submit()
        return {"status": "published", "url": payload["target_url"]}

    browser.manage_listing = wrong_receipt
    run_id = start(client, create(client, MANAGEMENT_CONFIG))
    jobs.execute_run(run_id, ALICE)
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert result["status"] == "unknown"
    assert result["platform_results"]["xianyu"]["operation"] == "delist"
    assert client.post(f"{BASE}/runs/{run_id}/retry", headers=HEADERS).status_code == 409


def test_management_block_before_submission_is_safe_to_retry(automation):
    client, _, _, browser, _ = automation
    submissions = []

    def blocked(*args, **kwargs):
        raise RuntimeError("source status does not match")

    browser.manage_listing = blocked
    run_id = start(client, create(client, MANAGEMENT_CONFIG))
    jobs.execute_run(run_id, ALICE)
    assert client.get(f"{BASE}/runs/{run_id}").json()["status"] == "failed"

    def recovered(platform, operation, payload, connection, before_submit):
        before_submit()
        submissions.append(operation)
        return {"status": "delisted", "url": payload["target_url"]}

    browser.manage_listing = recovered
    assert client.post(f"{BASE}/runs/{run_id}/retry", headers=HEADERS).status_code == 202
    jobs.execute_run(run_id, ALICE)
    assert submissions == ["delist"]
    assert client.get(f"{BASE}/runs/{run_id}").json()["status"] == "succeeded"


@pytest.mark.parametrize("url", [
    "https://www.goofish.com/item?id=123456789&categoryId=500100",
    "https://goofish.com/item?categoryId=500100&id=123456789&spm=a21ybx.shop.0.0.abC_123-xyz",
    "https://www.goofish.com/item?id=123456789&spm=a21ybx.shop",
    "https://www.goofish.com/item?id=123456789&categoryId=&spm=a21ybx.publish.0.0",
])
def test_management_normalizes_observed_listing_links(automation, url):
    client, factory, _, browser, _ = automation
    workflow_id = create(client, MANAGEMENT_CONFIG | {"target_url": url})
    workflow = client.get(f"{BASE}/workflows/{workflow_id}").json()
    assert workflow["config"]["target_url"] == MANAGEMENT_CONFIG["target_url"]
    seen = []

    def manage_listing(platform, operation, payload, connection, before_submit):
        seen.append(payload["target_url"])
        before_submit()
        return {"status": "delisted", "url": payload["target_url"]}

    browser.manage_listing = manage_listing
    run_id = start(client, workflow_id)
    jobs.execute_run(run_id, ALICE)
    assert seen == [MANAGEMENT_CONFIG["target_url"]]
    with factory() as session:
        assert "categoryId" not in session.get(AutomationRun, run_id).config_json
        assert "spm" not in session.get(AutomationRun, run_id).payload_json


@pytest.mark.parametrize("suffix", [
    "&categoryId=bad", "&categoryId=123&categoryId=123",
    "&spm=", "&spm=a&spm=b", "&spm=https://evil.example", "&other=value",
    "&spm=a%0Ab", "&categoryId=12&redirect=https://evil.example", "&spm",
])
def test_management_rejects_invalid_tracking_parameters(automation, suffix):
    client, _, _, _, _ = automation
    response = client.post(BASE + "/workflows", headers=HEADERS, json=MANAGEMENT_CONFIG | {
        "target_url": MANAGEMENT_CONFIG["target_url"] + suffix,
    })
    assert response.status_code == 422


CUSTOM_CONFIG = CONFIG | {
    "source_mode": "custom", "custom_product_name": "自定义茶杯",
    "custom_product_description": "玻璃材质，容量 200 毫升，无包装。", "product_id": "stale-product",
}


def test_custom_product_workflow_does_not_require_library_product(automation):
    client, factory, _, _, _ = automation
    workflow_id = create(client, CUSTOM_CONFIG)
    saved = client.get(f"{BASE}/workflows/{workflow_id}").json()["config"]
    assert saved["product_id"] is None
    assert saved["source_mode"] == "custom"
    assert saved["custom_product_name"] == "自定义茶杯"
    run_id = start(client, workflow_id)
    with factory() as session:
        snapshot = json.loads(session.get(AutomationRun, run_id).config_json)
    assert snapshot == saved


@pytest.mark.parametrize("updates", [
    {"custom_product_name": "   "}, {"custom_product_description": "\n\t "},
    {"custom_product_name": "名" * 121}, {"custom_product_description": "文" * 4001},
    {"image_ids": []}, {"source_mode": "unknown"},
    {"image_ids": [], "platforms": ["douyin"], "video_mode": "existing", "video_id": "video"},
])
def test_custom_product_requires_bounded_facts_and_selected_images(automation, updates):
    client, _, _, _, _ = automation
    response = client.post(BASE + "/workflows", headers=HEADERS, json=CUSTOM_CONFIG | updates)
    assert response.status_code == 422


def test_library_mode_remains_default_for_existing_workflows(automation):
    client, _, _, _, _ = automation
    workflow_id = create(client)
    saved = client.get(f"{BASE}/workflows/{workflow_id}").json()["config"]
    assert saved["source_mode"] == "library"
    assert saved["custom_product_name"] == ""
    assert saved["image_ids"] == CONFIG["image_ids"]


@pytest.mark.parametrize("updates", [
    {"platforms": ["xianyu", "douyin"]},
    {"purpose": "promotion", "platforms": ["xianyu"]},
    {"purpose": "listing", "platforms": ["douyin"]},
    {"purpose": "promotion", "platforms": ["douyin"], "operation": "delist"},
])
def test_workflow_rejects_cross_business_platforms_and_operations(automation, updates):
    client, _, _, _, _ = automation
    assert client.post(BASE + "/workflows", json=CONFIG | updates, headers=HEADERS).status_code == 422


def test_promotion_normalizes_transaction_fields_and_draft_cannot_restore_them(automation):
    client, _, _, _, calls = automation
    workflow_id = create(client, CONFIG | {"platforms": ["douyin"], "auto_publish": False,
        "condition": "九成新", "shipping_fee": "6", "location": "上海", "purpose": "promotion"})
    config_value = client.get(f"{BASE}/workflows/{workflow_id}").json()["config"]
    assert config_value["purpose"] == "promotion"
    assert {key: config_value[key] for key in jobs.TRANSACTION_FIELDS} == {
        "price": None, "condition": "", "shipping_fee": "0", "location": "",
    }
    run_id = start(client, workflow_id)
    jobs.execute_run(run_id, ALICE)
    response = client.put(f"{BASE}/runs/{run_id}/draft", headers=HEADERS, json={
        "title": "视频宣传标题", "description": "视频宣传内容", "category": "家居", "price": "999",
    })
    assert response.status_code == 200
    assert not set(response.json()["draft"]) & jobs.TRANSACTION_FIELDS
    assert client.post(f"{BASE}/runs/{run_id}/publish", headers=HEADERS).status_code == 202
    jobs.execute_run(run_id, ALICE)
    assert calls == ["douyin"]


def test_legacy_mixed_configuration_is_readable_but_cannot_execute_or_be_overwritten(automation):
    client, factory, _, _, calls = automation
    workflow_id = create(client)
    run_id = start(client, workflow_id)
    mixed = CONFIG | {"platforms": ["xianyu", "douyin"]}
    receipts = {"xianyu": {"status": "succeeded", "receipt": {"url": "existing"}}, "douyin": {"status": "failed"}}
    with factory() as session:
        session.get(AutomationWorkflow, workflow_id).config_json = jobs.encoded(mixed)
        row = session.get(AutomationRun, run_id)
        row.config_json, row.status = jobs.encoded(mixed), "partial"
        row.platform_results_json = jobs.encoded(receipts)
        session.commit()
    assert client.get(f"{BASE}/workflows/{workflow_id}").status_code == 200
    assert client.get(f"{BASE}/runs/{run_id}").status_code == 200
    response = client.post(f"{BASE}/workflows/{workflow_id}/runs", headers={**HEADERS, "Idempotency-Key": "new"})
    assert response.status_code == 409 and "拆分" in response.json()["detail"]
    assert client.put(f"{BASE}/workflows/{workflow_id}", json=CONFIG, headers=HEADERS).status_code == 409
    assert client.post(f"{BASE}/runs/{run_id}/retry", headers=HEADERS).status_code == 409
    assert client.post(f"{BASE}/runs/{run_id}/publish", headers=HEADERS).status_code == 409
    jobs.execute_run(run_id, ALICE)
    with factory() as session:
        row = session.get(AutomationRun, run_id)
        assert row.status == "partial"
        assert json.loads(row.platform_results_json) == receipts
    assert calls == []


def test_worker_stops_legacy_queued_mixed_run_before_content_or_browser(automation, monkeypatch):
    client, factory, _, _, calls = automation
    workflow_id = create(client)
    run_id = start(client, workflow_id)
    with factory() as session:
        row = session.get(AutomationRun, run_id)
        row.config_json = jobs.encoded(CONFIG | {"platforms": ["xianyu", "douyin"]})
        session.commit()
    content = __import__("sys").modules["content_service.automation_content"]
    monkeypatch.setattr(content, "prepare_content", lambda *_: pytest.fail("mixed workflow may not generate"))
    jobs.execute_run(run_id, ALICE)
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert result["status"] == "failed"
    assert "拆分" in result["error"]
    assert calls == []


def test_legacy_single_promotion_run_is_normalized_before_browser(automation):
    client, factory, _, browser, _ = automation
    workflow_id = create(client, CONFIG | {"platforms": ["douyin"]})
    run_id = start(client, workflow_id)
    with factory() as session:
        row = session.get(AutomationRun, run_id)
        row.config_json = jobs.encoded(CONFIG | {"platforms": ["douyin"], "execution_mode": "browser"})
        row.payload_json = jobs.encoded(PAYLOAD)
        session.commit()
    seen = []

    def publish(platform, payload, connection, before_submit):
        seen.append(dict(payload))
        assert platform == "douyin"
        assert not set(payload) & jobs.TRANSACTION_FIELDS
        before_submit()
        return {"status": "published"}

    browser.publish = publish
    jobs.execute_run(run_id, ALICE)
    assert len(seen) == 1
    with factory() as session:
        row = session.get(AutomationRun, run_id)
        assert json.loads(row.config_json)["purpose"] == "promotion"
        assert not set(json.loads(row.payload_json)) & jobs.TRANSACTION_FIELDS


@pytest.mark.parametrize("platforms,mode,expected", [
    (["douyin", "xiaohongshu", "kuaishou"], "android", "browser"),
    (["xianyu"], "android", "browser"),
])
def test_explicit_save_uses_business_destination_and_drops_old_account_binding(automation, platforms, mode, expected):
    client, _, _, _, _ = automation
    selected = CONFIG | {"platforms": platforms, "execution_mode": mode,
        "device_id": "old-device" if mode == "android" else None,
        "accounts": {platforms[0]: "old-account"}}
    workflow_id = create(client, selected)
    saved = client.get(f"{BASE}/workflows/{workflow_id}").json()["config"]
    assert saved["execution_mode"] == expected
    assert saved["accounts"] == {} and saved["device_id"] is None


@pytest.mark.parametrize("platform,purpose", [("douyin", "promotion"), ("xianyu", "listing")])
def test_old_android_run_remains_readable_but_cannot_execute_after_mode_switch(automation, monkeypatch, platform, purpose):
    client, factory, _, _, calls = automation
    workflow_id = create(client, CONFIG | {"platforms": [platform], "auto_publish": False})
    run_id = start(client, workflow_id)
    old = CONFIG | {"platforms": [platform], "execution_mode": "android", "device_id": "server", "accounts": {platform: "android-account"}}
    with factory() as session:
        session.get(AutomationWorkflow, workflow_id).config_json = jobs.encoded(old)
        row = session.get(AutomationRun, run_id)
        row.config_json, row.status, row.payload_json = jobs.encoded(old), "ready", jobs.encoded(PAYLOAD)
        session.commit()
    assert client.get(BASE + "/runs?purpose=" + purpose).json()["total"] == 1
    assert client.post(f"{BASE}/runs/{run_id}/publish", headers=HEADERS).status_code == 409
    assert client.post(f"{BASE}/workflows/{workflow_id}/runs", headers={**HEADERS, "Idempotency-Key": "old-mode"}).status_code == 409
    response = client.put(f"{BASE}/workflows/{workflow_id}", headers=HEADERS, json=old)
    assert response.status_code == 200
    assert response.json()["config"]["execution_mode"] == "browser"
    assert response.json()["config"]["accounts"] == {}
    assert client.get(f"{BASE}/runs/{run_id}").json()["config"]["execution_mode"] == "android"
    assert not calls


@pytest.mark.parametrize("auto_publish", [False, True])
@pytest.mark.parametrize("platform", ["douyin", "xianyu"])
def test_old_android_queue_never_generates_or_publishes(automation, monkeypatch, auto_publish, platform):
    client, factory, _, _, calls = automation
    flow = create(client, CONFIG | {"platforms": [platform], "auto_publish": auto_publish})
    run_id = start(client, flow)
    old = CONFIG | {"platforms": [platform], "execution_mode": "android", "auto_publish": auto_publish}
    with factory() as session:
        session.get(AutomationRun, run_id).config_json = jobs.encoded(old)
        session.commit()
    content = __import__("sys").modules["content_service.automation_content"]
    monkeypatch.setattr(content, "prepare_content", lambda *_: pytest.fail("old destination must not generate"))

    jobs.execute_run(run_id, ALICE)

    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert result["status"] == "failed" and result["draft"] == {}
    assert result["config"]["execution_mode"] == "android" and not calls


def test_workflow_cannot_change_business_purpose_in_place(automation):
    client, _, _, _, _ = automation
    workflow_id = create(client)
    response = client.put(f"{BASE}/workflows/{workflow_id}", json=CONFIG | {"platforms": ["douyin"]}, headers=HEADERS)
    assert response.status_code == 409
    assert "独立工作流" in response.json()["detail"]
    assert client.get(f"{BASE}/workflows/{workflow_id}").json()["config"]["purpose"] == "listing"


def test_purpose_filter_precedes_limit_and_counts_owner_scoped_matches(automation):
    client, factory, app, _, _ = automation
    listing = create(client)
    listing_run = start(client, listing)
    promotion = create(client, CONFIG | {"platforms": ["douyin"]})
    for _ in range(3):
        start(client, promotion)
    legacy = create(client)
    legacy_run = start(client, legacy)
    with factory() as session:
        mixed = jobs.encoded(CONFIG | {"platforms": ["xianyu", "douyin"]})
        session.get(AutomationWorkflow, legacy).config_json = mixed
        session.get(AutomationRun, legacy_run).config_json = mixed
        # Simulate an old single-platform workflow with no explicit purpose.
        session.get(AutomationWorkflow, listing).config_json = jobs.encoded(CONFIG)
        session.get(AutomationRun, listing_run).config_json = jobs.encoded(CONFIG)
        session.commit()
    app.dependency_overrides[routes.get_identity] = lambda: BOB
    create(client)
    app.dependency_overrides[routes.get_identity] = lambda: ALICE
    workflows = client.get(BASE + "/workflows?purpose=listing&limit=1").json()
    assert workflows["total"] == 1 and [row["id"] for row in workflows["items"]] == [listing]
    listing_runs = client.get(BASE + "/runs?purpose=listing&limit=1").json()
    assert listing_runs["total"] == 1 and [row["id"] for row in listing_runs["items"]] == [listing_run]
    promotion_runs = client.get(BASE + "/runs?purpose=promotion&limit=1").json()
    assert promotion_runs["total"] == 3 and len(promotion_runs["items"]) == 1
    legacy_runs = client.get(BASE + "/runs?purpose=legacy&limit=1").json()
    assert legacy_runs["total"] == 1 and legacy_runs["items"][0]["id"] == legacy_run
    assert client.get(f"{BASE}/runs?purpose=promotion&workflow_id={listing}").json() == {"items": [], "total": 0}
    assert client.get(BASE + "/workflows?purpose=unknown").status_code == 422


def test_android_platform_catalog_and_multi_platform_promotion_config(automation):
    client, _, _, _, _ = automation
    catalog = client.get(BASE + "/platforms").json()["items"]
    assert {spec["id"] for spec in catalog} == {"xianyu", "douyin", "xiaohongshu", "kuaishou"}
    assert next(spec for spec in catalog if spec["id"] == "xiaohongshu")["execution_modes"] == ["browser"]
    workflow_id = create(client, CONFIG | {
        "platforms": ["douyin", "xiaohongshu", "kuaishou"], "execution_mode": "android", "auto_publish": False,
    })
    saved = client.get(f"{BASE}/workflows/{workflow_id}").json()["config"]
    assert saved["purpose"] == "promotion"
    assert saved["device_id"] is None
    assert saved["price"] is None


@pytest.mark.parametrize("updates", [
    {"execution_mode": "remote"},
    {"execution_mode": "android", "device_id": "127.0.0.1:5555"},
    {"execution_mode": "android", "device_id": "../device"},
    {"execution_mode": "android", "device_id": "x" * 65},
    {"execution_mode": "android", "platforms": ["xianyu", "xiaohongshu"]},
    {"execution_mode": "android", "platforms": ["xiaohongshu", "xiaohongshu"]},
    {"execution_mode": "android", "platforms": ["kuaishou"], "video_mode": "existing"},
])
def test_platform_executor_and_device_validation(automation, updates):
    client, _, _, _, _ = automation
    assert client.post(BASE + "/workflows", headers=HEADERS, json=CONFIG | updates).status_code == 422


def test_android_management_can_save_unbound_structure(automation):
    client, _, _, _, _ = automation
    workflow_id = create(client, MANAGEMENT_CONFIG | {"execution_mode": "android", "accounts": {}})
    config = client.get(f"{BASE}/workflows/{workflow_id}").json()["config"]
    assert config["device_id"] is None and config["accounts"] == {}
    assert config["auto_publish"] is True


def test_android_draft_skips_device_checks_but_publish_preflights_before_adapter(automation, monkeypatch):
    client, _, _, browser, calls = automation
    from content_service import automation_executor as executor
    checks = []

    def blocked(config):
        checks.append(config["execution_mode"])
        raise BrowserPublishError("android_adapter_missing", "安卓 App 适配器尚未完成，不能发布")

    monkeypatch.setattr(executor, "preflight", blocked)
    browser.publish = lambda *_args, **_kwargs: pytest.fail("Android must never use browser fallback")
    workflow_id = create(client, CONFIG | {
        "platforms": ["xianyu"], "execution_mode": "android", "auto_publish": False,
    })
    run_id = start(client, workflow_id)
    jobs.execute_run(run_id, ALICE)
    assert client.get(f"{BASE}/runs/{run_id}").json()["status"] == "ready"
    assert not checks and not calls
    assert client.post(f"{BASE}/runs/{run_id}/publish", headers=HEADERS).status_code == 202
    jobs.execute_run(run_id, ALICE)
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert result["status"] == "failed"
    assert "适配器尚未完成" in result["error"]
    assert checks == ["browser"] and not calls
    assert not any(event["kind"] == "submitting" for event in result["events"])


@pytest.mark.parametrize("operation", ["publish", "relist", "delist"])
def test_android_auto_execution_preflights_before_content_or_submit(automation, monkeypatch, operation):
    client, _, _, _, calls = automation
    from content_service import automation_content as content
    from content_service import automation_executor as executor

    def blocked(config):
        raise BrowserPublishError("android_device_missing", "请先配置安卓设备")

    monkeypatch.setattr(executor, "preflight", blocked)
    monkeypatch.setattr(content, "prepare_content", lambda *_: pytest.fail("blocked executor must not call model"))
    selected = (CONFIG if operation == "publish" else MANAGEMENT_CONFIG) | {"operation": operation, "execution_mode": "android"}
    run_id = start(client, create(client, selected))
    jobs.execute_run(run_id, ALICE)
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert result["status"] == "failed" and result["error"] == "请先配置安卓设备"
    assert result["draft"] == {} and not calls
    assert not any(event["kind"] == "submitting" for event in result["events"])


def test_multi_platform_dispatch_keeps_device_snapshot_and_each_submit_receipt(automation, monkeypatch):
    client, _, _, _, browser_calls = automation
    from content_service import automation_executor as executor
    dispatches, checks = [], []
    monkeypatch.setattr(executor, "preflight", lambda config: checks.append(config["device_id"]))

    def execute(platform, operation, payload, connection, *, execution_mode, device_id, before_submit):
        dispatches.append((platform, execution_mode, device_id, connection["account_id"]))
        before_submit()
        return {"status": "published", "external_id": "app-item-" + platform, "url": "https://example.com/" + platform}

    monkeypatch.setattr(executor, "execute", execute)
    run_id = start(client, create(client, CONFIG | {
        "platforms": ["douyin", "xiaohongshu", "kuaishou"], "execution_mode": "browser",
        "device_id": None, "accounts": {"xiaohongshu": "red-account"},
    }))
    jobs.execute_run(run_id, ALICE)
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert result["status"] == "succeeded" and not browser_calls
    assert checks == [None] * 4
    assert dispatches == [("douyin", "browser", None, ""),
        ("xiaohongshu", "browser", None, "red-account"), ("kuaishou", "browser", None, "")]
    assert all(value["status"] == "succeeded" for value in result["platform_results"].values())
    for platform, value in result["platform_results"].items():
        assert value["receipt"]["external_id"] == "app-item-" + platform
    assert len([event for event in result["events"] if event["kind"] == "submitting"]) == 3


def test_multi_platform_draft_applies_strictest_title_limit(automation):
    client, _, _, _, _ = automation
    run_id = start(client, create(client, CONFIG | {
        "platforms": ["douyin", "xiaohongshu"], "execution_mode": "android", "auto_publish": False,
    }))
    jobs.execute_run(run_id, ALICE)
    draft = {"title": "字" * 21, "description": "商品真实介绍", "category": "家居"}
    assert client.put(f"{BASE}/runs/{run_id}/draft", headers=HEADERS, json=draft).status_code == 422
    response = client.put(f"{BASE}/runs/{run_id}/draft", headers=HEADERS, json=draft | {"title": "字" * 20})
    assert response.status_code == 200 and response.json()["draft"]["title"] == "字" * 20


def test_multi_platform_unknown_stops_remaining_targets_and_preserves_receipts(automation, monkeypatch):
    client, _, _, _, browser_calls = automation
    from content_service import automation_executor as executor
    dispatches = []
    monkeypatch.setattr(executor, "preflight", lambda _: None)

    def execute(platform, operation, payload, connection, *, execution_mode, device_id, before_submit):
        dispatches.append(platform)
        before_submit()
        if platform == "xiaohongshu":
            raise RuntimeError("connection lost after app submission")
        return {"status": "published", "url": "https://example.com/" + platform}

    monkeypatch.setattr(executor, "execute", execute)
    run_id = start(client, create(client, CONFIG | {
        "platforms": ["douyin", "xiaohongshu", "kuaishou"], "execution_mode": "android",
    }))
    jobs.execute_run(run_id, ALICE)
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert result["status"] == "unknown" and not browser_calls
    assert dispatches == ["douyin", "xiaohongshu"]
    assert result["platform_results"]["douyin"]["receipt"]["url"] == "https://example.com/douyin"
    assert result["platform_results"]["xiaohongshu"]["status"] == "unknown"
    assert result["platform_results"]["kuaishou"]["status"] == "pending"
    assert client.post(f"{BASE}/runs/{run_id}/retry", headers=HEADERS).status_code == 409


def test_executor_readiness_is_rechecked_before_submission(automation, monkeypatch):
    client, _, _, _, browser_calls = automation
    from content_service import automation_executor as executor
    checks = []

    def preflight(config):
        checks.append(config["execution_mode"])
        if len(checks) == 2:
            raise BrowserPublishError("android_device_offline", "设备已离线")

    monkeypatch.setattr(executor, "preflight", preflight)
    monkeypatch.setattr(executor, "execute", lambda *_args, **_kwargs: pytest.fail("disconnected device must not submit"))
    run_id = start(client, create(client, CONFIG | {"execution_mode": "android"}))
    jobs.execute_run(run_id, ALICE)
    result = client.get(f"{BASE}/runs/{run_id}").json()
    assert result["status"] == "failed" and not browser_calls
    assert result["platform_results"]["xianyu"]["error"] == "设备已离线"
    assert result["draft"]["title"] == PAYLOAD["title"]
    assert checks == ["browser", "browser"]
    assert not any(event["kind"] == "submitting" for event in result["events"])


def test_executor_status_and_explicit_device_inspection_permissions(automation, monkeypatch):
    client, _, app, _, _ = automation
    android = ModuleType("content_service.automation_android")
    calls = []
    android.status = lambda owner: {"available": False, "devices": []}
    android.inspect_devices = lambda owner: calls.append("inspect") or {"devices": [], "available": False}
    monkeypatch.setitem(__import__("sys").modules, "content_service.automation_android", android)
    result = client.get(BASE + "/executors").json()
    assert result["browser"]["available"] is True and result["android"]["available"] is False
    assert not calls
    assert client.post(BASE + "/android/inspect").status_code == 403
    assert not calls
    assert client.post(BASE + "/android/inspect", headers=HEADERS).status_code == 200
    assert calls == ["inspect"]
    app.dependency_overrides[routes.get_identity] = lambda: Identity("user", "User", "", frozenset({"user"}))
    assert client.get(BASE + "/platforms").status_code == 403
    assert client.get(BASE + "/executors").status_code == 403
    assert client.post(BASE + "/android/inspect", headers=HEADERS).status_code == 403
    assert calls == ["inspect"]


def test_web_and_legacy_xianyu_drafts_cannot_use_android_submission(automation, monkeypatch, tmp_path):
    from content_service import (
        android_routes,
    )

    client, factory, app, _, _ = automation
    app.include_router(android_routes.router)
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path))
    config = {"devices": {"server": {"serial": "emulator-5554", "platforms": ["xianyu"]}}}
    monkeypatch.setattr(android_routes, "_load_config", lambda: config)
    monkeypatch.setattr(android_routes, "execute_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(android_routes.android_publishing, "reserve_submission", lambda *args: pytest.fail("retired Android path must not reserve submission"))
    flow = create(client, CONFIG | {"name": "安卓商品", "purpose": "listing", "image_ids": ["image-1"],
        "platforms": ["xianyu"], "execution_mode": "android", "device_id": "server", "auto_publish": False})
    run_id = start(client, flow)
    jobs.execute_run(run_id, ALICE)
    endpoint = BASE + "/android/devices/server/publish-draft"
    body = {"run_id": run_id, "frame_id": "b" * 32, "media_confirmed": True}
    headers = {**HEADERS, "X-Android-Session": "a" * 32}
    app.dependency_overrides[routes.get_identity] = lambda: BOB
    assert client.post(endpoint, headers=headers, json=body).status_code == 404
    app.dependency_overrides[routes.get_identity] = lambda: ALICE
    assert client.post(endpoint, json=body).status_code in {403, 422}
    assert client.post(endpoint, headers=headers, json={**body, "media_confirmed": False}).status_code == 409
    assert client.post(endpoint, headers=headers, json=body).status_code == 409
    with factory() as session:
        row = session.get(AutomationRun, run_id)
        legacy = json.loads(row.config_json) | {"execution_mode": "android", "device_id": "server"}
        row.config_json = jobs.encoded(legacy)
        session.commit()
    assert client.post(endpoint, headers=headers, json=body).status_code == 409
    assert client.get(f"{BASE}/runs/{run_id}").json()["status"] == "ready"
