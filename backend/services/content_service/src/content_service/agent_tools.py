"""Workbench tools shared by the operations-assistant Agent and the MCP server.

Each tool is a thin call into the same catalog functions the pages use, with the
caller's identity, so ownership and permission checks stay in one place. Tools
never publish: publishing is outward-facing, so `prepare_publish` only returns a
link to the publish page, where a person reviews the preview and submits.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from agent_core import Identity

from . import db
from .access_control import has_permission
from .providers import KnowledgeQuery

MAX_GENERATE = 10
_STATUSES = ("queued", "running", "review", "approved", "rejected", "failed")
_STATUS_LABELS = {
    "queued": "排队中", "running": "生成中", "review": "待审核", "approved": "已通过",
    "rejected": "已驳回", "failed": "失败",
}


class ToolError(Exception):
    """A tool refused or failed in a way the model may read and recover from."""


@dataclass(frozen=True)
class Tool:
    name: str
    label: str
    description: str
    parameters: dict
    run: Callable[[Identity, dict], dict]
    writes: bool = False

    def openai(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def _schema(properties: dict | None = None, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False}


def _text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _limit(args: dict, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(args.get("limit") or default), maximum))
    except (TypeError, ValueError):
        return default


def _ids(args: dict, key: str, maximum: int) -> list[str]:
    values = args.get(key)
    if not isinstance(values, list) or not values:
        raise ToolError(f"{key} 需要至少一个 ID")
    ids = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    if len(ids) > maximum:
        raise ToolError(f"一次最多处理 {maximum} 个，请分批进行")
    return ids


def _require_writer(identity: Identity) -> None:
    if not has_permission(identity, "library.manage"):
        raise ToolError("当前账号没有资料库管理权限，不能发起生成；请让有权限的同事操作")


# ---------- read tools ----------

def _overview(identity: Identity, args: dict) -> dict:
    from .catalog_routes import overview

    with db.SessionLocal() as session:
        data = overview(identity=identity, session=session)
    items = {_STATUS_LABELS.get(key, key): value for key, value in data["items"].items()}
    return {"商品数": data["products"], "生成与审核": items, "内容库条数": data["contents"],
            "发布记录": data["records"]}


def _search_products(identity: Identity, args: dict) -> dict:
    from .catalog_routes import list_products

    with db.SessionLocal() as session:
        data = list_products(q=_text(args.get("keyword"), 120), page=1, page_size=_limit(args, 10, 30),
                             identity=identity, session=session)
    return {"total": data["total"], "products": [{
        "id": item["id"], "name": item["name"], "category": item["category"], "price": item["price"],
        "has_images": bool(item["images"]), "content_count": item["content_count"],
        "draft_status": _STATUS_LABELS.get(item["draft_status"] or "", "未生成"),
    } for item in data["items"]]}


def _list_items(identity: Identity, args: dict) -> dict:
    from .catalog_routes import list_items

    status = args.get("status") or ""
    if status and status not in _STATUSES:
        raise ToolError("status 只能是 " + "、".join(_STATUSES))
    with db.SessionLocal() as session:
        data = list_items(status=status or None, batch_id=None, product_id=None, page=1,
                          page_size=_limit(args, 10, 30), identity=identity, session=session)
    return {"total": data["total"],
            "counts": {_STATUS_LABELS.get(key, key): value for key, value in data["counts"].items()},
            "items": [{
                "id": item["id"], "product": item["product"]["name"], "product_id": item["product"]["id"],
                "status": _STATUS_LABELS.get(item["status"], item["status"]), "title": item["title"],
                "body": _text(item["body"], 160), "error": item["error"], "feedback": item["feedback"],
                "media": sorted({media["kind"] for media in item["media"]}),
            } for item in data["items"]]}


def _list_contents(identity: Identity, args: dict) -> dict:
    from .catalog_routes import list_contents

    with db.SessionLocal() as session:
        data = list_contents(q=_text(args.get("keyword"), 120), product_id=None, page=1,
                             page_size=_limit(args, 10, 30), identity=identity, session=session)
    return {"total": data["total"], "contents": [{
        "id": item["id"], "product": item["product"]["name"], "title": item["title"],
        "has_video": item["has_video"],
        "published": [f"{row['platform']}:{row['status']}" for row in item["publishes"]],
    } for item in data["items"]]}


def _list_records(identity: Identity, args: dict) -> dict:
    from .catalog_routes import list_records

    workflow = args.get("workflow") or None
    if workflow not in {None, "listing", "promotion"}:
        raise ToolError("workflow 只能是 listing 或 promotion")
    with db.SessionLocal() as session:
        data = list_records(workflow=workflow, status=args.get("status") or None, platform=None, page=1,
                            page_size=_limit(args, 10, 30), identity=identity, session=session)
    return {"total": data["total"], "counts": data["counts"], "records": [{
        "id": row["id"], "workflow": row["workflow"], "platform": row["platform"], "status": row["status"],
        "product": row["product_name"], "title": row["title"], "error": _text(row["error"], 120),
        "url": row["external_url"],
    } for row in data["items"]]}


def search_knowledge(identity: Identity, query: str, limit: int = 4, *, categories: tuple[str, ...] = ()):
    """Knowledge chunks the caller may read, via the configured provider."""
    from .provider_dependencies import get_knowledge_provider

    with db.SessionLocal() as session:
        provider = get_knowledge_provider(session)
        return list(provider.search(KnowledgeQuery(
            text=query, requester_id=identity.user_id, task_id="agent", categories=categories,
            allowed_data_levels=frozenset({"public", "internal"}), limit=limit,
            requester_department=identity.department, requester_roles=identity.roles,
        )))


def _search_knowledge(identity: Identity, args: dict) -> dict:
    query = _text(args.get("query"), 200)
    if not query:
        raise ToolError("query 不能为空")
    chunks = search_knowledge(identity, query, _limit(args, 4, 8))
    return {"results": [{"title": chunk.title, "category": chunk.category, "text": _text(chunk.text, 500),
                         "source": chunk.source_uri or ""} for chunk in chunks]}


def _check_compliance(identity: Identity, args: dict) -> dict:
    from .models import SensitiveRule

    text = str(args.get("text") or "")
    if not text.strip():
        raise ToolError("text 不能为空")
    folded = text.casefold()
    with db.SessionLocal() as session:
        phrases = session.scalars(select(SensitiveRule.phrase).where(SensitiveRule.active.is_(True))).all()
    hits = sorted({phrase for phrase in phrases if phrase and phrase.casefold() in folded})
    return {"passed": not hits, "hits": hits[:30], "rules_checked": len(phrases)}


# ---------- write tools ----------

def _start_generation(identity: Identity, args: dict) -> dict:
    from .catalog_routes import BatchInput, create_batch

    _require_writer(identity)
    ids = _ids(args, "product_ids", MAX_GENERATE)
    try:
        body = BatchInput(product_ids=ids, with_copy=bool(args.get("with_copy", True)),
                          with_images=bool(args.get("with_images", False)),
                          with_video=bool(args.get("with_video", False)),
                          instructions=_text(args.get("instructions"), 2000))
    except ValidationError:
        raise ToolError("生成参数不完整，请检查商品和生成项") from None
    with db.SessionLocal() as session:
        result = create_batch(body, identity=identity, session=session)
    return {"batch_id": result["id"], "batch_name": result["name"], "queued": result["total"],
            "note": "已进入后台生成，完成后在「生成与审核」由人工审核"}


def _regenerate(identity: Identity, args: dict) -> dict:
    from .catalog_routes import RegenerateInput, regenerate_item

    _require_writer(identity)
    item_id = _text(args.get("item_id"), 64)
    parts = args.get("parts") or ["copy"]
    try:
        body = RegenerateInput(parts=parts, feedback=_text(args.get("feedback"), 2000))
    except ValidationError:
        raise ToolError("parts 只能包含 copy、images、video") from None
    with db.SessionLocal() as session:
        item = regenerate_item(item_id, body, identity=identity, session=session)
    return {"item_id": item["id"], "product": item["product"]["name"], "status": "排队中"}


def _prepare_publish(identity: Identity, args: dict) -> dict:
    from .catalog_models import ProductContent

    workflow = args.get("workflow")
    if workflow not in {"listing", "promotion"}:
        raise ToolError("workflow 只能是 listing（闲鱼上架）或 promotion（抖音/小红书/快手宣传）")
    ids = _ids(args, "content_ids", 50)
    with db.SessionLocal() as session:
        found = set(session.scalars(select(ProductContent.id).where(
            ProductContent.id.in_(ids), ProductContent.owner_id == identity.user_id,
            ProductContent.active.is_(True))).all())
    missing = [identifier for identifier in ids if identifier not in found]
    if missing:
        raise ToolError("以下内容不在内容库中：" + "、".join(missing[:5]))
    page = "商品上架" if workflow == "listing" else "内容宣传"
    return {"action": {"type": "open_publish", "workflow": workflow, "content_ids": ids,
                       "path": f"/agent/content/{workflow}?ids=" + ",".join(ids),
                       "label": f"去「{page}」确认发布（已选 {len(ids)} 条）"},
            "note": "发布需要用户在页面上预览并确认，助手不会直接发布"}


_LIMIT = {"limit": {"type": "integer", "minimum": 1, "maximum": 30, "description": "最多返回条数，默认 10"}}

TOOLS: dict[str, Tool] = {tool.name: tool for tool in (
    Tool("workbench_overview", "查看工作台概况",
         "统计商品数、生成与审核各状态数量、内容库条数和发布记录状态。", _schema(), _overview),
    Tool("search_products", "查找商品",
         "按名称、SKU 或分类关键词查找商品库中的商品，返回商品 ID、是否有图片、已有内容数和最近生成状态。",
         _schema({"keyword": {"type": "string", "description": "关键词，留空表示最近更新的商品"}, **_LIMIT}),
         _search_products),
    Tool("list_review_items", "查看生成与审核",
         "列出批量生成的条目及状态（排队、生成中、待审核、已通过、已驳回、失败），含标题、文案摘要和错误原因。",
         _schema({"status": {"type": "string", "enum": list(_STATUSES)}, **_LIMIT}), _list_items),
    Tool("list_contents", "查看内容库",
         "列出审核通过、可以发布的内容，含内容 ID、标题、是否有视频以及各平台发布状态。",
         _schema({"keyword": {"type": "string"}, **_LIMIT}), _list_contents),
    Tool("list_publish_records", "查看发布记录",
         "列出闲鱼上架或抖音/小红书/快手宣传的发布记录、状态和失败原因。",
         _schema({"workflow": {"type": "string", "enum": ["listing", "promotion"]},
                  "status": {"type": "string", "description": "如 succeeded、failed、uncertain"}, **_LIMIT}),
         _list_records),
    Tool("search_knowledge", "检索企业资料",
         "在资料库中检索产品资料、品牌和企业介绍，回答事实问题或写文案前使用。",
         _schema({"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 8}},
                 ("query",)), _search_knowledge),
    Tool("check_compliance", "合规检测",
         "用广告法禁用词和敏感词规则检查一段文案，返回命中的词。", _schema({"text": {"type": "string"}}, ("text",)),
         _check_compliance),
    Tool("start_generation", "发起批量生成",
         f"为商品发起后台批量生成（文案、宣传图片、宣传视频），一次最多 {MAX_GENERATE} 个商品，商品需已有图片。"
         "生成结果进入「生成与审核」，必须由人工审核通过后才进入内容库。",
         _schema({"product_ids": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_GENERATE},
                  "with_copy": {"type": "boolean", "description": "生成标题和文案，默认是"},
                  "with_images": {"type": "boolean", "description": "生成宣传图片，默认否"},
                  "with_video": {"type": "boolean", "description": "生成宣传视频，默认否"},
                  "instructions": {"type": "string", "description": "额外的风格或卖点要求"}},
                 ("product_ids",)), _start_generation, writes=True),
    Tool("regenerate_item", "按意见重新生成",
         "对待审核、已驳回或失败的条目，按修改意见重新生成文案、图片或视频。",
         _schema({"item_id": {"type": "string"},
                  "parts": {"type": "array", "items": {"type": "string", "enum": ["copy", "images", "video"]}},
                  "feedback": {"type": "string", "description": "修改意见"}}, ("item_id", "feedback")),
         _regenerate, writes=True),
    Tool("prepare_publish", "准备发布（需人工确认）",
         "为内容库中的内容准备发布：返回发布页链接，由用户预览后确认提交。助手不能直接发布。",
         _schema({"workflow": {"type": "string", "enum": ["listing", "promotion"]},
                  "content_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 50}},
                 ("workflow", "content_ids")), _prepare_publish),
)}


def call(identity: Identity, name: str, arguments: dict | str | None) -> dict:
    """Run one tool. Returns {"ok", "result" | "error"}; never raises for tool-level failures."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"ok": False, "error": f"没有名为 {name} 的工具"}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            return {"ok": False, "error": "工具参数不是有效的 JSON"}
    if not isinstance(arguments, dict):
        return {"ok": False, "error": "工具参数必须是对象"}
    try:
        return {"ok": True, "result": tool.run(identity, arguments)}
    except ToolError as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException as exc:
        return {"ok": False, "error": str(exc.detail)}
