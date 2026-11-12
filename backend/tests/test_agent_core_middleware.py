import asyncio
import json
import logging
from enum import Enum

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy.exc import DatabaseError

from agent_core.middleware import (
    _json_safe,
    _log_unhandled,
    _title_for_status,
    install_request_context,
    request_id_var,
)


class _Kind(Enum):
    COPY = "copy"


def test_json_safe_and_status_titles():
    assert _json_safe(ValueError("boom")) == "boom"
    assert _json_safe(_Kind.COPY) == "copy"
    assert _json_safe({"n": 1, "err": ValueError("x")}) == {"n": 1, "err": "x"}
    assert _json_safe({1, 2}) == [1, 2]
    assert _title_for_status(404) == "资源不存在"
    assert _title_for_status(500) == "服务器内部错误"


def test_request_context_keeps_problem_json_contract():
    app = FastAPI()
    install_request_context(app, "content-test")

    @app.get("/http")
    def http_error():
        raise HTTPException(status_code=404, detail="没有这份资料")

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret-path")

    @app.get("/pydantic")
    def pydantic_error():
        class Item(BaseModel):
            count: int

        Item.model_validate({"count": "bad"})

    @app.get("/nested")
    def nested_detail():
        raise HTTPException(status_code=400, detail={"error": ValueError("bad")})

    client = TestClient(app, raise_server_exceptions=False)
    missing = client.get("/http", headers={"X-Request-Id": "req-1"})
    assert missing.status_code == 404
    assert missing.headers["X-Agent-Service"] == "content-test"
    assert missing.headers["X-Request-Id"] == "req-1"
    body = missing.json()
    assert body["detail"] == "没有这份资料"
    assert body["request_id"] == "req-1"

    crashed = client.get("/boom")
    assert crashed.status_code == 500
    assert "secret-path" not in crashed.text

    invalid = client.get("/pydantic")
    assert invalid.status_code == 422
    nested = client.get("/nested")
    assert nested.status_code == 400
    assert nested.json()["detail"]["error"] == "bad"


def test_unhandled_log_has_diagnostics_without_request_or_database_secrets(caplog):
    app = FastAPI()
    install_request_context(app, "content-test")

    class DriverError(Exception):
        pass

    @app.post("/items/{item_id}")
    def fail(item_id: str):
        try:
            raise DriverError("[CODE:-70011] private-driver-message password=driver-secret")
        except DriverError as original:
            raise DatabaseError(
                "INSERT INTO private_table VALUES (:secret)",
                {"secret": "database-parameter-secret"},
                original,
            ) from original

    with caplog.at_level(logging.ERROR, logger="agent_core.middleware"):
        response = TestClient(app).post(
            "/items/private-item-id?token=query-secret",
            json={"password": "body-secret"},
            headers={"Authorization": "Bearer header-secret", "X-Request-Id": "req-dm-failure"},
        )
    assert response.status_code == 500
    assert response.json()["detail"] == "服务器暂时无法处理请求"
    assert response.headers["X-Request-Id"] == "req-dm-failure"
    records = [record for record in caplog.records if record.name == "agent_core.middleware"]
    assert len(records) == 1
    assert records[0].exc_info is None
    diagnostic = json.loads(records[0].getMessage())
    assert diagnostic["route"] == "/items/{item_id}"
    assert diagnostic["method"] == "POST"
    assert diagnostic["request_id"] == "req-dm-failure"
    assert diagnostic["dm_codes"] == [-70011]
    assert [item["type"] for item in diagnostic["exceptions"]] == ["DatabaseError", "DriverError"]
    assert any(
        frame.startswith("test_agent_core_middleware.py:fail:")
        for item in diagnostic["exceptions"] for frame in item["frames"]
    )
    for secret in (
        "private-item-id", "query-secret", "body-secret", "header-secret", "private_table",
        "database-parameter-secret", "private-driver-message", "driver-secret", "password=",
    ):
        assert secret not in records[0].getMessage()
    assert "database-parameter-secret" not in response.text


def test_handler_and_boundary_log_once_and_bound_untrusted_identifiers(caplog):
    app = FastAPI()
    install_request_context(app, "content-test")
    request = Request({
        "type": "http", "method": "GET", "path": "/unregistered-path-secret",
        "headers": [(b"x-request-id", b"forged/header-token-" + b"x" * 200)],
        "query_string": b"token=query-secret",
    })
    token = request_id_var.set("")
    try:
        error = RuntimeError("do-not-log-exception-text")
        error.__context__ = error  # A cyclic cause must not hang diagnostic reporting.
        with caplog.at_level(logging.ERROR, logger="agent_core.middleware"):
            response = asyncio.run(app.exception_handlers[Exception](request, error))
            _log_unhandled(request, error, "content-test")
    finally:
        request_id_var.reset(token)
    assert response.status_code == 500
    records = [record for record in caplog.records if record.name == "agent_core.middleware"]
    assert len(records) == 1
    diagnostic = json.loads(records[0].getMessage())
    assert diagnostic["route"] == "<unregistered>"
    assert diagnostic["request_id"] == "<invalid>"
    assert diagnostic["exceptions"] == [{"type": "RuntimeError", "frames": []}]
    for secret in ("header-token", "unregistered-path-secret", "query-secret", "exception-text"):
        assert secret not in records[0].getMessage()


def test_expected_http_errors_do_not_log_as_unhandled(caplog):
    app = FastAPI()
    install_request_context(app, "content-test")

    @app.get("/denied")
    def denied():
        raise HTTPException(status_code=403, detail="无权限")

    with caplog.at_level(logging.ERROR, logger="agent_core.middleware"):
        assert TestClient(app).get("/denied").status_code == 403
        assert TestClient(app).get("/missing").status_code == 404
    assert not [record for record in caplog.records if record.name == "agent_core.middleware"]
