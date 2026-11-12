"""Operations-assistant Agent: workbench tools, the plan/act loop and the chat integration."""

import json
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_catalog import ALICE, BASE, HEADERS, catalog, create_product  # noqa: F401

from agent_core import Identity
from content_service import agent_tools, chat_agent, chat_generation, chat_live, chat_routes
from content_service.config import load_settings
from content_service.llm_adapters import ProviderCallError

READER = Identity("reader", "Reader", "marketing", frozenset({"user"}))
REAL_CLIENT = httpx.Client


@pytest.fixture
def settings(tmp_path):
    return replace(
        load_settings(),
        data_dir=tmp_path,
        llm_provider="openai_compatible",
        llm_base_url="https://models.example/v1",
        llm_api_key="private-test-key",
        llm_model="text-model",
    )


def reply(content=None, calls=()):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {"id": f"call-{index}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
            for index, (name, arguments) in enumerate(calls)
        ]
    return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}]})


def scripted_model(monkeypatch, script):
    """Answer each chat-completions request with the next scripted response; record the payloads."""
    payloads = []

    def handle(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        step = script[len(payloads) - 1]
        return step(payload) if callable(step) else step

    monkeypatch.setattr(chat_agent.httpx, "Client",
                        lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handle), **kwargs))
    return payloads


def approve_all(client):
    items = client.get(f"{BASE}/items", params={"status": "review"}).json()["items"]
    client.post(f"{BASE}/items/approve", headers=HEADERS, json={"ids": [item["id"] for item in items]})
    return [content["id"] for content in client.get(f"{BASE}/contents").json()["items"]]


def test_tools_query_generate_and_only_hand_off_publishing(catalog):  # noqa: F811
    client, published, _ = catalog
    product = create_product(client, "雅集酒（遇雅集）")
    create_product(client, "臻品雅酿")

    found = agent_tools.call(ALICE, "search_products", {"keyword": "雅集"})
    assert found["ok"] and found["result"]["total"] == 1
    assert found["result"]["products"][0] == {
        "id": product["id"], "name": "雅集酒（遇雅集）", "category": "白酒", "price": "499.00",
        "has_images": True, "content_count": 0, "draft_status": "未生成"}

    started = agent_tools.call(ALICE, "start_generation", {"product_ids": [product["id"]]})
    assert started["ok"] and started["result"]["queued"] == 1
    review = agent_tools.call(ALICE, "list_review_items", {"status": "review"})["result"]
    assert review["total"] == 1 and review["items"][0]["product"] == "雅集酒（遇雅集）"
    assert agent_tools.call(ALICE, "workbench_overview", {})["result"]["生成与审核"] == {"待审核": 1}

    content_ids = approve_all(client)
    handoff = agent_tools.call(ALICE, "prepare_publish", {"workflow": "promotion", "content_ids": content_ids})
    action = handoff["result"]["action"]
    assert action["type"] == "open_publish" and action["path"] == "/agent/content/promotion?ids=" + content_ids[0]
    assert published == []  # the Agent never publishes by itself

    assert not agent_tools.call(ALICE, "prepare_publish", {"workflow": "promotion", "content_ids": ["nope"]})["ok"]
    too_many = agent_tools.call(ALICE, "start_generation", {"product_ids": [str(n) for n in range(11)]})
    assert too_many == {"ok": False, "error": "一次最多处理 10 个，请分批进行"}
    denied = agent_tools.call(READER, "start_generation", {"product_ids": [product["id"]]})
    assert not denied["ok"] and "权限" in denied["error"]
    assert agent_tools.call(ALICE, "missing_tool", {}) == {"ok": False, "error": "没有名为 missing_tool 的工具"}
    assert not agent_tools.call(ALICE, "search_products", "{bad json")["ok"]


def test_compliance_tool_reports_rule_hits(catalog):  # noqa: F811
    _, _, factory = catalog
    from content_service.models import SensitiveRule

    with factory() as session:
        session.add(SensitiveRule(phrase="最佳", created_by="test"))
        session.commit()
    result = agent_tools.call(ALICE, "check_compliance", {"text": "这是最佳的白酒"})["result"]
    assert result["passed"] is False and result["hits"] == ["最佳"]


def test_agent_plans_calls_tools_and_records_a_trace(catalog, settings, monkeypatch):  # noqa: F811
    client, _, _ = catalog
    product = create_product(client, "雅集酒（遇雅集）")
    payloads = scripted_model(monkeypatch, [
        reply('{"plan": ["查找雅集酒", "为它生成文案"]}'),
        reply("先查找商品。", [("search_products", {"keyword": "雅集"})]),
        reply("找到了，开始生成。", [("start_generation", {"product_ids": [product["id"]]})]),
        reply("已为雅集酒发起文案生成，完成后请到「生成与审核」审核。"),
    ])
    chat_live.start("reply-1", "alice", "conversation-1")
    history = [{"role": "user", "content": "给雅集酒生成文案", "request": {}}]

    text, trace = chat_agent.run(settings, {"model": "text-model"}, history, ALICE, "reply-1")

    assert text.startswith("已为雅集酒发起文案生成")
    assert trace["plan"] == ["查找雅集酒", "为它生成文案"]
    assert [(step["tool"], step["status"]) for step in trace["steps"]] == [
        ("search_products", "done"), ("start_generation", "done")]
    assert trace["steps"][0]["summary"] == "找到 1 个商品：雅集酒（遇雅集）"
    assert trace["steps"][1]["writes"] is True and trace["steps"][0]["thought"] == "先查找商品。"
    assert trace["actions"] == [{"type": "open_page", "path": "/agent/content/review",
                                 "label": "去「生成与审核」查看进度"}]
    assert {"kind": "商品", "name": "雅集酒（遇雅集）", "id": product["id"]} in trace["objects"]
    # The plan call has no tools; the loop sends the tool list and replays tool results by call id.
    assert "tools" not in payloads[0] and payloads[1]["tool_choice"] == "auto"
    assert {tool["function"]["name"] for tool in payloads[1]["tools"]} == set(agent_tools.TOOLS)
    tool_message = payloads[2]["messages"][-1]
    assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "call-0"
    assert json.loads(tool_message["content"])["result"]["total"] == 1
    chat_live.end("reply-1")
    kinds = [kind for kind, _ in chat_live.follow("reply-1", "alice", "conversation-1")]
    assert kinds.count("plan") == 1 and kinds.count("step") == 4 and "delta" in kinds


def test_agent_keeps_completed_steps_when_the_model_fails(catalog, settings, monkeypatch):  # noqa: F811
    client, _, _ = catalog
    create_product(client, "雅集酒（遇雅集）")
    scripted_model(monkeypatch, [
        reply("not json"),
        reply(None, [("search_products", {"keyword": "雅集"})]),
        httpx.Response(502),
    ])
    chat_live.start("reply-2", "alice", "conversation-2")
    text, trace = chat_agent.run(settings, {"model": "text-model"}, [{"role": "user", "content": "找雅集"}],
                                 ALICE, "reply-2")
    assert trace["plan"] == [] and len(trace["steps"]) == 1
    assert text.startswith("运营助手完成 1 步后中断")

    scripted_model(monkeypatch, [reply('{"plan": []}'), httpx.Response(500)])
    with pytest.raises(ProviderCallError):
        chat_agent.run(settings, {"model": "text-model"}, [{"role": "user", "content": "你好"}], ALICE, "reply-3")


def test_agent_stops_after_the_round_limit(catalog, settings, monkeypatch):  # noqa: F811
    looping = reply(None, [("workbench_overview", {})])
    payloads = scripted_model(monkeypatch, [reply('{"plan": []}')] + [looping] * chat_agent.MAX_ROUNDS
                              + [reply("目前共有 0 个商品。")])
    text, trace = chat_agent.run(settings, {"model": "text-model"}, [{"role": "user", "content": "概况"}],
                                 ALICE, "reply-4")
    assert text == "目前共有 0 个商品。" and len(trace["steps"]) == chat_agent.MAX_ROUNDS
    assert "tools" not in payloads[-1]


def test_later_turns_see_the_ids_found_earlier():
    history = [
        {"role": "user", "content": "找雅集酒"},
        {"role": "assistant", "content": "找到 1 个商品", "request": {"agent": {
            "objects": [{"kind": "商品", "name": "雅集酒", "id": "p-1"}]}}},
        {"role": "user", "content": "给它生成视频"},
    ]
    messages = chat_agent._context(history)
    assert "商品「雅集酒」id=p-1" in messages[1]["content"] and messages[-1]["content"] == "给它生成视频"


def test_chat_routes_the_request_to_the_agent_and_saves_the_trace(catalog, settings, monkeypatch):  # noqa: F811
    client, _, _ = catalog
    create_product(client, "雅集酒（遇雅集）")
    scripted_model(monkeypatch, [
        reply(None, [("operate_workbench", {})]),  # the router's function call
        reply('{"plan": ["查看工作台概况"]}'),
        reply(None, [("workbench_overview", {})]),
        reply("现在有 1 个商品，还没有待审核内容。"),
    ])
    monkeypatch.setenv("CONTENT_CHAT_TOOL_ROUTING", "true")
    monkeypatch.setattr(chat_generation._executor, "submit", lambda function, *args: function(*args))
    monkeypatch.setattr(chat_routes, "get_runtime_settings", lambda: settings)
    app = FastAPI()
    app.include_router(chat_routes.router)
    headers = {"X-User-Id": "alice", "X-User-Name": "Alice", "X-Roles": "admin"}
    with TestClient(app) as chat:
        conversation = chat.post("/api/v1/chat/conversations", headers=headers, json={}).json()
        result = chat.post(f"/api/v1/chat/conversations/{conversation['id']}/messages", headers=headers,
                           json={"content": "工作台现在什么情况？"})
        assert result.status_code == 200, result.text
        saved = chat.get(f"/api/v1/chat/conversations/{conversation['id']}", headers=headers).json()
    question, answer = saved["messages"]
    assert question["routing"] == {"output": "agent", "tool": "operate_workbench", "method": "function_calling"}
    assert answer["status"] == "completed" and answer["content"].startswith("现在有 1 个商品")
    assert answer["agent"]["steps"][0]["summary"] == "商品 1 个，待审核 0 条，内容库 0 条"
