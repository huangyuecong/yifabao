import io
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from test_content_ai_model_config import headers as signed_model_headers
from test_content_ai_model_config import manager as manager
from test_content_ai_model_config import text_payload
from test_content_entry_identity import signed as signed

from agent_core import Identity
from content_service import chat_generation, chat_provider, chat_routes
from content_service.ai_model_config import require_manager
from content_service.chat_storage import ChatStore
from content_service.config import load_settings

URL = "/api/v1/chat"
OWNER = {"X-User-Id": "chat-user-a", "X-User-Name": "user-a", "X-Roles": "user"}
OTHER = {"X-User-Id": "chat-user-b", "X-User-Name": "user-b", "X-Roles": "admin"}


@pytest.fixture
def chat_client(tmp_path, monkeypatch):
    # Execute workers deterministically in these existing provider/ownership cases.
    # Dedicated creation tests exercise pending/polling with the real executor.
    monkeypatch.setattr(chat_generation._executor, "submit", lambda function, *args: function(*args))
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "development")
    settings = replace(
        load_settings(),
        data_dir=tmp_path,
        llm_provider="openai_compatible",
        llm_base_url="https://models.example/v1",
        llm_api_key="private-test-key",
        llm_model="text-model",
        llm_fallback_model="backup-model",
    )
    monkeypatch.setattr(chat_routes, "get_runtime_settings", lambda: settings)
    app = FastAPI()
    app.include_router(chat_routes.router)
    with TestClient(app) as client:
        yield client, settings, app


def create(client, model_id="text-default", headers=OWNER):
    response = client.post(URL + "/conversations", json={"model_id": model_id}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def send(client, cid, content="你好", model_id="text-default", attachments=(), headers=OWNER):
    return client.post(
        URL + f"/conversations/{cid}/messages",
        headers=headers,
        json={"model_id": model_id, "content": content, "attachment_ids": list(attachments)},
    )


def allow_vision(client, app):
    app.dependency_overrides[require_manager] = lambda: Identity(
        "manager", "管理员", "", frozenset()
    )
    body = {
        "expected_revision": 0,
        "models": [
            {
                "id": "vision",
                "name": "视觉模型",
                "model": "vision-model",
                "enabled": True,
                "supports_images": True,
                "supports_video": True,
            },
            {
                "id": "text",
                "name": "文字模型",
                "model": "text-model",
                "enabled": True,
                "supports_images": False,
                "supports_video": False,
            },
        ],
    }
    response = client.put(
        URL + "/models/admin", json=body, headers={**OWNER, "X-Content-Config-Request": "1"}
    )
    assert response.status_code == 200, response.text


def mock_provider(monkeypatch, responses, observed):
    original_client = httpx.Client

    def handle(request):
        if str(request.url) == "https://models.example/v1/responses":
            # Scripted failures model a provider outage, so the Responses fallback is down too.
            return httpx.Response(503, json={"error": {"message": "private-test-key"}})
        observed.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer private-test-key"
        assert str(request.url) == "https://models.example/v1/chat/completions"
        response = responses.pop(0)
        if isinstance(response, int):
            return httpx.Response(response, json={"error": {"message": "private-test-key"}})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": response}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr(
        chat_provider.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )


def test_real_provider_payload_contains_full_history_and_selected_model(chat_client, monkeypatch):
    client, _, _ = chat_client
    cid = create(client)
    observed = []
    mock_provider(monkeypatch, ["第一轮回复", "第二轮回复"], observed)
    assert send(client, cid, "我的产品是雅酿").json()["messages"][-1]["content"] == "第一轮回复"
    second = send(client, cid, "根据刚才的产品写一句话", model_id="text-fallback")
    assert second.status_code == 200
    assert second.json()["model_id"] == "text-fallback"
    assert observed[1]["model"] == "backup-model"
    assert [row["role"] for row in observed[1]["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert observed[1]["messages"][1]["content"] == "我的产品是雅酿"
    assert observed[1]["messages"][2]["content"] == "第一轮回复"
    assert "response_format" not in observed[1]
    assert "private-test-key" not in second.text
    assert "models.example" not in client.get(URL + "/models", headers=OWNER).text


def test_user_isolation_including_private_attachments(chat_client, monkeypatch):
    client, _, _ = chat_client
    cid = create(client)
    attachment = client.post(
        URL + "/attachments",
        headers=OWNER,
        files={"file": ("资料.txt", "资料事实".encode(), "text/plain")},
    ).json()
    assert client.get(attachment["url"], headers=OTHER).status_code == 404
    assert client.delete(URL + f"/attachments/{attachment['id']}", headers=OTHER).status_code == 404
    assert client.get(URL + "/conversations", headers=OTHER).json()["items"] == []
    assert client.get(URL + f"/conversations/{cid}", headers=OTHER).status_code == 404
    assert send(client, cid, headers=OTHER).status_code == 404
    assert client.delete(URL + f"/conversations/{cid}", headers=OTHER).status_code == 404
    other_cid = create(client, headers=OTHER)
    assert send(client, other_cid, attachments=[attachment["id"]], headers=OTHER).status_code == 404
    assert (
        client.get(attachment["url"], headers=OWNER).headers["cache-control"] == "private, no-store"
    )


def test_provider_failure_is_durable_and_retry_uses_original_question(chat_client, monkeypatch):
    client, _, _ = chat_client
    cid = create(client)
    observed = []
    mock_provider(monkeypatch, [503, "重试成功"], observed)
    result = send(client, cid, "请写一句话").json()
    assert result["messages"][-1]["status"] == "failed"
    assert result["messages"][0]["content"] == "请写一句话"
    assert "private-test-key" not in json.dumps(result)
    saved = client.get(URL + f"/conversations/{cid}", headers=OWNER).json()
    assert saved["messages"][-1]["error"]
    retried = client.post(
        URL + f"/conversations/{cid}/retry", json={"model_id": "text-fallback"}, headers=OWNER
    ).json()
    assert len(retried["messages"]) == 2
    assert retried["messages"][-1]["content"] == "重试成功"
    assert observed[1]["messages"][-1]["content"] == "请写一句话"
    assert observed[1]["model"] == "backup-model"


def test_regenerate_failure_preserves_successful_response(chat_client, monkeypatch):
    client, _, _ = chat_client
    cid = create(client)
    observed = []
    mock_provider(monkeypatch, ["原回复", 429, "新回复"], observed)
    send(client, cid)
    failed = client.post(URL + f"/conversations/{cid}/retry", json={}, headers=OWNER).json()
    assert failed["messages"][-1]["status"] == "failed"
    assert failed["messages"][-1]["content"] == "原回复"
    done = client.post(URL + f"/conversations/{cid}/retry", json={}, headers=OWNER).json()
    assert done["messages"][-1]["content"] == "新回复"


def test_retained_successful_reply_remains_in_followup_context(chat_client, monkeypatch):
    client, _, _ = chat_client
    cid = create(client)
    observed = []
    mock_provider(monkeypatch, ["先前已成功的回复", 503, "后续回复"], observed)
    send(client, cid)
    client.post(URL + f"/conversations/{cid}/retry", json={}, headers=OWNER)
    send(client, cid, "基于之前的回复继续")
    assert observed[-1]["messages"][-2]["content"] == "先前已成功的回复"


def test_signed_local_model_grants_protect_chat_configuration(manager, monkeypatch):
    client, settings = manager
    monkeypatch.setattr(chat_routes, "get_runtime_settings", lambda: settings)
    client.app.include_router(chat_routes.router)
    assert (
        client.get(
            URL + "/models/admin", headers=signed_model_headers(manage=False, roles=["admin"])
        ).status_code
        == 403
    )
    assert client.get(URL + "/models/admin", headers=signed_model_headers()).status_code == 200
    body = {
        "expected_revision": 0,
        "models": [
            {
                "id": "signed-text",
                "name": "获准的模型",
                "model": "configured-model",
                "enabled": True,
            }
        ],
    }
    response = client.put(URL + "/models/admin", json=body, headers=signed_model_headers())
    assert response.status_code == 200, response.text
    assert (
        client.get(URL + "/models", headers=signed_model_headers(manage=False)).json()["models"][0][
            "id"
        ]
        == "signed-text"
    )
    assert (
        client.get(URL + "/models", headers={"X-User-Id": "fake", "X-Roles": "admin"}).status_code
        == 401
    )
    # Other model configuration writes share the document and must retain the chat allow-list.
    changed = client.put(
        "/api/v1/ai-models/text", json=text_payload(revision=1), headers=signed_model_headers()
    )
    assert changed.status_code == 200, changed.text
    retained = client.get(URL + "/models/admin", headers=signed_model_headers()).json()
    assert retained["revision"] == 2
    assert retained["models"][0]["id"] == "signed-text"


def test_model_allowlist_validation_revision_and_local_permission(chat_client, monkeypatch):
    client, settings, app = chat_client
    assert client.get(URL + "/models/admin", headers=OTHER).status_code == 403
    allow_vision(client, app)
    rows = client.get(URL + "/models", headers=OWNER).json()["models"]
    assert {row["id"] for row in rows} == {"vision", "text"}
    cid = create(client, "text")
    assert send(client, cid, model_id="not-allowed").status_code == 422
    assert client.get(URL + f"/conversations/{cid}", headers=OWNER).json()["messages"] == []
    assert (
        client.put(
            URL + "/models/admin",
            json={"expected_revision": 0, "models": []},
            headers={**OWNER, "X-Content-Config-Request": "1"},
        ).status_code
        == 409
    )
    invalid = {
        "expected_revision": 1,
        "models": [
            {
                "id": "bad",
                "name": "坏配置",
                "model": "bad",
                "supports_video": True,
                "supports_images": False,
            }
        ],
    }
    assert (
        client.put(
            URL + "/models/admin", json=invalid, headers={**OWNER, "X-Content-Config-Request": "1"}
        ).status_code
        == 422
    )
    assert (
        client.put(
            URL + "/models/admin", json={"expected_revision": 1, "models": []}, headers=OWNER
        ).status_code
        == 403
    )
    disabled = {
        "expected_revision": 1,
        "models": [{"id": "text", "name": "文字模型", "model": "text-model", "enabled": False}],
    }
    assert (
        client.put(
            URL + "/models/admin", json=disabled, headers={**OWNER, "X-Content-Config-Request": "1"}
        ).status_code
        == 200
    )
    assert send(client, cid, model_id="text").status_code == 422
    assert "private-test-key" not in client.get(URL + "/models/admin", headers=OWNER).text


def test_document_extraction_and_real_image_inputs_across_rounds(chat_client, monkeypatch):
    client, _, app = chat_client
    allow_vision(client, app)
    image = io.BytesIO()
    Image.new("RGB", (16, 16), "green").save(image, "PNG")
    uploaded = client.post(
        URL + "/attachments",
        files={"file": ("照片.png", image.getvalue(), "image/png")},
        headers=OWNER,
    )
    assert uploaded.status_code == 201, uploaded.text
    aid = uploaded.json()["id"]
    text = client.post(
        URL + "/attachments",
        files={"file": ("品牌资料.md", "# 产品\n雅酿产品事实".encode(), "text/markdown")},
        headers=OWNER,
    ).json()
    cid = create(client, "vision")
    observed = []
    mock_provider(monkeypatch, ["识别成功", "继续分析"], observed)
    assert send(client, cid, "分析图片和资料", "vision", [aid, text["id"]]).status_code == 200
    assert "雅酿产品事实" in observed[0]["messages"][1]["content"][0]["text"]
    visual = observed[0]["messages"][1]["content"][-1]
    assert visual["type"] == "image_url"
    assert visual["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert send(client, cid, "继续", "vision").status_code == 200
    assert observed[1]["messages"][1]["content"][-1] == visual
    assert send(client, cid, "切换文字", "text").status_code == 422
    assert client.delete(URL + f"/attachments/{aid}", headers=OWNER).status_code == 409
    assert client.delete(URL + f"/conversations/{cid}", headers=OWNER).status_code == 200
    assert client.get(uploaded.json()["url"], headers=OWNER).status_code == 404


def test_invalid_uploads_and_docx_pdf_extraction(chat_client):
    from docx import Document
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    client, settings, _ = chat_client
    invalid = client.post(
        URL + "/attachments",
        files={"file": ("fake.png", b"not an image", "image/png")},
        headers=OWNER,
    )
    assert invalid.status_code == 422
    assert (
        client.post(
            URL + "/attachments", files={"file": ("empty.txt", b"", "text/plain")}, headers=OWNER
        ).status_code
        == 422
    )
    assert (
        client.post(
            URL + "/attachments",
            files={"file": ("bad.exe", b"MZ", "application/octet-stream")},
            headers=OWNER,
        ).status_code
        == 422
    )
    doc = Document()
    doc.add_paragraph("DOCX 产品事实")
    binary = io.BytesIO()
    doc.save(binary)
    response = client.post(
        URL + "/attachments",
        files={
            "file": (
                "fact.docx",
                binary.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        headers=OWNER,
    )
    assert response.status_code == 201, response.text
    assert "DOCX 产品事实" in response.json()["text_preview"]
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 100 Td (PDF product facts) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    binary = io.BytesIO()
    writer.write(binary)
    response = client.post(
        URL + "/attachments",
        files={"file": ("facts.pdf", binary.getvalue(), "application/pdf")},
        headers=OWNER,
    )
    assert response.status_code == 201, response.text
    assert "PDF product facts" in response.json()["text_preview"]
    blank = PdfWriter()
    blank.add_blank_page(width=200, height=200)
    binary = io.BytesIO()
    blank.write(binary)
    response = client.post(
        URL + "/attachments",
        files={"file": ("scan.pdf", binary.getvalue(), "application/pdf")},
        headers=OWNER,
    )
    assert response.status_code == 422
    assert "OCR" in response.text
    store = ChatStore(settings.data_dir)
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 2
    assert len(list((store.root / "attachments").iterdir())) == 2


def test_pending_attachment_removal_is_private_and_cleanup_failures_can_retry(
    chat_client, monkeypatch
):
    client, _, _ = chat_client
    uploaded = client.post(
        URL + "/attachments",
        files={"file": ("unused.txt", b"unused attachment", "text/plain")},
        headers=OWNER,
    ).json()
    original = chat_routes.remove_attachment_directory

    def unavailable(*args):
        raise HTTPException(503, "附件清理失败")

    monkeypatch.setattr(chat_routes, "remove_attachment_directory", unavailable)
    response = client.delete(URL + f"/attachments/{uploaded['id']}", headers=OWNER)
    assert response.status_code == 503
    assert client.get(uploaded["url"], headers=OWNER).status_code == 200
    monkeypatch.setattr(chat_routes, "remove_attachment_directory", original)
    assert client.delete(URL + f"/attachments/{uploaded['id']}", headers=OWNER).status_code == 200
    assert client.get(uploaded["url"], headers=OWNER).status_code == 404


def test_actual_video_frame_extraction_and_payload(chat_client, monkeypatch):
    import imageio_ffmpeg

    client, settings, app = chat_client
    allow_vision(client, app)
    video = settings.data_dir / "sample.mp4"
    result = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    response = client.post(
        URL + "/attachments",
        files={"file": ("片段.mp4", video.read_bytes(), "video/mp4")},
        headers=OWNER,
    )
    assert response.status_code == 201, response.text
    attachment = response.json()
    assert attachment["frame_count"] == 1
    assert "不包含音轨" in attachment["notice"]
    cid = create(client, "vision")
    observed = []
    mock_provider(monkeypatch, ["蓝色画面"], observed)
    result = send(client, cid, "描述视频", "vision", [attachment["id"]])
    assert result.json()["messages"][-1]["content"] == "蓝色画面"
    assert observed[0]["messages"][1]["content"][-1]["type"] == "image_url"
    assert "关键帧时间" in observed[0]["messages"][1]["content"][-2]["text"]


def test_concurrent_operations_are_serialized_and_stale_requests_recover(chat_client):
    client, settings, _ = chat_client
    cid = create(client)
    store = ChatStore(settings.data_dir)
    operation_id, assistant_id, _ = store.begin(cid, "chat-user-a", "text-default", "问题", [])
    with pytest.raises(HTTPException) as error:
        store.begin(cid, "chat-user-a", "text-default", "并发问题", [])
    assert error.value.status_code == 409
    assert client.delete(URL + f"/conversations/{cid}", headers=OWNER).status_code == 409
    with store.connect(write=True) as connection:
        connection.execute(
            "UPDATE conversations SET operation_until=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), cid),
        )
    recovered = client.get(URL + f"/conversations/{cid}", headers=OWNER).json()
    assert recovered["messages"][-1]["status"] == "failed"
    new_operation, new_assistant, _ = store.begin(
        cid, "chat-user-a", "text-default", "", [], retry=True
    )
    store.finish(cid, operation_id, assistant_id, content="过时结果")
    store.finish(cid, new_operation, new_assistant, content="新的结果")
    assert (
        client.get(URL + f"/conversations/{cid}", headers=OWNER).json()["messages"][-1]["content"]
        == "新的结果"
    )
