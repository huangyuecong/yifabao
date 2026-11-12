import json
from uuid import uuid4


def _assert_problem(response, status: int):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    for key in ("type", "title", "status", "detail", "instance", "request_id", "trace_id"):
        assert key in body
    assert body["status"] == status
    assert body["request_id"]
    assert body["trace_id"]
    return body


def test_empty_project_asset_patch_is_serializable_validation_problem(content_client, user_headers):
    project_id = str(uuid4())
    asset_id = str(uuid4())
    response = content_client.patch(
        f"/api/v1/video-projects/{project_id}/assets/{asset_id}",
        json={},
        headers={
            **user_headers,
            "X-Request-Id": "req-empty-patch",
            "X-Trace-Id": "trace-empty-patch",
        },
    )

    body = _assert_problem(response, 422)
    # This used to raise TypeError while encoding ctx.error and become a 500.
    json.dumps(body, ensure_ascii=False)
    assert "至少需要一个变更字段" in response.text
    assert body["request_id"] == "req-empty-patch"
    assert body["trace_id"] == "trace-empty-patch"
    assert "Traceback" not in response.text
    assert "content_service" not in response.text


def test_missing_task_project_and_asset_use_problem_contract(content_client, user_headers):
    missing = str(uuid4())
    task_body = _assert_problem(
        content_client.get(f"/api/v1/tasks/{missing}", headers=user_headers),
        404,
    )
    assert "任务" in task_body["detail"]

    project_body = _assert_problem(
        content_client.get(f"/api/v1/video-projects/{missing}", headers=user_headers),
        404,
    )
    assert "工程" in project_body["detail"]

    asset_body = _assert_problem(
        content_client.get(
            f"/api/v1/video-projects/{missing}/assets/{uuid4()}",
            headers=user_headers,
        ),
        404,
    )
    assert "工程" in asset_body["detail"]

    created_task = content_client.post(
        "/api/v1/tasks",
        json={"mode": "video", "name": f"error-contract-video-{uuid4()}"},
        headers=user_headers,
    )
    assert created_task.status_code == 200, created_task.text
    created_project = content_client.post(
        f"/api/v1/tasks/{created_task.json()['id']}/video-project",
        headers=user_headers,
    )
    assert created_project.status_code == 200, created_project.text
    asset_body = _assert_problem(
        content_client.get(
            f"/api/v1/video-projects/{created_project.json()['id']}/assets/{uuid4()}",
            headers=user_headers,
        ),
        404,
    )
    assert "素材" in asset_body["detail"]


def test_forbidden_task_access_uses_problem_contract(content_client, user_headers, other_headers):
    created = content_client.post(
        "/api/v1/tasks",
        json={"mode": "copy", "name": f"error-contract-{uuid4()}"},
        headers=user_headers,
    )
    assert created.status_code == 200, created.text

    body = _assert_problem(
        content_client.get(
            f"/api/v1/tasks/{created.json()['id']}",
            headers=other_headers,
        ),
        403,
    )
    assert "无权" in body["detail"]


def test_method_not_allowed_uses_problem_contract(content_client, user_headers):
    body = _assert_problem(content_client.post("/health", headers=user_headers), 405)
    assert body["title"] == "请求方法不允许"
    assert "Traceback" not in json.dumps(body, ensure_ascii=False)


def test_unhandled_exception_uses_safe_problem_contract(content_client, user_headers, caplog):
    # Exercise the shared handler without changing a business route.
    # A small temporary route is removed after the assertion so this test does
    # not affect the application contract seen by subsequent tests.
    from fastapi import Request

    from content_service import main as content_main

    @content_main.app.get("/__test_unhandled_error")
    async def _unhandled(_request: Request):
        raise RuntimeError("C:\\private\\content.db secret-token")

    try:
        response = content_client.get("/__test_unhandled_error", headers=user_headers)
        body = _assert_problem(response, 500)
        assert body["detail"] == "服务器暂时无法处理请求"
        assert "Traceback" not in response.text
        assert "content.db" not in response.text
        assert "secret-token" not in response.text
        # Creating the content client runs Alembic first. Its logging setup must
        # retain the shared middleware logger, including when imported earlier.
        records = [record for record in caplog.records if record.name == "agent_core.middleware"]
        assert len(records) == 1
        diagnostic = json.loads(records[0].getMessage())
        assert diagnostic["route"] == "/__test_unhandled_error"
        assert diagnostic["exceptions"][0]["type"] == "RuntimeError"
        assert "content.db" not in records[0].getMessage()
        assert "secret-token" not in records[0].getMessage()
    finally:
        content_main.app.router.routes.remove(
            next(
                route
                for route in content_main.app.router.routes
                if getattr(route, "path", None) == "/__test_unhandled_error"
            )
        )
