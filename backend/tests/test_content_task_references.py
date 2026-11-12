import base64
from typing import ClassVar
from uuid import uuid4

import pytest
from sqlalchemy import Text, event
from sqlalchemy.sql import operators, visitors
from sqlalchemy.sql.elements import BinaryExpression

from content_service.generation import (
    MAX_KNOWLEDGE_CHUNK_CHARS,
    MAX_KNOWLEDGE_CONTEXT_CHARS,
    MAX_KNOWLEDGE_SUMMARY_CHARS,
    _bounded_knowledge_context,
    _knowledge_summary,
)
from content_service.main import app, grounded_video_prompt
from content_service.provider_dependencies import (
    get_ai_video_generation_port,
    get_image_generation_port,
    get_knowledge_provider,
    get_text_provider,
)
from content_service.providers import (
    AiVideoGenerationRequest,
    AiVideoTask,
    ContentGenerationInput,
    GeneratedAsset,
    ImageGenerationRequest,
    KnowledgeChunk,
    KnowledgeQuery,
    MockTextProvider,
    ProviderStatus,
    TextGenerationResult,
)
from content_service.weknora import WeKnoraError


@pytest.fixture(autouse=True)
def require_lob_compatible_knowledge_queries(content_client):
    """Exercise real request paths without SQLite hiding CLOB comparisons."""
    from content_service import db

    def reject_clob_string_comparison(_connection, clause, *_args):
        for node in visitors.iterate(clause):
            if (
                isinstance(node, BinaryExpression)
                and node.operator in {operators.eq, operators.ne}
                and isinstance(getattr(node.left, "type", None), Text)
                and isinstance(getattr(node.right, "value", None), str)
            ):
                pytest.fail("Knowledge query compares a CLOB to a string in SQL")

    event.listen(db.engine, "before_execute", reject_clob_string_comparison)
    try:
        yield
    finally:
        event.remove(db.engine, "before_execute", reject_clob_string_comparison)


def create_text_resource(
    client,
    headers,
    *,
    resource_kind: str = "document",
    category: str = "culture",
    name: str,
    text: str,
    data_level: str = "internal",
) -> dict:
    response = client.post(
        "/api/v1/library/items",
        headers=headers,
        json={
            "resource_kind": resource_kind,
            "category": category,
            "name": name,
            "text_content": text,
            "data_level": data_level,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def upload_visual_resource(
    client,
    headers,
    *,
    resource_kind: str,
    name: str,
    content: bytes,
    data_level: str = "internal",
    category: str = "brand_visual",
    parent_id: str | None = None,
) -> dict:
    params = {
        "resource_kind": resource_kind,
        "category": category,
        "name": name,
        "data_level": data_level,
        "notes": f"{name} 仅用于本次品牌视觉测试",
    }
    if parent_id is not None:
        params["parent_id"] = parent_id
    response = client.post(
        "/api/v1/library/items/upload",
        headers=headers,
        params=params,
        files={"file": (f"{name}.png", content, "image/png")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_video_task_and_project(
    client,
    headers,
    *,
    token: str,
    knowledge_resource_ids: list[str],
    product_resource_id: str | None = None,
) -> tuple[dict, dict]:
    task_response = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "mode": "video",
            "name": f"知识驱动视频-{token}",
            "topic": "企业品牌主视觉",
            "content_type": "品牌短视频",
            "style": "品牌宣传型",
            "audience": "目标消费者",
            "knowledge_resource_ids": knowledge_resource_ids,
            "product_resource_id": product_resource_id,
        },
    )
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()
    project_response = client.post(
        f"/api/v1/tasks/{task['id']}/video-project",
        headers=headers,
    )
    assert project_response.status_code == 200, project_response.text
    return task, project_response.json()


class CapturingTextProvider:
    status = ProviderStatus(
        provider_id="capturing-text",
        name="capturing-text",
        capability="text-generation",
        mode="test",
        external=False,
        enabled=True,
        model="capturing-text-v1",
    )
    capabilities: ClassVar[dict] = {}

    def __init__(self):
        self.last_request: ContentGenerationInput | None = None

    def topics(self, request: ContentGenerationInput, count: int) -> TextGenerationResult:
        self.last_request = request
        return MockTextProvider().topics(request, count)

    def copies(
        self,
        request: ContentGenerationInput,
        count: int,
    ) -> TextGenerationResult:
        self.last_request = request
        source_ids = [item.id for item in request.knowledge]
        return TextGenerationResult(
            [
                {
                    "title": f"显式资料文案 {index + 1}",
                    "summary": "只使用任务显式选择的企业资料",
                    "body": "已根据所选企业文化与产品资料生成。",
                    "source_ids": source_ids,
                }
                for index in range(count)
            ],
            self.status.provider_id,
            self.status.model or "",
        )


class FailingKnowledgeProvider:
    status = ProviderStatus(
        provider_id="must-not-search",
        name="must-not-search",
        capability="knowledge-search",
        mode="test",
        external=False,
        enabled=True,
    )

    def search(self, query: KnowledgeQuery):
        raise AssertionError(f"显式选择资料时不应回退到全库搜索：{query.text}")


class PendingSelectedKnowledgeProvider(FailingKnowledgeProvider):
    def search_selected(self, *_args, **_kwargs):
        raise WeKnoraError("selected_resource_not_indexed")


class CapturingImageProvider:
    status = ProviderStatus(
        provider_id="capturing-image",
        name="capturing-image",
        capability="image-generation",
        mode="test",
        external=True,
        enabled=True,
        model="capturing-image-v1",
    )
    capabilities: ClassVar[dict] = {"count": {"max": 6}}

    def __init__(self):
        self.last_request: ImageGenerationRequest | None = None

    def generate(self, request: ImageGenerationRequest):
        self.last_request = request
        return (
            GeneratedAsset(
                content=b"\x89PNG\r\n\x1a\n-generated-test-image",
                media_type="image/png",
                extension="png",
                provider_id=self.status.provider_id,
                model=self.status.model or "",
            ),
        )


class SequencedCapturingImageProvider(CapturingImageProvider):
    """Capture every call and return distinct bytes for reference-chain assertions."""

    def __init__(self):
        super().__init__()
        self.requests: list[ImageGenerationRequest] = []

    def generate(self, request: ImageGenerationRequest):
        self.last_request = request
        self.requests.append(request)
        sequence = len(self.requests)
        return (
            GeneratedAsset(
                content=(b"\x89PNG\r\n\x1a\n-generated-sequence-" + str(sequence).encode()),
                media_type="image/png",
                extension="png",
                provider_id=self.status.provider_id,
                model=self.status.model or "",
            ),
        )


class CapturingAiVideoProvider:
    status = ProviderStatus(
        provider_id="capturing-ai-video",
        name="capturing-ai-video",
        capability="ai-video-generation",
        mode="test",
        external=True,
        enabled=True,
        model="capturing-ai-video-v1",
    )
    capabilities: ClassVar[dict] = {
        "async": True,
        "text_to_video": True,
        "reference_images": {"max": 9},
        "durations": [5],
        "ratios": ["16:9"],
        "resolutions": ["720p"],
    }

    def __init__(self):
        self.last_request: AiVideoGenerationRequest | None = None

    def submit(self, request: AiVideoGenerationRequest):
        self.last_request = request
        return AiVideoTask(
            provider_task_id=uuid4().hex,
            status="queued",
            provider_id=self.status.provider_id,
            model=self.status.model or "",
            progress=0,
        )

    def get(self, provider_task_id: str):
        return AiVideoTask(
            provider_task_id=provider_task_id,
            status="failed",
            provider_id=self.status.provider_id,
            model=self.status.model or "",
            progress=100,
            error_message="测试任务无需生成实际视频",
        )

    def cancel(self, provider_task_id: str):
        return self.get(provider_task_id)


def test_capabilities_expose_effective_reference_media_limits(
    content_client,
    user_headers,
):
    response = content_client.get("/api/v1/capabilities", headers=user_headers)
    assert response.status_code == 200, response.text
    capabilities = {item["capability"]: item for item in response.json()["capabilities"]}
    image_parameters = capabilities["image-generation"]["parameters"]
    video_parameters = capabilities["ai-video-generation"]["parameters"]

    assert image_parameters["reference_images"]["max"] == 9
    assert video_parameters["reference_images"]["max"] == 9
    assert video_parameters["reference_videos"]["max"] == 3
    assert video_parameters["reference_videos"]["total_duration_seconds"] == 15
    assert video_parameters["reference_videos"]["project_asset_transport_available"] is True
    assert video_parameters["reference_videos"]["project_asset_transport"] == "internal-direct"


def test_external_capabilities_disable_project_video_references_without_public_base(
    content_client,
    user_headers,
):
    class ExternalVideoProvider(CapturingAiVideoProvider):
        status = ProviderStatus(
            provider_id="external-video",
            name="external-video",
            capability="ai-video-generation",
            mode="api",
            external=True,
            enabled=True,
            model="external-video-v1",
        )
        capabilities: ClassVar[dict] = {
            **CapturingAiVideoProvider.capabilities,
            "reference_images": {"max": 9},
            "reference_videos": {"max": 3, "total_duration_seconds": 15},
        }

    app.dependency_overrides[get_ai_video_generation_port] = ExternalVideoProvider
    try:
        response = content_client.get("/api/v1/capabilities", headers=user_headers)
        assert response.status_code == 200, response.text
        video = next(
            item
            for item in response.json()["capabilities"]
            if item["capability"] == "ai-video-generation"
        )
        assert video["parameters"]["reference_videos"]["project_asset_transport_available"] is False
        assert video["parameters"]["reference_videos"]["project_asset_transport"] == ("unavailable")
    finally:
        app.dependency_overrides.pop(get_ai_video_generation_port, None)


def test_external_capabilities_enable_project_video_references_with_runtime_upload(
    content_client,
    user_headers,
):
    class RuntimeUploadVideoProvider(CapturingAiVideoProvider):
        status = ProviderStatus(
            provider_id="hiagent-video",
            name="hiagent-video",
            capability="ai-video-generation",
            mode="api",
            external=True,
            enabled=True,
            model="hiagent-video-v1",
        )
        capabilities: ClassVar[dict] = {
            **CapturingAiVideoProvider.capabilities,
            "reference_images": {"max": 9},
            "reference_videos": {
                "max": 1,
                "total_duration_seconds": 15,
                "local_file_upload": True,
            },
        }

    app.dependency_overrides[get_ai_video_generation_port] = RuntimeUploadVideoProvider
    try:
        response = content_client.get("/api/v1/capabilities", headers=user_headers)
        assert response.status_code == 200, response.text
        video = next(
            item
            for item in response.json()["capabilities"]
            if item["capability"] == "ai-video-generation"
        )
        assert video["parameters"]["reference_videos"]["project_asset_transport_available"] is True
        assert video["parameters"]["reference_videos"]["project_asset_transport"] == (
            "hiagent-runtime-upload"
        )
    finally:
        app.dependency_overrides.pop(get_ai_video_generation_port, None)


def test_task_round_trips_deduplicated_knowledge_resources_and_content_lengths(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    culture = create_text_resource(
        content_client,
        user_headers,
        name=f"臻品文化-{token}",
        text=f"文化事实-{token}",
    )
    product = create_text_resource(
        content_client,
        user_headers,
        resource_kind="product",
        category="product",
        name=f"产品资料-{token}",
        text=f"产品事实-{token}",
    )
    logo = upload_visual_resource(
        content_client,
        user_headers,
        resource_kind="logo",
        name=f"品牌Logo-{token}",
        content=b"\x89PNG\r\n\x1a\n-logo-round-trip",
    )

    created_response = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "copy",
            "name": f"多资料任务-{token}",
            "product_resource_id": product["resource_entry_id"],
            # Mix stable entry IDs and legacy item IDs, with duplicates. The API
            # must persist one canonical ResourceEntry ID per selected resource.
            "knowledge_resource_ids": [
                culture["resource_entry_id"],
                product["id"],
                culture["resource_entry_id"],
            ],
            "length": "短",
        },
    )
    assert created_response.status_code == 200, created_response.text
    created = created_response.json()
    assert created["length"] == "短"
    assert created["knowledge_resource_ids"] == [
        culture["resource_entry_id"],
        product["resource_entry_id"],
    ]

    fetched = content_client.get(
        f"/api/v1/tasks/{created['id']}",
        headers=user_headers,
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["knowledge_resource_ids"] == created["knowledge_resource_ids"]

    patched_response = content_client.patch(
        f"/api/v1/tasks/{created['id']}",
        headers=user_headers,
        json={
            "expected_version": created["version"],
            "knowledge_resource_ids": [
                logo["id"],
                culture["id"],
                logo["resource_entry_id"],
            ],
            "length": "长",
        },
    )
    assert patched_response.status_code == 200, patched_response.text
    patched = patched_response.json()
    assert patched["length"] == "长"
    assert patched["knowledge_resource_ids"] == [
        logo["resource_entry_id"],
        culture["resource_entry_id"],
    ]

    legacy_response = content_client.patch(
        f"/api/v1/tasks/{created['id']}",
        headers=user_headers,
        json={"expected_version": patched["version"], "length": "中等"},
    )
    assert legacy_response.status_code == 200, legacy_response.text
    assert legacy_response.json()["length"] == "中"

    default_response = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={"mode": "copy", "name": f"默认长度-{token}"},
    )
    assert default_response.status_code == 200, default_response.text
    assert default_response.json()["length"] == "中"

    invalid_create = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={"mode": "copy", "name": f"非法长度-{token}", "length": "超长"},
    )
    assert invalid_create.status_code == 422

    current = legacy_response.json()
    invalid_patch = content_client.patch(
        f"/api/v1/tasks/{created['id']}",
        headers=user_headers,
        json={"expected_version": current["version"], "length": "超长"},
    )
    assert invalid_patch.status_code == 422
    unchanged = content_client.get(f"/api/v1/tasks/{created['id']}", headers=user_headers).json()
    assert unchanged["version"] == current["version"]
    assert unchanged["length"] == "中"


def test_product_reference_replacement_removes_stale_grounding(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    culture = create_text_resource(
        content_client,
        user_headers,
        name=f"产品切换文化-{token}",
        text=f"文化事实-{token}",
    )
    product_a = create_text_resource(
        content_client,
        user_headers,
        resource_kind="product",
        category="product",
        name=f"产品A-{token}",
        text=f"产品A事实-{token}",
    )
    product_b = create_text_resource(
        content_client,
        user_headers,
        resource_kind="product",
        category="product",
        name=f"产品B-{token}",
        text=f"产品B事实-{token}",
    )
    created_response = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "copy",
            "name": f"产品切换任务-{token}",
            "product_resource_id": product_a["resource_entry_id"],
            "knowledge_resource_ids": [culture["resource_entry_id"]],
        },
    )
    assert created_response.status_code == 200, created_response.text
    created = created_response.json()

    replaced_response = content_client.patch(
        f"/api/v1/tasks/{created['id']}",
        headers=user_headers,
        json={
            "expected_version": created["version"],
            "product_resource_id": product_b["resource_entry_id"],
            "knowledge_resource_ids": [
                culture["resource_entry_id"],
                product_a["resource_entry_id"],
            ],
        },
    )
    assert replaced_response.status_code == 200, replaced_response.text
    replaced = replaced_response.json()
    assert replaced["knowledge_resource_ids"] == [
        culture["resource_entry_id"],
        product_b["resource_entry_id"],
    ]

    detached_response = content_client.patch(
        f"/api/v1/tasks/{created['id']}",
        headers=user_headers,
        json={
            "expected_version": replaced["version"],
            "product": "自由文本产品",
        },
    )
    assert detached_response.status_code == 200, detached_response.text
    detached = detached_response.json()
    assert detached["product_resource_id"] is None
    assert detached["knowledge_resource_ids"] == [culture["resource_entry_id"]]


def test_text_generation_knowledge_context_has_a_total_budget_and_keeps_each_source():
    chunks = tuple(
        KnowledgeChunk(
            id=f"knowledge-{index}",
            title=f"知识资料 {index}",
            text=str(index) * 5_000,
            category="culture",
            data_level="internal",
        )
        for index in range(20)
    )

    bounded = _bounded_knowledge_context(chunks)

    assert [item.id for item in bounded] == [item.id for item in chunks]
    assert sum(len(item.text) for item in bounded) <= MAX_KNOWLEDGE_CONTEXT_CHARS
    assert all(len(item.text) <= MAX_KNOWLEDGE_CHUNK_CHARS for item in bounded)
    assert all(item.metadata["context_truncated"] is True for item in bounded)


def test_selected_knowledge_is_summarized_locally_with_facts_first_and_a_hard_limit():
    summary, source_chars, truncated = _knowledge_summary(
        title="窖藏雅酿",
        resource_kind="product",
        category="产品资料",
        facts=["酒精度：53%vol", "净含量：500mL"],
        notes="品牌名称和产品规格不得改写",
        tags=["窖藏系列", "酱香型白酒"],
        relevant_text="与当前任务相关的正文。" + ("次要背景资料" * 800),
    )

    assert summary.startswith("资料信息：")
    assert "关键事实：酒精度：53%vol" in summary
    assert "规范备注：品牌名称和产品规格不得改写" in summary
    assert "标签：窖藏系列" in summary
    assert "相关内容：与当前任务相关的正文" in summary
    assert summary.index("关键事实：") < summary.index("相关内容：")
    assert len(summary) <= MAX_KNOWLEDGE_SUMMARY_CHARS
    assert source_chars > len(summary)
    assert truncated is True


def test_task_rejects_unauthorized_and_inactive_knowledge_resources(
    content_client,
    user_headers,
    other_headers,
):
    token = uuid4().hex[:8]
    private_resource = create_text_resource(
        content_client,
        user_headers,
        name=f"用户A内部资料-{token}",
        text=f"内部事实-{token}",
    )
    public_resource = create_text_resource(
        content_client,
        user_headers,
        name=f"公开资料-{token}",
        text=f"公开事实-{token}",
        data_level="public",
    )
    inactive_resource = create_text_resource(
        content_client,
        other_headers,
        name=f"待停用资料-{token}",
        text=f"待停用事实-{token}",
    )
    deleted = content_client.delete(
        f"/api/v1/library/items/{inactive_resource['id']}",
        headers=other_headers,
    )
    assert deleted.status_code == 200, deleted.text

    forbidden_create = content_client.post(
        "/api/v1/tasks",
        headers=other_headers,
        json={
            "mode": "copy",
            "name": f"越权新建-{token}",
            "knowledge_resource_ids": [private_resource["resource_entry_id"]],
        },
    )
    assert forbidden_create.status_code == 403

    accessible = content_client.post(
        "/api/v1/tasks",
        headers=other_headers,
        json={
            "mode": "copy",
            "name": f"公开资料任务-{token}",
            "knowledge_resource_ids": [public_resource["resource_entry_id"]],
        },
    )
    assert accessible.status_code == 200, accessible.text

    forbidden_patch = content_client.patch(
        f"/api/v1/tasks/{accessible.json()['id']}",
        headers=other_headers,
        json={
            "expected_version": accessible.json()["version"],
            "knowledge_resource_ids": [private_resource["resource_entry_id"]],
        },
    )
    assert forbidden_patch.status_code == 403

    inactive_create = content_client.post(
        "/api/v1/tasks",
        headers=other_headers,
        json={
            "mode": "copy",
            "name": f"停用资料任务-{token}",
            "knowledge_resource_ids": [inactive_resource["resource_entry_id"]],
        },
    )
    assert inactive_create.status_code == 422
    assert "停用" in inactive_create.json()["detail"]


@pytest.mark.parametrize("mode", ["full", "copy"])
@pytest.mark.parametrize("selection", ["knowledge", "product"])
def test_selected_resources_generate_titles_and_copy_before_knowledge_index_is_ready(
    content_client,
    user_headers,
    mode,
    selection,
):
    token = uuid4().hex[:8]
    product_fact = f"产品酒体与规格事实-{token}"
    product = create_text_resource(
        content_client, user_headers, resource_kind="product", category="product",
        name=f"未完成索引产品-{token}", text=product_fact,
    )
    field = "knowledge_resource_ids" if selection == "knowledge" else "product_resource_id"
    value = [product["id"]] if selection == "knowledge" else product["resource_entry_id"]
    created = content_client.post("/api/v1/tasks", headers=user_headers, json={
        "mode": mode, "name": f"未索引资料创作-{token}", "topic": "产品宣传", field: value,
    })
    assert created.status_code == 200, created.text
    task = created.json()
    assert task["knowledge_resource_ids"] == [product["resource_entry_id"]]
    provider = CapturingTextProvider()
    app.dependency_overrides[get_text_provider] = lambda: provider
    app.dependency_overrides[get_knowledge_provider] = PendingSelectedKnowledgeProvider
    try:
        titles = content_client.post(
            f"/api/v1/tasks/{task['id']}/stages/topic/generate", headers=user_headers,
            json={"count": 1},
        )
        assert titles.status_code == 200, titles.text
        assert product_fact in provider.last_request.knowledge[0].text
        title = titles.json()["artifacts"][0]
        confirmed = content_client.post(
            f"/api/v1/artifacts/{title['id']}/confirm", headers=user_headers,
        )
        assert confirmed.status_code == 200, confirmed.text
        copies = content_client.post(
            f"/api/v1/tasks/{task['id']}/stages/copy/generate", headers=user_headers,
            json={"count": 1},
        )
        assert copies.status_code == 200, copies.text
        assert product_fact in provider.last_request.knowledge[0].text
        assert provider.last_request.knowledge[0].metadata["entry_id"] == product["resource_entry_id"]
        assert copies.json()["artifacts"][0]["content"]["candidates"][0]["body"]
    finally:
        app.dependency_overrides.pop(get_text_provider, None)
        app.dependency_overrides.pop(get_knowledge_provider, None)


def test_selected_culture_and_product_resources_reach_copy_provider_knowledge(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    culture_text = f"臻品文化核心事实-{token}"
    product_text = f"产品酒体与规格事实-{token}"
    culture = create_text_resource(
        content_client,
        user_headers,
        name=f"文化资料-{token}",
        text=culture_text,
    )
    product = create_text_resource(
        content_client,
        user_headers,
        resource_kind="product",
        category="product",
        name=f"产品资料-{token}",
        text=product_text,
    )
    # This resource is intentionally a stronger lexical match but is not selected.
    unselected = create_text_resource(
        content_client,
        user_headers,
        name=f"未选择资料-{token}",
        text=f"精准主题 精准主题 精准主题 不应进入模型-{token}",
    )
    task_response = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "copy",
            "name": f"显式知识文案-{token}",
            "topic": "精准主题",
            "knowledge_resource_ids": [
                culture["resource_entry_id"],
                product["resource_entry_id"],
            ],
            "length": "短",
        },
    )
    assert task_response.status_code == 200, task_response.text

    text_provider = CapturingTextProvider()
    app.dependency_overrides[get_text_provider] = lambda: text_provider
    app.dependency_overrides[get_knowledge_provider] = lambda: FailingKnowledgeProvider()
    try:
        generated = content_client.post(
            f"/api/v1/tasks/{task_response.json()['id']}/generate-copy",
            headers=user_headers,
            json={"count": 1},
        )
        assert generated.status_code == 200, generated.text
        assert text_provider.last_request is not None
        knowledge = text_provider.last_request.knowledge
        assert [item.metadata["entry_id"] for item in knowledge] == [
            culture["resource_entry_id"],
            product["resource_entry_id"],
        ]
        assert culture_text in knowledge[0].text
        assert product_text in knowledge[1].text
        assert text_provider.last_request.length == "短"
        assert all(
            item.metadata["entry_id"] != unselected["resource_entry_id"] for item in knowledge
        )
    finally:
        app.dependency_overrides.pop(get_text_provider, None)
        app.dependency_overrides.pop(get_knowledge_provider, None)


def test_selected_logo_and_image_become_image_references_and_text_enters_prompt(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    culture_text = f"企业文化视觉原则-{token}"
    product_text = f"产品包装与规格事实-{token}"
    logo_bytes = b"\x89PNG\r\n\x1a\n-selected-logo-" + token.encode()
    image_bytes = b"\x89PNG\r\n\x1a\n-selected-image-" + token.encode()
    culture = create_text_resource(
        content_client,
        user_headers,
        name=f"企业文化-{token}",
        text=culture_text,
    )
    product = create_text_resource(
        content_client,
        user_headers,
        resource_kind="product",
        category="product",
        name=f"产品信息-{token}",
        text=product_text,
    )
    logo = upload_visual_resource(
        content_client,
        user_headers,
        resource_kind="logo",
        name=f"企业Logo-{token}",
        content=logo_bytes,
    )
    image = upload_visual_resource(
        content_client,
        user_headers,
        resource_kind="image",
        name=f"产品主图-{token}",
        content=image_bytes,
    )
    knowledge_ids = [
        culture["resource_entry_id"],
        product["resource_entry_id"],
        logo["resource_entry_id"],
        image["resource_entry_id"],
    ]
    task_response = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "image",
            "name": f"知识驱动图片-{token}",
            "topic": "企业产品主视觉",
            "knowledge_resource_ids": knowledge_ids,
        },
    )
    assert task_response.status_code == 200, task_response.text

    image_provider = CapturingImageProvider()
    app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    try:
        generated = content_client.post(
            f"/api/v1/tasks/{task_response.json()['id']}/generate-images",
            headers=user_headers,
            json={"count": 1, "parameters": {"prompt": "企业产品主视觉"}},
        )
        assert generated.status_code == 200, generated.text
        assert image_provider.last_request is not None
        assert image_provider.last_request.reference_images == (
            logo_bytes,
            image_bytes,
        )
        assert image_provider.last_request.reference_media_types == (
            "image/png",
            "image/png",
        )
        assert culture_text in image_provider.last_request.prompt
        assert product_text in image_provider.last_request.prompt
        content = generated.json()[0]["content"]
        assert content["knowledge_resource_ids"] == knowledge_ids
        assert {item["resource_entry_id"] for item in content["references"]} == set(knowledge_ids)
    finally:
        app.dependency_overrides.pop(get_image_generation_port, None)


def test_product_child_visuals_are_primary_deduplicated_and_survive_refinement(
    content_client,
    other_headers,
):
    token = uuid4().hex[:8]
    product = create_text_resource(
        content_client,
        other_headers,
        resource_kind="product",
        category="product",
        name=f"红金产品-{token}",
        text=f"产品包装事实-{token}：红金瓶身与固定品牌标签。",
    )
    product_id = product["resource_entry_id"]
    bottle_bytes = b"\x89PNG\r\n\x1a\n-product-bottle-" + token.encode()
    logo_bytes = b"\x89PNG\r\n\x1a\n-product-logo-" + token.encode()
    bottle = upload_visual_resource(
        content_client,
        other_headers,
        resource_kind="image",
        category="product_image",
        parent_id=product_id,
        name=f"产品瓶图-{token}",
        content=bottle_bytes,
    )
    logo = upload_visual_resource(
        content_client,
        other_headers,
        resource_kind="logo",
        category="product_image",
        parent_id=product_id,
        name=f"产品标签-{token}",
        content=logo_bytes,
    )
    task_response = content_client.post(
        "/api/v1/tasks",
        headers=other_headers,
        json={
            "mode": "image",
            "name": f"产品参考图-{token}",
            "topic": "生成产品主视觉",
            "product_resource_id": product_id,
        },
    )
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()
    assert task["knowledge_resource_ids"] == [product_id]

    image_provider = CapturingImageProvider()
    app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    try:
        first_response = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-images",
            headers=other_headers,
            json={
                "count": 1,
                "parameters": {
                    "prompt": "生成红金产品海报",
                    # The same product image is also explicitly selected.  It must
                    # still reach the provider exactly once.
                    "reference_asset_ids": [bottle["resource_entry_id"]],
                },
            },
        )
        assert first_response.status_code == 200, first_response.text
        assert image_provider.last_request is not None
        assert image_provider.last_request.reference_images == (
            bottle_bytes,
            logo_bytes,
        )
        assert "保持瓶型、包装结构、品牌 Logo 与标签文字一致" in (
            image_provider.last_request.prompt
        )
        first = first_response.json()[0]
        assert first["content"]["knowledge_resource_ids"] == [product_id]
        assert first["content"]["visual_reference_resource_ids"] == [
            bottle["resource_entry_id"],
            logo["resource_entry_id"],
        ]

        refined_response = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-images",
            headers=other_headers,
            json={
                "count": 1,
                "source_artifact_id": first["id"],
                "instruction": "保持原产品，背景改成户外",
                "parameters": {
                    # Exercise the backend fallback even if an older client sends
                    # an empty knowledge list and repeats the source reference.
                    "knowledge_resource_ids": [],
                    "reference_artifact_ids": [first["id"]],
                },
            },
        )
        assert refined_response.status_code == 200, refined_response.text
        assert image_provider.last_request is not None
        assert image_provider.last_request.reference_images == (
            bottle_bytes,
            logo_bytes,
            b"\x89PNG\r\n\x1a\n-generated-test-image",
        )
        refined = refined_response.json()[0]
        assert refined["content"]["knowledge_resource_ids"] == [product_id]
        assert refined["content"]["visual_reference_resource_ids"] == [
            bottle["resource_entry_id"],
            logo["resource_entry_id"],
        ]
    finally:
        app.dependency_overrides.pop(get_image_generation_port, None)


def test_image_refinement_prompt_scopes_idempotency_and_preserves_references(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    product = create_text_resource(
        content_client,
        user_headers,
        resource_kind="product",
        category="product",
        name=f"幂等产品-{token}",
        text=f"产品事实-{token}：保留白色瓶身和红色品牌标签。",
    )
    product_id = product["resource_entry_id"]
    product_image_bytes = b"\x89PNG\r\n\x1a\n-idempotency-product-" + token.encode()
    product_image = upload_visual_resource(
        content_client,
        user_headers,
        resource_kind="image",
        category="product_image",
        parent_id=product_id,
        name=f"幂等产品主图-{token}",
        content=product_image_bytes,
    )
    task_response = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "image",
            "name": f"图片修改幂等-{token}",
            "topic": "生成产品主视觉",
            "product_resource_id": product_id,
        },
    )
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()

    image_provider = SequencedCapturingImageProvider()
    app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    try:
        source_response = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-images",
            headers=user_headers,
            json={"count": 1, "parameters": {"prompt": "产品海报源图"}},
        )
        reference_response = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-images",
            headers=user_headers,
            json={"count": 1, "parameters": {"prompt": "辅助构图参考图"}},
        )
        assert source_response.status_code == 200, source_response.text
        assert reference_response.status_code == 200, reference_response.text
        source = source_response.json()[0]
        reference = reference_response.json()[0]
        shared_idempotency_key = f"refine-{token}"

        first_refinement_response = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-images",
            headers=user_headers,
            json={
                "count": 1,
                "source_artifact_id": source["id"],
                "instruction": "保持产品和构图，把背景改成清晨薄雾",
                "parameters": {
                    "prompt": "基于源图修改",
                    "reference_artifact_ids": [reference["id"]],
                },
                "idempotency_key": shared_idempotency_key,
            },
        )
        second_refinement_response = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-images",
            headers=user_headers,
            json={
                "count": 1,
                "source_artifact_id": source["id"],
                "instruction": "保持产品和构图，把背景改成深夜金色灯光",
                "parameters": {
                    "prompt": "基于源图修改",
                    "reference_artifact_ids": [reference["id"]],
                },
                "idempotency_key": shared_idempotency_key,
            },
        )
        assert first_refinement_response.status_code == 200, first_refinement_response.text
        assert second_refinement_response.status_code == 200, second_refinement_response.text
        first_refinement = first_refinement_response.json()[0]
        second_refinement = second_refinement_response.json()[0]

        assert len(image_provider.requests) == 4
        first_provider_request, second_provider_request = image_provider.requests[-2:]
        assert "清晨薄雾" in first_provider_request.prompt
        assert "深夜金色灯光" in second_provider_request.prompt
        expected_references = (
            product_image_bytes,
            b"\x89PNG\r\n\x1a\n-generated-sequence-1",
            b"\x89PNG\r\n\x1a\n-generated-sequence-2",
        )
        assert first_provider_request.reference_images == expected_references
        assert second_provider_request.reference_images == expected_references
        assert first_refinement["id"] != second_refinement["id"]
        assert second_refinement["version"] == first_refinement["version"] + 1
        assert first_refinement["parent_id"] == source["id"]
        assert second_refinement["parent_id"] == source["id"]
        assert first_refinement["content"]["source_artifact_id"] == source["id"]
        assert second_refinement["content"]["source_artifact_id"] == source["id"]
        assert first_refinement["content"]["reference_artifact_ids"] == [reference["id"]]
        assert second_refinement["content"]["reference_artifact_ids"] == [reference["id"]]
        assert first_refinement["content"]["visual_reference_resource_ids"] == [
            product_image["resource_entry_id"]
        ]
        assert second_refinement["content"]["visual_reference_resource_ids"] == [
            product_image["resource_entry_id"]
        ]

        first_run_response = content_client.get(
            f"/api/v1/generation-runs/{first_refinement['generation_run_id']}",
            headers=user_headers,
        )
        second_run_response = content_client.get(
            f"/api/v1/generation-runs/{second_refinement['generation_run_id']}",
            headers=user_headers,
        )
        assert first_run_response.status_code == 200, first_run_response.text
        assert second_run_response.status_code == 200, second_run_response.text
        first_run = first_run_response.json()
        second_run = second_run_response.json()
        assert first_run["idempotency_key"] != second_run["idempotency_key"]
        assert first_run["idempotency_key"].startswith(f"{shared_idempotency_key}:")
        assert second_run["idempotency_key"].startswith(f"{shared_idempotency_key}:")
        assert len(first_run["idempotency_key"]) <= 128
        assert len(second_run["idempotency_key"]) <= 128
        for run in (first_run, second_run):
            assert run["parameters"]["source_artifact_id"] == source["id"]
            assert run["parameters"]["reference_artifact_ids"] == [reference["id"]]

        replay_response = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-images",
            headers=user_headers,
            json={
                "count": 1,
                "source_artifact_id": source["id"],
                "instruction": "保持产品和构图，把背景改成深夜金色灯光",
                "parameters": {
                    "prompt": "基于源图修改",
                    "reference_artifact_ids": [reference["id"]],
                },
                "idempotency_key": shared_idempotency_key,
            },
        )
        assert replay_response.status_code == 200, replay_response.text
        assert replay_response.json()[0]["id"] == second_refinement["id"]
        assert len(image_provider.requests) == 4
    finally:
        app.dependency_overrides.pop(get_image_generation_port, None)


def test_external_image_rejects_restricted_product_child_before_provider(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    product = create_text_resource(
        content_client,
        user_headers,
        resource_kind="product",
        category="product",
        name=f"受限子图产品-{token}",
        text="可用于提示词的内部产品资料。",
    )
    product_id = product["resource_entry_id"]
    upload_visual_resource(
        content_client,
        user_headers,
        resource_kind="image",
        category="product_image",
        parent_id=product_id,
        name=f"受限产品瓶图-{token}",
        content=b"\x89PNG\r\n\x1a\n-restricted-product-" + token.encode(),
        data_level="restricted",
    )
    task = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "image",
            "name": f"受限产品图-{token}",
            "topic": "生成产品主视觉",
            "product_resource_id": product_id,
        },
    ).json()

    image_provider = CapturingImageProvider()
    app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    try:
        generated = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-images",
            headers=user_headers,
            json={"count": 1},
        )
        assert generated.status_code == 403, generated.text
        assert "受限" in generated.json()["detail"]
        assert image_provider.last_request is None
    finally:
        app.dependency_overrides.pop(get_image_generation_port, None)


def test_product_relationship_does_not_bypass_child_media_ownership(
    content_client,
    user_headers,
    other_headers,
):
    token = uuid4().hex[:8]
    product = create_text_resource(
        content_client,
        other_headers,
        resource_kind="product",
        category="product",
        name=f"他人子图产品-{token}",
        text="产品资料。",
    )
    product_id = product["resource_entry_id"]
    upload_visual_resource(
        content_client,
        user_headers,
        resource_kind="image",
        category="product_image",
        parent_id=product_id,
        name=f"不同所有者产品图-{token}",
        content=b"\x89PNG\r\n\x1a\n-cross-owner-product-" + token.encode(),
    )
    task_response = content_client.post(
        "/api/v1/tasks",
        headers=other_headers,
        json={
            "mode": "image",
            "name": f"产品子图权限-{token}",
            "topic": "生成产品主视觉",
            "product_resource_id": product_id,
        },
    )
    assert task_response.status_code == 200, task_response.text

    image_provider = CapturingImageProvider()
    app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    try:
        generated = content_client.post(
            f"/api/v1/tasks/{task_response.json()['id']}/generate-images",
            headers=other_headers,
            json={"count": 1},
        )
        assert generated.status_code == 403, generated.text
        assert "无权访问" in generated.json()["detail"]
        assert image_provider.last_request is None
    finally:
        app.dependency_overrides.pop(get_image_generation_port, None)


def test_product_child_visuals_share_the_nine_reference_limit(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    product = create_text_resource(
        content_client,
        user_headers,
        resource_kind="product",
        category="product",
        name=f"超限产品-{token}",
        text="产品资料。",
    )
    product_id = product["resource_entry_id"]
    for index in range(10):
        upload_visual_resource(
            content_client,
            user_headers,
            resource_kind="image",
            category="product_image",
            parent_id=product_id,
            name=f"产品角度-{token}-{index}",
            content=(b"\x89PNG\r\n\x1a\n-product-angle-" + token.encode() + str(index).encode()),
        )
    task = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "image",
            "name": f"产品图超限-{token}",
            "topic": "生成产品主视觉",
            "product_resource_id": product_id,
        },
    ).json()

    image_provider = CapturingImageProvider()
    app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    try:
        generated = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-images",
            headers=user_headers,
            json={"count": 1},
        )
        assert generated.status_code == 422, generated.text
        assert "最多 9 张" in generated.json()["detail"]
        assert image_provider.last_request is None
    finally:
        app.dependency_overrides.pop(get_image_generation_port, None)


def test_generated_image_can_be_used_as_real_reference_for_a_new_version(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    task_response = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "image",
            "name": f"图片续作-{token}",
            "topic": "东方酒文化主视觉",
        },
    )
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()

    image_provider = CapturingImageProvider()
    app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    try:
        first_response = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-images",
            headers=user_headers,
            json={"count": 1, "parameters": {"prompt": "生成初版主视觉"}},
        )
        assert first_response.status_code == 200, first_response.text
        first = first_response.json()[0]

        second_response = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-images",
            headers=user_headers,
            json={
                "count": 1,
                "source_artifact_id": first["id"],
                "instruction": "保留主体构图，背景改成夜景并增强金色光线",
                "parameters": {"prompt": "基于当前图片继续生成"},
            },
        )
        assert second_response.status_code == 200, second_response.text
        assert image_provider.last_request is not None
        assert image_provider.last_request.reference_images == (
            b"\x89PNG\r\n\x1a\n-generated-test-image",
        )
        assert image_provider.last_request.reference_media_types == ("image/png",)
        assert "背景改成夜景" in image_provider.last_request.prompt

        second = second_response.json()[0]
        assert second["version"] == first["version"] + 1
        assert second["parent_id"] == first["id"]
        assert second["content"]["source_artifact_id"] == first["id"]
    finally:
        app.dependency_overrides.pop(get_image_generation_port, None)


def test_image_generation_combines_knowledge_visuals_with_local_uploads(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    knowledge_logo_bytes = b"\x89PNG\r\n\x1a\n-knowledge-logo-" + token.encode()
    local_reference_bytes = b"\x89PNG\r\n\x1a\n-local-reference-" + token.encode()
    knowledge_logo = upload_visual_resource(
        content_client,
        user_headers,
        resource_kind="logo",
        name=f"知识库Logo-{token}",
        content=knowledge_logo_bytes,
    )
    local_reference = upload_visual_resource(
        content_client,
        user_headers,
        resource_kind="image",
        name=f"本地参考图-{token}",
        content=local_reference_bytes,
    )
    task_response = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "image",
            "name": f"知识库与本地图片合并-{token}",
            "topic": "把产品放入场景并加入品牌Logo",
            "knowledge_resource_ids": [knowledge_logo["resource_entry_id"]],
        },
    )
    assert task_response.status_code == 200, task_response.text

    image_provider = CapturingImageProvider()
    app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    try:
        generated = content_client.post(
            f"/api/v1/tasks/{task_response.json()['id']}/generate-images",
            headers=user_headers,
            json={
                "count": 1,
                "parameters": {
                    "prompt": "合并参考图并保留Logo",
                    "reference_asset_ids": [local_reference["resource_entry_id"]],
                    "knowledge_resource_ids": [knowledge_logo["resource_entry_id"]],
                },
            },
        )
        assert generated.status_code == 200, generated.text
        assert image_provider.last_request is not None
        assert image_provider.last_request.reference_images == (
            local_reference_bytes,
            knowledge_logo_bytes,
        )
        assert image_provider.last_request.reference_media_types == (
            "image/png",
            "image/png",
        )
    finally:
        app.dependency_overrides.pop(get_image_generation_port, None)


def test_generated_image_refinement_rejects_cross_task_source(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    tasks = []
    for index, mode in enumerate(("image", "video")):
        response = content_client.post(
            "/api/v1/tasks",
            headers=user_headers,
            json={
                "mode": mode,
                "name": f"图片任务-{token}-{index}",
                "topic": "品牌主视觉",
            },
        )
        assert response.status_code == 200, response.text
        tasks.append(response.json())

    image_provider = CapturingImageProvider()
    app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    try:
        source_response = content_client.post(
            f"/api/v1/tasks/{tasks[0]['id']}/generate-images",
            headers=user_headers,
            json={"count": 1},
        )
        assert source_response.status_code == 200, source_response.text
        source = source_response.json()[0]

        rejected = content_client.post(
            f"/api/v1/tasks/{tasks[1]['id']}/generate-images",
            headers=user_headers,
            json={
                "count": 1,
                "source_artifact_id": source["id"],
                "instruction": "继续生成",
            },
        )
        assert rejected.status_code == 422, rejected.text
        assert "当前任务" in rejected.json()["detail"]

        rejected_reference = content_client.post(
            f"/api/v1/tasks/{tasks[1]['id']}/generate-images",
            headers=user_headers,
            json={
                "count": 1,
                "parameters": {"reference_artifact_ids": [source["id"]]},
            },
        )
        assert rejected_reference.status_code == 422, rejected_reference.text
        assert "正式回引" in rejected_reference.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_image_generation_port, None)


def test_generated_image_refinement_allows_linked_source_and_reference_artifact(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    source_task_response = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "image",
            "name": f"图片来源-{token}",
            "topic": "品牌主视觉",
        },
    )
    target_task_response = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "video",
            "name": f"视频目标-{token}",
            "topic": "品牌短视频",
        },
    )
    assert source_task_response.status_code == 200, source_task_response.text
    assert target_task_response.status_code == 200, target_task_response.text
    source_task = source_task_response.json()
    target_task = target_task_response.json()

    image_provider = CapturingImageProvider()
    app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    try:
        source_response = content_client.post(
            f"/api/v1/tasks/{source_task['id']}/generate-images",
            headers=user_headers,
            json={"count": 1},
        )
        assert source_response.status_code == 200, source_response.text
        source = source_response.json()[0]
        confirmed = content_client.post(
            f"/api/v1/artifacts/{source['id']}/confirm",
            headers=user_headers,
        )
        assert confirmed.status_code == 200, confirmed.text
        linked = content_client.post(
            f"/api/v1/tasks/{target_task['id']}/artifact-links",
            headers=user_headers,
            json={
                "source_task_id": source_task["id"],
                "artifact_ids": [source["id"]],
                "expected_version": target_task["version"],
            },
        )
        assert linked.status_code == 200, linked.text

        refined = content_client.post(
            f"/api/v1/tasks/{target_task['id']}/generate-images",
            headers=user_headers,
            json={
                "count": 1,
                "source_artifact_id": source["id"],
                "instruction": "保留产品主体并改为电影光影",
                "parameters": {
                    "reference_artifact_ids": [source["id"]],
                },
            },
        )
        assert refined.status_code == 200, refined.text
        assert image_provider.last_request is not None
        assert image_provider.last_request.reference_images == (
            b"\x89PNG\r\n\x1a\n-generated-test-image",
        )
        result = refined.json()[0]
        assert result["task_id"] == target_task["id"]
        assert result["parent_id"] == source["id"]
        assert result["content"]["source_artifact_id"] == source["id"]
    finally:
        app.dependency_overrides.pop(get_image_generation_port, None)


def test_explicit_and_knowledge_visual_with_same_storage_are_sent_once(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    logo_bytes = b"\x89PNG\r\n\x1a\n-deduplicated-logo-" + token.encode()
    logo = upload_visual_resource(
        content_client,
        user_headers,
        resource_kind="logo",
        name=f"去重Logo-{token}",
        content=logo_bytes,
    )
    task_response = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "image",
            "name": f"去重图片-{token}",
            "topic": "企业主视觉",
            "knowledge_resource_ids": [logo["resource_entry_id"]],
        },
    )
    assert task_response.status_code == 200, task_response.text

    image_provider = CapturingImageProvider()
    app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    try:
        generated = content_client.post(
            f"/api/v1/tasks/{task_response.json()['id']}/generate-images",
            headers=user_headers,
            json={
                "count": 1,
                "parameters": {
                    "prompt": "企业主视觉",
                    "reference_asset_ids": [logo["resource_entry_id"]],
                },
            },
        )
        assert generated.status_code == 200, generated.text
        assert image_provider.last_request is not None
        assert image_provider.last_request.reference_images == (logo_bytes,)
        assert image_provider.last_request.reference_media_types == ("image/png",)
    finally:
        app.dependency_overrides.pop(get_image_generation_port, None)


def test_external_image_rejects_restricted_explicit_media_before_provider(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    restricted = upload_visual_resource(
        content_client,
        user_headers,
        resource_kind="logo",
        name=f"受限Logo-{token}",
        content=b"\x89PNG\r\n\x1a\n-restricted-logo-" + token.encode(),
        data_level="restricted",
    )
    task = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={"mode": "image", "name": f"受限图片-{token}", "topic": "企业主视觉"},
    ).json()
    image_provider = CapturingImageProvider()
    app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    try:
        generated = content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-images",
            headers=user_headers,
            json={
                "count": 1,
                "parameters": {
                    "prompt": "企业主视觉",
                    "reference_asset_ids": [restricted["resource_entry_id"]],
                },
            },
        )
        assert generated.status_code == 403, generated.text
        assert "受限" in generated.json()["detail"]
        assert image_provider.last_request is None
    finally:
        app.dependency_overrides.pop(get_image_generation_port, None)


def test_video_project_images_inherit_task_knowledge_prompt_logo_and_provenance(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    culture_text = f"视频工程必须遵循的企业文化事实-{token}"
    product_text = f"视频工程产品事实-{token}"
    product_image_bytes = b"\x89PNG\r\n\x1a\n-video-project-product-" + token.encode()
    logo_bytes = b"\x89PNG\r\n\x1a\n-video-project-logo-" + token.encode()
    culture = create_text_resource(
        content_client,
        user_headers,
        name=f"视频企业文化-{token}",
        text=culture_text,
    )
    product = create_text_resource(
        content_client,
        user_headers,
        resource_kind="product",
        category="product",
        name=f"视频产品-{token}",
        text=product_text,
    )
    product_image = upload_visual_resource(
        content_client,
        user_headers,
        resource_kind="image",
        category="product_image",
        parent_id=product["resource_entry_id"],
        name=f"视频产品瓶图-{token}",
        content=product_image_bytes,
    )
    logo = upload_visual_resource(
        content_client,
        user_headers,
        resource_kind="logo",
        name=f"视频品牌Logo-{token}",
        content=logo_bytes,
    )
    knowledge_ids = [
        culture["resource_entry_id"],
        product["resource_entry_id"],
        logo["resource_entry_id"],
    ]
    _, project = create_video_task_and_project(
        content_client,
        user_headers,
        token=token,
        knowledge_resource_ids=knowledge_ids,
    )

    image_provider = CapturingImageProvider()
    app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    try:
        generated = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-images",
            headers=user_headers,
            json={
                "expected_revision": project["current_revision"],
                "prompt": "生成企业品牌视频主画面",
                "role": "video",
                "count": 1,
            },
        )
        assert generated.status_code == 200, generated.text
        assert image_provider.last_request is not None
        assert culture_text in image_provider.last_request.prompt
        assert product_text in image_provider.last_request.prompt
        assert "保持瓶型、包装结构、品牌 Logo 与标签文字一致" in (
            image_provider.last_request.prompt
        )
        assert image_provider.last_request.reference_images == (
            product_image_bytes,
            logo_bytes,
        )
        assert image_provider.last_request.reference_media_types == (
            "image/png",
            "image/png",
        )

        metadata = generated.json()[0]["metadata"]
        assert metadata["knowledge_resource_ids"] == knowledge_ids
        assert metadata["provenance"]["request"]["knowledge_resource_ids"] == knowledge_ids
        assert metadata["visual_reference_resource_ids"] == [
            product_image["resource_entry_id"],
            logo["resource_entry_id"],
        ]
        assert metadata["provenance"]["request"]["visual_reference_resource_ids"] == [
            product_image["resource_entry_id"],
            logo["resource_entry_id"],
        ]
    finally:
        app.dependency_overrides.pop(get_image_generation_port, None)


def test_external_project_image_rejects_restricted_project_asset(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    restricted = upload_visual_resource(
        content_client,
        user_headers,
        resource_kind="image",
        name=f"受限工程图-{token}",
        content=b"\x89PNG\r\n\x1a\n-restricted-project-image-" + token.encode(),
        data_level="restricted",
    )
    _, project = create_video_task_and_project(
        content_client,
        user_headers,
        token=token,
        knowledge_resource_ids=[],
    )
    video_track = next(
        item for item in project["revision"]["timeline"]["tracks"] if item["kind"] == "video"
    )
    item_id = f"restricted-{token}"
    edited = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": project["current_revision"],
            "operations": [
                {
                    "op_id": f"attach-{token}",
                    "type": "add",
                    "track_id": video_track["id"],
                    "item": {
                        "id": item_id,
                        "resource_entry_id": restricted["resource_entry_id"],
                        "duration_seconds": 2,
                    },
                }
            ],
        },
    )
    assert edited.status_code == 200, edited.text
    project = edited.json()
    project_asset_id = next(
        item["project_asset_id"]
        for track in project["revision"]["timeline"]["tracks"]
        for item in track["items"]
        if item["id"] == item_id
    )
    image_provider = CapturingImageProvider()
    app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    try:
        generated = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-images",
            headers=user_headers,
            json={
                "expected_revision": project["current_revision"],
                "prompt": "生成品牌视觉",
                "role": "video",
                "count": 1,
                "reference_asset_ids": [project_asset_id],
            },
        )
        assert generated.status_code == 403, generated.text
        assert image_provider.last_request is None
    finally:
        app.dependency_overrides.pop(get_image_generation_port, None)


@pytest.mark.parametrize("selection", ["knowledge", "product"])
def test_video_project_generation_sends_the_knowledge_digest_to_ai(
    content_client,
    user_headers,
    selection,
):
    token = uuid4().hex[:8]
    product_fact = f"核心产品事实-{token}：酒体醇厚，包装主色为红金。"
    product_image_bytes = b"\x89PNG\r\n\x1a\n-ai-video-product-" + token.encode()
    product = create_text_resource(
        content_client,
        user_headers,
        resource_kind="product",
        category="product",
        name=f"视频产品资料-{token}",
        text=product_fact,
    )
    product_image = upload_visual_resource(
        content_client,
        user_headers,
        resource_kind="image",
        category="product_image",
        parent_id=product["resource_entry_id"],
        name=f"AI 视频产品瓶图-{token}",
        content=product_image_bytes,
    )
    _, project = create_video_task_and_project(
        content_client,
        user_headers,
        token=token,
        knowledge_resource_ids=[product["resource_entry_id"]] if selection == "knowledge" else [],
        product_resource_id=product["resource_entry_id"] if selection == "product" else None,
    )

    video_provider = CapturingAiVideoProvider()
    app.dependency_overrides[get_ai_video_generation_port] = lambda: video_provider
    try:
        generated = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-video",
            headers=user_headers,
            json={
                "expected_revision": project["current_revision"],
                "prompt": "镜头缓慢靠近产品",
                "duration_seconds": 5,
                "ratio": "16:9",
                "resolution": "720p",
            },
        )
        assert generated.status_code == 200, generated.text
        assert video_provider.last_request is not None
        assert "知识库重点摘要" in video_provider.last_request.prompt
        assert product_fact in video_provider.last_request.prompt
        assert "保持瓶型、包装结构、品牌 Logo 与标签文字一致" in (
            video_provider.last_request.prompt
        )
        assert len(video_provider.last_request.prompt) <= 10_000
        assert video_provider.last_request.image_url == (
            "data:image/png;base64," + base64.b64encode(product_image_bytes).decode("ascii")
        )
        assert video_provider.last_request.reference_image_urls == ()
        job_result = generated.json()["result"]
        request = job_result["request"]
        assert request["knowledge_resource_ids"] == [product["resource_entry_id"]]
        assert request["visual_reference_resource_ids"] == [product_image["resource_entry_id"]]
        assert request["effective_reference_image_count"] == 1
        assert job_result["provenance"]["request"]["visual_reference_resource_ids"] == [
            product_image["resource_entry_id"]
        ]
    finally:
        app.dependency_overrides.pop(get_ai_video_generation_port, None)


def test_external_ai_video_rejects_restricted_product_child_before_provider(
    content_client,
    other_headers,
):
    token = uuid4().hex[:8]
    product = create_text_resource(
        content_client,
        other_headers,
        resource_kind="product",
        category="product",
        name=f"视频受限子图产品-{token}",
        text="产品资料。",
    )
    upload_visual_resource(
        content_client,
        other_headers,
        resource_kind="image",
        category="product_image",
        parent_id=product["resource_entry_id"],
        name=f"视频受限产品瓶图-{token}",
        content=b"\x89PNG\r\n\x1a\n-video-restricted-product-" + token.encode(),
        data_level="restricted",
    )
    _, project = create_video_task_and_project(
        content_client,
        other_headers,
        token=token,
        knowledge_resource_ids=[product["resource_entry_id"]],
    )

    video_provider = CapturingAiVideoProvider()
    app.dependency_overrides[get_ai_video_generation_port] = lambda: video_provider
    try:
        generated = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-video",
            headers=other_headers,
            json={
                "expected_revision": project["current_revision"],
                "prompt": "镜头缓慢靠近产品",
            },
        )
        assert generated.status_code == 403, generated.text
        assert "受限" in generated.json()["detail"]
        assert video_provider.last_request is None
    finally:
        app.dependency_overrides.pop(get_ai_video_generation_port, None)


def test_ai_video_product_relationship_does_not_bypass_child_media_ownership(
    content_client,
    user_headers,
    other_headers,
):
    token = uuid4().hex[:8]
    product = create_text_resource(
        content_client,
        other_headers,
        resource_kind="product",
        category="product",
        name=f"视频他人子图产品-{token}",
        text="产品资料。",
    )
    upload_visual_resource(
        content_client,
        user_headers,
        resource_kind="image",
        category="product_image",
        parent_id=product["resource_entry_id"],
        name=f"视频不同所有者产品图-{token}",
        content=b"\x89PNG\r\n\x1a\n-video-cross-owner-product-" + token.encode(),
    )
    _, project = create_video_task_and_project(
        content_client,
        other_headers,
        token=token,
        knowledge_resource_ids=[product["resource_entry_id"]],
    )

    video_provider = CapturingAiVideoProvider()
    app.dependency_overrides[get_ai_video_generation_port] = lambda: video_provider
    try:
        generated = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-video",
            headers=other_headers,
            json={
                "expected_revision": project["current_revision"],
                "prompt": "生成产品视频",
            },
        )
        assert generated.status_code == 403, generated.text
        assert "无权访问" in generated.json()["detail"]
        assert video_provider.last_request is None
    finally:
        app.dependency_overrides.pop(get_ai_video_generation_port, None)


def test_ai_video_product_visuals_share_the_nine_reference_limit(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    product = create_text_resource(
        content_client,
        user_headers,
        resource_kind="product",
        category="product",
        name=f"视频超限产品-{token}",
        text="产品资料。",
    )
    for index in range(10):
        upload_visual_resource(
            content_client,
            user_headers,
            resource_kind="image",
            category="product_image",
            parent_id=product["resource_entry_id"],
            name=f"视频产品角度-{token}-{index}",
            content=(
                b"\x89PNG\r\n\x1a\n-video-product-angle-" + token.encode() + str(index).encode()
            ),
        )
    _, project = create_video_task_and_project(
        content_client,
        user_headers,
        token=token,
        knowledge_resource_ids=[product["resource_entry_id"]],
    )

    video_provider = CapturingAiVideoProvider()
    app.dependency_overrides[get_ai_video_generation_port] = lambda: video_provider
    try:
        generated = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-video",
            headers=user_headers,
            json={
                "expected_revision": project["current_revision"],
                "prompt": "生成产品视频",
            },
        )
        assert generated.status_code == 422, generated.text
        assert "最多允许 9 张参考图片" in generated.json()["detail"]
        assert video_provider.last_request is None
    finally:
        app.dependency_overrides.pop(get_ai_video_generation_port, None)


def test_grounded_video_prompt_keeps_the_user_request_and_bounded_digest():
    knowledge = (
        KnowledgeChunk(
            id="product-summary",
            title="产品资料",
            text="资料信息：产品资料；关键事实：酱香突出；相关内容：包装使用红金配色",
            category="product",
            data_level="internal",
        ),
    )
    prompt = grounded_video_prompt("镜头从酒瓶缓慢推近", knowledge, max_chars=180)

    assert prompt.startswith("镜头从酒瓶缓慢推近")
    assert "知识库重点摘要" in prompt
    assert "酱香突出" in prompt
    assert len(prompt) <= 180


def test_video_project_images_reject_more_than_nine_total_visual_references(
    content_client,
    user_headers,
):
    token = uuid4().hex[:8]
    visual_resources = [
        upload_visual_resource(
            content_client,
            user_headers,
            resource_kind="logo",
            name=f"超限Logo-{token}-{index}",
            content=b"\x89PNG\r\n\x1a\n-over-limit-logo-" + str(index).encode(),
        )
        for index in range(10)
    ]
    _, project = create_video_task_and_project(
        content_client,
        user_headers,
        token=token,
        knowledge_resource_ids=[item["resource_entry_id"] for item in visual_resources],
    )

    image_provider = CapturingImageProvider()
    app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    try:
        generated = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-images",
            headers=user_headers,
            json={
                "expected_revision": project["current_revision"],
                "prompt": "生成品牌视觉",
                "role": "video",
                "count": 1,
            },
        )
        assert generated.status_code == 422, generated.text
        assert "9" in generated.json()["detail"]
        assert image_provider.last_request is None
    finally:
        app.dependency_overrides.pop(get_image_generation_port, None)
