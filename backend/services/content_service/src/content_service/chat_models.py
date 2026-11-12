"""Chat and creation models, using the existing private provider configuration."""

import re
from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .ai_model_config import _apply, locked_document, read_document, write_document
from .config import Settings


class ChatModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=200)
    enabled: StrictBool = True
    kind: Literal["chat", "image", "video"] = "chat"
    provider_source: Literal["text", "image", "video"] = "text"
    supports_images: StrictBool = False
    supports_video: StrictBool = False
    supports_image_edit: StrictBool = False


class ChatModelsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    models: list[ChatModelSelection] = Field(max_length=32)


def configured(settings: Settings, kind="chat") -> bool:
    prefix = {"chat": "llm", "image": "image", "video": "ai_video"}[kind]
    provider = getattr(settings, prefix + "_provider")
    if kind == "chat":
        return bool(
            provider in {"openai_compatible", "volcengine_ark"}
            and settings.llm_base_url
            and settings.llm_api_key
        )
    if provider == "volcengine_ark" or (kind == "image" and provider == "openai_compatible"):
        return bool(
            getattr(settings, prefix + "_base_url") and getattr(settings, prefix + "_api_key")
        )
    if provider == "hiagent":
        return bool(
            settings.hiagent_base_url
            and settings.hiagent_workspace_id
            and settings.hiagent_secret_key
            and (getattr(settings, prefix + "_api_key") or settings.hiagent_access_key)
        )
    return False


def _text_settings(base: Settings, document: dict) -> Settings:
    return _source_settings(base, document, "text")


def _source_settings(base: Settings, document: dict, source: str) -> Settings:
    try:
        return _apply(base, source, document["overrides"].get(source, {}))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(503, "模型接口配置无效，请联系模型管理员恢复配置") from None


def _models(base: Settings, document: dict) -> list[dict]:
    settings = _text_settings(base, document)
    stored = document.get("chat_models")
    if stored is None:
        stored = (
            [
                {
                    "id": "text-default",
                    "name": "默认模型",
                    "model": settings.llm_model,
                    "enabled": True,
                    "supports_images": False,
                    "supports_video": False,
                }
            ]
            if configured(settings) and settings.llm_model
            else []
        )
        if configured(settings) and settings.llm_fallback_model:
            stored.append(
                {
                    "id": "text-fallback",
                    "name": "备用模型",
                    "model": settings.llm_fallback_model,
                    "enabled": True,
                    "supports_images": False,
                    "supports_video": False,
                }
            )
    try:
        if not isinstance(stored, list) or len(stored) > 32:
            raise ValueError()
        rows = [ChatModelSelection.model_validate(item).model_dump() for item in stored]
        _validate(rows)
    except (ValueError, TypeError):
        raise HTTPException(503, "AI 对话模型配置无效，请联系模型管理员") from None
    result = []
    for item in rows:
        runtime = _source_settings(base, document, item["provider_source"])
        prefix = {"chat": "llm", "image": "image", "video": "ai_video"}[item["kind"]]
        result.append(
            {
                **item,
                "provider": getattr(runtime, prefix + "_provider"),
                "configured": configured(runtime, item["kind"]),
            }
        )
    return result


def _validate(rows: list[dict]) -> None:
    if len({item["id"] for item in rows}) != len(rows):
        raise ValueError("模型标识不能重复")
    if len({(item["kind"], item["model"]) for item in rows}) != len(rows):
        raise ValueError("同一模型不能重复添加")
    for row in rows:
        for key in ("name", "model"):
            value = row[key]
            if value != value.strip() or re.search(r"[\x00-\x1f\x7f]", value):
                raise ValueError("模型名称与标识不能包含控制字符或首尾空格")
        if row["supports_video"] and not row["supports_images"]:
            raise ValueError("视频关键帧识别需要同时开启图片识别")
        if (
            row["provider_source"]
            != {"chat": "text", "image": "image", "video": "video"}[row["kind"]]
        ):
            raise ValueError("模型类型和接口类别不一致")
        if row["kind"] != "chat" and (row["supports_images"] or row["supports_video"]):
            raise ValueError("图片和视频识别属于对话模型的能力")
        if row["kind"] != "image" and row["supports_image_edit"]:
            raise ValueError("参考图编辑仅适用于图片生成模型")


def chat_configuration(base: Settings, *, admin=False) -> dict:
    document = read_document(base)
    settings = _text_settings(base, document)
    rows = _models(base, document)
    ready = any(row["enabled"] and row["configured"] for row in rows)
    result = {
        "models": rows if admin else [row for row in rows if row["enabled"]],
        "configured": ready,
        "message": "" if ready else "请管理员在 AI 服务状态中配置接口并开放模型",
        "document_formats": ["doc", "docx", "pdf"],
        "capabilities": {
            **{
                kind: any(
                    row["enabled"] and row["configured"] and row["kind"] == kind for row in rows
                )
                for kind in ("chat", "image", "video")
            },
            "vision": any(
                row["enabled"] and row["configured"] and row["supports_images"] for row in rows
            ),
            "video_read": any(
                row["enabled"] and row["configured"] and row["supports_video"] for row in rows
            ),
            "image_edit": any(
                row["enabled"] and row["configured"] and row["supports_image_edit"] for row in rows
            ),
            "documents": True,
        },
    }
    if admin:
        available = [
            {"id": name, "name": name, "kind": "chat", "provider_source": "text"}
            for name in dict.fromkeys([settings.llm_model, settings.llm_fallback_model])
            if name
        ]
        for kind, prefix in (("image", "image"), ("video", "ai_video")):
            current = _source_settings(base, document, kind)
            name = getattr(current, prefix + "_model")
            if name:
                available.append({"id": name, "name": name, "kind": kind, "provider_source": kind})
        result.update(
            revision=document["revision"],
            provider=settings.llm_provider,
            available_models=available,
        )
    return result


def resolve_chat_model(base: Settings, model_id: str | None) -> tuple[Settings, dict]:
    document = read_document(base)
    rows = [row for row in _models(base, document) if row["enabled"]]
    if model_id == "auto":
        model_id = None
    selected = (
        next((row for row in rows if row["id"] == model_id), None)
        if model_id
        else next((row for row in rows if row["configured"] and row["kind"] == "chat"), None)
        or next((row for row in rows if row["configured"]), None)
    )
    if selected is None:
        raise HTTPException(422, "所选模型未开放，请刷新模型列表后重试")
    if not selected["configured"]:
        raise HTTPException(503, "所选模型的接口尚未配置，请联系模型管理员")
    settings = _source_settings(base, document, selected["provider_source"])
    return settings, selected


def save_chat_models(base: Settings, payload: ChatModelsUpdate, identity) -> dict:
    rows = [item.model_dump() for item in payload.models]
    try:
        _validate(rows)
        with locked_document(base) as document:
            if document["revision"] != payload.expected_revision:
                raise HTTPException(409, "模型配置已更新，请重新加载后保存")
            document["chat_models"] = rows
            document["revision"] += 1
            document["events"].append(
                {
                    "revision": document["revision"],
                    "capability": "chat",
                    "action": "saved",
                    "actor": identity.user_id,
                    "name": identity.name,
                    "at": datetime.now(UTC).isoformat(),
                    "fields": ["chat_models"],
                }
            )
            document["events"] = document["events"][-200:]
            write_document(base, document)
        return chat_configuration(base, admin=True)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except OSError:
        raise HTTPException(503, "AI 对话模型配置保存失败，请检查服务器存储") from None
