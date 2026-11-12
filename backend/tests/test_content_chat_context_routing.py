"""User-facing regression scenarios for automatic creation and continuing old conversations."""

import io
import json
from dataclasses import replace

import pytest
from docx import Document
from PIL import Image
from pypdf import PdfReader
from test_content_chat import OWNER, URL, allow_vision
from test_content_chat import chat_client as chat_client
from test_content_chat_creation import configure_media, media_settings

from content_service import chat_generation, chat_routes
from content_service.chat_provider import MAX_CONTEXT_CHARACTERS, build_messages
from content_service.chat_storage import ChatStore


def automatic_conversation(client):
    response = client.post(URL + "/conversations", headers=OWNER, json={})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def automatic_send(client, cid, content, **values):
    return client.post(
        URL + f"/conversations/{cid}/messages",
        headers=OWNER,
        json={"content": content, **values},
    )


@pytest.mark.parametrize(
    "content,expected",
    [
        ("把这个直接变成pdf格式给我", "pdf"),
        ("那我要.doc格式的文件", "doc"),
        ("那给我DOCX格式的文档", "docx"),
        ("PDF格式给我", "pdf"),
        (".doc", "doc"),
        ("换成 PDF 格式", "pdf"),
        ("帮我做个产品海报", "image"),
        ("不要生成视频，只需要帮我写一份文案", "text"),
        ("生成一个产品视频脚本", "text"),
        ("生成一张带文字的海报", "image"),
        ("怎么将它转为pdf", "text"),
        ("PDF是怎样转换的，给我操作步骤", "text"),
    ],
)
def test_natural_destination_routing(content, expected):
    assert chat_generation.infer_output(content) == expected


def test_followup_format_conversion_creates_real_files_without_calling_model_again(
    chat_client, monkeypatch
):
    client, _, _ = chat_client
    cid = automatic_conversation(client)
    calls = []
    body = "# 产品方案\n\n记住产品是雅酿，预算 38000 元。\n\n第二节：发布安排。"

    def answer(*args):
        calls.append(args)
        return body

    monkeypatch.setattr(chat_generation, "call_chat", answer)
    first = automatic_send(client, cid, "生成一份产品方案文档").json()
    assert first["messages"][-1]["artifacts"][0]["kind"] == "docx"
    converted = automatic_send(client, cid, "把这个直接变成pdf格式给我").json()
    assert converted["messages"][-1]["status"] == "completed", converted
    pdf = converted["messages"][-1]["artifacts"][0]
    assert pdf["kind"] == "pdf"
    blob = client.get(pdf["url"], headers=OWNER).content
    assert "38000" in "".join(page.extract_text() for page in PdfReader(io.BytesIO(blob)).pages)
    word = automatic_send(client, cid, "那我要.docx格式的文件").json()
    docx = word["messages"][-1]["artifacts"][0]
    assert docx["kind"] == "docx"
    generated = Document(io.BytesIO(client.get(docx["url"], headers=OWNER).content))
    assert "预算 38000" in "\n".join(p.text for p in generated.paragraphs)
    assert len(calls) == 1


def test_doc_intent_dispatches_real_legacy_exporter_without_model_call(chat_client, monkeypatch):
    client, _, _ = chat_client
    cid = automatic_conversation(client)
    monkeypatch.setattr(chat_generation, "call_chat", lambda *args: "# 现成的正文\n产品事实")
    automatic_send(client, cid, "生成一份文档")
    original = chat_generation.create_chat_document
    formats = []

    def export(directory, *, content, title, format):
        formats.append((format, content))
        # Binary Word integration is tested separately against the bundled Linux converter.
        result = original(directory, content=content, title=title, format="docx")
        result["name"] = "legacy.doc"
        result["mime_type"] = "application/msword"
        return result

    monkeypatch.setattr(chat_generation, "create_chat_document", export)
    monkeypatch.setattr(
        chat_generation, "call_chat", lambda *args: pytest.fail("conversion called model")
    )
    result = automatic_send(client, cid, "那我要.doc格式的文件").json()
    assert result["messages"][-1]["artifacts"][0]["kind"] == "doc"
    assert formats == [("doc", "# 现成的正文\n产品事实")]


def test_explicit_reference_does_not_override_new_destination_and_conversion_needs_no_model(
    chat_client, monkeypatch
):
    client, _, app = chat_client
    configure_media(client, app)
    cid = automatic_conversation(client)
    monkeypatch.setattr(chat_generation, "call_chat", lambda *args: "# 活动正文\n批准的事实")
    initial = automatic_send(client, cid, "生成一份文档").json()
    aid = initial["messages"][-1]["artifacts"][0]["id"]
    disabled = client.put(
        URL + "/models/admin",
        headers={**OWNER, "X-Content-Config-Request": "1"},
        json={"expected_revision": 1, "models": []},
    )
    assert disabled.status_code == 200
    monkeypatch.setattr(
        chat_generation, "call_chat", lambda *args: pytest.fail("conversion called model")
    )
    result = automatic_send(client, cid, "改成PDF格式", reference_artifact_id=aid).json()
    assert result["messages"][-1]["artifacts"][0]["kind"] == "pdf"
    assert result["messages"][-1]["model_id"] == "document-local"
    rejected = automatic_send(client, cid, "生成一张新图片")
    assert rejected.status_code == 422
    assert "图片模型" in rejected.json()["detail"]


def test_new_document_topic_is_written_instead_of_exporting_previous_artifact(
    chat_client, monkeypatch
):
    client, _, _ = chat_client
    cid = automatic_conversation(client)
    calls = []

    def answer(settings, model, messages):
        calls.append(messages[-1]["content"])
        return "# 活动方案\n第一份正文" if len(calls) == 1 else "# 新品新闻稿\n第二份全新正文"

    monkeypatch.setattr(chat_generation, "call_chat", answer)
    automatic_send(client, cid, "生成一份活动方案文档")
    result = automatic_send(client, cid, "给我生成一份PDF新品新闻稿").json()
    assert result["messages"][-1]["artifacts"][0]["text_preview"] == "# 新品新闻稿\n第二份全新正文"
    assert len(calls) == 2


def test_upload_conversion_can_use_existing_attachment_without_provider(chat_client, monkeypatch):
    client, _, _ = chat_client
    cid = automatic_conversation(client)
    upload = client.post(
        URL + "/attachments",
        headers=OWNER,
        files={"file": ("事实.txt", "批准事实：时间待定".encode(), "text/plain")},
    ).json()
    monkeypatch.setattr(
        chat_generation, "call_chat", lambda *args: pytest.fail("conversion called model")
    )
    result = automatic_send(client, cid, "把附件转成PDF", attachment_ids=[upload["id"]]).json()
    artifact = result["messages"][-1]["artifacts"][0]
    assert artifact["kind"] == "pdf"
    assert "批准事实" in artifact["text_preview"]


def test_automatic_router_selects_vision_from_multiple_text_models(chat_client, monkeypatch):
    client, _, app = chat_client
    allow_vision(client, app)
    cid = automatic_conversation(client)
    source = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(source, "PNG")
    upload = client.post(
        URL + "/attachments",
        headers=OWNER,
        files={"file": ("产品.png", source.getvalue(), "image/png")},
    ).json()
    selected = []

    def answer(settings, model, messages):
        selected.append(model["id"])
        assert any(isinstance(message["content"], list) for message in messages)
        return "记住了，图片是红色的。"

    monkeypatch.setattr(chat_generation, "call_chat", answer)
    assert automatic_send(client, cid, "分析图片", attachment_ids=[upload["id"]]).status_code == 200
    assert automatic_send(client, cid, "刚才图片是什么颜色").status_code == 200
    assert selected == ["vision", "vision"]


def test_automatic_media_routing_does_not_lock_next_question_to_last_kind(chat_client, monkeypatch):
    client, settings, app = chat_client
    monkeypatch.setattr(chat_routes, "get_runtime_settings", lambda: media_settings(settings))
    configure_media(client, app)
    cid = automatic_conversation(client)
    observed = []

    def generate(settings, model, *, prompt, output_dir, reference_path):
        observed.append(model["kind"])
        suffix = "png" if model["kind"] == "image" else "mp4"
        path = output_dir / ("result." + suffix)
        path.write_bytes(b"fixture")
        return {
            "path": path,
            "name": path.name,
            "kind": model["kind"],
            "mime_type": "image/png" if suffix == "png" else "video/mp4",
        }

    monkeypatch.setattr(chat_generation, "generate_chat_media", generate)
    monkeypatch.setattr(
        chat_generation, "call_chat", lambda settings, model, messages: "文字回复：" + model["kind"]
    )
    assert (
        automatic_send(client, cid, "生成一张图片").json()["messages"][-1]["artifacts"][0]["kind"]
        == "image"
    )
    assert (
        automatic_send(client, cid, "生成一个视频").json()["messages"][-1]["artifacts"][0]["kind"]
        == "video"
    )
    final = automatic_send(client, cid, "你好，帮我想一个标题").json()
    assert final["messages"][-1]["content"] == "文字回复：chat"
    assert observed == ["image", "video"]


def test_implicit_image_edit_uses_latest_generated_artifact_without_reference_button(
    chat_client, monkeypatch
):
    client, settings, app = chat_client
    monkeypatch.setattr(chat_routes, "get_runtime_settings", lambda: media_settings(settings))
    configure_media(client, app)
    cid = automatic_conversation(client)
    observed = []
    source = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(source, "PNG")

    def generate(settings, model, *, prompt, output_dir, reference_path):
        observed.append(reference_path.read_bytes() if reference_path else None)
        path = output_dir / "result.png"
        path.write_bytes(source.getvalue())
        return {"path": path, "name": path.name, "kind": "image", "mime_type": "image/png"}

    monkeypatch.setattr(chat_generation, "generate_chat_media", generate)
    first = automatic_send(client, cid, "生成一张产品图片").json()
    assert first["messages"][-1]["status"] == "completed"
    edited = automatic_send(client, cid, "把背景改为蓝色").json()
    assert edited["messages"][-1]["status"] == "completed", edited
    assert edited["messages"][-1]["artifacts"][0]["kind"] == "image"
    assert observed == [None, source.getvalue()]


def test_document_refusal_gets_one_content_retry_and_never_exports_a_tutorial(
    chat_client, monkeypatch
):
    client, _, _ = chat_client
    cid = automatic_conversation(client)
    responses = iter(
        [
            "我无法直接生成并发送PDF文件，请自行使用浏览器打印。",
            "# 产品新闻稿\n已确认的新品发布事实。",
        ]
    )
    monkeypatch.setattr(chat_generation, "call_chat", lambda *args: next(responses))
    result = automatic_send(client, cid, "生成一份PDF新品新闻稿").json()
    assert result["messages"][-1]["status"] == "completed", result
    assert result["messages"][-1]["artifacts"][0]["text_preview"].startswith("# 产品新闻稿")


def test_persistent_document_refusal_is_a_retriable_error_not_a_bogus_file(
    chat_client, monkeypatch
):
    client, _, _ = chat_client
    cid = automatic_conversation(client)
    monkeypatch.setattr(chat_generation, "call_chat", lambda *args: "我无法生成PDF文件。")
    result = automatic_send(client, cid, "生成一份PDF新品新闻稿").json()
    assert result["messages"][-1]["status"] == "failed"
    assert result["messages"][-1]["artifacts"] == []
    assert "没有返回文档正文" in result["messages"][-1]["error"]


def test_unconfigured_video_is_rejected_before_message_is_saved(chat_client):
    client, _, _ = chat_client
    cid = automatic_conversation(client)
    result = automatic_send(client, cid, "制作一个产品视频")
    assert result.status_code == 422
    assert "视频模型" in result.json()["detail"]
    assert client.get(URL + f"/conversations/{cid}", headers=OWNER).json()["messages"] == []


def test_context_budget_keeps_early_requirements_recent_messages_and_relevant_document_passages(
    tmp_path,
):
    history = [
        {"role": "user", "content": "请记住品牌是雅酿，预算不得超过38000元。", "attachments": []}
    ]
    for index in range(80):
        history.append(
            {
                "role": "assistant" if index % 2 == 0 else "user",
                "content": f"第{index}轮讨论。" + "活动内容。" * 1000,
                "attachments": [],
            }
        )
    history.append(
        {
            "role": "user",
            "content": "继续按最早的品牌和预算要求，把发布日期加入内容。",
            "attachments": [
                {
                    "kind": "document",
                    "name": "资料.txt",
                    "metadata_json": json.dumps(
                        {
                            "text": "普通资料。" * 6000
                            + "\n发布日期是2026年10月20日。\n"
                            + "补充背景。" * 6000
                        }
                    ),
                }
            ],
        }
    )
    messages = build_messages(
        ChatStore(tmp_path), history, {"supports_images": False, "supports_video": False}
    )
    content = "\n".join(message["content"] for message in messages)
    assert "品牌是雅酿" in content
    assert "38000元" in content
    assert "2026年10月20日" in content
    assert "继续按最早的品牌和预算要求" in messages[-1]["content"]
    assert len(content) < MAX_CONTEXT_CHARACTERS + 2000
    assert len(history) == 82  # Saved source history is not mutated by the provider budget.


def test_document_artifact_body_survives_conversion_acknowledgement_in_context(tmp_path):
    history = [
        {
            "role": "assistant",
            "content": "已转换为 PDF 文件，可以直接下载。",
            "artifacts": [
                {
                    "kind": "pdf",
                    "metadata_json": json.dumps({"text": "预算是38000元，产品是雅酿。"}),
                }
            ],
        },
        {"role": "user", "content": "预算是多少？", "attachments": []},
    ]
    messages = build_messages(
        ChatStore(tmp_path), history, {"supports_images": False, "supports_video": False}
    )
    assert "预算是38000元" in messages[1]["content"]


def test_missing_visual_model_explains_required_capability(chat_client):
    client, _, _ = chat_client
    cid = automatic_conversation(client)
    source = io.BytesIO()
    Image.new("RGB", (20, 20)).save(source, "PNG")
    upload = client.post(
        URL + "/attachments",
        headers=OWNER,
        files={"file": ("产品.png", source.getvalue(), "image/png")},
    ).json()
    result = automatic_send(client, cid, "看看这张图", attachment_ids=[upload["id"]])
    assert result.status_code == 422
    assert "多模态文本模型" in result.json()["detail"]


def test_default_creation_has_no_provider_requirement(chat_client, monkeypatch):
    client, settings, _ = chat_client
    monkeypatch.setattr(
        chat_routes, "get_runtime_settings", lambda: replace(settings, llm_api_key="")
    )
    cid = automatic_conversation(client)
    rejected = automatic_send(client, cid, "你好")
    assert rejected.status_code == 422
    assert "对话模型" in rejected.json()["detail"]
