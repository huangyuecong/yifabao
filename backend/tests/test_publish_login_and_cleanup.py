"""Publishing checks the platform login first; failed records can be deleted; logins are watched."""

from fastapi import HTTPException
from test_catalog import BASE, HEADERS, create_product
from test_catalog import catalog as catalog

from content_service import automation_executor, login_monitor, remote_browser


def approved_content(client):
    product = create_product(client)
    client.post(f"{BASE}/batches", headers=HEADERS, json={"product_ids": [product["id"]], "with_copy": True})
    item = client.get(f"{BASE}/items").json()["items"][0]
    client.post(f"{BASE}/items/{item['id']}/approve", headers=HEADERS, json={"revision": 1})
    return client.get(f"{BASE}/contents").json()["items"][0]


def test_signed_out_platform_is_refused_without_a_record(catalog):
    client, published, _ = catalog
    content = approved_content(client)
    login_monitor.forget()
    login_monitor.remember({"platform": "xianyu", "state": "login_required", "message": ""})
    try:
        refused = client.post(f"{BASE}/publish/listing", headers=HEADERS, json={
            "content_ids": [content["id"]], "location": "贵阳市"})
        assert refused.status_code == 409 and "闲鱼还没有登录" in refused.json()["detail"]
        assert client.get(f"{BASE}/records").json()["items"] == []
        # Logged in again: publishing goes ahead.
        login_monitor.remember({"platform": "xianyu", "state": "logged_in", "message": ""})
        assert client.post(f"{BASE}/publish/listing", headers=HEADERS, json={
            "content_ids": [content["id"]], "location": "贵阳市"}).status_code == 202
        assert published
    finally:
        login_monitor.forget()


def test_a_closed_local_browser_does_not_block(catalog):
    client, _, _ = catalog
    content = approved_content(client)
    login_monitor.remember({"platform": "xianyu", "state": "not_opened", "message": ""})
    try:
        assert client.post(f"{BASE}/publish/listing", headers=HEADERS, json={
            "content_ids": [content["id"]], "location": "贵阳市"}).status_code == 202
    finally:
        login_monitor.forget()


def test_failed_records_can_be_deleted(catalog, monkeypatch):
    client, _, _ = catalog
    content = approved_content(client)
    client.post(f"{BASE}/publish/listing", headers=HEADERS, json={"content_ids": [content["id"]], "location": "贵阳市"})
    good = client.get(f"{BASE}/records").json()["items"][0]
    assert good["status"] == "succeeded"
    assert client.delete(f"{BASE}/records/{good['id']}", headers=HEADERS).status_code == 409

    def fail(*args, **kwargs):
        raise HTTPException(503, "浏览器未登录")

    monkeypatch.setattr(automation_executor, "execute", fail)
    for _ in range(3):
        client.post(f"{BASE}/publish/listing", headers=HEADERS, json={"content_ids": [content["id"]], "location": "贵阳市"})
    failed = [row for row in client.get(f"{BASE}/records").json()["items"] if row["status"] == "failed"]
    assert len(failed) == 3
    assert client.delete(f"{BASE}/records/{failed[0]['id']}", headers=HEADERS).status_code == 204
    assert client.post(f"{BASE}/records/delete-failed", headers=HEADERS, json={"workflow": "promotion"}).json() == {"deleted": 0}
    assert client.post(f"{BASE}/records/delete-failed", headers=HEADERS, json={"workflow": "listing"}).json() == {"deleted": 2}
    assert [row["status"] for row in client.get(f"{BASE}/records").json()["items"]] == ["succeeded"]


def test_monitor_notices_a_lost_login(monkeypatch):
    from content_service import automation_browser_session as sessions

    login_monitor.forget()
    login_monitor.remember({"platform": "douyin", "state": "logged_in", "account_label": "抖音号：x"})
    checked = []

    def fake_sessions(platforms):
        checked.append(platforms)
        assert remote_browser.monitoring.get() is True
        return {"items": [{"platform": "douyin", "state": "login_required", "message": "请扫码"}]}

    monkeypatch.setattr(sessions, "sessions", fake_sessions)
    monkeypatch.setattr(login_monitor, "LOCAL_SECONDS", 0)
    try:
        login_monitor.run_once()
        assert checked == [("douyin",)]
        (entry,) = login_monitor.snapshot()
        assert entry["state"] == "login_required" and entry["lost_at"]
        # Nothing logged in: nothing to re-check.
        login_monitor.run_once()
        assert len(checked) == 1
    finally:
        login_monitor.forget()


def test_remote_browsers_closing_drop_their_logins(monkeypatch):
    monkeypatch.setenv("CONTENT_REMOTE_BROWSER", "true")
    monkeypatch.setattr(remote_browser, "running_owners", list)
    login_monitor.remember({"platform": "kuaishou", "state": "logged_in"}, owner="alice")
    try:
        login_monitor.run_once()
        (entry,) = login_monitor.snapshot(owner="alice")
        assert entry["state"] == "not_opened" and entry["lost_at"]
        assert login_monitor.signed_out(["kuaishou"], owner="alice") == ["kuaishou"]
    finally:
        login_monitor.forget(owner="alice")
