"""Permission-scoped chat API with durable sessions, attachments and retry."""

import json
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_core import Identity

from . import chat_live, chat_tools
from .access_control import get_content_identity as get_identity
from .access_routes import PrivateAccessRoute
from .ai_model_config import require_manager, write_header
from .chat_artifacts import artifact_path, remove_artifact_directory
from .chat_attachments import (
    MAX_ATTACHMENT_BYTES,
    SUPPORTED_EXTENSIONS,
    attachment_directory,
    remove_attachment_directory,
    store_upload,
)
from .chat_generation import (
    plan_generation,
    prepare_generation_request,
    release_generation,
    reserve_generation,
    submit_generation,
    validate_generation,
)
from .chat_models import ChatModelsUpdate, chat_configuration, resolve_chat_model, save_chat_models
from .chat_storage import ChatStore, identifier, now
from .provider_dependencies import get_runtime_settings

router = APIRouter(prefix="/api/v1/chat", tags=["AI chat"], route_class=PrivateAccessRoute)


def get_store() -> ChatStore:
    return ChatStore(get_runtime_settings().data_dir)


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str | None = Field(default=None, min_length=1, max_length=64)
    title: str = Field(default="新对话", min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value):
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("请输入有效的对话名称")
        return value.strip()


class ChatMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str | None = Field(default=None, min_length=1, max_length=64)
    content: str = Field(default="", max_length=16000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=8)
    output: Literal["auto", "text", "doc", "docx", "pdf", "image", "video"] = "auto"
    reference_artifact_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @field_validator("attachment_ids")
    @classmethod
    def distinct_ids(cls, value):
        if len(value) != len(set(value)) or any(len(item) != 32 for item in value):
            raise ValueError("附件列表无效")
        return value


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str | None = Field(default=None, min_length=1, max_length=64)


@router.get("/models")
def models(_: Identity = Depends(get_identity)):
    return {
        **chat_configuration(get_runtime_settings()),
        "max_attachment_bytes": MAX_ATTACHMENT_BYTES,
        "max_files": 8,
        "supported_extensions": SUPPORTED_EXTENSIONS,
    }


@router.get("/models/admin")
def admin_models(_: Identity = Depends(require_manager)):
    return chat_configuration(get_runtime_settings(), admin=True)


@router.put("/models/admin", dependencies=[Depends(write_header)])
def update_models(body: ChatModelsUpdate, identity: Identity = Depends(require_manager)):
    return save_chat_models(get_runtime_settings(), body, identity)


@router.get("/conversations")
def list_conversations(
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(get_identity),
    store: ChatStore = Depends(get_store),
):
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count FROM conversations c WHERE owner=? ORDER BY updated_at DESC,id LIMIT ? OFFSET ?",
            (identity.user_id, limit, offset),
        ).fetchall()
        total = connection.execute(
            "SELECT COUNT(*) FROM conversations WHERE owner=?", (identity.user_id,)
        ).fetchone()[0]
        return {
            "items": [
                {
                    key: row[key]
                    for key in (
                        "id",
                        "title",
                        "model_id",
                        "created_at",
                        "updated_at",
                        "message_count",
                    )
                }
                for row in rows
            ],
            "total": total,
        }


@router.post("/conversations", status_code=201)
def create_conversation(
    body: ConversationCreate,
    identity: Identity = Depends(get_identity),
    store: ChatStore = Depends(get_store),
):
    model_id = body.model_id or "auto"
    if model_id != "auto":
        _, model = resolve_chat_model(get_runtime_settings(), model_id)
        model_id = model["id"]
    with store.connect(write=True) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM conversations WHERE owner=?", (identity.user_id,)
        ).fetchone()[0]
        if count >= 1000:
            raise HTTPException(422, "对话数量已达到上限，请先删除旧对话")
        cid, created = identifier(), now()
        connection.execute(
            "INSERT INTO conversations(id,owner,title,model_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (cid, identity.user_id, body.title, model_id, created, created),
        )
        return store.document(connection, store.owned(connection, cid, identity.user_id))


@router.get("/conversations/{conversation_id}")
def conversation(
    conversation_id: str,
    identity: Identity = Depends(get_identity),
    store: ChatStore = Depends(get_store),
):
    with store.connect(write=True) as connection:
        row = store.recover(connection, store.owned(connection, conversation_id, identity.user_id))
        return store.document(connection, row)


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    identity: Identity = Depends(get_identity),
    store: ChatStore = Depends(get_store),
):
    with store.connect(write=True) as connection:
        row = store.recover(connection, store.owned(connection, conversation_id, identity.user_id))
        if row["operation_id"]:
            raise HTTPException(409, "对话正在生成回复，请稍后删除")
        ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM attachments WHERE conversation_id=? AND owner=?",
                (conversation_id, identity.user_id),
            )
        ]
        # Keep database ownership records until private file cleanup succeeds, so failures are retryable.
        for aid in ids:
            remove_attachment_directory(store, aid)
        for artifact in connection.execute(
            "SELECT id FROM artifacts WHERE conversation_id=? AND owner=?",
            (conversation_id, identity.user_id),
        ):
            remove_artifact_directory(store, artifact["id"])
        connection.execute(
            "DELETE FROM attachments WHERE conversation_id=? AND owner=?",
            (conversation_id, identity.user_id),
        )
        connection.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
    return {"deleted": True, "id": conversation_id}


def _generate(
    store,
    conversation_id,
    identity,
    settings,
    model,
    content="",
    attachment_ids=(),
    *,
    retry=False,
    output="auto",
    reference_artifact_id=None,
):
    # Validate ownership, media capability and context limits before saving a new user message.
    with store.connect() as connection:
        row = store.owned(connection, conversation_id, identity.user_id)
        history = store.history(connection, row)
        if retry:
            if history and history[-1]["role"] == "assistant":
                history = history[:-1]
            request = next(
                (item.get("request", {}) for item in reversed(history) if item["role"] == "user"),
                {},
            )
        else:
            attachments = [
                dict(store.attachment(connection, aid, identity.user_id)) for aid in attachment_ids
            ]
            history.append({"role": "user", "content": content, "attachments": attachments})
            request = {"output": output, "reference_artifact_id": reference_artifact_id}
    if not retry and output == "auto":
        # The model picks the tool; the rule-based router remains the fallback.
        routing = chat_tools.route(settings, model, history)
        if routing:
            request.update(output=routing["output"], routing=routing)
    request = prepare_generation_request(store, identity.user_id, conversation_id, history, request)
    settings, model, output = plan_generation(settings, model, history, request)
    validate_generation(
        store, identity.user_id, conversation_id, settings, model, history, request, output
    )
    request = {**request, "output": output}
    reserve_generation()
    try:
        operation_id, assistant_id, history = store.begin(
            conversation_id,
            identity.user_id,
            model["id"],
            content,
            list(attachment_ids),
            retry=retry,
            lease_seconds=120,
            request=request,
        )
    except Exception:
        release_generation()
        raise
    chat_live.start(assistant_id, identity.user_id, conversation_id)
    routing = request.get("routing") or {}
    if routing.get("tool"):
        how = "模型通过函数调用选择" if routing.get("method") == "function_calling" else "按规则匹配"
        chat_live.stage(assistant_id, f"{how}工具：{chat_tools.TOOL_LABELS[routing['tool']]}")
    try:
        submit_generation(
            store,
            identity.user_id,
            conversation_id,
            operation_id,
            assistant_id,
            settings,
            model,
            history,
            request,
            output,
        )
    except Exception:
        store.finish(conversation_id, operation_id, assistant_id, error="生成任务未启动，请重试")
        chat_live.end(assistant_id)
    with store.connect() as connection:
        return store.document(
            connection, store.owned(connection, conversation_id, identity.user_id)
        )


@router.post("/conversations/{conversation_id}/messages")
def send_message(
    conversation_id: str,
    body: ChatMessageCreate,
    identity: Identity = Depends(get_identity),
    store: ChatStore = Depends(get_store),
):
    if not body.content.strip() and not body.attachment_ids and not body.reference_artifact_id:
        raise HTTPException(422, "请输入问题或添加附件")
    settings = get_runtime_settings()
    model = None
    if body.model_id and body.model_id != "auto":
        _, model = resolve_chat_model(settings, body.model_id)
    return _generate(
        store,
        conversation_id,
        identity,
        settings,
        model,
        body.content.strip() or ("请分析参考文件" if body.reference_artifact_id else ""),
        body.attachment_ids,
        output=body.output,
        reference_artifact_id=body.reference_artifact_id,
    )


@router.get("/conversations/{conversation_id}/messages/{message_id}/stream")
def stream_message(
    conversation_id: str,
    message_id: str,
    identity: Identity = Depends(get_identity),
    store: ChatStore = Depends(get_store),
):
    """Server-sent events for a reply in progress: stage, delta, reset, end."""
    with store.connect() as connection:
        store.owned(connection, conversation_id, identity.user_id)

    def events():
        for kind, data in chat_live.follow(message_id, identity.user_id, conversation_id):
            if kind == "ping":
                yield ": ping\n\n"
            else:
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/conversations/{conversation_id}/retry")
def retry_message(
    conversation_id: str,
    body: RetryRequest,
    identity: Identity = Depends(get_identity),
    store: ChatStore = Depends(get_store),
):
    with store.connect() as connection:
        store.owned(connection, conversation_id, identity.user_id)
    settings = get_runtime_settings()
    model = None
    if body.model_id and body.model_id != "auto":
        _, model = resolve_chat_model(settings, body.model_id)
    return _generate(store, conversation_id, identity, settings, model, retry=True)


@router.get("/artifacts/{artifact_id}/content")
def artifact_content(
    artifact_id: str,
    identity: Identity = Depends(get_identity),
    store: ChatStore = Depends(get_store),
):
    with store.connect() as connection:
        artifact = dict(store.artifact(connection, artifact_id, identity.user_id))
    return FileResponse(
        artifact_path(store, artifact),
        media_type=artifact["mime_type"],
        filename=artifact["name"],
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/attachments", status_code=201)
def upload_attachment(
    file: UploadFile = File(...),
    identity: Identity = Depends(get_identity),
    store: ChatStore = Depends(get_store),
):
    return store_upload(store, get_runtime_settings(), identity.user_id, file)


@router.get("/attachments/{attachment_id}/content")
def attachment_content(
    attachment_id: str,
    identity: Identity = Depends(get_identity),
    store: ChatStore = Depends(get_store),
):
    with store.connect() as connection:
        row = store.attachment(connection, attachment_id, identity.user_id)
        suffix = json.loads(row["metadata_json"])["suffix"]
    source = attachment_directory(store, attachment_id) / ("original" + suffix)
    if not source.is_file():
        raise HTTPException(410, "附件文件已丢失，请重新上传")
    return FileResponse(
        source,
        media_type=row["mime_type"],
        filename=row["name"],
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/attachments/{attachment_id}")
def delete_attachment(
    attachment_id: str,
    identity: Identity = Depends(get_identity),
    store: ChatStore = Depends(get_store),
):
    with store.connect(write=True) as connection:
        row = store.attachment(connection, attachment_id, identity.user_id)
        if row["conversation_id"] is not None:
            raise HTTPException(409, "附件已用于对话，请通过删除对话清理附件")
        remove_attachment_directory(store, attachment_id)
        connection.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
    return {"deleted": True, "id": attachment_id}
