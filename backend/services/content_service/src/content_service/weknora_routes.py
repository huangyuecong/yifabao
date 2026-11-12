"""Authorized status and retry controls for the external knowledge index."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from agent_core import Identity

from .access_control import get_content_identity, has_permission
from .db import get_session
from .models import LibraryItem, ResourceEntry
from .provider_dependencies import get_runtime_settings, get_weknora_client
from .services import audit

router = APIRouter(prefix="/api/v1/library/items", tags=["library"])


def knowledge_sync_status(session: Session, entry: ResourceEntry | None, item: LibraryItem | None = None) -> dict | None:
    settings = get_runtime_settings()
    configured = all((settings.weknora_base_url, settings.weknora_api_key, settings.weknora_knowledge_base_id))
    if settings.knowledge_provider != "weknora" and not configured:
        return None
    from .weknora_sync import get_sync_status

    if settings.knowledge_provider != "weknora":
        result = {"status": "local", "can_retry": False}
    elif entry is None:
        result = {"status": "not_synced", "can_retry": False, "error": "请编辑资料并保存新版本"}
    elif entry.data_level not in settings.weknora_allowed_data_levels:
        result = {"status": "excluded", "can_retry": False, "error": "该数据等级未获准同步至知识平台"}
    else:
        result = get_sync_status(session, entry.id, get_weknora_client().connection_id)
    result["sync_enabled"] = settings.knowledge_provider == "weknora" and settings.weknora_sync_enabled
    if not result["sync_enabled"]:
        result["can_retry"] = False
    if item is not None:
        result["original"] = original_archive_status(session, item)
    return result


def original_archive_status(session: Session, item: LibraryItem) -> dict:
    settings = get_runtime_settings()
    if not item.storage_key:
        return {"status": "no_file", "can_retry": False}
    from .original_archives import get_archive_status

    state = get_archive_status(session, item.storage_key, get_weknora_client().connection_id)
    # Internal storage identifiers are unnecessary in this public status response.
    state.pop("storage_key", None)
    state.pop("archive_id", None)
    if state["status"] == "not_archived":
        if item.data_level not in settings.weknora_allowed_data_levels:
            state["status"] = "excluded"
        elif not settings.weknora_originals_enabled:
            state["status"] = "disabled"
    if not settings.weknora_originals_enabled or item.data_level not in settings.weknora_allowed_data_levels:
        state["can_retry"] = False
    state["sync_enabled"] = settings.weknora_originals_enabled
    return state


def _item(session: Session, item_id: str, identity: Identity, *, write: bool = False) -> LibraryItem:
    item = session.get(LibraryItem, item_id)
    if item is None or not item.active:
        raise HTTPException(404, "资料不存在或已停用")
    if (item.owner_id != identity.user_id and not has_permission(identity, "library.manage")
            and (write or item.data_level != "public")):
        raise HTTPException(403, "无权操作该资料")
    return item


@router.get("/{item_id}/knowledge-sync")
def read_knowledge_sync(
    item_id: str, session: Session = Depends(get_session),
    identity: Identity = Depends(get_content_identity),
) -> dict:
    item = _item(session, item_id, identity)
    entry = session.get(ResourceEntry, item.resource_entry_id) if item.resource_entry_id else None
    return knowledge_sync_status(session, entry, item) or {"status": "local", "can_retry": False}


@router.post("/{item_id}/knowledge-sync/retry")
def retry_knowledge_sync(
    item_id: str, session: Session = Depends(get_session),
    identity: Identity = Depends(get_content_identity),
) -> dict:
    item = _item(session, item_id, identity, write=True)
    settings = get_runtime_settings()
    if settings.knowledge_provider != "weknora" or not settings.weknora_sync_enabled:
        raise HTTPException(409, "知识平台同步未启用")
    entry = session.get(ResourceEntry, item.resource_entry_id) if item.resource_entry_id else None
    if entry is None or not entry.active:
        raise HTTPException(409, "资料缺少有效的当前版本")
    if entry.data_level not in settings.weknora_allowed_data_levels:
        raise HTTPException(403, "该数据等级未获准同步至知识平台")
    from .weknora_sync import retry_sync_job

    try:
        retry_sync_job(session, entry.id, get_weknora_client().connection_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    audit(session, identity.user_id, "knowledge.sync_retried", "library_item", item.id)
    session.commit()
    return knowledge_sync_status(session, entry, item)


@router.post("/{item_id}/original-archive/retry")
def retry_original_archive(
    item_id: str, session: Session = Depends(get_session),
    identity: Identity = Depends(get_content_identity),
) -> dict:
    item = _item(session, item_id, identity, write=True)
    settings = get_runtime_settings()
    if not settings.weknora_originals_enabled:
        raise HTTPException(409, "原件托管未启用")
    if item.data_level not in settings.weknora_allowed_data_levels:
        raise HTTPException(403, "该数据等级未获准托管至知识平台")
    if not item.storage_key:
        raise HTTPException(409, "该资料没有附件")
    from .original_archives import retry_archive

    try:
        retry_archive(session, item.storage_key, get_weknora_client().connection_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    audit(session, identity.user_id, "knowledge.original_retried", "library_item", item.id)
    session.commit()
    entry = session.get(ResourceEntry, item.resource_entry_id) if item.resource_entry_id else None
    return knowledge_sync_status(session, entry, item)
