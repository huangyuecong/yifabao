"""Product catalog, AI generation review queue, approved contents and publish records.

The flow is: products -> generation_items (draft, reviewed by an admin) ->
product_contents (approved, reusable) -> publish_records (one row per
platform submission, for both listing and promotion workflows).
"""

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import new_id, utcnow


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(120))
    sku: Mapped[str] = mapped_column(String(64), default="")
    category: Mapped[str] = mapped_column(String(64), default="")
    # Money is stored as integer cents to avoid floating point drift.
    price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # [{"name": "规格", "value": "500ml"}, ...]
    specs_json: Mapped[str] = mapped_column(Text, default="[]")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    legacy_entry_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, index=True)


class ProductImage(Base):
    __tablename__ = "product_images"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    # Key inside the content asset store (data_dir/objects).
    storage_key: Mapped[str] = mapped_column(String(512))
    media_type: Mapped[str] = mapped_column(String(64), default="image/jpeg")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class GenerationBatch(Base):
    __tablename__ = "generation_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(120))
    with_copy: Mapped[bool] = mapped_column(Boolean, default=True)
    with_images: Mapped[bool] = mapped_column(Boolean, default=False)
    with_video: Mapped[bool] = mapped_column(Boolean, default=False)
    image_count: Mapped[int] = mapped_column(Integer, default=3)
    # Preferred image model; empty means the configured order. Other models are
    # still tried when it fails.
    image_model: Mapped[str] = mapped_column(String(128), default="")
    video_voice: Mapped[bool] = mapped_column(Boolean, default=True)
    video_music: Mapped[bool] = mapped_column(Boolean, default=False)
    # slideshow: animated photos; ai: Seedance image-to-video clips.
    video_mode: Mapped[str] = mapped_column(String(16), default="slideshow")
    ai_clip_count: Mapped[int] = mapped_column(Integer, default=3)
    instructions: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class GenerationItem(Base):
    """One product inside a batch. Status: queued, running, review, approved, rejected, failed."""

    __tablename__ = "generation_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(ForeignKey("generation_batches.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(120), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    listing_title: Mapped[str] = mapped_column(String(60), default="")
    listing_description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), default="")
    video_stale: Mapped[bool] = mapped_column(Boolean, default=False)
    # Why narration or music was skipped in the last video, shown to the reviewer.
    video_note: Mapped[str] = mapped_column(String(300), default="")
    video_model: Mapped[str] = mapped_column(String(128), default="")
    # Parts requested by the next run, e.g. "copy,images,video".
    pending_parts: Mapped[str] = mapped_column(String(64), default="")
    feedback: Mapped[str] = mapped_column(Text, default="")
    review_note: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(String(500), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    image_model: Mapped[str] = mapped_column(String(128), default="")
    # Knowledge passages retrieved for the copy (RAG), shown to the reviewer.
    references_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    content_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, index=True)


class GenerationMedia(Base):
    __tablename__ = "generation_media"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    item_id: Mapped[str] = mapped_column(ForeignKey("generation_items.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # image | video
    # Path relative to data_dir/automation-media; files are never rewritten in place.
    path: Mapped[str] = mapped_column(String(512))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ProductContent(Base):
    """An approved, immutable content package that the publishing workflows consume."""

    __tablename__ = "product_contents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("generation_items.id"), unique=True)
    title: Mapped[str] = mapped_column(String(120), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    listing_title: Mapped[str] = mapped_column(String(60), default="")
    listing_description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), default="")
    approved_by: Mapped[str] = mapped_column(String(128))
    approved_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class ContentMedia(Base):
    __tablename__ = "content_media"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    content_id: Mapped[str] = mapped_column(ForeignKey("product_contents.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(512))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class PublishRecord(Base):
    """One submission of one approved content to one platform.

    Status: queued, running, submitting, succeeded, failed, unknown. ``unknown``
    means the final button may have been clicked; it is never retried
    automatically and must be reconciled by a person.
    """

    __tablename__ = "publish_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    workflow: Mapped[str] = mapped_column(String(16), index=True)  # listing | promotion
    content_id: Mapped[str] = mapped_column(ForeignKey("product_contents.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    platform: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    title: Mapped[str] = mapped_column(String(120), default="")
    # Listing terms, captured at submission time.
    price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shipping_fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    condition: Mapped[str] = mapped_column(String(40), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(64), default="")
    include_generated_images: Mapped[bool] = mapped_column(Boolean, default=False)
    account_label: Mapped[str] = mapped_column(String(120), default="")
    external_url: Mapped[str] = mapped_column(String(1024), default="")
    external_id: Mapped[str] = mapped_column(String(128), default="")
    error: Mapped[str] = mapped_column(String(500), default="")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
