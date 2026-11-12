"""Online trial: each visitor may start a limited number of model-billed videos per day."""

from contextvars import ContextVar

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from test_catalog import BASE, HEADERS, catalog, create_product  # noqa: F401

from content_service import demo_quota


@pytest.fixture
def quota(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_DEMO_VIDEO_DAILY_LIMIT", "5")
    monkeypatch.delenv("CONTENT_DEMO_EXEMPT_USERS", raising=False)
    monkeypatch.setattr(demo_quota, "_usage_path", lambda: tmp_path / "demo-usage.json")
    return tmp_path / "demo-usage.json"


def as_visitor(key):
    return demo_quota.visitor.set(key)


def test_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTENT_DEMO_VIDEO_DAILY_LIMIT", raising=False)
    monkeypatch.setattr(demo_quota, "_usage_path", lambda: tmp_path / "demo-usage.json")
    token = as_visitor("demo@1.1.1.1")
    try:
        demo_quota.use_videos(100)
    finally:
        demo_quota.visitor.reset(token)
    assert not (tmp_path / "demo-usage.json").exists()


def test_proxy_header_is_trusted_only_from_the_local_proxy():
    assert demo_quota.visitor_key("127.0.0.1", "demo@203.0.113.7") == "demo@203.0.113.7"
    assert demo_quota.visitor_key("203.0.113.7", "owner@1.2.3.4") == "203.0.113.7"
    assert demo_quota.visitor_key("127.0.0.1", None) == "127.0.0.1"


def test_each_visitor_gets_five_videos_a_day(quota, monkeypatch):
    token = as_visitor("demo@1.1.1.1")
    try:
        demo_quota.use_videos(3)
        demo_quota.check(2)
        with pytest.raises(HTTPException) as refused:
            demo_quota.use_videos(3)
        assert refused.value.status_code == 429 and "今天还能生成 2 条" in refused.value.detail
        demo_quota.use_videos(2)
        with pytest.raises(HTTPException) as done:
            demo_quota.check(1)
        assert "今天的次数已用完" in done.value.detail
    finally:
        demo_quota.visitor.reset(token)

    other = as_visitor("demo@2.2.2.2")  # another person on the same trial login
    try:
        demo_quota.use_videos(5)
    finally:
        demo_quota.visitor.reset(other)

    monkeypatch.setattr(demo_quota, "_today", lambda: "2099-01-01")  # a new day starts fresh
    token = as_visitor("demo@1.1.1.1")
    try:
        demo_quota.use_videos(5)
    finally:
        demo_quota.visitor.reset(token)


def test_owner_login_is_not_limited(quota, monkeypatch):
    monkeypatch.setenv("CONTENT_DEMO_EXEMPT_USERS", "owner")
    token = as_visitor("owner@1.1.1.1")
    try:
        demo_quota.use_videos(50)
    finally:
        demo_quota.visitor.reset(token)


def test_middleware_tags_each_request_with_its_visitor():
    app = FastAPI()
    app.middleware("http")(demo_quota.tag_visitor)

    @app.get("/who")
    def who(request: Request):
        return {"visitor": demo_quota.visitor.get()}

    with TestClient(app) as client:
        # TestClient connects as "testclient", not the local proxy: the header is ignored.
        assert client.get("/who", headers={"X-Demo-Visitor": "owner@1.2.3.4"}).json() == {"visitor": "testclient"}


@pytest.fixture
def one_visitor(monkeypatch):
    # Requests in the catalog fixture all come from one tester.
    monkeypatch.setattr(demo_quota, "visitor", ContextVar("demo_visitor", default="demo@198.51.100.9"))


def test_catalog_video_batches_count_one_video_per_product(catalog, quota, one_visitor):  # noqa: F811
    client, _, _ = catalog
    products = [create_product(client, f"商品{index}")["id"] for index in range(4)]
    first = client.post(f"{BASE}/batches", headers=HEADERS,
                        json={"product_ids": products, "with_copy": False, "with_video": True})
    assert first.status_code == 201, first.text
    second = client.post(f"{BASE}/batches", headers=HEADERS,
                         json={"product_ids": products[:2], "with_copy": False, "with_video": True})
    assert second.status_code == 429 and "今天还能生成 1 条" in second.json()["detail"]
    copy_only = client.post(f"{BASE}/batches", headers=HEADERS, json={"product_ids": products[:2]})
    assert copy_only.status_code == 201, "copy-only batches are not limited"


def test_regenerating_a_video_counts(catalog, quota, one_visitor):  # noqa: F811
    client, _, _ = catalog
    product = create_product(client)["id"]
    client.post(f"{BASE}/batches", headers=HEADERS, json={"product_ids": [product], "with_video": True})
    item = client.get(f"{BASE}/items", params={"status": "review"}).json()["items"][0]
    for _ in range(4):
        response = client.post(f"{BASE}/items/{item['id']}/regenerate", headers=HEADERS,
                               json={"parts": ["video"], "feedback": "再来一版"})
        assert response.status_code == 202, response.text
    refused = client.post(f"{BASE}/items/{item['id']}/regenerate", headers=HEADERS,
                          json={"parts": ["video"], "feedback": "再来一版"})
    assert refused.status_code == 429
