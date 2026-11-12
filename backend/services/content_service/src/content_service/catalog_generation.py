"""Background generation of copy, images and video for catalog products.

Each generation item is claimed with a compare-and-set on its status, so a
product is never generated twice concurrently. A failed run keeps the last
successful draft and media.
"""

import json
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Lock
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, true, update

from . import db
from .catalog_media import absolute, new_work_dir, product_image_path, relative, save_jpeg
from .catalog_models import GenerationBatch, GenerationItem, GenerationMedia, Product, ProductImage
from .models import SensitiveRule, utcnow

logger = logging.getLogger(__name__)
LEASE = timedelta(minutes=30)
PARTS = ("copy", "images", "video")
COPY_FIELDS = ("title", "body", "listing_title", "listing_description", "category", "model")

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="catalog-generation")
_inflight: set[str] = set()
_lock = Lock()


def price_text(cents: int | None) -> str:
    return "" if cents is None else f"{cents / 100:.2f}"


def product_facts(product: Product) -> str:
    lines = [product.name]
    if product.category:
        lines.append(f"分类：{product.category}")
    if product.price_cents is not None:
        lines.append(f"价格：{price_text(product.price_cents)} 元")
    for spec in json.loads(product.specs_json or "[]"):
        lines.append(f"{spec.get('name', '')}：{spec.get('value', '')}")
    if product.description:
        lines.append(product.description)
    return "\n".join(line for line in lines if line.strip())


_model_cache: tuple[float, list[dict]] | None = None
MAX_IMAGE_MODELS = 4


def image_models(refresh: bool = False) -> dict:
    """The configured image models plus the other image models the key can use."""
    import time

    from .ai_model_config import discover_models, model_settings
    from .provider_dependencies import get_runtime_settings

    global _model_cache
    settings = model_settings(get_runtime_settings(), "image")
    configured = list(dict.fromkeys([settings.image_model, *settings.image_fallback_models]))
    discovered: list[dict] = []
    error = ""
    remote = settings.image_provider in {"openai_compatible", "openai_responses"}
    if remote and settings.image_base_url and settings.image_api_key:
        if not refresh and _model_cache and time.monotonic() - _model_cache[0] < 600:
            discovered = _model_cache[1]
        else:
            try:
                rows = discover_models(settings, "image", settings.image_provider)
                discovered = [row for row in rows if "image" in row["id"].lower() or "dall-e" in row["id"].lower()]
                _model_cache = (time.monotonic(), discovered)
            except Exception:
                error = "暂时无法从接口读取模型列表，只显示已配置的模型"
    names = {row["id"]: row["name"] for row in discovered}
    # Provider lists are usually oldest first; prefer newer models after the configured ones.
    others = [row["id"] for row in reversed(discovered) if row["id"] not in configured]
    return {
        "default": settings.image_model,
        "configured": configured,
        "models": [{"id": model, "name": names.get(model, model), "configured": model in configured}
                   for model in [*configured, *others]],
        "error": error,
    }


def image_model_order(preferred: str = "") -> list[str]:
    try:
        models = [row["id"] for row in image_models()["models"]]
    except Exception:
        models = []
    if preferred:
        models = [preferred, *[model for model in models if model != preferred]]
    return models[:MAX_IMAGE_MODELS]


def submit(item_ids: list[str]) -> None:
    for item_id in item_ids:
        with _lock:
            if item_id in _inflight:
                continue
            _inflight.add(item_id)
        _pool.submit(_run_guarded, item_id)


def _run_guarded(item_id: str) -> None:
    try:
        run_item(item_id)
    except Exception:  # pragma: no cover - logged, the item is marked failed by run_item
        logger.exception("catalog generation crashed for %s", item_id)
    finally:
        with _lock:
            _inflight.discard(item_id)


def resume_pending(session, owner_id: str) -> None:
    """Expire abandoned runs and pick up queued items after a restart."""
    now = utcnow()
    session.execute(update(GenerationItem).where(
        GenerationItem.owner_id == owner_id, GenerationItem.status == "running",
        GenerationItem.lease_expires_at < now,
    ).values(status="failed", error="生成中断，可重新生成", lease_token=None, lease_expires_at=None, updated_at=now))
    session.commit()
    queued = session.scalars(select(GenerationItem.id).where(
        GenerationItem.owner_id == owner_id, GenerationItem.status == "queued",
    )).all()
    with _lock:
        missing = [item_id for item_id in queued if item_id not in _inflight]
    submit(missing)


def _claim(item_id: str) -> str | None:
    token = str(uuid4())
    with db.SessionLocal() as session:
        changed = session.execute(update(GenerationItem).where(
            GenerationItem.id == item_id, GenerationItem.status == "queued",
        ).values(status="running", lease_token=token, lease_expires_at=utcnow() + LEASE, error="", updated_at=utcnow()))
        session.commit()
    return token if changed.rowcount == 1 else None


def _source_images(session, product: Product, directory) -> list[dict]:
    rows = session.scalars(select(ProductImage).where(ProductImage.product_id == product.id)
                           .order_by(ProductImage.sort_order, ProductImage.created_at)).all()
    images = []
    for index, row in enumerate(rows[:9]):
        target = directory / f"source-{index + 1:02d}.jpg"
        save_jpeg(product_image_path(row.storage_key), target)
        images.append({"id": row.id, "name": f"{product.name} 图{index + 1}", "path": target})
    return images


def _write(item_id: str, token: str, values: dict) -> bool:
    with db.SessionLocal() as session:
        changed = session.execute(update(GenerationItem).where(
            GenerationItem.id == item_id, GenerationItem.lease_token == token, GenerationItem.status == "running",
        ).values(**values, updated_at=utcnow()))
        session.commit()
        return changed.rowcount == 1


def _safe_error(exc: Exception) -> str:
    from .automation_jobs import _safe_error as automation_error

    if isinstance(exc, HTTPException) and isinstance(exc.detail, str):
        return exc.detail[:500]
    return automation_error(exc, preparing=True)


def run_item(item_id: str) -> None:
    from .automation_content import generate_draft, generate_promotion_images
    from .provider_dependencies import get_runtime_settings

    token = _claim(item_id)
    if token is None:
        return
    settings = get_runtime_settings()
    directory = None
    values: dict = {}
    parts: set[str] = set()
    had_video = False
    try:
        with db.SessionLocal() as session:
            item = session.get(GenerationItem, item_id)
            batch = session.get(GenerationBatch, item.batch_id)
            product = session.get(Product, item.product_id)
            parts = set(filter(None, item.pending_parts.split(","))) & set(PARTS)
            existing = session.scalars(select(GenerationMedia).where(GenerationMedia.item_id == item.id)
                                       .order_by(GenerationMedia.sort_order)).all()
            old_images = [absolute(row.path) for row in existing if row.kind == "image"]
            had_video = any(row.kind == "video" for row in existing)
            rules = list(session.scalars(select(SensitiveRule.phrase).where(SensitiveRule.active == true())))
            directory = new_work_dir("catalog", item.id)
            sources = _source_images(session, product, directory)
            facts = product_facts(product)
            instructions = "\n".join(filter(None, [
                batch.instructions,
                f"修改意见：{item.feedback}" if item.feedback else "",
                ("当前文案：" + json.dumps({"title": item.title, "description": item.body}, ensure_ascii=False))
                if item.feedback and item.title else "",
            ]))
            preferred_model = batch.image_model
            draft = {
                "title": item.title, "description": item.body, "listing_title": item.listing_title,
                "listing_description": item.listing_description, "category": item.category,
            }
            image_count = batch.image_count
            video_voice, video_music = batch.video_voice, batch.video_music
            video_mode, ai_clip_count = batch.video_mode, batch.ai_clip_count
            batch_instructions = batch.instructions
        if not sources and ({"images", "video"} & parts or "copy" in parts):
            raise HTTPException(422, "商品没有图片，请先在商品库上传至少一张图片")
        if "copy" in parts:
            config = {"purpose": "creation", "platforms": [], "instructions": instructions}
            product_doc = {"name": product.name, "text": facts, "category": product.category}
            generated, model = generate_draft(config, product_doc, sources[:4], rules, settings)
            draft.update(generated)
            values.update(title=generated["title"], body=generated["description"],
                          listing_title=generated["listing_title"][:60],
                          listing_description=generated["listing_description"],
                          category=(product.category or generated.get("category") or "")[:64], model=model or "")
        elif not draft["title"]:
            # Copy was not requested: fall back to the product's own facts.
            draft.update(title=product.name[:20], description=facts[:900],
                         listing_title=product.name[:30], listing_description=facts[:1000])
            values.update(title=draft["title"], body=draft["description"], listing_title=draft["listing_title"],
                          listing_description=draft["listing_description"], category=product.category)
        new_media: dict[str, list[str]] = {}
        image_paths = old_images
        if "images" in parts:
            config = {"purpose": "creation", "instructions": instructions, "generated_image_count": image_count}
            image_sources = {"product": {"name": product.name, "text": facts},
                             "images": [{"path": str(source["path"])} for source in sources]}
            from .provider_dependencies import image_port_for_models

            provider = image_port_for_models(image_model_order(preferred_model))
            paths, image_model = generate_promotion_images(config, image_sources, draft, directory, settings, provider)
            image_paths = [Path(path) for path in paths]
            new_media["image"] = [relative(path) for path in image_paths]
            values["image_model"] = image_model or ""
        if "video" in parts or ("copy" in parts and had_video):
            from .catalog_video import render_product_video

            # Generated scenes first, then the real product photos.
            frames = [*image_paths, *[source["path"] for source in sources]]
            notes, clips, script = [], None, None
            if video_mode == "ai":
                from .catalog_ai_video import generate_ai_clips, short_voiceover

                clips = generate_ai_clips(frames, product.name, batch_instructions, ai_clip_count, directory)
                notes += clips.notes
                if clips.paths and video_voice:
                    script = short_voiceover(draft["title"], draft["description"], rules)
                elif not clips.paths:
                    notes.append("AI 动态镜头没有生成成功，本次改用图片动画")
            video = render_product_video(frames, draft["title"], draft["description"], directory, settings,
                                         voice=video_voice, music=video_music,
                                         ai_clips=clips.paths if clips and clips.paths else None,
                                         narration_override=script)
            new_media["video"] = [relative(video.path)]
            values["video_stale"] = False
            values["video_note"] = "；".join([*notes, *video.notes])[:300]
            values["video_model"] = f"{clips.model}（{len(clips.paths)} 段 AI 镜头）" if clips and clips.paths else ""
        with db.SessionLocal() as session:
            item = session.get(GenerationItem, item_id)
            if item.lease_token != token or item.status != "running":
                return
            for kind, paths in new_media.items():
                for row in session.scalars(select(GenerationMedia).where(
                        GenerationMedia.item_id == item_id, GenerationMedia.kind == kind)).all():
                    session.delete(row)
                for index, path in enumerate(paths):
                    session.add(GenerationMedia(item_id=item_id, kind=kind, path=path, sort_order=index))
            for key, value in values.items():
                setattr(item, key, value)
            item.status, item.revision = "review", item.revision + 1
            item.pending_parts, item.error, item.feedback = "", "", ""
            item.lease_token, item.lease_expires_at = None, None
            session.commit()
        if not new_media and directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
    except Exception as exc:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        # Keep copy that was already generated; only the parts after it need another run.
        kept = {key: value for key, value in values.items() if key in COPY_FIELDS} if "copy" in parts else {}
        if kept:
            kept["pending_parts"] = ",".join(part for part in PARTS if part in parts and part != "copy")
            kept["video_stale"] = had_video
        _write(item_id, token, {**kept, "status": "failed", "error": _safe_error(exc),
                                "lease_token": None, "lease_expires_at": None})
