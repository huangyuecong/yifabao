"""Exercise local capability separation against the real business routes."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_content_ai_model_config import manager as manager
from test_content_entry_identity import signed as signed
from test_content_entry_identity import token

from content_service import db
from content_service import provider_dependencies as dependencies
from content_service.main import app
from content_service.models import LocalUser


@pytest.fixture
def business(manager):
    # The model fixture supplies an isolated database; no background job lifespan is needed.
    # Import the app before the fixture patches SessionLocal, otherwise the
    # module captures this temporary factory for later background-job tests.
    caches = (dependencies.get_asset_store_port, dependencies.get_knowledge_catalog_port)
    for factory in caches:
        factory.cache_clear()
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        for factory in caches:
            factory.cache_clear()


def headers(subject):
    return {"X-Entry-Identity": token(subject=subject)}


def grant(client, subject, permissions, *, admin=False):
    response = client.get("/api/v1/access/me", headers=headers(subject))
    assert response.status_code == 200, response.text
    with db.SessionLocal() as session:
        user = session.scalar(select(LocalUser).where(LocalUser.subject == subject))
        user.permissions_json = json.dumps(permissions)
        user.is_project_admin = admin
        session.commit()


@pytest.mark.parametrize("permissions", [[], ["models.manage"], ["library.manage"], ["rules.manage"], ["audit.read"]])
def test_each_business_capability_is_independent(business, permissions):
    subject = "scoped-user"
    grant(business, subject, permissions)
    identity = headers(subject)
    owned = business.post("/api/v1/library/items", headers=headers("library-owner"), json={
        "category": "company", "name": "权限隔离资料", "text_content": "示例资料", "data_level": "internal",
    })
    assert owned.status_code == 200, owned.text
    item_id = owned.json()["id"]
    assert business.delete(f"/api/v1/library/items/{item_id}", headers=identity).status_code == (
        200 if "library.manage" in permissions else 403
    )
    rule = business.post("/api/v1/sensitive-rules", headers=identity, json={
        "phrase": "独立权限敏感词", "level": "warning",
    })
    assert rule.status_code == (200 if "rules.manage" in permissions else 403)
    if rule.status_code == 200:
        assert business.delete(f"/api/v1/sensitive-rules/{rule.json()['id']}", headers=identity).status_code == 200
    assert business.get("/api/v1/ai-models", headers=identity).status_code == (
        200 if "models.manage" in permissions else 403
    )
    assert business.get("/api/v1/audit-logs", headers=identity).status_code == (
        200 if "audit.read" in permissions else 403
    )
    assert business.get("/api/v1/access/users", headers=identity).status_code == 403
    me = business.get("/api/v1/access/me", headers=identity).json()
    assert me["permissions"] == permissions and not me["is_project_admin"]


def test_ordinary_owner_keeps_own_library_and_task_operations(business):
    identity = headers("ordinary-owner")
    item = business.post("/api/v1/library/items", headers=identity, json={
        "category": "company", "name": "自己的资料", "text_content": "资料正文",
    })
    assert item.status_code == 200, item.text
    assert item.json()["owner_id"] == "ordinary-owner"
    listed = business.get("/api/v1/library/items", headers=identity)
    assert listed.status_code == 200 and listed.json()[0]["owner_id"] == "ordinary-owner"
    detail = business.get(f"/api/v1/library/items/{item.json()['id']}", headers=identity)
    assert detail.status_code == 200 and detail.json()["owner_id"] == "ordinary-owner"
    assert business.delete(f"/api/v1/library/items/{item.json()['id']}", headers=identity).status_code == 200
    task = business.post("/api/v1/tasks", headers=identity, json={
        "mode": "copy", "name": "自己的任务", "topic": "日常生活", "audience": "成年消费者",
    })
    assert task.status_code == 200, task.text
    path = f"/api/v1/tasks/{task.json()['id']}"
    assert business.get(path, headers=identity).status_code == 200
    assert business.get(path, headers=headers("different-person")).status_code in {403, 404}


def test_project_admin_authorizes_and_revokes_business_grant(business):
    grant(business, "project-admin", [], admin=True)
    target = business.get("/api/v1/access/me", headers=headers("rule-editor"))
    assert target.status_code == 200
    people = business.get("/api/v1/access/users", headers=headers("project-admin")).json()["items"]
    person = next(item for item in people if item["subject"] == "rule-editor")
    route = f"/api/v1/access/users/{person['id']}"
    admin_headers = {**headers("project-admin"), "X-Content-Access-Request": "1"}
    response = business.put(route, headers=admin_headers, json={
        "expected_revision": person["revision"], "is_project_admin": False, "permissions": ["rules.manage"],
    })
    assert response.status_code == 200, response.text
    payload = {"phrase": "授权撤回测试", "level": "warning"}
    assert business.post("/api/v1/sensitive-rules", headers=headers("rule-editor"), json=payload).status_code == 200
    revoked = business.put(route, headers=admin_headers, json={
        "expected_revision": response.json()["revision"], "is_project_admin": False, "permissions": [],
    })
    assert revoked.status_code == 200
    assert business.post("/api/v1/sensitive-rules", headers=headers("rule-editor"), json=payload).status_code == 403
    logs = business.get("/api/v1/audit-logs?action=access.permissions.update", headers=headers("project-admin"))
    assert logs.status_code == 200 and len(logs.json()) == 2
