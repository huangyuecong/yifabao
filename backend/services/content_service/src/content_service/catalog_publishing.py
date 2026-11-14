"""Execute publish records through the existing browser publishers.

Records run one at a time because all platforms share the dedicated browser.
The ``before_submit`` callback moves a record to ``submitting`` just before
the final button is clicked; anything that goes wrong after that becomes
``unknown`` and is never retried automatically.
"""

import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Lock
from uuid import uuid4

from sqlalchemy import select, update

from . import db, remote_browser
from .automation_platforms import get_platform
from .catalog_media import absolute, new_work_dir, product_image_path, save_jpeg
from .catalog_models import ContentMedia, Product, ProductContent, ProductImage, PublishRecord
from .models import utcnow

logger = logging.getLogger(__name__)
# Covers a slow video upload (up to 20 minutes) plus filling the form and submitting.
LEASE = timedelta(minutes=45)

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="catalog-publish")
_inflight: set[str] = set()
_lock = Lock()


class Stopped(Exception):
    pass


def submit(record_ids: list[str]) -> None:
    for record_id in record_ids:
        with _lock:
            if record_id in _inflight:
                continue
            _inflight.add(record_id)
        _pool.submit(_run_guarded, record_id)


def _run_guarded(record_id: str) -> None:
    try:
        run_record(record_id)
    except Exception:  # pragma: no cover
        logger.exception("catalog publish crashed for %s", record_id)
    finally:
        with _lock:
            _inflight.discard(record_id)


def resume_pending(session, owner_id: str) -> None:
    now = utcnow()
    session.execute(update(PublishRecord).where(
        PublishRecord.owner_id == owner_id, PublishRecord.status == "running", PublishRecord.lease_expires_at < now,
    ).values(status="failed", error="执行中断，尚未提交，可重试", lease_token=None, lease_expires_at=None, finished_at=now))
    session.execute(update(PublishRecord).where(
        PublishRecord.owner_id == owner_id, PublishRecord.status == "submitting", PublishRecord.lease_expires_at < now,
    ).values(status="unknown", error="提交过程中断，请到平台核查结果", lease_token=None, lease_expires_at=None, finished_at=now))
    session.commit()
    queued = session.scalars(select(PublishRecord.id).where(
        PublishRecord.owner_id == owner_id, PublishRecord.status == "queued",
    ).order_by(PublishRecord.created_at)).all()
    with _lock:
        missing = [record_id for record_id in queued if record_id not in _inflight]
    submit(missing)


def _fenced(session, record_id: str, token: str, status: str, values: dict) -> None:
    changed = session.execute(update(PublishRecord).where(
        PublishRecord.id == record_id, PublishRecord.lease_token == token, PublishRecord.status == status,
    ).values(**values))
    if changed.rowcount != 1:
        session.rollback()
        raise Stopped()
    session.commit()


def _listing_payload(session, record: PublishRecord, content: ProductContent, directory) -> dict:
    product = session.get(Product, record.product_id)
    sources = [product_image_path(row.storage_key) for row in session.scalars(
        select(ProductImage).where(ProductImage.product_id == product.id)
        .order_by(ProductImage.sort_order, ProductImage.created_at)).all()]
    if record.include_generated_images:
        sources += [absolute(row.path) for row in session.scalars(select(ContentMedia).where(
            ContentMedia.content_id == content.id, ContentMedia.kind == "image").order_by(ContentMedia.sort_order)).all()]
    paths = [str(save_jpeg(source, directory / f"image-{index + 1:02d}.jpg")) for index, source in enumerate(sources[:9])]
    return {
        "title": (content.listing_title or content.title)[:30],
        "description": content.listing_description or content.body,
        "category": record.category, "condition": record.condition, "location": record.location,
        "price": f"{(record.price_cents or 0) / 100:.2f}", "shipping_fee": f"{record.shipping_fee_cents / 100:.2f}",
        "image_paths": paths,
    }


def _promotion_payload(session, content: ProductContent) -> dict:
    video = session.scalar(select(ContentMedia).where(ContentMedia.content_id == content.id, ContentMedia.kind == "video"))
    if video is None:
        raise ValueError("该内容没有视频，无法发布到视频平台")
    return {"title": content.title, "description": content.body, "video_path": str(absolute(video.path))}


def run_record(record_id: str) -> None:
    from .automation_executor import execute
    from .catalog_generation import _safe_error

    token = str(uuid4())
    with db.SessionLocal() as session:
        changed = session.execute(update(PublishRecord).where(
            PublishRecord.id == record_id, PublishRecord.status == "queued",
        ).values(status="running", lease_token=token, lease_expires_at=utcnow() + LEASE,
                 started_at=utcnow(), error=""))
        session.commit()
        if changed.rowcount != 1:
            return
    directory = None
    submitted = False
    try:
        with db.SessionLocal() as session:
            record = session.get(PublishRecord, record_id)
            content = session.get(ProductContent, record.content_id)
            if record.workflow == "listing":
                directory = new_work_dir("publish", record.id)
                payload = _listing_payload(session, record, content, directory)
            else:
                payload = _promotion_payload(session, content)
            platform = record.platform
            submitted_by = record.submitted_by or None
        get_platform(platform)
        # Online trial: publish with the submitting account's own browser and logins.
        remote_browser.task_owner.set(submitted_by)

        def before_submit():
            nonlocal submitted
            with db.SessionLocal() as session:
                _fenced(session, record_id, token, "running", {"status": "submitting", "lease_expires_at": utcnow() + LEASE})
            submitted = True

        receipt = execute(platform, "publish", payload, {}, execution_mode="browser", before_submit=before_submit)
        if not submitted or receipt.get("status") != "published":
            raise RuntimeError("平台未返回发布回执")
        with db.SessionLocal() as session:
            _fenced(session, record_id, token, "submitting", {
                "status": "succeeded", "external_url": str(receipt.get("url") or "")[:1024],
                "external_id": str(receipt.get("external_id") or "")[:128],
                "account_label": str(receipt.get("account_label") or "")[:120],
                "lease_token": None, "lease_expires_at": None, "finished_at": utcnow(),
            })
    except Stopped:
        return
    except Exception as exc:
        uncertain = submitted or getattr(exc, "status", "") == "unknown"
        message = str(exc) if isinstance(exc, ValueError) else _safe_error(exc)
        if hasattr(exc, "message"):
            message = str(exc.message)
        with db.SessionLocal() as session:
            session.execute(update(PublishRecord).where(
                PublishRecord.id == record_id, PublishRecord.lease_token == token,
                PublishRecord.status.in_(("running", "submitting")),
            ).values(status="unknown" if uncertain else "failed", error=message[:500],
                     lease_token=None, lease_expires_at=None, finished_at=utcnow()))
            session.commit()
        if directory is not None and not submitted:
            shutil.rmtree(directory, ignore_errors=True)
