"""Online-trial accounts: every request needs an account, and changes are logged per account."""

import base64
import io

import pytest
from test_content_workflow_v2 import create_task

from content_service import trial_accounts


def basic(username, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()}


@pytest.fixture
def trial(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTENT_TRIAL_ACCOUNTS", "true")
    monkeypatch.setattr(trial_accounts, "_private_dir", lambda: tmp_path)
    trial_accounts._verified.clear()
    trial_accounts._failures.clear()
    for name, owner in (("boss", True), ("tester", False)):
        monkeypatch.setattr("sys.stdin", io.StringIO("secret-pass\n"))
        assert trial_accounts.main(["set", name, "--display-name", name.upper()] + (["--owner"] if owner else [])) == 0
    yield tmp_path
    trial_accounts._verified.clear()
    trial_accounts._failures.clear()


def headers(user_headers, name):
    return {**user_headers, **basic(name, "secret-pass")}


def test_off_by_default_everything_passes(content_client, user_headers):
    assert content_client.get("/api/v1/trial/me", headers=user_headers).json() == {"enabled": False}
    assert content_client.get("/api/v1/trial/auth-check").status_code == 204


def test_requests_need_a_valid_account(trial, content_client, user_headers):
    response = content_client.get("/api/v1/identity", headers=user_headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")
    assert content_client.get("/api/v1/identity", headers={**user_headers, **basic("tester", "nope")}).status_code == 401
    assert content_client.get("/api/v1/trial/auth-check", headers=basic("tester", "nope")).status_code == 401
    assert content_client.get("/api/v1/trial/auth-check", headers=basic("tester", "secret-pass")).status_code == 204
    assert content_client.get("/health").status_code == 200

    # The page shows the signed-in account, whatever name the browser sent.
    identity = content_client.get("/api/v1/identity", headers=headers(user_headers, "tester")).json()
    assert identity["display_name"] == "tester"
    me = content_client.get("/api/v1/trial/me", headers=headers(user_headers, "tester")).json()
    assert me == {"enabled": True, "username": "tester", "display_name": "TESTER", "is_owner": False,
                  "remote_browser": False}


def test_changes_are_logged_under_the_account(trial, content_client, user_headers):
    create_task(content_client, headers(user_headers, "tester"), "full")
    create_task(content_client, headers(user_headers, "boss"), "copy")

    own = content_client.get("/api/v1/trial/operations", headers=headers(user_headers, "tester")).json()
    assert [(item["username"], item["action"]) for item in own["items"]] == [("tester", "创建内容任务")]
    # A tester cannot read someone else's log by asking for it.
    other = content_client.get("/api/v1/trial/operations", params={"username": "boss"},
                               headers=headers(user_headers, "tester")).json()
    assert {item["username"] for item in other["items"]} == {"tester"}

    everyone = content_client.get("/api/v1/trial/operations", headers=headers(user_headers, "boss")).json()
    assert [item["username"] for item in everyone["items"]] == ["boss", "tester"]
    assert everyone["items"][0]["display_name"] == "BOSS"


def test_owner_manages_accounts(trial, content_client, user_headers):
    boss, tester = headers(user_headers, "boss"), headers(user_headers, "tester")
    assert content_client.get("/api/v1/trial/accounts", headers=tester).status_code == 403
    created = content_client.post("/api/v1/trial/accounts", headers=boss,
                                  json={"username": "zhang", "display_name": "张三", "password": "zhang-123"})
    assert created.status_code == 201
    assert "password_hash" not in created.json()
    assert content_client.post("/api/v1/trial/accounts", headers=boss,
                               json={"username": "zhang", "password": "zhang-123"}).status_code == 409
    assert content_client.get("/api/v1/trial/me", headers={**user_headers, **basic("zhang", "zhang-123")}).json()["display_name"] == "张三"

    assert content_client.patch("/api/v1/trial/accounts/zhang", headers=boss, json={"enabled": False}).status_code == 200
    assert content_client.get("/api/v1/trial/me", headers={**user_headers, **basic("zhang", "zhang-123")}).status_code == 401
    assert content_client.patch("/api/v1/trial/accounts/boss", headers=boss, json={"enabled": False}).status_code == 409
    assert content_client.delete("/api/v1/trial/accounts/boss", headers=boss).status_code == 409
    assert content_client.delete("/api/v1/trial/accounts/zhang", headers=boss).status_code == 204
    names = [item["username"] for item in content_client.get("/api/v1/trial/accounts", headers=boss).json()["items"]]
    log = content_client.get("/api/v1/trial/operations", headers=boss).json()["items"]
    assert [item["action"] for item in log if item["status"] < 400][:3] == ["删除试用账号", "修改试用账号", "新建试用账号"]
    assert names == ["boss", "tester"]
    assert "secret-pass" not in (trial / "trial-accounts.json").read_text(encoding="utf-8")


def test_repeated_failures_lock_the_address(trial, content_client, user_headers):
    for _ in range(trial_accounts.FAILURE_LIMIT):
        content_client.get("/api/v1/trial/me", headers={**user_headers, **basic("tester", "wrong")})
    assert content_client.get("/api/v1/trial/me", headers=headers(user_headers, "tester")).status_code == 403


def test_action_labels():
    assert trial_accounts.action_label("POST", "/api/v1/tasks/abc/stages/topic/generate") == "生成：智能标题"
    assert trial_accounts.action_label("POST", "/api/v1/catalog/publish/promotion") == "提交平台发布"
    assert trial_accounts.action_label("DELETE", "/api/v1/chat/conversations/x") == "删除 AI 对话"
    assert trial_accounts.action_label("PATCH", "/api/v1/unknown/thing") == "修改其他"
