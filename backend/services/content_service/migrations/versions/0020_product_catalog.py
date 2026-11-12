"""Dedicated product catalog, generation review queue, approved contents and publish records.

Existing products lived in the generic resource library (resource_kind = product)
with specs in free text and images as child resources. They are copied into
the new tables once; the library rows are left untouched.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "content_0020"
down_revision = "content_0019"
branch_labels = None
depends_on = None


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("specs_json", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("legacy_entry_id", sa.String(36), nullable=True),
        *_timestamps(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("owner_id", "active", "legacy_entry_id", "updated_at"):
        op.create_index("ix_products_" + column, "products", [column])

    op.create_table(
        "product_images",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("product_id", sa.String(36), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("media_type", sa.String(64), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_product_images_product_id", "product_images", ["product_id"])

    op.create_table(
        "generation_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("with_copy", sa.Boolean(), nullable=False),
        sa.Column("with_images", sa.Boolean(), nullable=False),
        sa.Column("with_video", sa.Boolean(), nullable=False),
        sa.Column("image_count", sa.Integer(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_generation_batches_owner_id", "generation_batches", ["owner_id"])
    op.create_index("ix_generation_batches_created_at", "generation_batches", ["created_at"])

    op.create_table(
        "generation_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("generation_batches.id"), nullable=False),
        sa.Column("product_id", sa.String(36), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("listing_title", sa.String(60), nullable=False),
        sa.Column("listing_description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("video_stale", sa.Boolean(), nullable=False),
        sa.Column("pending_parts", sa.String(64), nullable=False),
        sa.Column("feedback", sa.Text(), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=False),
        sa.Column("error", sa.String(500), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("image_model", sa.String(128), nullable=False),
        sa.Column("reviewed_by", sa.String(128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_id", sa.String(36), nullable=True),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("batch_id", "product_id", "owner_id", "status", "updated_at"):
        op.create_index("ix_generation_items_" + column, "generation_items", [column])

    op.create_table(
        "generation_media",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("item_id", sa.String(36), sa.ForeignKey("generation_items.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_generation_media_item_id", "generation_media", ["item_id"])

    op.create_table(
        "product_contents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("product_id", sa.String(36), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("item_id", sa.String(36), sa.ForeignKey("generation_items.id"), nullable=False, unique=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("listing_title", sa.String(60), nullable=False),
        sa.Column("listing_description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    for column in ("owner_id", "product_id", "approved_at", "active"):
        op.create_index("ix_product_contents_" + column, "product_contents", [column])

    op.create_table(
        "content_media",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("content_id", sa.String(36), sa.ForeignKey("product_contents.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
    )
    op.create_index("ix_content_media_content_id", "content_media", ["content_id"])

    op.create_table(
        "publish_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("workflow", sa.String(16), nullable=False),
        sa.Column("content_id", sa.String(36), sa.ForeignKey("product_contents.id"), nullable=False),
        sa.Column("product_id", sa.String(36), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("platform", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=True),
        sa.Column("shipping_fee_cents", sa.Integer(), nullable=False),
        sa.Column("condition", sa.String(40), nullable=False),
        sa.Column("location", sa.String(200), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("include_generated_images", sa.Boolean(), nullable=False),
        sa.Column("account_label", sa.String(120), nullable=False),
        sa.Column("external_url", sa.String(1024), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("error", sa.String(500), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in ("owner_id", "workflow", "content_id", "product_id", "platform", "status", "created_at"):
        op.create_index("ix_publish_records_" + column, "publish_records", [column])

    _import_library_products()


def _specs(facts_json: str | None) -> list[dict]:
    try:
        facts = json.loads(facts_json or "[]")
    except ValueError:
        return []
    specs = []
    for fact in facts if isinstance(facts, list) else []:
        if not isinstance(fact, str):
            continue
        for separator in ("：", ":"):
            if separator in fact:
                name, value = fact.split(separator, 1)
                if name.strip() and value.strip():
                    specs.append({"name": name.strip()[:40], "value": value.strip()[:200]})
                break
    return specs[:30]


def _import_library_products() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if not {"resource_entries", "resource_versions", "asset_refs"} <= tables:
        return
    # The app stores naive UTC wall-clock values (see time_utils.UTCDateTime).
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = bind.execute(sa.text(
        "SELECT e.id, e.owner_id, e.title, v.facts_json "
        "FROM resource_entries e LEFT JOIN resource_versions v ON v.id = e.current_version_id "
        "WHERE e.resource_kind = 'product' AND e.active = :active ORDER BY e.created_at, e.id"
    ), {"active": True}).fetchall()
    products = sa.table(
        "products", *(sa.column(name) for name in (
            "id", "owner_id", "name", "sku", "category", "price_cents", "description",
            "specs_json", "active", "legacy_entry_id", "created_at", "updated_at",
        )),
    )
    images = sa.table(
        "product_images", *(sa.column(name) for name in (
            "id", "product_id", "storage_key", "media_type", "sort_order", "created_at",
        )),
    )
    for entry_id, owner_id, title, facts_json in rows:
        product_id = str(uuid4())
        bind.execute(products.insert().values(
            id=product_id, owner_id=owner_id, name=(title or "未命名商品")[:120], sku="", category="",
            price_cents=None, description="", specs_json=json.dumps(_specs(facts_json), ensure_ascii=False),
            active=True, legacy_entry_id=entry_id, created_at=now, updated_at=now,
        ))
        keys = bind.execute(sa.text(
            "SELECT a.storage_key, a.media_type FROM resource_entries c "
            "JOIN asset_refs a ON a.entry_id = c.id "
            "WHERE c.parent_id = :entry AND c.resource_kind = 'image' AND c.active = :active "
            "ORDER BY c.created_at, c.id, a.created_at"
        ), {"entry": entry_id, "active": True}).fetchall()
        seen = set()
        for key, media_type in keys:
            if key in seen or len(seen) >= 9:
                continue
            seen.add(key)
            bind.execute(images.insert().values(
                id=str(uuid4()), product_id=product_id, storage_key=key,
                media_type=media_type or "image/jpeg", sort_order=len(seen) - 1, created_at=now,
            ))


def downgrade() -> None:
    for table in (
        "publish_records", "content_media", "product_contents", "generation_media",
        "generation_items", "generation_batches", "product_images", "products",
    ):
        op.drop_table(table)
