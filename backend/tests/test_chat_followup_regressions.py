"""Regressions found while exercising references across different chat capabilities."""

import io

import pytest
from PIL import Image
from test_content_chat import OTHER, OWNER, URL
from test_content_chat import chat_client as chat_client
from test_content_chat_context_routing import automatic_conversation, automatic_send
from test_content_chat_creation import configure_media, media_settings

from content_service import chat_generation, chat_routes


def png_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(stream, "PNG")
    return stream.getvalue()


def mock_media(monkeypatch, observed):
    def generate(settings, model, *, prompt, output_dir, reference_path):
        observed.append((model["kind"], reference_path.read_bytes() if reference_path else None))
        path = output_dir / ("result.png" if model["kind"] == "image" else "result.mp4")
        path.write_bytes(png_bytes() if model["kind"] == "image" else b"fixture-video")
        return {
            "path": path,
            "name": path.name,
            "kind": model["kind"],
            "mime_type": "image/png" if model["kind"] == "image" else "video/mp4",
        }

    monkeypatch.setattr(chat_generation, "generate_chat_media", generate)


@pytest.mark.parametrize("prompt", ["这个做成视频", "用它生成视频", "根据这个制作产品视频"])
def test_generated_image_is_actual_reference_for_followup_video(chat_client, monkeypatch, prompt):
    client, settings, app = chat_client
    monkeypatch.setattr(chat_routes, "get_runtime_settings", lambda: media_settings(settings))
    configure_media(client, app)
    cid = automatic_conversation(client)
    observed = []
    mock_media(monkeypatch, observed)
    automatic_send(client, cid, "生成一张图片")
    result = automatic_send(client, cid, prompt).json()
    assert result["messages"][-1]["status"] == "completed", result
    assert observed == [("image", None), ("video", png_bytes())]


def test_new_image_conversion_cannot_silently_export_previous_document(chat_client, monkeypatch):
    client, _, _ = chat_client
    cid = automatic_conversation(client)
    monkeypatch.setattr(chat_generation, "call_chat", lambda *args: "# 完全无关的旧文档\n旧内容")
    automatic_send(client, cid, "生成一份文档")
    uploaded = client.post(
        URL + "/attachments",
        headers=OWNER,
        files={"file": ("新图片.png", png_bytes(), "image/png")},
    ).json()
    response = automatic_send(client, cid, "把这个转成PDF", attachment_ids=[uploaded["id"]])
    assert response.status_code == 422
    assert "多模态" in response.json()["detail"]


def test_latest_plain_text_is_exported_instead_of_stale_artifact(chat_client, monkeypatch):
    client, _, _ = chat_client
    cid = automatic_conversation(client)
    replies = iter(["# 旧文档\n已弃用", "# 最新新闻稿\n全新的正文内容"])
    monkeypatch.setattr(chat_generation, "call_chat", lambda *args: next(replies))
    automatic_send(client, cid, "生成一份Word文档")
    automatic_send(client, cid, "重新写一段全新的新闻文案")
    result = automatic_send(client, cid, "把这个变成PDF").json()
    assert result["messages"][-1]["artifacts"][0]["text_preview"] == "# 最新新闻稿\n全新的正文内容"


def test_explicit_attachment_conversion_uses_recent_upload_not_older_document(
    chat_client, monkeypatch
):
    client, _, _ = chat_client
    cid = automatic_conversation(client)
    replies = iter(["# 旧文档\n已弃用", "附件分析摘要"])
    monkeypatch.setattr(chat_generation, "call_chat", lambda *args: next(replies))
    automatic_send(client, cid, "生成一份文档")
    uploaded = client.post(
        URL + "/attachments",
        headers=OWNER,
        files={"file": ("新事实.txt", "新的附件原文，预算12345元。".encode(), "text/plain")},
    ).json()
    automatic_send(client, cid, "帮我看看附件", attachment_ids=[uploaded["id"]])
    result = automatic_send(client, cid, "把附件转成PDF").json()
    assert result["messages"][-1]["artifacts"][0]["text_preview"] == "新的附件原文，预算12345元。"


def test_new_document_creation_and_export_does_not_become_previous_text_conversion(
    chat_client, monkeypatch
):
    client, _, _ = chat_client
    cid = automatic_conversation(client)
    calls = []

    def answer(*args):
        calls.append(True)
        return "# 上次内容" if len(calls) == 1 else "# 全新发布方案\n新的内容"

    monkeypatch.setattr(chat_generation, "call_chat", answer)
    automatic_send(client, cid, "写一句宣传语")
    result = automatic_send(client, cid, "帮我生成一份全新的产品发布方案，导出PDF").json()
    assert result["messages"][-1]["artifacts"][0]["text_preview"] == "# 全新发布方案\n新的内容"
    assert len(calls) == 2
    uploaded = client.post(
        URL + "/attachments",
        headers=OWNER,
        files={"file": ("事实.txt", "只能作为参考的原文".encode(), "text/plain")},
    ).json()
    from_upload = automatic_send(
        client, cid, "根据附件生成一份详细的推广方案，导出PDF", attachment_ids=[uploaded["id"]]
    ).json()
    assert from_upload["messages"][-1]["artifacts"][0]["text_preview"] == "# 全新发布方案\n新的内容"
    assert len(calls) == 3


def test_document_analysis_question_does_not_create_a_revised_document(chat_client, monkeypatch):
    client, _, _ = chat_client
    cid = automatic_conversation(client)
    observed = []

    def answer(settings, model, messages):
        observed.append(messages)
        return "可以改进标题和段落结构。"

    monkeypatch.setattr(chat_generation, "call_chat", answer)
    uploaded = client.post(
        URL + "/attachments",
        headers=OWNER,
        files={"file": ("草稿.txt", "需要分析的草稿".encode(), "text/plain")},
    ).json()
    result = automatic_send(
        client, cid, "帮我看看这份文档哪些地方需要修改", attachment_ids=[uploaded["id"]]
    ).json()
    assert result["messages"][-1]["artifacts"] == []
    assert "需要分析的草稿" in str(observed[-1])
    automatic_send(client, cid, "生成Word文档")
    followup = automatic_send(client, cid, "看看这个文档有哪些需要修改的地方").json()
    assert followup["messages"][-1]["artifacts"] == []
    revised = automatic_send(client, cid, "按你刚才的建议继续修改").json()
    assert revised["messages"][-1]["artifacts"][0]["kind"] == "docx"


def test_generated_image_analysis_uses_actual_image_with_automatic_vision_model(
    chat_client, monkeypatch
):
    client, settings, app = chat_client
    monkeypatch.setattr(chat_routes, "get_runtime_settings", lambda: media_settings(settings))
    config = configure_media(client, app)
    rows = [
        {key: value for key, value in row.items() if key not in {"provider", "configured"}}
        for row in config["models"]
    ]
    rows[0]["supports_images"] = True
    saved = client.put(
        URL + "/models/admin",
        headers={**OWNER, "X-Content-Config-Request": "1"},
        json={"expected_revision": config["revision"], "models": rows},
    )
    assert saved.status_code == 200
    cid = automatic_conversation(client)
    mock_media(monkeypatch, [])
    observed = []

    def answer(settings, model, messages):
        observed.append(messages)
        return "图片是红色的。"

    monkeypatch.setattr(chat_generation, "call_chat", answer)
    automatic_send(client, cid, "生成一张图片")
    result = automatic_send(client, cid, "分析这个是什么颜色").json()
    assert result["messages"][-1]["content"] == "图片是红色的。"
    assert any(
        isinstance(message["content"], list)
        and any(part["type"] == "image_url" for part in message["content"])
        for message in observed[-1]
    )


def test_previous_image_attachment_is_used_for_video_and_internal_reference_is_not_client_input(
    chat_client, monkeypatch
):
    client, settings, app = chat_client
    monkeypatch.setattr(chat_routes, "get_runtime_settings", lambda: media_settings(settings))
    configure_media(client, app)
    cid = automatic_conversation(client)
    observed = []
    mock_media(monkeypatch, observed)
    uploaded = client.post(
        URL + "/attachments", headers=OWNER, files={"file": ("图片.png", png_bytes(), "image/png")}
    ).json()
    first = automatic_send(client, cid, "生成一张改图", attachment_ids=[uploaded["id"]]).json()
    assert first["messages"][-1]["status"] == "completed"
    # Explicit “uploaded attachment” selects the source upload, not the generated result.
    video = automatic_send(client, cid, "把上传的附件做成视频").json()
    assert video["messages"][-1]["status"] == "completed", video
    assert observed[-1][0] == "video" and observed[-1][1].startswith(b"\xff\xd8")
    retry = client.post(URL + f"/conversations/{cid}/retry", headers=OWNER, json={}).json()
    assert retry["messages"][-1]["status"] == "completed", retry
    assert observed[-1] == observed[-2]
    refused = automatic_send(client, cid, "读取附件", reference_attachment_id=uploaded["id"])
    assert refused.status_code == 422
    assert client.get(uploaded["url"], headers=OTHER).status_code == 404
