import io
import json
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CONTENT_SRC = ROOT / "services" / "content_service" / "src"
if str(CONTENT_SRC) not in sys.path:
    sys.path.insert(0, str(CONTENT_SRC))

from content_service import llm_adapters
from content_service.config import load_settings
from content_service.db import SessionLocal
from content_service.generation import (
    expand_allowed_data_levels,
    generation_source_content,
)
from content_service.knowledge import (
    JsonlKnowledgeProvider,
    LibraryKnowledgeProvider,
)
from content_service.llm_adapters import (
    FailoverTextProvider,
    OpenAICompatibleTextProvider,
    ProviderCallError,
    VolcengineArkTextGenerationPort,
)
from content_service.main import app
from content_service.models import Artifact, LibraryItem
from content_service.provider_dependencies import (
    get_knowledge_provider,
    get_text_provider,
)
from content_service.providers import (
    ContentGenerationInput,
    KnowledgeChunk,
    KnowledgeQuery,
    ProviderStatus,
    TextGenerationResult,
    transform_required_fields,
)

CONTENT_CONFIG_ENV = {
    "CONTENT_ARK_API_KEY",
    "CONTENT_ARK_API_KEY_FILE",
    "CONTENT_LLM_PROVIDER",
    "CONTENT_LLM_PROVIDER_ID",
    "CONTENT_LLM_BASE_URL",
    "CONTENT_LLM_API_KEY",
    "CONTENT_LLM_API_KEY_FILE",
    "CONTENT_LLM_MODEL",
    "CONTENT_LLM_FALLBACK_MODEL",
    "CONTENT_KNOWLEDGE_PROVIDER",
    "CONTENT_KNOWLEDGE_DIR",
}


def test_egress_data_level_grants_include_lower_risk_levels() -> None:
    assert expand_allowed_data_levels(["public"]) == {"public"}
    assert expand_allowed_data_levels(["internal"]) == {"public", "internal"}
    assert expand_allowed_data_levels(["restricted"]) == {
        "public",
        "internal",
        "restricted",
    }


def test_generation_source_preserves_the_live_selected_candidate() -> None:
    content = {
        "candidates": [
            {"title": "候选一", "body": "正文一", "source_ids": []},
            {"title": "候选二（已编辑）", "body": "正文二（已编辑）", "source_ids": []},
        ],
        "selected_candidate_index": 1,
        "selected_candidate": {
            "title": "候选二（旧快照）",
            "body": "正文二（旧快照）",
            "source_ids": [],
        },
    }

    source = generation_source_content(content)

    assert source["selected_candidate_index"] == 1
    assert source["selected_candidate"]["title"] == "候选二（已编辑）"
    assert source["selected_candidate"]["body"] == "正文二（已编辑）"


def reset_content_provider_env(monkeypatch) -> None:
    for name in CONTENT_CONFIG_ENV:
        monkeypatch.delenv(name, raising=False)


def generation_input(*chunks: KnowledgeChunk) -> ContentGenerationInput:
    return ContentGenerationInput(
        task_name="官网资料文案",
        topic="臻品文化",
        product="雅酿",
        keywords=("臻品文化",),
        audience="成年消费者",
        content_type="品牌宣传文案",
        style="正式、克制",
        length="短",
        requirements="只使用可核验资料",
        instruction="",
        knowledge=tuple(chunks),
    )


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return None
        request = httpx.Request("POST", "https://model.example/v1/chat/completions")
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError(
            f"HTTP {self.status_code}",
            request=request,
            response=response,
        )

    def json(self) -> dict:
        return self.payload


class FakeClient:
    def __init__(self, payloads: list[dict], status_codes: list[int] | None = None):
        self.payloads = list(payloads)
        self.status_codes = list(status_codes or [200] * len(payloads))
        self.posts: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def post(self, endpoint: str, *, headers: dict, json: dict) -> FakeResponse:
        self.posts.append({"endpoint": endpoint, "headers": headers, "json": json})
        return FakeResponse(self.payloads.pop(0), self.status_codes.pop(0))


def chat_payload(content: str | None) -> dict:
    return {
        "choices": [
            {
                "message": {"content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    }


def test_settings_require_explicit_external_model_and_knowledge_configuration(
    monkeypatch,
    tmp_path,
):
    reset_content_provider_env(monkeypatch)
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path / "mock"))
    settings = load_settings()
    assert settings.llm_provider == "mock"
    assert settings.knowledge_provider == "library_db"
    assert settings.llm_api_key is None

    monkeypatch.setenv("CONTENT_LLM_PROVIDER", "openai_compatible")
    with pytest.raises(ValueError, match="外部内容模型缺少配置"):
        load_settings()

    monkeypatch.setenv("CONTENT_LLM_PROVIDER_ID", "configured-provider")
    monkeypatch.setenv("CONTENT_LLM_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("CONTENT_LLM_API_KEY", "local-test-key")
    monkeypatch.setenv("CONTENT_LLM_MODEL", "configured-model")
    monkeypatch.setenv("CONTENT_LLM_FALLBACK_MODEL", "configured-model")
    with pytest.raises(ValueError, match="不能与 CONTENT_LLM_MODEL 相同"):
        load_settings()
    monkeypatch.delenv("CONTENT_LLM_FALLBACK_MODEL")
    monkeypatch.setenv("CONTENT_KNOWLEDGE_PROVIDER", "jsonl")
    with pytest.raises(ValueError, match="CONTENT_KNOWLEDGE_DIR"):
        load_settings()


def test_jsonl_knowledge_provider_uses_only_explicit_directory():
    directory = ROOT / "tests" / "fixtures" / "content_knowledge" / "yifabao_official"
    provider = JsonlKnowledgeProvider(directory, "test-content")
    chunks = provider.search(
        KnowledgeQuery(
            text="臻品文化 仁者五德 恭宽信敏惠",
            requester_id="user-a",
            task_id="task-a",
            categories=("culture",),
            allowed_data_levels=frozenset({"public"}),
            limit=2,
        )
    )
    assert chunks
    assert chunks[0].category == "culture"
    assert all(chunk.data_level == "public" for chunk in chunks)
    assert any("恭、宽、信、敏、惠" in chunk.text for chunk in chunks)


def test_jsonl_knowledge_provider_rejects_manifest_path_escape(tmp_path):
    directory = tmp_path / "knowledge"
    directory.mkdir()
    (tmp_path / "outside.jsonl").write_text("{}\n", encoding="utf-8")
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "categories": [
                    {"id": "culture", "file": "../outside.jsonl"},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="目录之外"):
        JsonlKnowledgeProvider(directory, "test-content")


def test_library_knowledge_provider_does_not_leak_other_users_internal_data(content_client):
    token = uuid4().hex
    with SessionLocal() as session:
        own = LibraryItem(
            owner_id="isolation-user-a",
            category="brand",
            name=f"自有资料-{token}",
            text_content=f"{token} 自有内部事实",
            data_level="internal",
        )
        public = LibraryItem(
            owner_id="isolation-user-b",
            category="brand",
            name=f"公开资料-{token}",
            text_content=f"{token} 企业公开事实",
            data_level="public",
        )
        private = LibraryItem(
            owner_id="isolation-user-b",
            category="brand",
            name=f"他人资料-{token}",
            text_content=f"{token} 他人内部秘密",
            data_level="internal",
        )
        session.add_all([own, public, private])
        session.commit()
        provider = LibraryKnowledgeProvider(session, "test-content")
        chunks = provider.search(
            KnowledgeQuery(
                text=token,
                requester_id="isolation-user-a",
                task_id="task-a",
                categories=("brand",),
                allowed_data_levels=frozenset({"public", "internal"}),
                limit=10,
            )
        )
        ids = {chunk.id for chunk in chunks}
        assert own.id in ids
        assert public.id in ids
        assert private.id not in ids


def test_openai_compatible_adapter_uses_configured_values_and_retries_blank(monkeypatch):
    chunk = KnowledgeChunk(
        id="culture-1",
        title="臻品文化",
        text="仁者五德为恭、宽、信、敏、惠。",
        category="culture",
        data_level="public",
    )
    valid = json.dumps(
        {
            "candidates": [
                {
                    "title": "臻品文化",
                    "summary": "文化摘要",
                    "body": "臻品文化倡导恭、宽、信、敏、惠。",
                    "source_ids": ["culture-1"],
                }
            ]
        },
        ensure_ascii=False,
    )
    client = FakeClient([chat_payload(""), chat_payload(valid)])
    monkeypatch.setattr(llm_adapters.httpx, "Client", lambda **_: client)
    provider = OpenAICompatibleTextProvider(
        provider_id="configured-provider",
        base_url="https://model.example/custom/v1",
        api_key="secret-value",
        model="configured-model",
        timeout_seconds=10,
        max_output_tokens=800,
        temperature=0.1,
        max_attempts=2,
    )

    result = provider.copies(generation_input(chunk), 1)

    assert result.content[0]["source_ids"] == ["culture-1"]
    assert len(client.posts) == 2
    assert client.posts[0]["endpoint"] == "https://model.example/custom/v1/chat/completions"
    assert client.posts[0]["json"]["model"] == "configured-model"
    assert "reasoning_effort" not in client.posts[0]["json"]
    assert client.posts[0]["headers"]["Authorization"] == "Bearer secret-value"


def test_transform_adapter_retries_unchanged_body_and_allows_preserved_title(monkeypatch):
    source = {
        "title": "用户选中的标题",
        "summary": "原摘要",
        "body": "用户选中的正文。",
        "source_ids": [],
    }
    responses = [
        {**source, "title": "第一次只改标题"},
        {**source, "body": "第二次只改正文。"},
    ]
    client = FakeClient(
        [
            chat_payload(json.dumps({"candidate": candidate}, ensure_ascii=False))
            for candidate in responses
        ]
    )
    monkeypatch.setattr(llm_adapters.httpx, "Client", lambda **_: client)
    provider = OpenAICompatibleTextProvider(
        provider_id="configured-provider",
        base_url="https://model.example/v1",
        api_key="secret-value",
        model="configured-model",
        timeout_seconds=10,
        max_output_tokens=800,
        temperature=0.1,
        max_attempts=2,
    )
    request = replace(
        generation_input(),
        instruction="写得更有感染力",
        source_content={
            "candidates": [
                {
                    "title": "未选择的标题",
                    "summary": "未选择的摘要",
                    "body": "未选择的正文。",
                    "source_ids": [],
                },
                source,
            ],
            "selected_candidate_index": 1,
            # The live candidate list wins over a potentially stale snapshot.
            "selected_candidate": {**source, "title": "过期的标题快照"},
        },
    )

    result = provider.transform_copy(request, "rewrite")

    assert result.content["title"] == source["title"]
    assert result.content["body"] == "第二次只改正文。"
    assert len(client.posts) == 2
    first_request = json.loads(client.posts[0]["json"]["messages"][1]["content"])
    assert first_request["source_candidate"]["title"] == "用户选中的标题"
    assert first_request["transformation_requirements"]["must_change_fields"] == ["body"]
    assert "正文与原文相同" in client.posts[1]["json"]["messages"][1]["content"]


def test_transform_adapter_also_changes_title_when_instruction_requests_it(monkeypatch):
    source = {
        "title": "原始标题",
        "summary": "原摘要",
        "body": "原始正文。",
        "source_ids": [],
    }
    responses = [
        {**source, "body": "第一次只改正文。"},
        {**source, "title": "更简洁的标题", "body": "第二次正文也已改写。"},
    ]
    client = FakeClient(
        [
            chat_payload(json.dumps({"candidate": candidate}, ensure_ascii=False))
            for candidate in responses
        ]
    )
    monkeypatch.setattr(llm_adapters.httpx, "Client", lambda **_: client)
    provider = OpenAICompatibleTextProvider(
        provider_id="configured-provider",
        base_url="https://model.example/v1",
        api_key="secret-value",
        model="configured-model",
        timeout_seconds=10,
        max_output_tokens=800,
        temperature=0.1,
        max_attempts=2,
    )
    request = replace(
        generation_input(),
        instruction="标题更简洁，正文更有感染力",
        source_content={"candidates": [source]},
    )

    result = provider.transform_copy(request, "rewrite")

    assert result.content["title"] == "更简洁的标题"
    assert result.content["body"] == "第二次正文也已改写。"
    assert len(client.posts) == 2
    first_request = json.loads(client.posts[0]["json"]["messages"][1]["content"])
    assert first_request["transformation_requirements"]["must_change_fields"] == [
        "title",
        "body",
    ]
    assert "标题与原文相同" in client.posts[1]["json"]["messages"][1]["content"]


@pytest.mark.parametrize(
    "instruction",
    ["换个标题", "重新起标题", "另拟标题", "change the title", "retitle this copy"],
)
def test_transform_recognizes_common_title_change_instructions(instruction):
    assert transform_required_fields(
        {"title": "原标题", "body": "原正文"},
        instruction,
    ) == ("title", "body")


def test_transform_adapter_rejects_unchanged_topic_title(monkeypatch):
    unchanged = {
        "candidate": {
            "direction": "品牌故事",
            "title": "原始标题",
            "summary": "原摘要",
            "source_ids": [],
        }
    }
    client = FakeClient([chat_payload(json.dumps(unchanged, ensure_ascii=False)) for _ in range(2)])
    monkeypatch.setattr(llm_adapters.httpx, "Client", lambda **_: client)
    provider = OpenAICompatibleTextProvider(
        provider_id="configured-provider",
        base_url="https://model.example/v1",
        api_key="secret-value",
        model="configured-model",
        timeout_seconds=10,
        max_output_tokens=800,
        temperature=0.1,
        max_attempts=2,
    )
    request = replace(
        generation_input(),
        instruction="标题更简洁",
        source_content={
            "candidates": [unchanged["candidate"]],
            "selected_candidate_index": 0,
        },
    )

    with pytest.raises(ProviderCallError, match="标题与原文相同"):
        provider.transform_copy(request, "rewrite")

    assert len(client.posts) == 2


def test_content_text_provider_fails_over_to_secondary_model(monkeypatch):
    valid = json.dumps(
        {
            "candidates": [
                {
                    "title": "兜底文案",
                    "summary": "主模型暂时不可用时生成",
                    "body": "本次结果由备用模型生成。",
                    "source_ids": [],
                }
            ]
        },
        ensure_ascii=False,
    )
    client = FakeClient([{}, chat_payload(valid)], status_codes=[503, 200])
    monkeypatch.setattr(llm_adapters.httpx, "Client", lambda **_: client)
    common = {
        "provider_id": "configured-provider",
        "base_url": "https://model.example/v1",
        "api_key": "secret-value",
        "timeout_seconds": 10,
        "max_output_tokens": 800,
        "temperature": 0.1,
        "max_attempts": 1,
    }
    provider = FailoverTextProvider(
        OpenAICompatibleTextProvider(model="chat-max", **common),
        OpenAICompatibleTextProvider(model="chat-ds-flash", **common),
    )

    result = provider.copies(generation_input(), 1)

    assert [item["json"]["model"] for item in client.posts] == [
        "chat-max",
        "chat-ds-flash",
    ]
    assert result.model == "chat-ds-flash"
    assert result.content[0]["title"] == "兜底文案"


def test_content_text_provider_does_not_fail_over_on_auth_error(monkeypatch):
    client = FakeClient([{}], status_codes=[401])
    monkeypatch.setattr(llm_adapters.httpx, "Client", lambda **_: client)
    common = {
        "provider_id": "configured-provider",
        "base_url": "https://model.example/v1",
        "api_key": "secret-value",
        "timeout_seconds": 10,
        "max_output_tokens": 800,
        "temperature": 0.1,
        "max_attempts": 1,
    }
    provider = FailoverTextProvider(
        OpenAICompatibleTextProvider(model="chat-max", **common),
        OpenAICompatibleTextProvider(model="chat-ds-flash", **common),
    )

    with pytest.raises(ProviderCallError, match="HTTP 401"):
        provider.copies(generation_input(), 1)

    assert [item["json"]["model"] for item in client.posts] == ["chat-max"]


def test_content_text_provider_fails_over_after_primary_invalid_output(monkeypatch):
    valid = json.dumps(
        {
            "candidates": [
                {
                    "title": "结构化兜底文案",
                    "summary": "备用模型返回有效结构",
                    "body": "主模型输出为空后自动兜底。",
                    "source_ids": [],
                }
            ]
        },
        ensure_ascii=False,
    )
    client = FakeClient([chat_payload(""), chat_payload(valid)])
    monkeypatch.setattr(llm_adapters.httpx, "Client", lambda **_: client)
    common = {
        "provider_id": "configured-provider",
        "base_url": "https://model.example/v1",
        "api_key": "secret-value",
        "timeout_seconds": 10,
        "max_output_tokens": 800,
        "temperature": 0.1,
        "max_attempts": 1,
    }
    provider = FailoverTextProvider(
        OpenAICompatibleTextProvider(model="chat-max", **common),
        OpenAICompatibleTextProvider(model="chat-ds-flash", **common),
    )

    result = provider.copies(generation_input(), 1)

    assert [item["json"]["model"] for item in client.posts] == [
        "chat-max",
        "chat-ds-flash",
    ]
    assert result.model == "chat-ds-flash"


def test_volcengine_ark_text_adapter_disables_reasoning(monkeypatch):
    content = json.dumps(
        {
            "candidates": [
                {
                    "title": "测试文案",
                    "summary": "测试摘要",
                    "body": "测试正文。",
                    "source_ids": [],
                }
            ]
        },
        ensure_ascii=False,
    )
    client = FakeClient([chat_payload(content)])
    monkeypatch.setattr(llm_adapters.httpx, "Client", lambda **_: client)
    provider = VolcengineArkTextGenerationPort(
        provider_id="volcengine-ark",
        base_url="https://ark.example.invalid/api/v3",
        api_key="secret-value",
        model="doubao-test",
        timeout_seconds=10,
        max_output_tokens=800,
        temperature=0.1,
        max_attempts=1,
    )

    provider.copies(generation_input(), 1)

    assert client.posts[0]["json"]["reasoning_effort"] == "minimal"
    assert client.posts[0]["json"]["response_format"] == {"type": "json_object"}


def test_text_adapter_compacts_retry_after_token_limit(monkeypatch):
    truncated = chat_payload('{"candidates":[{"title":"未闭合')
    truncated["choices"][0]["finish_reason"] = "length"
    valid = chat_payload(
        json.dumps(
            {
                "candidates": [
                    {
                        "title": "完整标题",
                        "summary": "完整摘要",
                        "body": "完整正文。",
                        "source_ids": [],
                    }
                ]
            },
            ensure_ascii=False,
        )
    )
    client = FakeClient([truncated, valid])
    monkeypatch.setattr(llm_adapters.httpx, "Client", lambda **_: client)
    provider = VolcengineArkTextGenerationPort(
        provider_id="volcengine-ark",
        base_url="https://ark.example.invalid/api/v3",
        api_key="secret-value",
        model="doubao-test",
        timeout_seconds=10,
        max_output_tokens=800,
        temperature=0.1,
        max_attempts=2,
    )

    result = provider.copies(generation_input(), 1)

    assert result.content[0]["title"] == "完整标题"
    retry_prompt = client.posts[1]["json"]["messages"][1]["content"]
    assert "达到 800 token 上限并被截断" in retry_prompt
    assert "优先保证 JSON 完整闭合" in retry_prompt


def test_openai_compatible_adapter_wraps_malformed_supplier_responses(monkeypatch):
    client = FakeClient(
        [
            {"choices": {"unexpected": True}},
            {"choices": [{"message": "unexpected"}]},
        ]
    )
    monkeypatch.setattr(llm_adapters.httpx, "Client", lambda **_: client)
    provider = OpenAICompatibleTextProvider(
        provider_id="configured-provider",
        base_url="https://model.example/v1",
        api_key="secret-value",
        model="configured-model",
        timeout_seconds=10,
        max_output_tokens=800,
        temperature=0.1,
        max_attempts=2,
    )

    with pytest.raises(ProviderCallError, match="连续 2 次输出无效"):
        provider.copies(generation_input(), 1)

    assert len(client.posts) == 2


class FakeExternalTextProvider:
    status = ProviderStatus(
        provider_id="fake-external",
        name="fake-external-text",
        capability="text-generation",
        mode="test",
        external=True,
        enabled=True,
        model="fake-model",
    )

    def __init__(self):
        self.calls = 0
        self.last_request = None

    def copies(self, request: ContentGenerationInput, count: int) -> TextGenerationResult:
        self.calls += 1
        self.last_request = request
        candidates = [
            {
                "title": "公开资料文案",
                "summary": "仅使用已授权的公开资料",
                "body": "公开事实可用于本次测试。",
                "source_ids": [item.id for item in request.knowledge],
            }
            for _ in range(count)
        ]
        return TextGenerationResult(
            candidates,
            self.status.provider_id,
            self.status.model or "",
        )

    def transform_copy(
        self,
        request: ContentGenerationInput,
        operation: str,
    ) -> TextGenerationResult:
        self.calls += 1
        self.last_request = request
        source_ids = [item.id for item in request.knowledge]
        source_ids.extend((request.source_content or {}).get("source_reference_ids", []))
        source_candidates = (request.source_content or {}).get("candidates", [])
        source_title = str(source_candidates[0].get("title") or "")
        return TextGenerationResult(
            {
                "title": source_title,
                "summary": "保留来源的改写结果",
                "body": f"已执行 {operation}。",
                "source_ids": list(dict.fromkeys(source_ids)),
            },
            self.status.provider_id,
            self.status.model or "",
        )


class NoOpTransformTextProvider(FakeExternalTextProvider):
    def transform_copy(
        self,
        request: ContentGenerationInput,
        operation: str,
    ) -> TextGenerationResult:
        self.calls += 1
        self.last_request = request
        source_candidates = (request.source_content or {}).get("candidates", [])
        candidate = dict(source_candidates[0])
        return TextGenerationResult(
            candidate,
            self.status.provider_id,
            self.status.model or "",
        )


class FakeKnowledgeProvider:
    status = ProviderStatus(
        provider_id="fake-knowledge",
        name="fake-knowledge",
        capability="knowledge-search",
        mode="test",
        external=False,
        enabled=True,
    )

    def __init__(self):
        self.queries = []

    def search(self, query: KnowledgeQuery):
        self.queries.append(query)
        candidates = [
            KnowledgeChunk("public-doc", "公开资料", "公开事实", "brand", "public"),
            KnowledgeChunk("internal-doc", "内部资料", "内部事实", "brand", "internal"),
        ]
        return [item for item in candidates if item.data_level in query.allowed_data_levels]


class LeakyKnowledgeProvider(FakeKnowledgeProvider):
    def search(self, query: KnowledgeQuery):
        self.queries.append(query)
        return [
            KnowledgeChunk("public-doc", "公开资料", "公开事实", "brand", "public"),
            KnowledgeChunk("internal-doc", "内部资料", "内部事实", "brand", "internal"),
            KnowledgeChunk(
                "restricted-doc",
                "受限资料",
                "不得外发的事实",
                "brand",
                "restricted",
            ),
        ]


def test_external_generation_uses_safe_default_without_task_grant(
    content_client,
    user_headers,
):
    text_provider = FakeExternalTextProvider()
    knowledge_provider = FakeKnowledgeProvider()
    app.dependency_overrides[get_text_provider] = lambda: text_provider
    app.dependency_overrides[get_knowledge_provider] = lambda: knowledge_provider
    try:
        task = content_client.post(
            "/api/v1/tasks",
            headers=user_headers,
            json={
                "mode": "copy",
                "name": f"外发隔离-{uuid4().hex[:8]}",
                "topic": "企业公开资料",
            },
        ).json()
        endpoint = f"/api/v1/tasks/{task['id']}/generate-copy"

        egress_status = content_client.get(
            f"/api/v1/tasks/{task['id']}/egress-status",
            headers=user_headers,
        )
        assert egress_status.status_code == 200
        assert egress_status.json() == {
            "provider_id": "fake-external",
            "external": True,
            "required": False,
            "granted": True,
            "allowed_data_levels": ["public", "internal"],
            "allowed_source_data_levels": ["public", "internal"],
            "allow_task_context": True,
            "allow_compliance_terms": True,
            "can_approve": False,
        }

        wrong_provider = content_client.post(
            f"/api/v1/tasks/{task['id']}/egress-grants",
            headers=user_headers,
            json={"provider": "hard-coded-provider", "allowed_data_levels": ["public"]},
        )
        assert wrong_provider.status_code == 422

        legacy_grant = content_client.post(
            f"/api/v1/tasks/{task['id']}/egress-grants",
            headers=user_headers,
            json={
                "provider": "fake-external",
                "allowed_data_levels": ["restricted"],
            },
        )
        assert legacy_grant.status_code == 200

        generated = content_client.post(endpoint, headers=user_headers, json={"count": 1})
        assert generated.status_code == 200, generated.text
        content = generated.json()["content"]
        assert text_provider.calls == 1
        assert [item.id for item in text_provider.last_request.knowledge] == [
            "public-doc",
            "internal-doc",
        ]
        assert content["reference_item_ids"] == ["public-doc", "internal-doc"]
        assert content["generation"]["provider_id"] == "fake-external"
    finally:
        app.dependency_overrides.pop(get_text_provider, None)
        app.dependency_overrides.pop(get_knowledge_provider, None)


def test_generation_layer_rejects_knowledge_provider_scope_leak(
    content_client,
    user_headers,
):
    text_provider = FakeExternalTextProvider()
    knowledge_provider = LeakyKnowledgeProvider()
    app.dependency_overrides[get_text_provider] = lambda: text_provider
    app.dependency_overrides[get_knowledge_provider] = lambda: knowledge_provider
    try:
        task = content_client.post(
            "/api/v1/tasks",
            headers=user_headers,
            json={"mode": "copy", "name": f"知识二次校验-{uuid4().hex[:8]}"},
        ).json()
        denied = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-copy",
            headers=user_headers,
            json={"count": 1},
        )

        assert denied.status_code == 502
        assert "未授权等级" in denied.json()["detail"]
        assert len(knowledge_provider.queries) == 1
        assert text_provider.calls == 0
    finally:
        app.dependency_overrides.pop(get_text_provider, None)
        app.dependency_overrides.pop(get_knowledge_provider, None)


def test_external_transform_uses_public_internal_knowledge_and_internal_draft(
    content_client,
    user_headers,
):
    text_provider = FakeExternalTextProvider()
    knowledge_provider = FakeKnowledgeProvider()
    app.dependency_overrides[get_text_provider] = lambda: text_provider
    app.dependency_overrides[get_knowledge_provider] = lambda: knowledge_provider
    try:
        task = content_client.post(
            "/api/v1/tasks",
            headers=user_headers,
            json={"mode": "copy", "name": f"来源分级-{uuid4().hex[:8]}"},
        ).json()
        generated = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-copy",
            headers=user_headers,
            json={"count": 1},
        )
        assert generated.status_code == 200, generated.text
        generated_content = generated.json()["content"]
        assert generated_content["content_data_level"] == "internal"

        transformed = content_client.post(
            f"/api/v1/artifacts/{generated.json()['id']}/transform-copy",
            headers=user_headers,
            json={"operation": "rewrite", "instruction": "更简洁"},
        )

        assert transformed.status_code == 200, transformed.text
        content = transformed.json()["content"]
        assert content["content_data_level"] == "internal"
        assert content["reference_item_ids"] == ["public-doc", "internal-doc"]
        assert text_provider.calls == 2
    finally:
        app.dependency_overrides.pop(get_text_provider, None)
        app.dependency_overrides.pop(get_knowledge_provider, None)


def test_transform_api_rejects_provider_response_with_unchanged_body(
    content_client,
    user_headers,
):
    text_provider = NoOpTransformTextProvider()
    knowledge_provider = FakeKnowledgeProvider()
    app.dependency_overrides[get_text_provider] = lambda: text_provider
    app.dependency_overrides[get_knowledge_provider] = lambda: knowledge_provider
    try:
        task = content_client.post(
            "/api/v1/tasks",
            headers=user_headers,
            json={"mode": "copy", "name": f"无效改写-{uuid4().hex[:8]}"},
        ).json()
        generated = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-copy",
            headers=user_headers,
            json={"count": 1},
        ).json()

        transformed = content_client.post(
            f"/api/v1/artifacts/{generated['id']}/transform-copy",
            headers=user_headers,
            json={
                "operation": "expand",
                "instruction": "把正文扩写得更生动",
                "preview": True,
            },
        )

        assert transformed.status_code == 502
        assert "正文与原内容相同" in transformed.json()["detail"]["message"]
        assert text_provider.calls == 2
    finally:
        app.dependency_overrides.pop(get_text_provider, None)
        app.dependency_overrides.pop(get_knowledge_provider, None)


def test_default_external_policy_rejects_restricted_source_content(
    content_client,
    user_headers,
):
    text_provider = FakeExternalTextProvider()
    knowledge_provider = FakeKnowledgeProvider()
    app.dependency_overrides[get_text_provider] = lambda: text_provider
    app.dependency_overrides[get_knowledge_provider] = lambda: knowledge_provider
    try:
        task = content_client.post(
            "/api/v1/tasks",
            headers=user_headers,
            json={"mode": "video", "name": f"未分类来源-{uuid4().hex[:8]}"},
        ).json()
        with SessionLocal() as session:
            source = Artifact(
                task_id=task["id"],
                kind="copy",
                version=1,
                content_json=json.dumps(
                    {
                        "candidates": [
                            {
                                "title": "人工内容",
                                "summary": "无资料等级",
                                "body": "未分类的内部正文",
                                "source_ids": [],
                            }
                        ],
                        "content_data_level": "restricted",
                    },
                    ensure_ascii=False,
                ),
                created_by="user-a",
            )
            session.add(source)
            session.commit()
            source_id = source.id

        denied = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-storyboard",
            headers=user_headers,
            json={"count": 2, "source_artifact_id": source_id},
        )
        assert denied.status_code == 403
        assert "restricted" in denied.json()["detail"]
        assert knowledge_provider.queries == []
        assert text_provider.calls == 0
    finally:
        app.dependency_overrides.pop(get_text_provider, None)
        app.dependency_overrides.pop(get_knowledge_provider, None)


def test_content_provider_code_has_no_supplier_or_test_fixture_defaults():
    adapter_source = (CONTENT_SRC / "content_service" / "llm_adapters.py").read_text(
        encoding="utf-8"
    )
    config_source = (CONTENT_SRC / "content_service" / "config.py").read_text(encoding="utf-8")
    assert "deepseek" not in adapter_source.lower()
    assert "dashscope" not in adapter_source.lower()
    assert "tests/fixtures" not in config_source.replace("\\", "/")


def test_text_export_and_generated_media_versions_are_immutable(
    content_client,
    user_headers,
):
    task = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "full",
            "name": f"版本留存-{uuid4().hex[:8]}",
            "topic": "臻品文化",
            "style": "正式、克制",
        },
    ).json()
    copy = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-copy",
        headers=user_headers,
        json={"count": 1},
    ).json()
    copy_file = content_client.get(
        f"/api/v1/artifacts/{copy['id']}/download",
        headers=user_headers,
    )
    assert copy_file.status_code == 200
    assert copy_file.headers["content-type"].startswith("text/plain")
    assert "臻品文化" in copy_file.content.decode("utf-8")

    image_results = []
    for _ in range(2):
        generated = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-images",
            headers=user_headers,
            json={
                "count": 1,
                "parameters": {"size": "320x480", "ratio": "2:3"},
            },
        ).json()[0]
        image_results.append(generated)

    assert image_results[0]["storage_key"] != image_results[1]["storage_key"]
    for artifact in image_results:
        downloaded = content_client.get(
            f"/api/v1/artifacts/{artifact['id']}/download",
            headers=user_headers,
        )
        assert downloaded.status_code == 200
        with Image.open(io.BytesIO(downloaded.content)) as image:
            assert image.size == (320, 480)


def test_storyboard_source_is_isolated_to_copy_artifacts_in_same_task(
    content_client,
    user_headers,
):
    first_task = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={"mode": "copy", "name": f"来源任务-{uuid4().hex[:8]}"},
    ).json()
    second_task = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={"mode": "video", "name": f"目标任务-{uuid4().hex[:8]}"},
    ).json()
    copy = content_client.post(
        f"/api/v1/tasks/{first_task['id']}/generate-copy",
        headers=user_headers,
        json={"count": 1},
    ).json()
    image = content_client.post(
        f"/api/v1/tasks/{second_task['id']}/generate-images",
        headers=user_headers,
        json={"count": 1},
    ).json()[0]

    cross_task = content_client.post(
        f"/api/v1/tasks/{second_task['id']}/generate-storyboard",
        headers=user_headers,
        json={"count": 2, "source_artifact_id": copy["id"]},
    )
    wrong_kind = content_client.post(
        f"/api/v1/tasks/{second_task['id']}/generate-storyboard",
        headers=user_headers,
        json={"count": 2, "source_artifact_id": image["id"]},
    )

    assert cross_task.status_code == 422
    assert "当前任务" in cross_task.json()["detail"]
    assert wrong_kind.status_code == 422
    assert "文案版本" in wrong_kind.json()["detail"]


def test_human_revision_cannot_replace_generated_reference_provenance(
    content_client,
    user_headers,
):
    token = uuid4().hex
    library = content_client.post(
        "/api/v1/library/items",
        headers=user_headers,
        json={
            "category": "brand",
            "name": f"可追溯资料-{token}",
            "text_content": f"{token} 可核验品牌事实",
            "data_level": "public",
        },
    ).json()
    task = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={"mode": "copy", "name": f"人工修订-{token}", "topic": token},
    ).json()
    generated = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-copy",
        headers=user_headers,
        json={"count": 1},
    ).json()
    reference = next(
        item
        for item in generated["content"]["references"]
        if item["resource_entry_id"] == library["resource_entry_id"]
    )
    assert reference["resource_version_id"] == library["resource_version_id"]
    assert reference["legacy_library_item_id"] == library["id"]
    assert reference["id"] in generated["content"]["reference_item_ids"]
    assert generated["content"]["content_data_level"] == "internal"

    revised = content_client.patch(
        f"/api/v1/artifacts/{generated['id']}",
        headers=user_headers,
        json={
            "expected_version": generated["version"],
            "content": {
                "candidates": [
                    {
                        "title": "人工修订",
                        "summary": "摘要",
                        "body": "正文",
                        "source_ids": ["forged-reference"],
                    }
                ],
                "reference_item_ids": ["forged-reference"],
                "provider": "forged-provider",
                "generation": {"model": "forged-model"},
                "references": [
                    {"id": "forged-reference", "data_level": "public"},
                ],
            },
        },
    )

    assert revised.status_code == 200, revised.text
    content = revised.json()["content"]
    assert content["reference_item_ids"] == generated["content"]["reference_item_ids"]
    assert content["references"] == generated["content"]["references"]
    assert content["candidates"][0]["source_ids"] == []
    assert content["content_data_level"] == "internal"
    assert "provider" not in content
    assert content["source_generation"] == generated["content"]["generation"]
    assert content["revision"]["method"] == "human"
