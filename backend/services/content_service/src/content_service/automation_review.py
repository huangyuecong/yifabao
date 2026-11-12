"""Versioned human approval, bound to the exact draft, files and destinations."""

import hashlib
import json
from pathlib import Path

from fastapi import HTTPException

from .models import utcnow
from .provider_dependencies import get_runtime_settings


def required(config):
    return config.get('operation', 'publish') == 'publish' and config.get('require_review', False)


def revision(payload):
    return payload.get('_review', {}).get('revision', 1)


def check_version(payload, expected):
    if expected != revision(payload):
        raise HTTPException(409, '草稿版本已变化，请刷新并检查最新内容后再操作')


def media_path(value):
    root = (get_runtime_settings().data_dir / 'automation-media').resolve()
    path = Path(value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(409, '草稿素材已失效，请重新生成')
    return path


def fingerprint(payload, config):
    fields = {key: value for key, value in payload.items() if not key.startswith('_')}
    files = []
    for value in [*payload.get('image_paths', []), *([payload['video_path']] if payload.get('video_path') else []),
                  *[item['path'] for item in payload.get('_sources', {}).get('images', [])]]:
        with media_path(value).open('rb') as stream:
            files.append(hashlib.file_digest(stream, 'sha256').hexdigest())
    raw = json.dumps([fields, config, files], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def new_version(payload, previous=None, feedback='首次生成'):
    history = list((previous or {}).get('_history', []))
    if previous:
        history.append({key: value for key, value in previous.items() if key not in {'_history', '_revision_request'}})
    payload['_history'] = history[-20:]
    payload['_review'] = {'revision': revision(previous) + 1 if previous else 1,
                          'state': 'pending', 'feedback': feedback, 'created_at': utcnow().isoformat()}
    payload.pop('_revision_request', None)
    payload.pop('_library_ids', None)
    payload.pop('_package_id', None)
    return payload


def approve(payload, config, owner, expected):
    check_version(payload, expected)
    if config.get('purpose') == 'creation' and (payload.get('_generation_error') or not payload.get('image_paths') or not payload.get('video_path')):
        raise HTTPException(409, '图片或视频尚未生成完成，请处理失败步骤后再审核入库')
    if payload.get('video_stale'):
        raise HTTPException(409, '文案已修改，请先更新视频字幕并预览，再审核通过')
    if payload.get('_revision_request'):
        raise HTTPException(409, '内容正在修改，请等待新版本')
    payload['_review'] = {**payload.get('_review', {}), 'revision': revision(payload),
                          'state': 'approved', 'approved_by': owner,
                          'approved_at': utcnow().isoformat(), 'fingerprint': fingerprint(payload, config)}
    return payload


def ensure_approved(payload, config, *, force=False):
    if not required(config) and not force:
        return
    review = payload.get('_review', {})
    if (review.get('state') != 'approved' or not review.get('approved_by')
            or review.get('fingerprint') != fingerprint(payload, config)):
        raise HTTPException(409, '当前内容尚未通过人工审核，或审核后内容发生变化，请重新审核')


def public_review(payload):
    value = payload.get('_review', {})
    return {key: value[key] for key in ('revision', 'state', 'feedback', 'created_at', 'approved_at') if key in value}


def archive_to_library(session, run, payload):
    """Idempotent per-version library snapshots; only called after human approval."""
    from uuid import NAMESPACE_URL, uuid5

    from .models import AssetRef, LibraryItem, ResourceEntry, ResourceVersion
    from .provider_dependencies import get_asset_store_port

    store = get_asset_store_port()
    content = [('document', None, '文案')]
    content += [('image', path, f'图片 {index + 1}') for index, path in enumerate(payload.get('image_paths', []))]
    if payload.get('video_path'):
        content.append(('video', payload['video_path'], '视频'))
    creation = json.loads(getattr(run, 'config_json', '{}')).get('purpose') == 'creation'
    if creation:
        content += [('image', item['path'], f'商品原图 {index + 1}')
                    for index, item in enumerate(payload.get('_sources', {}).get('images', []))]
    ids = []
    for index, (kind, path, label) in enumerate(content):
        identifier = str(uuid5(NAMESPACE_URL, f'automation:{run.id}:{revision(payload)}:{index}'))
        ids.append(identifier)
        if session.get(ResourceEntry, identifier):
            continue
        title = f"{payload['title']} · {label} · 第 {revision(payload)} 版"[:255]
        text = payload['description']
        metadata = json.dumps({'automation_run_id': run.id, 'review_revision': revision(payload), 'reviewed': True})
        entry = ResourceEntry(id=identifier, owner_id=run.owner_id, resource_kind=kind,
                              title=title, category=kind, data_level='internal', metadata_json=metadata)
        session.add(entry)
        session.flush()
        version = ResourceVersion(entry_id=identifier, version=1, text_content=text,
                                  created_by=run.owner_id, metadata_json=metadata)
        session.add(version)
        session.flush()
        entry.current_version_id = version.id
        key = None
        if path:
            local = media_path(path)
            key = f'automation-approved/{identifier}{local.suffix.lower()}'
            media_type = 'image/jpeg' if kind == 'image' else 'video/mp4' if local.suffix.lower() == '.mp4' else 'video/quicktime'
            with local.open('rb') as stream:
                stored = store.put_stream(key, stream, media_type)
            session.add(AssetRef(entry_id=identifier, version_id=version.id, storage_key=key,
                                 media_type=media_type, size_bytes=stored.size, checksum=stored.checksum_sha256))
        session.add(LibraryItem(id=identifier, owner_id=run.owner_id, resource_entry_id=identifier,
                                category=kind, name=title, text_content=text, storage_key=key, data_level='internal'))
    payload['_library_ids'] = ids
    return ids
