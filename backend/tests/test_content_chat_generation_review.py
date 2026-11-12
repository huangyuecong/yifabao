"""Independent regression checks for chat routing, leases and artifact ownership."""

import json

import pytest
from fastapi import HTTPException

from content_service import chat_generation
from content_service.chat_storage import ChatStore, identifier, now


def _store(tmp_path):
    store = ChatStore(tmp_path)
    cid = identifier()
    with store.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO conversations(id,owner,title,model_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (cid, "owner", "新对话", "model", now(), now()),
        )
    return store, cid


def _artifact():
    return {
        "id": identifier(),
        "name": "result.pdf",
        "filename": "result.pdf",
        "kind": "pdf",
        "mime_type": "application/pdf",
        "size": 100,
        "metadata": {"text": "version one"},
    }


@pytest.mark.parametrize(
    "content, expected",
    [
        ("根据这个 PDF 生成产品海报", "image"),
        ("把 Word 内容转成视频", "video"),
        ("能不能帮我生成一张产品图片", "image"),
        ("能否生成一个视频", "video"),
        ("生成一张产品海报，给我暖色调的效果", "image"),
        ("生成一张产品图片，保存为 PNG", "image"),
        ("怎么在别的软件里生成图片", "text"),
    ],
)
def test_routing_uses_requested_target_and_accepts_polite_creation_requests(content, expected):
    assert chat_generation.infer_output(content) == expected


def test_current_media_instruction_survives_two_long_documents():
    instruction = "生成产品海报，背景必须用红色，右上角写新品上市"
    attachments = [
        {"kind": "document", "metadata_json": json.dumps({"text": marker * 12000})}
        for marker in ("甲", "乙")
    ]
    prompt = chat_generation._media_prompt(
        [{"role": "user", "content": instruction, "attachments": attachments}], ""
    )
    assert instruction in prompt
    assert len(prompt) <= 60000


def test_auto_reference_edit_chooses_an_opened_edit_capable_model(monkeypatch):
    chat = {"id": "chat", "kind": "chat"}
    images = [
        {"id": "generation", "kind": "image", "configured": True, "supports_image_edit": False},
        {"id": "editor", "kind": "image", "configured": True, "supports_image_edit": True},
    ]
    monkeypatch.setattr(chat_generation, "chat_configuration", lambda base: {"models": images})
    monkeypatch.setattr(
        chat_generation,
        "resolve_chat_model",
        lambda base, mid: (base, next(item for item in images if item["id"] == mid)),
    )
    history = [{"role": "user", "content": "修改背景为蓝色", "attachments": [{"kind": "image"}]}]
    _, selected, output = chat_generation.plan_generation(None, chat, history, {"output": "auto"})
    assert output == "image"
    assert selected["id"] == "editor"


def test_stale_worker_cannot_renew_or_save_artifacts_after_recovery(tmp_path):
    store, cid = _store(tmp_path)
    old, assistant, _ = store.begin(cid, "owner", "model", "生成文件", [], lease_seconds=-1)
    new, retry_assistant, _ = store.begin(cid, "owner", "model", "", [], retry=True)
    assert retry_assistant == assistant
    assert not store.renew(cid, old)
    assert not store.finish(cid, old, assistant, content="旧任务结果", artifacts=[_artifact()])
    assert store.finish(cid, new, assistant, content="新任务结果", artifacts=[_artifact()])
    with store.connect() as connection:
        document = store.document(connection, store.owned(connection, cid, "owner"))
        assert len(document["messages"][-1]["artifacts"]) == 1
        assert document["messages"][-1]["content"] == "新任务结果"
        assert not document["busy"]


def test_retry_failure_and_new_success_keep_previous_artifact(tmp_path):
    store, cid = _store(tmp_path)
    original = _artifact()
    op, assistant, _ = store.begin(cid, "owner", "model", "生成文档", [], request={"output": "pdf"})
    assert store.finish(cid, op, assistant, content="原文档正文", artifacts=[original])
    retry, _, _ = store.begin(cid, "owner", "other", "", [], retry=True)
    assert store.finish(cid, retry, assistant, error="暂时繁忙")
    with store.connect() as connection:
        document = store.document(connection, store.owned(connection, cid, "owner"))
        assert document["messages"][-1]["content"] == "原文档正文"
        assert document["messages"][-1]["artifacts"][0]["id"] == original["id"]
    retry, _, _ = store.begin(cid, "owner", "other", "", [], retry=True)
    assert store.finish(cid, retry, assistant, content="新文档正文", artifacts=[_artifact()])
    with store.connect() as connection:
        document = store.document(connection, store.owned(connection, cid, "owner"))
        assert len(document["messages"][-1]["artifacts"]) == 2
        assert len(document["messages"]) == 2


def test_reference_artifact_rejects_other_owner_and_other_conversation(tmp_path):
    store, cid = _store(tmp_path)
    original = _artifact()
    op, assistant, _ = store.begin(cid, "owner", "model", "生成文档", [])
    assert store.finish(cid, op, assistant, content="正文", artifacts=[original])
    for owner, conversation in (("other-owner", cid), ("owner", identifier())):
        with pytest.raises(HTTPException) as error:
            chat_generation.reference_context(
                store, owner, conversation, [], {"reference_artifact_id": original["id"]}
            )
        assert error.value.status_code == 404


def test_heartbeat_renews_current_operation_without_blocking_a_request(monkeypatch):
    observed = []

    class Stop:
        count = 0

        def wait(self, seconds):
            assert seconds == 30
            self.count += 1
            return self.count > 2

    class Store:
        def renew(self, cid, op):
            observed.append((cid, op))
            return True

    monkeypatch.setattr(chat_generation, "monotonic", lambda: 1)
    chat_generation._heartbeat(Store(), "conversation", "operation", Stop())
    assert observed == [("conversation", "operation")] * 2
