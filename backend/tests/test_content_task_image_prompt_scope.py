"""Exercise task-image prompt improvements without invoking paid providers."""

from uuid import uuid4

import pytest
from test_content_task_references import (
    SequencedCapturingImageProvider,
    create_text_resource,
    upload_visual_resource,
)

from content_service.main import PRODUCT_VISUAL_CONSISTENCY_PROMPT, app
from content_service.provider_dependencies import get_image_generation_port

EDIT_PRIORITY = "本次图片修改要求（优先执行）"
LEGACY_REFINEMENT_PREFIX = (
    "以当前参考图片为基础生成新版本，保留未明确要求修改的主体、构图和品牌元素。\n"
    "本次必须执行的修改要求："
)


@pytest.fixture
def image_prompt_provider(monkeypatch):
    provider = SequencedCapturingImageProvider()
    monkeypatch.setitem(app.dependency_overrides, get_image_generation_port, lambda: provider)
    return provider


def create_product_with_duplicate_visuals(client, headers, token):
    product = create_text_resource(
        client,
        headers,
        resource_kind="product",
        category="product",
        name=f"改图隔离产品-{token}",
        text=f"已核实的产品事实-{token}：绿色瓶身与固定品牌标签。",
    )
    product_id = product["resource_entry_id"]
    bottle_bytes = b"\x89PNG\r\n\x1a\n-scope-bottle-" + token.encode()
    label_bytes = b"\x89PNG\r\n\x1a\n-scope-label-" + token.encode()
    visuals = []
    for index, content in enumerate((bottle_bytes, label_bytes, bottle_bytes)):
        visuals.append(
            upload_visual_resource(
                client,
                headers,
                resource_kind="image",
                category="product_image",
                parent_id=product_id,
                name=f"产品角度-{token}-{index}",
                content=content,
            )
        )
    return product_id, visuals, (bottle_bytes, label_bytes)


def create_image_task(client, headers, mode, token, product_id=None):
    payload = {
        "mode": mode,
        "name": f"图片提示词隔离-{mode}-{token}",
        "topic": "春节产品主视觉",
        "audience": "目标消费者",
    }
    if product_id is not None:
        payload["product_resource_id"] = product_id
    response = client.post("/api/v1/tasks", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def generate_image(client, headers, task_id, payload, *, stage_route):
    suffix = "stages/image/generate" if stage_route else "generate-images"
    response = client.post(f"/api/v1/tasks/{task_id}/{suffix}", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    return result["artifacts"][0] if stage_route else result[0]


@pytest.mark.parametrize("mode", ("full", "image"))
@pytest.mark.parametrize("stage_route", (False, True))
def test_task_image_refinement_roles_follow_actual_deduplicated_references(
    content_client, user_headers, image_prompt_provider, mode, stage_route
):
    token = uuid4().hex[:8]
    product_id, visuals, product_bytes = create_product_with_duplicate_visuals(
        content_client, user_headers, token
    )
    task = create_image_task(content_client, user_headers, mode, token, product_id)
    source = generate_image(
        content_client,
        user_headers,
        task["id"],
        {"count": 1, "parameters": {"prompt": "原始春节团圆画面"}},
        stage_route=stage_route,
    )
    first_request = image_prompt_provider.last_request
    assert first_request is not None
    assert first_request.reference_images == product_bytes
    assert "参考图1、参考图2仅用于核对产品外观" in first_request.prompt
    assert "接触阴影" in first_request.prompt
    assert "本次待修改画面" not in first_request.prompt
    instruction = "更换为有庭院落地窗的明亮背景，产品自然一点，不要像贴图。"
    parameters = {
        "prompt": LEGACY_REFINEMENT_PREFIX + instruction,
        "knowledge_resource_ids": [],
        "reference_artifact_ids": [source["id"], source["id"]],
        "reference_asset_ids": [visuals[0]["resource_entry_id"]],
        "scene": "生活方式",
        "style": "简约高级",
        "size": "4K",
        "ratio": "3:4",
        "watermark": True,
        "seed": 123456,
        "extra": {"test_parameter": "unchanged"},
    }
    payload = {
        "count": 1,
        "source_artifact_id": source["id"],
        "instruction": instruction,
        "parameters": parameters,
        "idempotency_key": f"scope-refinement-{token}",
    }
    refined = generate_image(
        content_client, user_headers, task["id"], payload, stage_route=stage_route
    )
    request = image_prompt_provider.last_request
    assert request is not None
    assert request.reference_images == (
        *product_bytes,
        b"\x89PNG\r\n\x1a\n-generated-sequence-1",
    )
    assert request.reference_media_types == ("image/png",) * 3
    assert EDIT_PRIORITY in request.prompt
    assert "参考图3是本次待修改画面" in request.prompt
    assert "参考图1、参考图2仅用于核对产品外观" in request.prompt
    assert "参考图4是本次待修改画面" not in request.prompt
    assert LEGACY_REFINEMENT_PREFIX not in request.prompt
    assert request.prompt.count(instruction) == 1
    assert request.prompt.index(instruction) < request.prompt.index(
        "场景参考（本次修改优先）：生活方式"
    )
    assert "风格参考（本次修改优先）：简约高级" in request.prompt
    assert "保持瓶型、包装结构、品牌 Logo 与标签文字一致" in request.prompt
    assert "接触阴影" in request.prompt
    assert request.count == 1
    assert request.size == "4K"
    assert request.ratio == "3:4"
    assert request.watermark is True
    assert request.seed == 123456
    assert request.extra == {"test_parameter": "unchanged"}

    assert refined["id"] != source["id"]
    assert refined["parent_id"] == source["id"]
    assert refined["version"] == source["version"] + 1
    assert refined["content"]["source_artifact_id"] == source["id"]
    assert refined["content"]["instruction"] == instruction
    assert refined["content"]["knowledge_resource_ids"] == [product_id]
    assert refined["content"]["reference_artifact_ids"] == [source["id"], source["id"]]

    replayed = generate_image(
        content_client, user_headers, task["id"], payload, stage_route=stage_route
    )
    assert replayed["id"] == refined["id"]
    assert len(image_prompt_provider.requests) == 2
    changed = generate_image(
        content_client,
        user_headers,
        task["id"],
        {**payload, "instruction": "将背景换成雪后的室外庭院"},
        stage_route=stage_route,
    )
    assert changed["id"] != refined["id"]
    assert changed["parent_id"] == source["id"]
    assert len(image_prompt_provider.requests) == 3


@pytest.mark.parametrize("mode", ("full", "image"))
def test_task_image_edit_instruction_survives_long_old_prompt_and_knowledge(
    content_client, user_headers, image_prompt_provider, mode
):
    token = uuid4().hex[:8]
    product_id, _, _ = create_product_with_duplicate_visuals(content_client, user_headers, token)
    task = create_image_task(content_client, user_headers, mode, token, product_id)
    source = generate_image(
        content_client,
        user_headers,
        task["id"],
        {"count": 1},
        stage_route=False,
    )
    end_marker = f"修改末尾要求-{token}"
    instruction = ("请调整背景光照与产品接触阴影。" * 200)[: 2000 - len(end_marker)]
    instruction += end_marker
    assert len(instruction) == 2000
    generate_image(
        content_client,
        user_headers,
        task["id"],
        {
            "count": 1,
            "source_artifact_id": source["id"],
            "instruction": instruction,
            "parameters": {"prompt": "历史主题与旧生成要求。" * 1200},
        },
        stage_route=True,
    )
    request = image_prompt_provider.last_request
    assert request is not None
    assert instruction in request.prompt
    assert end_marker in request.prompt
    assert "参考图3是本次待修改画面" in request.prompt
    assert len(request.prompt) <= 10_000


def test_quick_video_task_image_endpoint_keeps_existing_prompt(
    content_client, user_headers, image_prompt_provider
):
    task = create_image_task(content_client, user_headers, "video", uuid4().hex[:8])
    generate_image(
        content_client,
        user_headers,
        task["id"],
        {
            "count": 1,
            "instruction": "保留现有视频画面用途",
            "parameters": {
                "prompt": "快捷视频原来的图片提示词",
                "scene": "室内",
                "style": "自然",
                "ratio": "16:9",
            },
        },
        stage_route=False,
    )
    request = image_prompt_provider.last_request
    assert request is not None
    assert request.prompt == (
        "快捷视频原来的图片提示词；场景：室内；风格：自然；画面比例：16:9；"
        "补充要求：保留现有视频画面用途"
    )
    assert request.reference_images == ()


@pytest.mark.parametrize("mode", ["full", "video"])
def test_video_editor_image_candidates_keep_existing_shared_product_prompt(
    content_client, user_headers, image_prompt_provider, mode
):
    token = uuid4().hex[:8]
    product_id, _, product_bytes = create_product_with_duplicate_visuals(
        content_client, user_headers, token
    )
    task = create_image_task(content_client, user_headers, mode, token, product_id)
    project_response = content_client.post(
        f"/api/v1/tasks/{task['id']}/video-project", headers=user_headers
    )
    assert project_response.status_code == 200, project_response.text
    project = project_response.json()
    response = content_client.post(
        f"/api/v1/video-projects/{project['id']}/generate-images",
        headers=user_headers,
        json={
            "expected_revision": project["current_revision"],
            "prompt": "视频编辑器既有图片候选请求",
            "role": "video",
            "count": 1,
        },
    )
    assert response.status_code == 200, response.text
    request = image_prompt_provider.last_request
    assert request is not None
    assert request.prompt.startswith(
        PRODUCT_VISUAL_CONSISTENCY_PROMPT + "；视频编辑器既有图片候选请求；"
    )
    assert EDIT_PRIORITY not in request.prompt
    assert "仅用于核对产品外观" not in request.prompt
    assert "本次待修改画面" not in request.prompt
    # The separate project-candidate pipeline already deduplicates the child
    # images itself. Keep its existing bytes and ordering unchanged as well.
    assert request.reference_images == product_bytes
