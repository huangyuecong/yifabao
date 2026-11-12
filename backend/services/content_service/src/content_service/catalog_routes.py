"""Product catalog API: products, batch generation, review, approved contents, publishing."""

import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from agent_core import Identity

from . import catalog_generation, catalog_publishing, db
from .access_control import get_content_identity as get_identity
from .access_routes import PrivateAccessRoute
from .ai_model_config import write_header
from .automation_platforms import PLATFORMS
from .automation_routes import require_publisher
from .catalog_media import (
    IMAGE_TYPES,
    MAX_UPLOAD_BYTES,
    absolute,
    media_type,
    product_image_path,
    save_jpeg,
)
from .catalog_models import (
    ContentMedia,
    GenerationBatch,
    GenerationItem,
    GenerationMedia,
    Product,
    ProductContent,
    ProductImage,
    PublishRecord,
)
from .models import AuditLog, SensitiveRule, new_id, utcnow
from .provider_dependencies import get_asset_store_port, get_runtime_settings
from .xianyu_web import CATEGORY as XIANYU_CATEGORY

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"], route_class=PrivateAccessRoute)
WRITE = [Depends(write_header)]
PROMOTION_PLATFORMS = ("douyin", "xiaohongshu", "kuaishou")
REVIEWABLE = ("review", "rejected", "failed")


# ---------- helpers ----------

def to_cents(value: Decimal | None) -> int | None:
    if value is None:
        return None
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(cents: int | None) -> str | None:
    return None if cents is None else f"{cents / 100:.2f}"


def audit(session, identity: Identity, action: str, resource_id: str, details: dict | None = None):
    session.add(AuditLog(owner_id=identity.user_id, action="catalog." + action, resource_type="catalog",
                         resource_id=resource_id, details_json=json.dumps(details or {}, ensure_ascii=False)))


def owned(session, model, identifier: str, identity: Identity):
    row = session.get(model, identifier)
    if row is None or row.owner_id != identity.user_id:
        raise HTTPException(404, "记录不存在")
    return row


def owned_product(session, identifier: str, identity: Identity) -> Product:
    product = owned(session, Product, identifier, identity)
    if not product.active:
        raise HTTPException(404, "商品已删除")
    return product


def clean(value: str) -> str:
    value = value.strip()
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError("不能包含控制字符")
    return value


def check_rules(session, *texts: str):
    text = "\n".join(texts).casefold()
    for phrase in session.scalars(select(SensitiveRule.phrase).where(SensitiveRule.active.is_(True))):
        if phrase and phrase.casefold() in text:
            raise HTTPException(422, f"文案命中合规规则「{phrase}」，请修改后再保存")


def product_images(session, product_ids: list[str]) -> dict[str, list[ProductImage]]:
    result: dict[str, list[ProductImage]] = {identifier: [] for identifier in product_ids}
    if product_ids:
        for row in session.scalars(select(ProductImage).where(ProductImage.product_id.in_(product_ids))
                                   .order_by(ProductImage.sort_order, ProductImage.created_at)):
            result[row.product_id].append(row)
    return result


def product_document(product: Product, images: list[ProductImage], stats: dict | None = None) -> dict:
    return {
        "id": product.id, "name": product.name, "sku": product.sku, "category": product.category,
        "price": money(product.price_cents), "description": product.description,
        "specs": json.loads(product.specs_json or "[]"),
        "images": [{"id": image.id} for image in images],
        "content_count": (stats or {}).get("contents", 0),
        "draft_status": (stats or {}).get("draft_status"),
        "created_at": product.created_at, "updated_at": product.updated_at,
    }


def product_summary(product: Product, images: list[ProductImage]) -> dict:
    return {"id": product.id, "name": product.name, "price": money(product.price_cents),
            "category": product.category, "image_id": images[0].id if images else None}


def media_documents(rows) -> list[dict]:
    return [{"id": row.id, "kind": row.kind} for row in rows]


def item_document(session, item: GenerationItem, product: Product, images: list[ProductImage], batch=None) -> dict:
    media = session.scalars(select(GenerationMedia).where(GenerationMedia.item_id == item.id)
                            .order_by(GenerationMedia.kind, GenerationMedia.sort_order)).all()
    batch = batch or session.get(GenerationBatch, item.batch_id)
    return {
        "id": item.id, "batch_id": item.batch_id, "batch_name": batch.name if batch else "",
        "status": item.status, "revision": item.revision,
        "title": item.title, "body": item.body, "listing_title": item.listing_title,
        "listing_description": item.listing_description, "category": item.category,
        "video_stale": item.video_stale, "video_note": item.video_note, "video_model": item.video_model, "pending_parts": [part for part in item.pending_parts.split(",") if part],
        "feedback": item.feedback, "review_note": item.review_note, "error": item.error,
        "model": item.model, "image_model": item.image_model, "content_id": item.content_id,
        "options": {"copy": batch.with_copy, "images": batch.with_images, "video": batch.with_video,
                    "voice": batch.video_voice, "music": batch.video_music,
                    "ai_video": batch.video_mode == "ai"} if batch else {},
        "media": media_documents(media), "product": product_summary(product, images),
        "reviewed_at": item.reviewed_at, "created_at": item.created_at, "updated_at": item.updated_at,
    }


def publish_summary(session, content_ids: list[str]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {identifier: [] for identifier in content_ids}
    if content_ids:
        rows = session.scalars(select(PublishRecord).where(PublishRecord.content_id.in_(content_ids))
                               .order_by(PublishRecord.created_at.desc())).all()
        seen = set()
        for row in rows:
            key = (row.content_id, row.platform)
            if key not in seen:
                seen.add(key)
                result[row.content_id].append({"platform": row.platform, "status": row.status,
                                               "workflow": row.workflow, "url": row.external_url})
    return result


def content_document(session, content: ProductContent, product: Product, images, publishes=None) -> dict:
    media = session.scalars(select(ContentMedia).where(ContentMedia.content_id == content.id)
                            .order_by(ContentMedia.kind, ContentMedia.sort_order)).all()
    return {
        "id": content.id, "item_id": content.item_id, "title": content.title, "body": content.body,
        "listing_title": content.listing_title, "listing_description": content.listing_description,
        "category": content.category, "approved_at": content.approved_at, "approved_by": content.approved_by,
        "media": media_documents(media), "has_video": any(row.kind == "video" for row in media),
        "product": product_summary(product, images), "publishes": publishes or [],
    }


def record_document(row: PublishRecord, product: Product | None) -> dict:
    return {
        "id": row.id, "workflow": row.workflow, "platform": row.platform, "status": row.status,
        "content_id": row.content_id, "product_id": row.product_id,
        "product_name": product.name if product else "", "title": row.title,
        "price": money(row.price_cents), "shipping_fee": money(row.shipping_fee_cents),
        "condition": row.condition, "location": row.location, "category": row.category,
        "account_label": row.account_label, "external_url": row.external_url, "external_id": row.external_id,
        "error": row.error, "attempt": row.attempt, "created_at": row.created_at,
        "started_at": row.started_at, "finished_at": row.finished_at,
    }


# ---------- products ----------

class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=200)

    @field_validator("name", "value")
    @classmethod
    def strip(cls, value):
        return clean(value)


class ProductInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    sku: str = Field(default="", max_length=64)
    category: str = Field(default="", max_length=64)
    price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    description: str = Field(default="", max_length=4000)
    specs: list[Spec] = Field(default_factory=list, max_length=30)

    @field_validator("name", "sku", "category", "description")
    @classmethod
    def strip(cls, value):
        return clean(value)

    @field_validator("name")
    @classmethod
    def required_name(cls, value):
        if not value:
            raise ValueError("商品名称不能为空")
        return value


@router.get("/products")
def list_products(q: str = Query("", max_length=120), page: int = Query(1, ge=1),
                  page_size: int = Query(20, ge=1, le=200), identity: Identity = Depends(get_identity),
                  session: Session = Depends(db.get_session)):
    query = select(Product).where(Product.owner_id == identity.user_id, Product.active.is_(True))
    if q.strip():
        pattern = f"%{q.strip()}%"
        query = query.where(or_(Product.name.like(pattern), Product.sku.like(pattern), Product.category.like(pattern)))
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(query.order_by(Product.updated_at.desc(), Product.id)
                           .offset((page - 1) * page_size).limit(page_size)).all()
    ids = [row.id for row in rows]
    images = product_images(session, ids)
    contents = dict(session.execute(select(ProductContent.product_id, func.count()).where(
        ProductContent.product_id.in_(ids), ProductContent.active.is_(True)).group_by(ProductContent.product_id)).all()) if ids else {}
    drafts: dict[str, str] = {}
    if ids:
        for product_id, status in session.execute(select(GenerationItem.product_id, GenerationItem.status).where(
                GenerationItem.product_id.in_(ids)).order_by(GenerationItem.created_at.desc())):
            drafts.setdefault(product_id, status)
    return {"items": [product_document(row, images[row.id], {"contents": contents.get(row.id, 0),
                                                              "draft_status": drafts.get(row.id)}) for row in rows],
            "total": total}


@router.get("/products/{product_id}")
def get_product(product_id: str, identity: Identity = Depends(get_identity), session: Session = Depends(db.get_session)):
    product = owned_product(session, product_id, identity)
    return product_document(product, product_images(session, [product.id])[product.id])


def _apply_product(product: Product, body: ProductInput):
    product.name, product.sku, product.category = body.name, body.sku, body.category
    product.price_cents, product.description = to_cents(body.price), body.description
    product.specs_json = json.dumps([spec.model_dump() for spec in body.specs], ensure_ascii=False)


@router.post("/products", status_code=201, dependencies=WRITE)
def create_product(body: ProductInput, identity: Identity = Depends(require_publisher),
                   session: Session = Depends(db.get_session)):
    count = session.scalar(select(func.count()).select_from(Product).where(
        Product.owner_id == identity.user_id, Product.active.is_(True)))
    if count >= 5000:
        raise HTTPException(422, "商品数量已达上限")
    product = Product(owner_id=identity.user_id)
    _apply_product(product, body)
    session.add(product)
    session.flush()
    audit(session, identity, "product_create", product.id)
    session.commit()
    return product_document(product, [])


@router.put("/products/{product_id}", dependencies=WRITE)
def update_product(product_id: str, body: ProductInput, identity: Identity = Depends(require_publisher),
                   session: Session = Depends(db.get_session)):
    product = owned_product(session, product_id, identity)
    _apply_product(product, body)
    product.updated_at = utcnow()
    audit(session, identity, "product_update", product.id)
    session.commit()
    return product_document(product, product_images(session, [product.id])[product.id])


@router.delete("/products/{product_id}", status_code=204, dependencies=WRITE)
def delete_product(product_id: str, identity: Identity = Depends(require_publisher),
                   session: Session = Depends(db.get_session)):
    product = owned_product(session, product_id, identity)
    product.active, product.updated_at = False, utcnow()
    audit(session, identity, "product_delete", product.id)
    session.commit()


@router.post("/products/{product_id}/images", dependencies=WRITE)
def upload_images(product_id: str, files: list[UploadFile] = File(...),
                  identity: Identity = Depends(require_publisher), session: Session = Depends(db.get_session)):
    product = owned_product(session, product_id, identity)
    existing = product_images(session, [product.id])[product.id]
    if len(existing) + len(files) > 9:
        raise HTTPException(422, "每个商品最多 9 张图片")
    store = get_asset_store_port()
    order = max((image.sort_order for image in existing), default=-1)
    for upload in files:
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in IMAGE_TYPES:
            raise HTTPException(422, "仅支持 JPG、PNG、WEBP 图片")
        content = upload.file.read(MAX_UPLOAD_BYTES + 1)
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(422, "单张图片不能超过 20 MB")
        image_id, order = new_id(), order + 1
        key = f"catalog/products/{product.id}/{image_id}.jpg"
        save_jpeg(content, store.path_for(key))
        session.add(ProductImage(id=image_id, product_id=product.id, storage_key=key,
                                 media_type="image/jpeg", sort_order=order))
    product.updated_at = utcnow()
    audit(session, identity, "product_images", product.id, {"count": len(files)})
    session.commit()
    return product_document(product, product_images(session, [product.id])[product.id])


@router.delete("/products/{product_id}/images/{image_id}", dependencies=WRITE)
def delete_image(product_id: str, image_id: str, identity: Identity = Depends(require_publisher),
                 session: Session = Depends(db.get_session)):
    product = owned_product(session, product_id, identity)
    image = session.get(ProductImage, image_id)
    if image is None or image.product_id != product.id:
        raise HTTPException(404, "图片不存在")
    session.delete(image)
    product.updated_at = utcnow()
    session.commit()
    return product_document(product, product_images(session, [product.id])[product.id])


class ImageOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=9)


@router.put("/products/{product_id}/images/order", dependencies=WRITE)
def order_images(product_id: str, body: ImageOrder, identity: Identity = Depends(require_publisher),
                 session: Session = Depends(db.get_session)):
    product = owned_product(session, product_id, identity)
    images = {image.id: image for image in product_images(session, [product.id])[product.id]}
    if set(body.ids) != set(images) or len(body.ids) != len(images):
        raise HTTPException(422, "图片顺序与当前图片不一致，请刷新")
    for index, identifier in enumerate(body.ids):
        images[identifier].sort_order = index
    session.commit()
    return product_document(product, product_images(session, [product.id])[product.id])


def _thumbnail(source: Path, cache_key: str) -> Path:
    directory = get_runtime_settings().data_dir / "catalog-thumbs"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{cache_key}.jpg"
    if not target.is_file() or target.stat().st_mtime < source.stat().st_mtime:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((480, 480))
            image.save(target, format="JPEG", quality=84)
    return target


@router.get("/products/{product_id}/images/{image_id}")
def product_image(product_id: str, image_id: str, size: Literal["full", "thumb"] = "full",
                  identity: Identity = Depends(get_identity), session: Session = Depends(db.get_session)):
    product = owned(session, Product, product_id, identity)
    image = session.get(ProductImage, image_id)
    if image is None or image.product_id != product.id:
        raise HTTPException(404, "图片不存在")
    path = product_image_path(image.storage_key)
    if size == "thumb":
        return FileResponse(_thumbnail(path, image.id), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=3600"})
    return FileResponse(path, media_type=media_type(path), headers={"Cache-Control": "private, max-age=3600"})


# ---------- generation batches & review ----------

class BatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_ids: list[str] = Field(min_length=1, max_length=100)
    with_copy: bool = True
    with_images: bool = False
    with_video: bool = False
    image_count: int = Field(default=3, ge=1, le=6)
    instructions: str = Field(default="", max_length=2000)
    name: str = Field(default="", max_length=120)
    image_model: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9._:/-]*$")
    video_voice: bool = True
    video_music: bool = False
    video_mode: Literal["slideshow", "ai"] = "slideshow"
    ai_clip_count: int = Field(default=3, ge=2, le=3)


@router.post("/batches", status_code=201, dependencies=WRITE)
def create_batch(body: BatchInput, identity: Identity = Depends(require_publisher),
                 session: Session = Depends(db.get_session)):
    if not (body.with_copy or body.with_images or body.with_video):
        raise HTTPException(422, "请至少选择一项生成内容")
    ids = list(dict.fromkeys(body.product_ids))
    products = session.scalars(select(Product).where(Product.id.in_(ids), Product.owner_id == identity.user_id,
                                                      Product.active.is_(True))).all()
    if len(products) != len(ids):
        raise HTTPException(422, "部分商品不存在或已删除，请刷新后重试")
    images = product_images(session, ids)
    missing = [product.name for product in products if not images[product.id]]
    if missing:
        raise HTTPException(422, "以下商品还没有图片：" + "、".join(missing[:5]) + ("等" if len(missing) > 5 else ""))
    active = session.scalar(select(func.count()).select_from(GenerationItem).where(
        GenerationItem.owner_id == identity.user_id, GenerationItem.status.in_(("queued", "running"))))
    if active + len(ids) > 200:
        raise HTTPException(429, f"当前还有 {active} 个商品在生成中，请稍后再提交")
    parts = [part for part, wanted in (("copy", body.with_copy), ("images", body.with_images),
                                       ("video", body.with_video)) if wanted]
    labels = {"copy": "文案", "images": "图片", "video": "视频"}
    name = clean(body.name) or f"{len(ids)} 个商品 · " + "、".join(labels[part] for part in parts)
    batch = GenerationBatch(owner_id=identity.user_id, name=name[:120], with_copy=body.with_copy,
                            with_images=body.with_images, with_video=body.with_video,
                            image_count=body.image_count, instructions=clean(body.instructions),
                            image_model=body.image_model if body.with_images else "",
                            video_voice=body.video_voice, video_music=body.video_music,
                            video_mode=body.video_mode, ai_clip_count=body.ai_clip_count)
    session.add(batch)
    session.flush()
    items = []
    for product_id in ids:
        item = GenerationItem(batch_id=batch.id, product_id=product_id, owner_id=identity.user_id,
                              status="queued", pending_parts=",".join(parts))
        session.add(item)
        items.append(item)
    session.flush()
    audit(session, identity, "batch_create", batch.id, {"products": len(ids), "parts": parts})
    session.commit()
    catalog_generation.submit([item.id for item in items])
    return {"id": batch.id, "name": batch.name, "total": len(items)}


@router.get("/image-models")
def list_image_models(refresh: bool = False, _: Identity = Depends(get_identity)):
    return catalog_generation.image_models(refresh=refresh)


@router.get("/batches")
def list_batches(limit: int = Query(30, ge=1, le=100), identity: Identity = Depends(get_identity),
                 session: Session = Depends(db.get_session)):
    rows = session.scalars(select(GenerationBatch).where(GenerationBatch.owner_id == identity.user_id)
                           .order_by(GenerationBatch.created_at.desc()).limit(limit)).all()
    counts: dict[str, dict[str, int]] = {row.id: {} for row in rows}
    if rows:
        for batch_id, status, count in session.execute(select(
                GenerationItem.batch_id, GenerationItem.status, func.count()).where(
                GenerationItem.batch_id.in_(list(counts))).group_by(GenerationItem.batch_id, GenerationItem.status)):
            counts[batch_id][status] = count
    return {"items": [{
        "id": row.id, "name": row.name, "created_at": row.created_at, "instructions": row.instructions,
        "options": {"copy": row.with_copy, "images": row.with_images, "video": row.with_video},
        "image_model": row.image_model,
        "counts": counts[row.id], "total": sum(counts[row.id].values()),
    } for row in rows]}


@router.get("/items")
def list_items(status: str | None = Query(None, max_length=64), batch_id: str | None = None,
               product_id: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100),
               identity: Identity = Depends(get_identity), session: Session = Depends(db.get_session)):
    catalog_generation.resume_pending(session, identity.user_id)
    query = select(GenerationItem).where(GenerationItem.owner_id == identity.user_id)
    if status:
        query = query.where(GenerationItem.status.in_(status.split(",")))
    if batch_id:
        query = query.where(GenerationItem.batch_id == batch_id)
    if product_id:
        query = query.where(GenerationItem.product_id == product_id)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(query.order_by(GenerationItem.updated_at.desc(), GenerationItem.id)
                           .offset((page - 1) * page_size).limit(page_size)).all()
    products = {row.id: row for row in session.scalars(select(Product).where(
        Product.id.in_({row.product_id for row in rows})))} if rows else {}
    images = product_images(session, list(products))
    batches = {row.id: row for row in session.scalars(select(GenerationBatch).where(
        GenerationBatch.id.in_({row.batch_id for row in rows})))} if rows else {}
    counts = dict(session.execute(select(GenerationItem.status, func.count()).where(
        GenerationItem.owner_id == identity.user_id).group_by(GenerationItem.status)).all())
    return {"items": [item_document(session, row, products[row.product_id], images[row.product_id],
                                    batches.get(row.batch_id)) for row in rows],
            "total": total, "counts": counts}


@router.get("/items/{item_id}")
def get_item(item_id: str, identity: Identity = Depends(get_identity), session: Session = Depends(db.get_session)):
    item = owned(session, GenerationItem, item_id, identity)
    product = session.get(Product, item.product_id)
    return item_document(session, item, product, product_images(session, [product.id])[product.id])


class ItemEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=4000)
    listing_title: str = Field(min_length=1, max_length=30)
    listing_description: str = Field(min_length=1, max_length=1000)
    category: str = Field(default="", max_length=64)

    @field_validator("title", "body", "listing_title", "listing_description", "category")
    @classmethod
    def strip(cls, value):
        return clean(value)


@router.put("/items/{item_id}", dependencies=WRITE)
def edit_item(item_id: str, body: ItemEdit, identity: Identity = Depends(require_publisher),
              session: Session = Depends(db.get_session)):
    item = owned(session, GenerationItem, item_id, identity)
    if item.status not in REVIEWABLE:
        raise HTTPException(409, "生成中或已通过的内容不能修改")
    check_rules(session, body.title, body.body, body.listing_title, body.listing_description)
    has_video = session.scalar(select(func.count()).select_from(GenerationMedia).where(
        GenerationMedia.item_id == item.id, GenerationMedia.kind == "video"))
    if has_video and (body.title != item.title or body.body != item.body):
        item.video_stale = True
    item.title, item.body = body.title, body.body
    item.listing_title, item.listing_description, item.category = body.listing_title, body.listing_description, body.category
    if item.status == "rejected":
        item.status = "review"
    item.updated_at = utcnow()
    audit(session, identity, "item_edit", item.id)
    session.commit()
    product = session.get(Product, item.product_id)
    return item_document(session, item, product, product_images(session, [product.id])[product.id])


class RegenerateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parts: list[Literal["copy", "images", "video"]] = Field(min_length=1, max_length=3)
    feedback: str = Field(default="", max_length=2000)


@router.post("/items/{item_id}/regenerate", status_code=202, dependencies=WRITE)
def regenerate_item(item_id: str, body: RegenerateInput, identity: Identity = Depends(require_publisher),
                    session: Session = Depends(db.get_session)):
    item = owned(session, GenerationItem, item_id, identity)
    if item.status not in REVIEWABLE:
        raise HTTPException(409, "当前状态不能重新生成")
    item.status, item.pending_parts = "queued", ",".join(dict.fromkeys(body.parts))
    item.feedback, item.error, item.updated_at = clean(body.feedback), "", utcnow()
    audit(session, identity, "item_regenerate", item.id, {"parts": body.parts})
    session.commit()
    catalog_generation.submit([item.id])
    product = session.get(Product, item.product_id)
    return item_document(session, item, product, product_images(session, [product.id])[product.id])


def _approve(session, item: GenerationItem, identity: Identity) -> ProductContent:
    if item.status != "review":
        raise HTTPException(409, "只有待审核的内容可以通过")
    if not item.title or not item.body:
        raise HTTPException(409, "文案为空，不能通过")
    if item.video_stale:
        raise HTTPException(409, "文案已修改，视频字幕还是旧文案，请先重新合成视频")
    check_rules(session, item.title, item.body, item.listing_title, item.listing_description)
    content = ProductContent(owner_id=identity.user_id, product_id=item.product_id, item_id=item.id,
                             title=item.title, body=item.body, listing_title=item.listing_title,
                             listing_description=item.listing_description, category=item.category,
                             approved_by=identity.user_id, approved_at=utcnow())
    session.add(content)
    session.flush()
    for row in session.scalars(select(GenerationMedia).where(GenerationMedia.item_id == item.id)):
        session.add(ContentMedia(content_id=content.id, kind=row.kind, path=row.path, sort_order=row.sort_order))
    item.status, item.content_id = "approved", content.id
    item.reviewed_by, item.reviewed_at, item.review_note = identity.user_id, utcnow(), ""
    audit(session, identity, "item_approve", item.id, {"content_id": content.id, "revision": item.revision})
    return content


class ApproveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)


@router.post("/items/{item_id}/approve", dependencies=WRITE)
def approve_item(item_id: str, body: ApproveInput, identity: Identity = Depends(require_publisher),
                 session: Session = Depends(db.get_session)):
    item = owned(session, GenerationItem, item_id, identity)
    if item.revision != body.revision:
        raise HTTPException(409, "内容已更新，请查看最新版本后再审核")
    content = _approve(session, item, identity)
    session.commit()
    return {"id": item.id, "status": item.status, "content_id": content.id}


class BulkApproveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=100)


@router.post("/items/approve", dependencies=WRITE)
def approve_items(body: BulkApproveInput, identity: Identity = Depends(require_publisher),
                  session: Session = Depends(db.get_session)):
    approved, skipped = [], []
    for item_id in dict.fromkeys(body.ids):
        item = session.get(GenerationItem, item_id)
        if item is None or item.owner_id != identity.user_id:
            skipped.append({"id": item_id, "reason": "记录不存在"})
            continue
        try:
            # _approve validates everything before its first write.
            _approve(session, item, identity)
            approved.append(item_id)
        except HTTPException as exc:
            skipped.append({"id": item_id, "reason": exc.detail})
    session.commit()
    return {"approved": approved, "skipped": skipped}


class RejectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=1000)


@router.post("/items/{item_id}/reject", dependencies=WRITE)
def reject_item(item_id: str, body: RejectInput, identity: Identity = Depends(require_publisher),
                session: Session = Depends(db.get_session)):
    item = owned(session, GenerationItem, item_id, identity)
    if item.status not in ("review", "failed"):
        raise HTTPException(409, "只有待审核的内容可以驳回")
    item.status, item.review_note = "rejected", clean(body.note)
    item.reviewed_by, item.reviewed_at = identity.user_id, utcnow()
    audit(session, identity, "item_reject", item.id)
    session.commit()
    return {"id": item.id, "status": item.status}


def _media_response(path_value: str):
    path = absolute(path_value)
    return FileResponse(path, media_type=media_type(path), headers={"Cache-Control": "private, max-age=600"})


@router.get("/items/{item_id}/media/{media_id}")
def item_media(item_id: str, media_id: str, identity: Identity = Depends(get_identity),
               session: Session = Depends(db.get_session)):
    owned(session, GenerationItem, item_id, identity)
    row = session.get(GenerationMedia, media_id)
    if row is None or row.item_id != item_id:
        raise HTTPException(404, "文件不存在")
    return _media_response(row.path)


# ---------- approved contents ----------

@router.get("/contents")
def list_contents(q: str = Query("", max_length=120), product_id: str | None = None,
                  page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100),
                  identity: Identity = Depends(get_identity), session: Session = Depends(db.get_session)):
    query = (select(ProductContent).join(Product, Product.id == ProductContent.product_id)
             .where(ProductContent.owner_id == identity.user_id, ProductContent.active.is_(True)))
    if product_id:
        query = query.where(ProductContent.product_id == product_id)
    if q.strip():
        pattern = f"%{q.strip()}%"
        query = query.where(or_(Product.name.like(pattern), ProductContent.title.like(pattern)))
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(query.order_by(ProductContent.approved_at.desc(), ProductContent.id)
                           .offset((page - 1) * page_size).limit(page_size)).all()
    products = {row.id: row for row in session.scalars(select(Product).where(
        Product.id.in_({row.product_id for row in rows})))} if rows else {}
    images = product_images(session, list(products))
    publishes = publish_summary(session, [row.id for row in rows])
    return {"items": [content_document(session, row, products[row.product_id], images[row.product_id],
                                       publishes[row.id]) for row in rows], "total": total}


@router.delete("/contents/{content_id}", status_code=204, dependencies=WRITE)
def withdraw_content(content_id: str, identity: Identity = Depends(require_publisher),
                     session: Session = Depends(db.get_session)):
    content = owned(session, ProductContent, content_id, identity)
    content.active = False
    audit(session, identity, "content_withdraw", content.id)
    session.commit()


@router.get("/contents/{content_id}/media/{media_id}")
def content_media(content_id: str, media_id: str, identity: Identity = Depends(get_identity),
                  session: Session = Depends(db.get_session)):
    owned(session, ProductContent, content_id, identity)
    row = session.get(ContentMedia, media_id)
    if row is None or row.content_id != content_id:
        raise HTTPException(404, "文件不存在")
    return _media_response(row.path)


# ---------- publishing ----------

def _contents(session, ids: list[str], identity: Identity) -> list[ProductContent]:
    ids = list(dict.fromkeys(ids))
    rows = {row.id: row for row in session.scalars(select(ProductContent).where(
        ProductContent.id.in_(ids), ProductContent.owner_id == identity.user_id, ProductContent.active.is_(True)))}
    if len(rows) != len(ids):
        raise HTTPException(422, "部分内容不存在或已撤回，请刷新")
    return [rows[identifier] for identifier in ids]


def _busy(session, identity: Identity, count: int):
    active = session.scalar(select(func.count()).select_from(PublishRecord).where(
        PublishRecord.owner_id == identity.user_id, PublishRecord.status.in_(("queued", "running", "submitting"))))
    if active + count > 100:
        raise HTTPException(429, f"还有 {active} 条发布在排队，请稍后再提交")


class ListingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_ids: list[str] = Field(min_length=1, max_length=50)
    condition: str = Field(default="全新", min_length=1, max_length=40)
    shipping_fee: Decimal = Field(default=Decimal("0"), ge=0, max_digits=8, decimal_places=2)
    location: str = Field(min_length=1, max_length=200)
    prices: dict[str, Decimal] = Field(default_factory=dict)
    include_generated_images: bool = False

    @field_validator("condition", "location")
    @classmethod
    def strip(cls, value):
        return clean(value)


@router.post("/publish/listing", status_code=202, dependencies=WRITE)
def publish_listing(body: ListingInput, identity: Identity = Depends(require_publisher),
                    session: Session = Depends(db.get_session)):
    contents = _contents(session, body.content_ids, identity)
    _busy(session, identity, len(contents))
    problems, records = [], []
    for content in contents:
        product = session.get(Product, content.product_id)
        price = body.prices.get(content.id)
        price_cents = to_cents(price) if price is not None else product.price_cents
        if not price_cents or price_cents <= 0:
            problems.append(f"{product.name}：未设置售价")
        elif not session.scalar(select(func.count()).select_from(ProductImage).where(ProductImage.product_id == product.id)):
            problems.append(f"{product.name}：没有商品图片")
        records.append(PublishRecord(
            owner_id=identity.user_id, workflow="listing", content_id=content.id, product_id=product.id,
            platform="xianyu", title=(content.listing_title or content.title)[:30], price_cents=price_cents,
            shipping_fee_cents=to_cents(body.shipping_fee) or 0, condition=body.condition, location=body.location,
            category=XIANYU_CATEGORY, include_generated_images=body.include_generated_images,
        ))
    if problems:
        raise HTTPException(422, "；".join(problems[:6]))
    session.add_all(records)
    session.flush()
    audit(session, identity, "publish_listing", records[0].id, {"count": len(records)})
    session.commit()
    catalog_publishing.submit([row.id for row in records])
    return {"items": [record.id for record in records], "total": len(records)}


class PromotionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_ids: list[str] = Field(min_length=1, max_length=50)
    platforms: list[Literal["douyin", "xiaohongshu", "kuaishou"]] = Field(min_length=1, max_length=3)


@router.post("/publish/promotion", status_code=202, dependencies=WRITE)
def publish_promotion(body: PromotionInput, identity: Identity = Depends(require_publisher),
                      session: Session = Depends(db.get_session)):
    contents = _contents(session, body.content_ids, identity)
    platforms = list(dict.fromkeys(body.platforms))
    _busy(session, identity, len(contents) * len(platforms))
    problems, records = [], []
    for content in contents:
        product = session.get(Product, content.product_id)
        if not session.scalar(select(func.count()).select_from(ContentMedia).where(
                ContentMedia.content_id == content.id, ContentMedia.kind == "video")):
            problems.append(f"{product.name}：没有视频")
            continue
        for platform in platforms:
            spec = PLATFORMS[platform]
            if len(content.title) > spec.title_limit or len(content.body) > spec.description_limit:
                problems.append(f"{product.name}：标题或正文超出{spec.label}字数限制")
                continue
            records.append(PublishRecord(owner_id=identity.user_id, workflow="promotion", content_id=content.id,
                                         product_id=product.id, platform=platform, title=content.title[:120]))
    if problems:
        raise HTTPException(422, "；".join(dict.fromkeys(problems[:6])))
    session.add_all(records)
    session.flush()
    audit(session, identity, "publish_promotion", records[0].id, {"count": len(records)})
    session.commit()
    catalog_publishing.submit([row.id for row in records])
    return {"items": [record.id for record in records], "total": len(records)}


@router.get("/records")
def list_records(workflow: Literal["listing", "promotion"] | None = None, status: str | None = Query(None, max_length=64),
                 platform: str | None = Query(None, max_length=24), page: int = Query(1, ge=1),
                 page_size: int = Query(30, ge=1, le=100), identity: Identity = Depends(get_identity),
                 session: Session = Depends(db.get_session)):
    catalog_publishing.resume_pending(session, identity.user_id)
    query = select(PublishRecord).where(PublishRecord.owner_id == identity.user_id)
    if workflow:
        query = query.where(PublishRecord.workflow == workflow)
    if status:
        query = query.where(PublishRecord.status.in_(status.split(",")))
    if platform:
        query = query.where(PublishRecord.platform == platform)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(query.order_by(PublishRecord.created_at.desc(), PublishRecord.id)
                           .offset((page - 1) * page_size).limit(page_size)).all()
    products = {row.id: row for row in session.scalars(select(Product).where(
        Product.id.in_({row.product_id for row in rows})))} if rows else {}
    counts_query = select(PublishRecord.status, func.count()).where(PublishRecord.owner_id == identity.user_id)
    if workflow:
        counts_query = counts_query.where(PublishRecord.workflow == workflow)
    counts = dict(session.execute(counts_query.group_by(PublishRecord.status)).all())
    return {"items": [record_document(row, products.get(row.product_id)) for row in rows],
            "total": total, "counts": counts}


@router.post("/records/{record_id}/retry", status_code=202, dependencies=WRITE)
def retry_record(record_id: str, identity: Identity = Depends(require_publisher),
                 session: Session = Depends(db.get_session)):
    record = owned(session, PublishRecord, record_id, identity)
    if record.status != "failed":
        raise HTTPException(409, "只有明确失败（未提交）的记录可以重试")
    record.status, record.error, record.attempt = "queued", "", record.attempt + 1
    record.started_at = record.finished_at = None
    audit(session, identity, "record_retry", record.id)
    session.commit()
    catalog_publishing.submit([record.id])
    return record_document(record, session.get(Product, record.product_id))


class ResolveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["succeeded", "failed"]
    external_url: str = Field(default="", max_length=1024)

    @field_validator("external_url")
    @classmethod
    def https_only(cls, value):
        value = value.strip()
        if value and not value.startswith("https://"):
            raise ValueError("链接必须以 https:// 开头")
        return value


@router.post("/records/{record_id}/resolve", dependencies=WRITE)
def resolve_record(record_id: str, body: ResolveInput, identity: Identity = Depends(require_publisher),
                   session: Session = Depends(db.get_session)):
    record = owned(session, PublishRecord, record_id, identity)
    if record.status != "unknown":
        raise HTTPException(409, "只有结果待核查的记录需要人工确认")
    record.status = body.status
    record.external_url = body.external_url or record.external_url
    record.error = "" if body.status == "succeeded" else (record.error or "人工核查确认未发布")
    record.finished_at = utcnow()
    audit(session, identity, "record_resolve", record.id, {"status": body.status})
    session.commit()
    return record_document(record, session.get(Product, record.product_id))


# ---------- overview ----------

@router.get("/overview")
def overview(identity: Identity = Depends(get_identity), session: Session = Depends(db.get_session)):
    owner = identity.user_id
    products = session.scalar(select(func.count()).select_from(Product).where(
        Product.owner_id == owner, Product.active.is_(True)))
    items = dict(session.execute(select(GenerationItem.status, func.count()).where(
        GenerationItem.owner_id == owner).group_by(GenerationItem.status)).all())
    contents = session.scalar(select(func.count()).select_from(ProductContent).where(
        ProductContent.owner_id == owner, ProductContent.active.is_(True)))
    records = {}
    for workflow, status, count in session.execute(select(PublishRecord.workflow, PublishRecord.status, func.count())
                                                   .where(PublishRecord.owner_id == owner)
                                                   .group_by(PublishRecord.workflow, PublishRecord.status)):
        records.setdefault(workflow, {})[status] = count
    return {"products": products, "items": items, "contents": contents, "records": records,
            "platforms": {key: {"label": spec.label, "purpose": spec.purpose} for key, spec in PLATFORMS.items()
                          if key == "xianyu" or key in PROMOTION_PLATFORMS}}
