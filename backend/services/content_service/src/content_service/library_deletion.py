"""Hard-delete library records, with durable cleanup of owned stored objects."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, or_, select, update

from .models import (
    Artifact,
    AssetRef,
    ContentTask,
    ExternalRef,
    Job,
    LibraryItem,
    OriginalArchive,
    ProjectAsset,
    ResourceChunk,
    ResourceEntry,
    ResourceVersion,
    WeKnoraSyncJob,
)


class LibraryDeletionConflict(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False)


def _freeze_jobs(session, model, entry_id):
    rows = list(session.scalars(select(model).where(model.entry_id == entry_id)))
    now = datetime.now(UTC)
    for row in rows:
        changed = session.execute(
            update(model)
            .where(
                model.id == row.id,
                or_(model.lease_expires_at.is_(None), model.lease_expires_at <= now),
            )
            .values(status="deleted")
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise LibraryDeletionConflict("该资料正在同步，请稍后再删除")
    return rows


def delete_library_records(session, item: LibraryItem, *, deleted_by: str):
    """Remove every version in one transaction; never leave a hidden active=False row."""
    entry_id = item.resource_entry_id
    items = (
        list(
            session.scalars(
                select(LibraryItem).where(
                    LibraryItem.resource_entry_id == entry_id,
                )
            )
        )
        if entry_id
        else [item]
    )
    item_ids = [row.id for row in items]
    assets = (
        list(
            session.scalars(
                select(AssetRef).where(
                    AssetRef.entry_id == entry_id,
                )
            )
        )
        if entry_id
        else []
    )
    asset_ids = [row.id for row in assets]
    if session.scalar(
        select(ProjectAsset.id)
        .where(
            or_(
                ProjectAsset.library_item_id.in_(item_ids),
                ProjectAsset.asset_ref_id.in_(asset_ids),
                ProjectAsset.resource_entry_id.in_([entry_id] if entry_id else []),
                ProjectAsset.source_id.in_(
                    [*item_ids, *asset_ids, *([entry_id] if entry_id else [])]
                ),
            )
        )
        .limit(1)
    ):
        raise LibraryDeletionConflict("该资料仍被视频工程引用，请先在工程中移除引用后再删除")
    sync_rows = _freeze_jobs(session, WeKnoraSyncJob, entry_id) if entry_id else []
    archives = _freeze_jobs(session, OriginalArchive, entry_id) if entry_id else []
    keys = sorted({row.storage_key for row in [*items, *assets, *archives] if row.storage_key})
    catalog_sources = [
        {
            "owner": row.owner_id,
            "category": row.category,
            "key": row.storage_key,
            "text_hash": hashlib.sha256(row.text_content.encode("utf-8")).hexdigest(),
        }
        for row in items
        if row.storage_key and row.text_content
    ]
    remote = []
    for row in sync_rows:
        if row.remote_id or row.upload_attempted:
            remote.append(
                {
                    "connection_id": row.connection_id,
                    "knowledge_base_id": row.knowledge_base_id,
                    "id": row.remote_id,
                    "marker": f"content-agent:{row.entry_id}:{row.version_id}",
                    "original": False,
                }
            )
    for row in archives:
        if row.remote_id or row.upload_attempted:
            remote.append(
                {
                    "connection_id": row.connection_id,
                    "knowledge_base_id": row.knowledge_base_id,
                    "id": row.remote_id,
                    "marker": row.remote_source_marker or row.source_marker,
                    "original": True,
                }
            )
    removed_ids = {*item_ids, *([entry_id] if entry_id else [])}
    # JSON is a CLOB on Dameng: membership must be checked in Python.
    for task in session.scalars(select(ContentTask)):
        refs = json.loads(task.knowledge_resource_ids_json or "[]")
        filtered = [ref for ref in refs if ref not in removed_ids]
        changed = False
        if filtered != refs:
            task.knowledge_resource_ids_json = _json(filtered)
            changed = True
        if entry_id and task.product_resource_id == entry_id:
            task.product_resource_id = None
            changed = True
        if changed:
            task.version += 1
    session.flush()
    if entry_id:
        session.execute(
            update(ResourceEntry).where(ResourceEntry.parent_id == entry_id).values(parent_id=None)
        )
        versions = select(ResourceVersion.id).where(ResourceVersion.entry_id == entry_id)
        session.execute(delete(ResourceChunk).where(ResourceChunk.version_id.in_(versions)))
        for model in (WeKnoraSyncJob, OriginalArchive, ExternalRef, AssetRef):
            session.execute(delete(model).where(model.entry_id == entry_id))
    session.execute(delete(LibraryItem).where(LibraryItem.id.in_(item_ids)))
    if entry_id:
        session.execute(delete(ResourceVersion).where(ResourceVersion.entry_id == entry_id))
        session.execute(delete(ResourceEntry).where(ResourceEntry.id == entry_id))
    cleanup = Job(
        task_id=entry_id or item.id,
        owner_id=deleted_by,
        kind="library_cleanup",
        status="cleanup_pending",
        result_json=_json(
            {"local_keys": keys, "remote": remote, "catalog_sources": catalog_sources}
        ),
    )
    session.add(cleanup)
    session.flush()
    return cleanup.id


def _local_referenced(session, key):
    return any(
        session.scalar(select(model.id).where(model.storage_key == key).limit(1))
        for model in (LibraryItem, AssetRef, OriginalArchive, Artifact)
    )


def _clean_remote(session_factory, client, target):
    if client.connection_id != target["connection_id"]:
        raise LibraryDeletionConflict("清理等待恢复原知识库连接")
    document_id = target["id"]
    if not document_id:
        doc = client.find_document_by_source(target["marker"])
        if doc is None:
            # An upload with an unknown outcome may still appear later. Keep the marker.
            raise LibraryDeletionConflict("等待核实原上传结果")
        document_id = target["id"] = doc.id
    with session_factory() as session:
        shared = session.scalar(
            select(OriginalArchive.id)
            .where(
                OriginalArchive.connection_id == target["connection_id"],
                OriginalArchive.remote_id == document_id,
            )
            .limit(1)
        )
        shared_index = session.scalar(
            select(ExternalRef.id)
            .where(
                ExternalRef.system == "weknora",
                ExternalRef.external_id == document_id,
            )
            .limit(1)
        )
        if shared or shared_index:
            return True
    try:
        doc = client.get_document(document_id)
    except Exception as exc:
        if getattr(exc, "status_code", None) == 404:
            return True
        raise
    if (
        doc.knowledge_base_id != target["knowledge_base_id"]
        or doc.metadata.get("local_source_marker") != target["marker"]
    ):
        raise LibraryDeletionConflict("远端文件归属不匹配，清理已暂停")
    client.delete_document(document_id)
    return False  # Confirm disappearance on the next pass, including async remote deletion.


def run_library_cleanup(session_factory, asset_store, client_factory, limit=10):
    """Resume safely after restart; an unavailable remote never rolls back local deletion."""
    now = datetime.now(UTC)
    with session_factory() as session:
        ids = list(
            session.scalars(
                select(Job.id)
                .where(
                    Job.kind == "library_cleanup",
                    Job.status == "cleanup_pending",
                    Job.available_at <= now,
                    or_(Job.lease_expires_at.is_(None), Job.lease_expires_at <= now),
                )
                .limit(limit)
            )
        )
    for job_id in ids:
        token = uuid4().hex
        with session_factory() as session:
            changed = session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.status == "cleanup_pending",
                    or_(Job.lease_expires_at.is_(None), Job.lease_expires_at <= now),
                )
                .values(lease_token=token, lease_expires_at=now + timedelta(minutes=10))
            )
            session.commit()
            if changed.rowcount != 1:
                continue
            job = session.get(Job, job_id)
            data = json.loads(job.result_json)
        error = None
        try:
            local = getattr(asset_store, "local_store", asset_store)
            sources = data.get("catalog_sources", [])
            if sources:
                with session_factory() as session:
                    sources = [item for item in sources if not _local_referenced(session, item["key"])]
                from .provider_dependencies import get_knowledge_catalog_port

                catalog_ids = {
                    hashlib.sha256(
                        "\0".join(
                            [
                                item["owner"],
                                item["category"],
                                str(local.path_for(item["key"]).resolve()),
                                item["text_hash"],
                            ]
                        ).encode("utf-8")
                    ).hexdigest()[:36]
                    for item in sources
                }
                get_knowledge_catalog_port().remove_documents(catalog_ids)
                data["catalog_sources"] = []
            for key in list(data["local_keys"]):
                with session_factory() as session:
                    referenced = _local_referenced(session, key)
                if not referenced:
                    local.path_for(key).unlink(missing_ok=True)
                data["local_keys"].remove(key)
            for target in list(data["remote"]):
                if _clean_remote(session_factory, client_factory(), target):
                    data["remote"].remove(target)
        except Exception:
            error = "附件清理等待重试；请检查存储及原知识库连接"
        with session_factory() as session:
            job = session.get(Job, job_id)
            if job is None or job.lease_token != token:
                continue
            job.result_json = _json(data)
            job.status = (
                "cleanup_pending"
                if data["local_keys"] or data["remote"] or data.get("catalog_sources")
                else "succeeded"
            )
            job.progress = 100 if job.status == "succeeded" else 0
            job.error = error
            job.attempt += 1
            job.available_at = datetime.now(UTC) + timedelta(seconds=30)
            job.lease_token = job.lease_expires_at = None
            session.commit()


def library_cleanup_loop(session_factory, asset_store, client_factory, stop_event):
    while not stop_event.is_set():
        try:
            run_library_cleanup(session_factory, asset_store, client_factory)
        except Exception:
            logging.getLogger(__name__).warning("资料附件清理暂未完成，稍后继续")
        stop_event.wait(15)
