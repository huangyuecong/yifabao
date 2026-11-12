import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from docx import Document
from pypdf import PdfReader
from test_content_chat import OTHER, OWNER, URL, create
from test_content_chat import chat_client as chat_client

from agent_core import Identity
from content_service import chat_generation, chat_routes
from content_service.ai_model_config import require_manager
from content_service.chat_storage import ChatStore


def send(
    client,
    cid,
    *,
    content="生成一份报告",
    output="auto",
    model="text-default",
    attachments=(),
    reference=None,
):
    return client.post(
        URL + f"/conversations/{cid}/messages",
        headers=OWNER,
        json={
            "model_id": model,
            "content": content,
            "output": output,
            "attachment_ids": list(attachments),
            "reference_artifact_id": reference,
        },
    )


def configure_media(client, app, *, edit=True):
    app.dependency_overrides[require_manager] = lambda: Identity(
        "manager", "管理员", "", frozenset()
    )
    rows = [
        {"id": "text-default", "name": "文档模型", "model": "text-model"},
        {
            "id": "image",
            "name": "生图模型",
            "model": "image-model",
            "kind": "image",
            "provider_source": "image",
            "supports_image_edit": edit,
        },
        {
            "id": "video",
            "name": "视频模型",
            "model": "video-model",
            "kind": "video",
            "provider_source": "video",
        },
    ]
    response = client.put(
        URL + "/models/admin",
        json={"expected_revision": 0, "models": rows},
        headers={**OWNER, "X-Content-Config-Request": "1"},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("format", ["docx", "pdf"])
def test_document_from_uploaded_docx_is_real_downloadable_and_can_be_revised(
    chat_client, monkeypatch, format
):
    client, settings, _ = chat_client
    cid = create(client)
    document = Document()
    document.add_paragraph("已批准的产品事实：仅供内部讨论")
    source = io.BytesIO()
    document.save(source)
    uploaded = client.post(
        URL + "/attachments",
        headers=OWNER,
        files={
            "file": (
                "材料.docx",
                source.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    ).json()
    observed = []

    def model_call(settings, model, messages):
        observed.append(messages)
        return "# 雅酿活动方案\n\n仅供内部讨论\n\n| 项目 | 内容 |\n| --- | --- |\n| 时间 | 待确认 |"

    monkeypatch.setattr(chat_generation, "call_chat", model_call)
    result = send(
        client, cid, content=f"根据附件生成{format}文档", attachments=[uploaded["id"]]
    ).json()
    assert result["messages"][-1]["status"] == "completed", result
    assert "已批准的产品事实" in observed[0][-1]["content"]
    assert "可下载" in observed[0][0]["content"]
    artifact = result["messages"][-1]["artifacts"][0]
    assert artifact["kind"] == format
    blob = client.get(artifact["url"], headers=OWNER)
    assert blob.status_code == 200
    assert blob.headers["cache-control"] == "private, no-store"
    if format == "docx":
        generated = Document(io.BytesIO(blob.content))
        assert "雅酿活动方案" in "\n".join(p.text for p in generated.paragraphs)
        assert generated.tables[0].cell(1, 1).text == "待确认"
    else:
        generated = PdfReader(io.BytesIO(blob.content))
        assert "雅酿活动方案" in "".join(p.extract_text() for p in generated.pages)
    assert client.get(artifact["url"], headers=OTHER).status_code == 404
    other_cid = create(client)
    assert send(client, other_cid, reference=artifact["id"]).status_code == 404
    revised = send(client, cid, content="修改为简短版本", reference=artifact["id"]).json()
    assert revised["messages"][-1]["artifacts"][0]["kind"] == format
    assert "用户选择的生成文档资料" in observed[-1][-1]["content"]
    assert client.get(artifact["url"], headers=OWNER).status_code == 200
    assert client.delete(URL + f"/conversations/{cid}", headers=OWNER).status_code == 200
    assert client.get(artifact["url"], headers=OWNER).status_code == 404
    assert not list((settings.data_dir / "private/chat/artifacts").glob("*/*"))


def test_creation_model_config_is_backwards_compatible_and_keeps_secrets_private(chat_client):
    client, _, app = chat_client
    defaults = client.get(URL + "/models", headers=OWNER).json()["models"]
    assert defaults[0]["kind"] == "chat"
    assert defaults[0]["configured"] is True
    saved = configure_media(client, app)
    assert saved["models"][1]["configured"] is False  # mock provider is never advertised as usable
    assert "private-test-key" not in json.dumps(saved)
    cid = create(client)
    assert send(client, cid, content="生成图片", output="image").status_code == 422
    assert client.get(URL + f"/conversations/{cid}", headers=OWNER).json()["messages"] == []


def media_settings(settings):
    return replace(
        settings,
        image_provider="volcengine_ark",
        image_base_url="https://media.example/v1",
        image_api_key="image-private",
        ai_video_provider="volcengine_ark",
        ai_video_base_url="https://media.example/v1",
        ai_video_api_key="video-private",
    )


def test_auto_media_uses_opened_model_and_uploaded_reference_and_retains_artifact(
    chat_client, monkeypatch
):
    from PIL import Image

    client, settings, app = chat_client
    monkeypatch.setattr(chat_routes, "get_runtime_settings", lambda: media_settings(settings))
    configure_media(client, app)
    cid = create(client)
    image = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(image, "PNG")
    uploaded = client.post(
        URL + "/attachments",
        headers=OWNER,
        files={"file": ("产品.png", image.getvalue(), "image/png")},
    ).json()
    observed = []

    def generate(settings, model, *, prompt, output_dir, reference_path):
        observed.append((model, prompt, reference_path.read_bytes() if reference_path else None))
        path = output_dir / "result.png"
        path.write_bytes(image.getvalue())
        return {
            "path": path,
            "name": "修改结果.png",
            "kind": "image",
            "mime_type": "image/png",
            "size": path.stat().st_size,
        }

    monkeypatch.setattr(chat_generation, "generate_chat_media", generate)
    first = send(client, cid, content="把背景改为白色", attachments=[uploaded["id"]]).json()
    assert first["messages"][-1]["status"] == "completed", first
    assert observed[0][0]["id"] == "image"
    assert observed[0][2] is not None
    artifact = first["messages"][-1]["artifacts"][0]
    follow = send(client, cid, content="修改为蓝色背景", reference=artifact["id"]).json()
    assert observed[-1][2] == image.getvalue()
    assert follow["messages"][-1]["artifacts"]
    assert client.get(artifact["url"], headers=OWNER).content == image.getvalue()


def test_unsupported_edit_is_rejected_before_saving_question(chat_client, monkeypatch):
    from PIL import Image

    client, settings, app = chat_client
    monkeypatch.setattr(chat_routes, "get_runtime_settings", lambda: media_settings(settings))
    configure_media(client, app, edit=False)
    cid = create(client)
    image = io.BytesIO()
    Image.new("RGB", (10, 10)).save(image, "PNG")
    uploaded = client.post(
        URL + "/attachments",
        headers=OWNER,
        files={"file": ("产品.png", image.getvalue(), "image/png")},
    ).json()
    result = send(client, cid, content="修改图片", model="image", attachments=[uploaded["id"]])
    assert result.status_code == 422
    assert "未开放参考图编辑" in result.json()["detail"]
    assert client.get(URL + f"/conversations/{cid}", headers=OWNER).json()["messages"] == []


def test_video_background_pending_concurrency_completion_and_failure_retry(
    chat_client, monkeypatch
):
    from content_service.llm_adapters import ProviderCallError

    client, settings, app = chat_client
    monkeypatch.setattr(chat_routes, "get_runtime_settings", lambda: media_settings(settings))
    configure_media(client, app)
    cid = create(client)
    started, release, finished = Event(), Event(), Event()
    attempts = []

    def generate(settings, model, *, prompt, output_dir, reference_path):
        attempts.append(prompt)
        started.set()
        assert release.wait(5)
        if len(attempts) == 1:
            raise ProviderCallError("视频接口暂时繁忙")
        path = output_dir / "result.mp4"
        path.write_bytes(b"fixture video")
        return {
            "path": path,
            "name": "result.mp4",
            "kind": "video",
            "mime_type": "video/mp4",
            "size": 13,
        }

    monkeypatch.setattr(chat_generation, "generate_chat_media", generate)
    original_finish = ChatStore.finish

    def finish(self, *args, **kwargs):
        result = original_finish(self, *args, **kwargs)
        finished.set()
        return result

    monkeypatch.setattr(ChatStore, "finish", finish)
    with ThreadPoolExecutor(max_workers=1) as worker:
        monkeypatch.setattr(chat_generation._executor, "submit", worker.submit)
        first = send(client, cid, content="生成产品视频", model="video").json()
        assert first["busy"] and first["messages"][-1]["status"] == "pending"
        assert started.wait(2)
        assert send(client, cid, content="并发消息", model="video").status_code == 409
        assert client.delete(URL + f"/conversations/{cid}", headers=OWNER).status_code == 409
        release.set()
        assert finished.wait(3)
        failed = client.get(URL + f"/conversations/{cid}", headers=OWNER).json()
        assert failed["messages"][-1]["status"] == "failed"
        assert "暂时繁忙" in failed["messages"][-1]["error"]
        finished.clear()
        retry = client.post(
            URL + f"/conversations/{cid}/retry", headers=OWNER, json={"model_id": "video"}
        )
        assert retry.status_code == 200
        assert finished.wait(3)
        completed = client.get(URL + f"/conversations/{cid}", headers=OWNER).json()
        assert len(completed["messages"]) == 2
        assert completed["messages"][-1]["artifacts"][0]["kind"] == "video"
        assert attempts[0] == attempts[1]


def test_legacy_private_chat_store_upgrades_without_losing_history(tmp_path):
    store = ChatStore(tmp_path)
    with sqlite3.connect(store.database) as connection:
        connection.execute(
            "CREATE TABLE messages(sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,conversation_id TEXT,role TEXT,content TEXT,model_id TEXT,created_at TEXT,status TEXT,error TEXT,attachments_json TEXT DEFAULT '[]')"
        )
        connection.execute(
            "INSERT INTO messages(id,conversation_id,role,content,model_id,created_at,status) VALUES('message','old','user','旧消息','old','2026-10-09','completed')"
        )
    with store.connect(write=True) as connection:
        row = connection.execute("SELECT * FROM messages").fetchone()
        assert row["content"] == "旧消息"
        assert row["request_json"] == "{}"
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "prompt, expected",
    [
        ("根据这个PDF生成产品海报", "image"),
        ("把Word内容转成视频", "video"),
        ("能不能帮我生成一张产品图片", "image"),
        ("把PDF转成Word", "docx"),
        ("把Word转成PDF", "pdf"),
        ("如何生成PDF", "text"),
    ],
)
def test_auto_creation_uses_requested_destination_instead_of_source_format(prompt, expected):
    assert chat_generation.infer_output(prompt) == expected


def test_media_prompt_keeps_latest_instruction_when_multiple_documents_fill_context():
    history = [
        {
            "role": "user",
            "content": "海报必须使用红色背景和中文标题",
            "attachments": [
                {"kind": "document", "metadata_json": json.dumps({"text": "长资料甲" * 16000})},
                {"kind": "document", "metadata_json": json.dumps({"text": "长资料乙" * 16000})},
            ],
        }
    ]
    prompt = chat_generation._media_prompt(history, "参考文档" * 20000)
    assert len(prompt) <= 60000
    assert prompt.endswith("海报必须使用红色背景和中文标题")
