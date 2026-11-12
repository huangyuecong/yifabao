"""Owner-scoped automation APIs with local browser and Android execution targets."""

import hashlib
import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_core import Identity

from . import automation_review as review
from . import db
from .access_control import get_content_identity as get_identity
from .access_control import has_permission
from .access_routes import PrivateAccessRoute
from .ai_model_config import write_header
from .automation_jobs import (
    ACTIVE,
    LEASE_SECONDS,
    audit,
    business_config,
    encoded,
    event,
    execute_creation_batch,
    execute_run,
    recover_interrupted,
)
from .automation_models import AutomationEvent, AutomationRun, AutomationWorkflow
from .automation_platforms import catalog_document, get_platform
from .models import SensitiveRule, utcnow

router = APIRouter(prefix="/api/v1/automation", tags=["automation"], route_class=PrivateAccessRoute)
WRITE = [Depends(write_header)]


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    purpose: Literal["listing", "promotion", "creation"] | None = None
    operation: Literal["publish", "relist", "delist"] = "publish"
    target_url: str | None = Field(default=None, max_length=500)
    target_title: str | None = Field(default=None, max_length=120)
    source_mode: Literal["library", "custom"] = "library"
    copy_mode: Literal["ai", "manual"] = "ai"
    custom_product_name: str = Field(default="", max_length=120)
    custom_product_description: str = Field(default="", max_length=4000)
    product_id: str | None = Field(default=None, min_length=1, max_length=64)
    approved_content_id: str | None = Field(default=None, min_length=1, max_length=64)
    image_ids: list[str] = Field(default_factory=list, max_length=9)
    video_id: str | None = Field(default=None, min_length=1, max_length=64)
    platforms: list[str] = Field(default_factory=list, max_length=8)
    execution_mode: Literal["browser", "android"] = "browser"
    device_id: str | None = Field(default=None, min_length=1, max_length=64)
    price: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    condition: str = Field(default="全新", max_length=80)
    shipping_fee: Decimal = Field(default=Decimal("0"), ge=0, max_digits=8, decimal_places=2)
    location: str = Field(default="", max_length=200)
    instructions: str = Field(default="", max_length=4000)
    category: str | None = Field(default=None, max_length=120)
    auto_publish: StrictBool = True
    require_review: StrictBool = True
    image_mode: Literal["existing", "generate"] = "existing"
    generated_image_count: int = Field(default=3, ge=1, le=9)
    video_mode: Literal["existing", "slideshow"] = "slideshow"
    accounts: dict[str, str] = Field(default_factory=dict)

    @field_validator("name", "condition", "location", "instructions", "category", "target_url", "target_title", "custom_product_name", "custom_product_description")
    @classmethod
    def clean_text(cls, value):
        if value is None:
            return value
        value = value.strip()
        if any(ord(char) < 32 and char not in "\n\t" for char in value):
            raise ValueError("不能包含控制字符")
        return value

    @model_validator(mode="after")
    def valid_workflow(self):
        if not self.name:
            raise ValueError("工作流名称不能为空")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) for value in self.accounts.values()):
            raise ValueError("发布账号标识无效")
        if not set(self.accounts) <= set(self.platforms):
            raise ValueError("账号必须属于所选平台")
        normalized = business_config(self.model_dump(mode="json"), normalize_destination=True)
        self.purpose = normalized["purpose"]
        self.execution_mode = normalized["execution_mode"]
        self.device_id = normalized["device_id"]
        self.accounts = normalized.get("accounts", {})
        if self.purpose == "creation":
            self.require_review, self.auto_publish = True, False
            if self.approved_content_id or self.copy_mode != "ai":
                raise ValueError("产品创作需要根据原始商品资料生成内容")
        if self.approved_content_id:
            if self.operation != "publish":
                raise ValueError("已审核内容仅用于新发布")
            # The package is authoritative; discard unrelated source/generation inputs.
            self.source_mode, self.copy_mode = "library", "ai"
            self.product_id, self.image_ids, self.video_id = None, [], None
            self.image_mode, self.video_mode = "existing", "existing"
        if self.copy_mode == "manual" and (self.purpose != "listing" or self.operation != "publish" or self.source_mode != "custom"):
            raise ValueError("直接使用填写文案仅支持自定义商品新上架")
        if self.copy_mode == "manual" and (len(self.custom_product_name) > 30 or len(self.custom_product_description) > 1000):
            raise ValueError("直接上架的商品名称最多 30 字，描述最多 1000 字")
        if self.copy_mode == "manual" and (not self.category or len(self.category) > 64):
            raise ValueError("直接上架请填写闲鱼商品分类，最多 64 字")
        if self.purpose in {"promotion", "creation"}:
            self.price, self.condition, self.shipping_fee, self.location = None, "", Decimal("0"), ""
        if self.image_mode == "generate" and self.purpose not in {"promotion", "creation"}:
            raise ValueError("商品上架使用真实商品图片，AI 场景图用于内容宣传")
        if self.require_review and self.operation == "publish":
            self.auto_publish = False
        if len(self.image_ids) != len(set(self.image_ids)) or any(
            not 1 <= len(value) <= 64 for value in self.image_ids
        ):
            raise ValueError("图片标识无效或重复")
        if len(self.platforms) != len(set(self.platforms)):
            raise ValueError("发布平台不能重复")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) for value in self.accounts.values()):
            raise ValueError("发布账号标识无效")
        if not set(self.accounts) <= set(self.platforms):
            raise ValueError("账号必须属于所选平台")
        if self.operation in {"relist", "delist"}:
            if self.platforms != ["xianyu"]:
                raise ValueError("商品上下架仅支持闲鱼")
            if not self.target_title or "\n" in self.target_title or "\t" in self.target_title:
                raise ValueError("请输入与闲鱼商品页面完全一致的商品标题")
            if not self.target_url or not _valid_listing_url(self.target_url):
                raise ValueError("请输入 HTTPS 闲鱼商品链接，例如 https://www.goofish.com/item?id=123456")
            item_id = parse_qs(urlsplit(self.target_url).query)["id"][0]
            self.target_url = "https://www.goofish.com/item?id=" + item_id
            if not self.auto_publish:
                raise ValueError("商品上下架按规则直接执行，不支持生成草稿")
        else:
            if self.target_url or self.target_title:
                raise ValueError("新发布工作流不使用已有商品链接和标题")
            if self.source_mode == "custom":
                if not self.custom_product_name or not self.custom_product_description:
                    raise ValueError("自定义商品必须填写名称和真实商品描述")
                if not self.image_ids:
                    raise ValueError("自定义商品请至少选择一张资料库图片")
                # Keep the snapshot unambiguous when switching from a library product.
                self.product_id = None
            elif not self.approved_content_id and not self.product_id and not self.image_ids:
                raise ValueError("请选择商品或图片素材")
            if self.purpose == "listing" and (self.price is None or not self.location or not self.condition):
                raise ValueError("商品上架必须设置售价、成色和发货地")
            if (self.purpose == "creation" or any(get_platform(platform).requires_video for platform in self.platforms)) and self.video_mode == "existing" and not self.video_id and not self.approved_content_id:
                raise ValueError("请选择已有视频，或使用图片合成视频")
        return self


def _valid_listing_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        return bool(
            parsed.scheme == "https" and parsed.hostname in {"goofish.com", "www.goofish.com"}
            and parsed.username is None and parsed.password is None and parsed.port in {None, 443}
            and parsed.path == "/item" and not parsed.fragment
            and "id" in query and set(query) <= {"id", "categoryId", "spm"}
            and all(len(values) == 1 for values in query.values())
            and re.fullmatch(r"[0-9]{1,30}", query["id"][0])
            and ("categoryId" not in query or re.fullmatch(r"[0-9]{0,30}", query["categoryId"][0]))
            and ("spm" not in query or re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", query["spm"][0]))
            and not any(char.isspace() for char in value)
        )
    except (ValueError, TypeError):
        return False


class DraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=4000)
    category: str = Field(min_length=1, max_length=64)
    price: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    expected_revision: int | None = Field(default=None, ge=1)
    listing_title: str | None = Field(default=None, min_length=1, max_length=30)
    listing_description: str | None = Field(default=None, min_length=1, max_length=1000)

    @field_validator("title", "description", "category")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("内容不能为空")
        return value


def require_publisher(identity: Identity = Depends(get_identity)):
    if not has_permission(identity, "library.manage"):
        raise HTTPException(403, "自动化发布需要资料库管理权限")
    return identity


def owned(session, model, identifier, identity):
    row = session.scalar(select(model).where(model.id == identifier, model.owner_id == identity.user_id))
    if row is None:
        raise HTTPException(404, "工作流或执行记录不存在")
    return row


def execution_config(config_json):
    try:
        return business_config(json.loads(config_json))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


def config_purpose(config_json):
    try:
        return business_config(json.loads(config_json), validate_destination=False)["purpose"]
    except (ValueError, TypeError):
        return "legacy"


def purpose_page(session, model, query, purpose, limit):
    """Filter portable JSON snapshots before pagination without database-specific JSON SQL."""
    selected, total = [], 0
    # Only identifiers and config snapshots stream through the owner-scoped scan.
    for identifier, config_json in session.execute(query.with_only_columns(model.id, model.config_json)
        .execution_options(yield_per=100)):
        if purpose is None or config_purpose(config_json) == purpose:
            total += 1
            if len(selected) < limit:
                selected.append(identifier)
    rows = {row.id: row for row in session.scalars(select(model).where(model.id.in_(selected))).all()} if selected else {}
    return [rows[identifier] for identifier in selected], total


def workflow_document(row):
    return {"id": row.id, "name": row.name, "config": json.loads(row.config_json),
            "created_at": row.created_at, "updated_at": row.updated_at}


def run_document(session, row):
    payload = json.loads(row.payload_json)
    draft = {key: payload[key] for key in (
        "title", "description", "listing_title", "listing_description", "price", "category", "condition", "shipping_fee", "location", "model", "image_model",
    ) if key in payload}
    if payload:
        draft["classifications"] = [{key: value[key] for key in (
            "id", "role", "label", "reason", "confidence",
        ) if key in value} for value in payload.get("classifications", []) if isinstance(value, dict)]
        source = payload.get("source_summary", {})
        draft["source_summary"] = {key: source[key] for key in ("product_name", "image_count") if key in source}
        draft["has_video"] = bool(payload.get("video_path"))
        draft["image_count"] = len(payload.get("image_paths", []))
        draft["video_stale"] = bool(payload.get("video_stale"))
        draft["review"] = review.public_review(payload)
        draft["history"] = [{"review": review.public_review(value), "title": value.get("title", ""),
                            "description": value.get("description", "")} for value in payload.get("_history", [])]
        draft["library_ids"] = payload.get("_library_ids", [])
        draft["approved_content_id"] = payload.get("_package_id")
        draft["generation_error"] = payload.get("_generation_error")
    events = session.scalars(select(AutomationEvent).where(AutomationEvent.run_id == row.id)
        .order_by(AutomationEvent.created_at.desc(), AutomationEvent.id).limit(100)).all()
    return {
        "id": row.id, "workflow_id": row.workflow_id, "status": row.status,
        "config": json.loads(row.config_json), "draft": draft,
        "platform_results": json.loads(row.platform_results_json),
        "events": [{"id": value.id, "kind": value.kind, "message": value.message,
                    "platform": value.platform, "created_at": value.created_at} for value in reversed(events)],
        "error": row.error, "cancel_requested": row.cancel_requested, "attempt": row.attempt,
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


@router.get("/browser")
def browser_status(_: Identity = Depends(require_publisher)):
    from .automation_browser import status
    return status()


@router.get("/platforms")
def platform_catalog(_: Identity = Depends(require_publisher)):
    return {"items": catalog_document()}


@router.get("/executors")
def executor_status(identity: Identity = Depends(require_publisher)):
    from .automation_android import status as android_status
    from .automation_browser import status

    return {"browser": status(), "android": android_status(identity.user_id)}


@router.post("/android/inspect", dependencies=WRITE)
def inspect_android(identity: Identity = Depends(require_publisher)):
    from .automation_android import inspect_devices

    return inspect_devices(identity.user_id)


@router.get("/workflows")
def workflows(limit: int = Query(100, ge=1, le=100),
              purpose: Literal["listing", "promotion", "creation", "legacy"] | None = None,
              identity: Identity = Depends(get_identity),
              session: Session = Depends(db.get_session)):
    query = select(AutomationWorkflow).where(AutomationWorkflow.owner_id == identity.user_id)
    rows, total = purpose_page(session, AutomationWorkflow,
        query.order_by(AutomationWorkflow.updated_at.desc(), AutomationWorkflow.id), purpose, limit)
    return {"items": [workflow_document(row) for row in rows], "total": total}


@router.get("/approved-contents")
def approved_contents(identity: Identity = Depends(get_identity), session: Session = Depends(db.get_session)):
    from .content_packages import list_packages
    items = list_packages(session, identity.user_id)
    return {"items": items, "total": len(items)}


@router.get("/approved-contents/{identifier}")
def approved_content(identifier: str, identity: Identity = Depends(get_identity), session: Session = Depends(db.get_session)):
    from .content_packages import document, package_entry
    return document(*package_entry(session, identifier, identity.user_id))


@router.post("/workflows", status_code=201, dependencies=WRITE)
def create_workflow(body: WorkflowConfig, identity: Identity = Depends(require_publisher),
                    session: Session = Depends(db.get_session)):
    count = session.scalar(select(func.count()).select_from(AutomationWorkflow)
        .where(AutomationWorkflow.owner_id == identity.user_id))
    if count >= 200:
        raise HTTPException(422, "最多保存 200 个工作流")
    row = AutomationWorkflow(owner_id=identity.user_id, name=body.name,
                             config_json=encoded(body.model_dump(mode="json")))
    session.add(row)
    session.flush()
    audit(session, identity.user_id, "workflow_create", row.id)
    session.commit()
    return workflow_document(row)


@router.get("/workflows/{workflow_id}")
def workflow(workflow_id: str, identity: Identity = Depends(get_identity), session: Session = Depends(db.get_session)):
    return workflow_document(owned(session, AutomationWorkflow, workflow_id, identity))


@router.put("/workflows/{workflow_id}", dependencies=WRITE)
def update_workflow(workflow_id: str, body: WorkflowConfig, identity: Identity = Depends(require_publisher),
                    session: Session = Depends(db.get_session)):
    row = owned(session, AutomationWorkflow, workflow_id, identity)
    previous_purpose = config_purpose(row.config_json)
    if previous_purpose != body.purpose:
        raise HTTPException(409, "商品上架与视频宣传使用独立工作流，请新建对应业务的工作流")
    row.name, row.config_json = body.name, encoded(body.model_dump(mode="json"))
    audit(session, identity.user_id, "workflow_update", row.id)
    session.commit()
    return workflow_document(row)


def check_capacity(session, identity):
    count = session.scalar(select(func.count()).select_from(AutomationRun).where(
        AutomationRun.owner_id == identity.user_id, AutomationRun.status.in_(ACTIVE),
    ))
    if count >= 20:
        raise HTTPException(429, "正在执行的工作流过多，请稍后再试")


@router.post("/workflows/{workflow_id}/runs", status_code=202, dependencies=WRITE)
def create_run(workflow_id: str, background_tasks: BackgroundTasks,
               idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128,
                                             pattern=r"^[A-Za-z0-9._:-]+$"),
               identity: Identity = Depends(require_publisher), session: Session = Depends(db.get_session)):
    workflow = owned(session, AutomationWorkflow, workflow_id, identity)
    config = execution_config(workflow.config_json)
    previous = session.scalar(select(AutomationRun).where(
        AutomationRun.owner_id == identity.user_id, AutomationRun.idempotency_key == idempotency_key,
    ))
    if previous:
        if previous.workflow_id != workflow_id:
            raise HTTPException(409, "该执行请求标识已用于其他工作流")
        return run_document(session, previous)
    check_capacity(session, identity)
    row = AutomationRun(
        workflow_id=workflow.id, owner_id=identity.user_id, idempotency_key=idempotency_key,
        config_json=encoded(config),
        platform_results_json=encoded({platform: {"status": "pending", "operation": config.get("operation", "publish")} for platform in config["platforms"]}),
        lease_expires_at=utcnow() + timedelta(seconds=LEASE_SECONDS),
    )
    session.add(row)
    try:
        session.flush()
        event(session, row, "queued", "工作流已加入执行队列")
        audit(session, identity.user_id, "run_create", row.id)
        session.commit()
    except IntegrityError:
        session.rollback()
        previous = session.scalar(select(AutomationRun).where(
            AutomationRun.owner_id == identity.user_id, AutomationRun.idempotency_key == idempotency_key,
        ))
        if previous is None or previous.workflow_id != workflow_id:
            raise HTTPException(409, "该执行请求标识已被使用") from None
        return run_document(session, previous)
    document = run_document(session, row)
    background_tasks.add_task(execute_run, row.id, identity)
    return document


class CreationBatchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[WorkflowConfig] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def independent_products(self):
        if any(item.purpose != 'creation' for item in self.items):
            raise ValueError('批量创作只能包含独立产品创作任务')
        identifiers = [item.product_id for item in self.items if item.product_id]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError('同一批次不能重复选择产品')
        return self


@router.post('/creation-batches', status_code=202, dependencies=WRITE)
def create_creation_batch(body: CreationBatchRequest, background_tasks: BackgroundTasks,
                          idempotency_key: str = Header(alias='Idempotency-Key', min_length=1, max_length=128,
                                                        pattern=r'^[A-Za-z0-9._:-]+$'),
                          identity: Identity = Depends(require_publisher), session: Session = Depends(db.get_session)):
    prefix = 'creation:' + hashlib.sha256(idempotency_key.encode()).hexdigest() + ':'
    configs = [encoded(item.model_dump(mode='json')) for item in body.items]

    def previous_batch():
        previous = session.scalars(select(AutomationRun).where(AutomationRun.owner_id == identity.user_id,
            AutomationRun.idempotency_key.like(prefix + '%'))).all()
        if not previous:
            return None
        by_key = {row.idempotency_key: row for row in previous}
        if len(previous) != len(configs) or any(prefix + str(index) not in by_key
            or by_key[prefix + str(index)].config_json != config for index, config in enumerate(configs)):
            raise HTTPException(409, '该批次请求标识已用于不同的产品或创作要求')
        return [by_key[prefix + str(index)] for index in range(len(configs))]

    previous = previous_batch()
    if previous is not None:
        return {'items': [run_document(session, row) for row in previous], 'total': len(previous)}
    active = session.scalar(select(func.count()).select_from(AutomationRun).where(
        AutomationRun.owner_id == identity.user_id, AutomationRun.status.in_(ACTIVE)))
    if active + len(configs) > 20:
        raise HTTPException(429, f'当前还有 {active} 个任务正在处理，本批最多可创建 {20 - active} 个产品任务')
    count = session.scalar(select(func.count()).select_from(AutomationWorkflow).where(AutomationWorkflow.owner_id == identity.user_id))
    if count + len(configs) > 200:
        raise HTTPException(422, '保存的工作流已达到容量上限')
    rows = []
    try:
        for index, (item, config) in enumerate(zip(body.items, configs, strict=True)):
            workflow = AutomationWorkflow(owner_id=identity.user_id, name=item.name, config_json=config)
            session.add(workflow)
            session.flush()
            row = AutomationRun(workflow_id=workflow.id, owner_id=identity.user_id, config_json=config,
                idempotency_key=prefix + str(index), platform_results_json='{}',
                lease_expires_at=utcnow() + timedelta(seconds=LEASE_SECONDS))
            session.add(row)
            session.flush()
            event(session, row, 'queued', f'批量创作：第 {index + 1}/{len(configs)} 个产品，独立生成并审核')
            audit(session, identity.user_id, 'creation_batch', row.id)
            rows.append(row)
        session.commit()
    except IntegrityError:
        session.rollback()
        previous = previous_batch()
        if previous is None:
            raise HTTPException(409, '创建批次发生冲突，请重试同一请求') from None
        return {'items': [run_document(session, row) for row in previous], 'total': len(previous)}
    result = {'items': [run_document(session, row) for row in rows], 'total': len(rows)}
    background_tasks.add_task(execute_creation_batch, [row.id for row in rows], identity)
    return result


@router.get("/runs")
def runs(workflow_id: str | None = None, limit: int = Query(50, ge=1, le=100),
         purpose: Literal["listing", "promotion", "creation", "legacy"] | None = None,
         identity: Identity = Depends(get_identity), session: Session = Depends(db.get_session)):
    recover_interrupted()
    query = select(AutomationRun).where(AutomationRun.owner_id == identity.user_id)
    if workflow_id:
        owned(session, AutomationWorkflow, workflow_id, identity)
        query = query.where(AutomationRun.workflow_id == workflow_id)
    rows, total = purpose_page(session, AutomationRun,
        query.order_by(AutomationRun.created_at.desc(), AutomationRun.id), purpose, limit)
    return {"items": [run_document(session, row) for row in rows], "total": total}


@router.get("/runs/{run_id}")
def run(run_id: str, identity: Identity = Depends(get_identity), session: Session = Depends(db.get_session)):
    recover_interrupted()
    return run_document(session, owned(session, AutomationRun, run_id, identity))


@router.get("/runs/{run_id}/video")
def video(run_id: str, identity: Identity = Depends(get_identity), session: Session = Depends(db.get_session)):
    from .provider_dependencies import get_runtime_settings

    row = owned(session, AutomationRun, run_id, identity)
    value = json.loads(row.payload_json).get("video_path")
    if not value:
        raise HTTPException(404, "执行记录没有视频")
    root = (get_runtime_settings().data_dir / "automation-media").resolve()
    path = Path(value).resolve()
    media_types = {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v"}
    if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in media_types:
        raise HTTPException(404, "视频不可用")
    return FileResponse(path, media_type=media_types[path.suffix.lower()], filename="automation-video" + path.suffix.lower())


@router.put("/runs/{run_id}/draft", dependencies=WRITE)
def edit_draft(run_id: str, body: DraftUpdate, identity: Identity = Depends(require_publisher),
               session: Session = Depends(db.get_session)):
    row = owned(session, AutomationRun, run_id, identity)
    if row.status != "ready":
        raise HTTPException(409, "仅待发布草稿可以修改")
    config, payload = json.loads(row.config_json), json.loads(row.payload_json)
    config = execution_config(row.config_json)
    if review.required(config) or body.expected_revision is not None:
        review.check_version(payload, body.expected_revision)
    if config.get("operation", "publish") != "publish":
        raise HTTPException(409, "商品上下架操作不支持编辑发布草稿")
    if config["purpose"] == "listing" and body.price is None:
        raise HTTPException(422, "商品上架必须填写售价")
    specs = [get_platform(platform) for platform in config["platforms"]]
    title_limit = min((spec.title_limit for spec in specs), default=20)
    description_limit = min((spec.description_limit for spec in specs), default=900)
    if len(body.title) > title_limit or len(body.description) > description_limit:
        raise HTTPException(422, f"所选平台要求标题不超过 {title_limit} 字，文案不超过 {description_limit} 字")
    text = (body.title + "\n" + body.description + "\n" + (body.listing_title or "") + "\n" + (body.listing_description or "")).casefold()
    rules = session.scalars(select(SensitiveRule.phrase).where(SensitiveRule.active.is_(True)))
    if any(phrase and phrase.casefold() in text for phrase in rules):
        raise HTTPException(422, "发布文案命中了现有合规规则，请调整后重试")
    previous = dict(payload)
    changes = body.model_dump(mode="json", exclude={"expected_revision"}, exclude_unset=True)
    if config["purpose"] != "creation":
        changes.pop("listing_title", None)
        changes.pop("listing_description", None)
    elif any(key in changes and not str(changes[key] or "").strip() for key in ("listing_title", "listing_description")):
        raise HTTPException(422, "商品描述和标题不能为空")
    if config["purpose"] in {"promotion", "creation"}:
        changes.pop("price", None)
    if all(payload.get(key) == value for key, value in changes.items()):
        return run_document(session, row)
    payload.update(changes)
    if payload.get("video_path") and config.get("video_mode") == "slideshow" and any(
        previous.get(key) != payload.get(key) for key in ("title", "description")
    ):
        payload["video_stale"] = True
    review.new_version(payload, previous, "人工编辑文案")
    if config["purpose"] in {"promotion", "creation"}:
        for field in ("price", "condition", "shipping_fee", "location"):
            payload.pop(field, None)
    changed = session.execute(update(AutomationRun).where(AutomationRun.id == row.id, AutomationRun.status == "ready",
        AutomationRun.payload_json == row.payload_json)
        .values(payload_json=encoded(payload), publish_requested=False, updated_at=utcnow()))
    if not changed.rowcount:
        raise HTTPException(409, "执行状态已变化，请刷新")
    event(session, row, "draft_edited", "已保存新版本，等待人工审核")
    audit(session, identity.user_id, "draft_update", row.id)
    session.commit()
    session.refresh(row)
    return run_document(session, row)


def requeue(session, row, identity, background_tasks, *, publish=False):
    config = execution_config(row.config_json)
    check_capacity(session, identity)
    old_status = row.status
    if publish:
        if config["purpose"] == "creation":
            raise HTTPException(409, "请先审核入库，再从内容库选择宣传或商品上架")
        if json.loads(row.config_json).get("operation", "publish") != "publish":
            raise HTTPException(409, "商品上下架操作不支持手动发布草稿")
        if old_status != "ready":
            raise HTTPException(409, "只有待发布内容可以确认发布")
        review.ensure_approved(json.loads(row.payload_json), config)
    else:
        results = json.loads(row.platform_results_json)
        if old_status not in {"failed", "partial"} or any(
            value.get("status") in {"unknown", "submitting"} for value in results.values()
        ):
            raise HTTPException(409, "当前记录不能安全重试；结果不明时请先到平台核查")
    changed = session.execute(update(AutomationRun).where(AutomationRun.id == row.id, AutomationRun.status == old_status,
        AutomationRun.payload_json == row.payload_json)
        .values(status="queued", config_json=encoded(config), cancel_requested=False, error="", attempt=row.attempt + 1,
                publish_requested=True if publish else row.publish_requested,
                lease_token=None, lease_expires_at=utcnow() + timedelta(seconds=LEASE_SECONDS), updated_at=utcnow()))
    if not changed.rowcount:
        raise HTTPException(409, "执行状态已变化，请刷新")
    action = "publish_requested" if publish else "retry_requested"
    event(session, row, action, "已安排发布" if publish else "仅重试尚未成功且结果明确的步骤")
    audit(session, identity.user_id, action, row.id)
    session.commit()
    session.refresh(row)
    document = run_document(session, row)
    background_tasks.add_task(execute_run, row.id, identity)
    return document


@router.post("/runs/{run_id}/retry", status_code=202, dependencies=WRITE)
def retry(run_id: str, background_tasks: BackgroundTasks, identity: Identity = Depends(require_publisher),
          session: Session = Depends(db.get_session)):
    return requeue(session, owned(session, AutomationRun, run_id, identity), identity, background_tasks)


@router.post("/runs/{run_id}/publish", status_code=202, dependencies=WRITE)
def publish(run_id: str, background_tasks: BackgroundTasks, identity: Identity = Depends(require_publisher),
            session: Session = Depends(db.get_session)):
    return requeue(session, owned(session, AutomationRun, run_id, identity), identity, background_tasks, publish=True)


@router.post("/runs/{run_id}/cancel", dependencies=WRITE)
def cancel(run_id: str, identity: Identity = Depends(require_publisher), session: Session = Depends(db.get_session)):
    row = owned(session, AutomationRun, run_id, identity)
    if row.status not in {*ACTIVE, "ready"}:
        raise HTTPException(409, "该执行已结束")
    values = {"cancel_requested": True, "updated_at": utcnow()}
    if row.status in {"queued", "ready"}:
        values.update(status="cancelled", lease_token=None, lease_expires_at=None)
    changed = session.execute(update(AutomationRun).where(AutomationRun.id == row.id, AutomationRun.status == row.status).values(**values))
    if not changed.rowcount:
        raise HTTPException(409, "执行状态已变化，请刷新")
    event(session, row, "cancel_requested", "已请求停止；已经提交的平台将继续核对结果")
    audit(session, identity.user_id, "cancel_requested", row.id)
    session.commit()
    session.refresh(row)
    return run_document(session, row)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class RevisionRequest(ReviewRequest):
    feedback: str = Field(min_length=1, max_length=2000)
    scope: Literal["copy", "images", "video", "all", "originals"] = "copy"


@router.post("/runs/{run_id}/approve", dependencies=WRITE)
def approve_draft(run_id: str, body: ReviewRequest, identity: Identity = Depends(require_publisher),
                  session: Session = Depends(db.get_session)):
    row = owned(session, AutomationRun, run_id, identity)
    if row.status != "ready":
        raise HTTPException(409, "仅待审核草稿可以审核")
    config = execution_config(row.config_json)
    existing = json.loads(row.payload_json)
    if config["purpose"] == "creation" and existing.get("_package_id") and existing.get("_review", {}).get("state") == "approved":
        review.check_version(existing, body.expected_revision)
        review.ensure_approved(existing, config)
        return run_document(session, row)
    payload = review.approve(json.loads(row.payload_json), config, identity.user_id, body.expected_revision)
    changed = session.execute(update(AutomationRun).where(AutomationRun.id == row.id,
        AutomationRun.status == "ready", AutomationRun.payload_json == row.payload_json)
        .values(payload_json=encoded(payload), updated_at=utcnow()))
    if changed.rowcount != 1:
        raise HTTPException(409, "草稿已变化，请刷新")
    if config["purpose"] == "creation":
        from .content_packages import archive_creation
        archive_creation(session, row, payload)
        row.payload_json = encoded(payload)
    event(session, row, "approved", f"第 {body.expected_revision} 版已审核入库" if config["purpose"] == "creation" else f"人工审核通过第 {body.expected_revision} 版，可发布到所选平台")
    audit(session, identity.user_id, "approved", row.id, {"revision": body.expected_revision})
    session.commit()
    session.refresh(row)
    return run_document(session, row)


@router.post("/runs/{run_id}/revise", status_code=202, dependencies=WRITE)
def revise_draft(run_id: str, body: RevisionRequest, background_tasks: BackgroundTasks,
                 identity: Identity = Depends(require_publisher), session: Session = Depends(db.get_session)):
    row = owned(session, AutomationRun, run_id, identity)
    if row.status != "ready" or not body.feedback.strip():
        raise HTTPException(409, "请在草稿准备完成后填写修改意见")
    config = execution_config(row.config_json)
    results = json.loads(row.platform_results_json)
    if body.scope == 'originals' and config['purpose'] != 'creation':
        raise HTTPException(422, '仅产品创作支持切换商品原图')
    if any(value.get("status") in {"succeeded", "submitting", "unknown"} for value in results.values()):
        raise HTTPException(409, "已提交平台的内容不可覆盖，请新建工作流")
    if body.scope == "images" and config["purpose"] not in {"promotion", "creation"}:
        raise HTTPException(422, "商品上架使用真实图片")
    payload = json.loads(row.payload_json)
    review.check_version(payload, body.expected_revision)
    if len(payload.get("_history", [])) >= 20:
        raise HTTPException(422, "本次运行已达到 20 个修改版本，请新建运行")
    check_capacity(session, identity)
    payload["_revision_request"] = {"feedback": body.feedback.strip(), "scope": body.scope}
    changed = session.execute(update(AutomationRun).where(AutomationRun.id == row.id,
        AutomationRun.status == "ready", AutomationRun.payload_json == row.payload_json)
        .values(status="queued", payload_json=encoded(payload), publish_requested=False,
                error="", lease_token=None, lease_expires_at=utcnow() + timedelta(seconds=LEASE_SECONDS), updated_at=utcnow()))
    if changed.rowcount != 1:
        raise HTTPException(409, "草稿已变化，请刷新")
    event(session, row, "revision_requested", "已收到修改意见，修改完成后需重新人工审核")
    session.commit()
    session.refresh(row)
    background_tasks.add_task(execute_run, row.id, identity)
    return run_document(session, row)


@router.get("/runs/{run_id}/images/{index}")
def draft_image(run_id: str, index: int, identity: Identity = Depends(get_identity), session: Session = Depends(db.get_session)):
    row = owned(session, AutomationRun, run_id, identity)
    paths = json.loads(row.payload_json).get("image_paths", [])
    if index < 0 or index >= len(paths):
        raise HTTPException(404, "图片不存在")
    path = review.media_path(paths[index])
    return FileResponse(path, media_type="image/jpeg", filename=f"draft-{index + 1}.jpg")


@router.post("/runs/{run_id}/archive", dependencies=WRITE)
def archive_draft(run_id: str, body: ReviewRequest, identity: Identity = Depends(require_publisher),
                  session: Session = Depends(db.get_session)):
    row = owned(session, AutomationRun, run_id, identity)
    if row.status not in {"ready", "succeeded", "partial"}:
        raise HTTPException(409, "请在草稿或发布完成后保存素材")
    payload = json.loads(row.payload_json)
    config = execution_config(row.config_json)
    review.check_version(payload, body.expected_revision)
    review.ensure_approved(payload, config, force=True)
    changed = session.execute(update(AutomationRun).where(AutomationRun.id == row.id,
        AutomationRun.status == row.status, AutomationRun.payload_json == row.payload_json)
        .values(updated_at=utcnow()))
    if changed.rowcount != 1:
        raise HTTPException(409, "草稿已变化，请刷新")
    review.archive_to_library(session, row, payload)
    row.payload_json = encoded(payload)
    event(session, row, "archived", "审核通过的文案、图片和视频已保存到资料库，可再次选择使用")
    session.commit()
    return run_document(session, row)
