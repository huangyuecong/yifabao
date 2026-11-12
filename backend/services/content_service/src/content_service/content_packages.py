"""Approved product content snapshots, independent of destinations and AI calls."""

import hashlib
import json
import shutil
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import select

from . import automation_review as review
from . import db
from .automation_platforms import get_platform
from .models import AssetRef, LibraryItem, ResourceEntry, ResourceVersion, SensitiveRule
from .provider_dependencies import get_asset_store_port, get_runtime_settings


def archive_creation(session, run, payload):
    """Called in the approval transaction: either approval and library commit, or neither."""
    if not payload.get('listing_title') or not payload.get('listing_description'):
        raise HTTPException(409, '商品描述尚未生成完整，请修改文案后再审核')
    review.ensure_approved(payload, json.loads(run.config_json), force=True)
    ids = review.archive_to_library(session, run, payload)
    session.flush()
    package_id = ids[0]
    source = payload.get('_sources', {}).get('product') or {}
    product_id = source.get('entry_id')
    if not product_id:
        # A custom product becomes a reusable product record only on human approval.
        product_id = str(uuid5(NAMESPACE_URL, f'creation-product:{run.id}'))
        if session.get(ResourceEntry, product_id) is None:
            entry = ResourceEntry(id=product_id, owner_id=run.owner_id, resource_kind='product',
                                  title=source.get('name') or payload['listing_title'], category='product')
            session.add(entry)
            session.flush()
            version = ResourceVersion(entry_id=product_id, version=1, created_by=run.owner_id,
                                      text_content=source.get('text') or payload['listing_description'])
            session.add(version)
            session.flush()
            entry.current_version_id = version.id
            session.add(LibraryItem(id=product_id, owner_id=run.owner_id, resource_entry_id=product_id,
                                    category='product', name=entry.title, text_content=version.text_content))
    count = len(payload.get('image_paths', []))
    video_id = ids[count + 1] if payload.get('video_path') else None
    original_ids = ids[count + 1 + bool(video_id):]
    metadata = {
        'kind': 'approved_content', 'schema': 1, 'run_id': run.id, 'revision': review.revision(payload),
        'product_id': product_id, 'product_name': source.get('name') or payload['listing_title'],
        'title': payload['title'], 'description': payload['description'],
        'listing_title': payload['listing_title'], 'listing_description': payload['listing_description'],
        'category': payload.get('category') or '', 'model': payload.get('model'),
        'image_model': payload.get('image_model'), 'image_ids': ids[1:count + 1],
        'original_image_ids': original_ids, 'video_id': video_id,
        'approved_at': payload['_review']['approved_at'], 'assets': {},
    }
    for identifier in ids[1:]:
        entry = session.get(ResourceEntry, identifier)
        entry.parent_id = product_id if identifier in original_ids else package_id
        asset = session.scalar(select(AssetRef).where(AssetRef.entry_id == identifier,
                                                     AssetRef.version_id == entry.current_version_id))
        metadata['assets'][identifier] = {'asset_id': asset.id, 'checksum': asset.checksum}
    entry = session.get(ResourceEntry, package_id)
    entry.parent_id, entry.category = product_id, 'approved_content'
    entry.tags_json = json.dumps(['已审核', '产品内容'], ensure_ascii=False)
    entry.metadata_json = json.dumps(metadata, ensure_ascii=False)
    version = session.get(ResourceVersion, entry.current_version_id)
    version.metadata_json = entry.metadata_json
    item = session.get(LibraryItem, package_id)
    item.category = 'approved_content'
    payload['_package_id'] = package_id
    return package_id


def package_entry(session, identifier, owner):
    entry = session.scalar(select(ResourceEntry).where(ResourceEntry.id == identifier,
        ResourceEntry.owner_id == owner, ResourceEntry.active.is_(True), ResourceEntry.category == 'approved_content'))
    if entry is None:
        raise HTTPException(404, '已审核内容不存在或已停用')
    # Version 1 is the immutable approved snapshot, independent of later library edits.
    version = session.scalar(select(ResourceVersion).where(ResourceVersion.entry_id == entry.id,
                                                          ResourceVersion.version == 1))
    data = json.loads(version.metadata_json) if version else {}
    if data.get('kind') != 'approved_content':
        raise HTTPException(409, '该内容没有完整审核记录')
    return entry, data


def document(entry, data):
    return {'id': entry.id, **{key: data.get(key) for key in (
        'run_id', 'revision', 'product_id', 'product_name', 'title', 'description', 'listing_title',
        'listing_description', 'category', 'image_ids', 'original_image_ids', 'video_id', 'approved_at',
    )}}


def list_packages(session, owner):
    entries = session.scalars(select(ResourceEntry).where(ResourceEntry.owner_id == owner,
        ResourceEntry.active.is_(True), ResourceEntry.category == 'approved_content')
        .order_by(ResourceEntry.created_at.desc(), ResourceEntry.id).limit(200)).all()
    return [document(*package_entry(session, entry.id, owner)) for entry in entries]


def prepare_package(config, identity):
    """Use exactly the reviewed copy and bytes. Never regenerate while publishing."""
    settings, store = get_runtime_settings(), get_asset_store_port()
    directory = settings.data_dir / 'automation-media' / uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    try:
        with db.SessionLocal() as session:
            entry, data = package_entry(session, config['approved_content_id'], identity.user_id)
            listing = config['purpose'] == 'listing'
            image_ids = data['original_image_ids'] if listing else data['image_ids']
            video_id = None if listing else data['video_id']
            title = data['listing_title'] if listing else data['title']
            description = data['listing_description'] if listing else data['description']
            specs = [get_platform(platform) for platform in config['platforms']]
            if len(title) > min(spec.title_limit for spec in specs) or len(description) > min(spec.description_limit for spec in specs):
                raise HTTPException(422, '已审核文案超出所选平台长度，请回到产品创作修改并重新审核')
            rules = session.scalars(select(SensitiveRule.phrase).where(SensitiveRule.active.is_(True)))
            if any(term and term.casefold() in (title + '\n' + description).casefold() for term in rules):
                raise HTTPException(422, '已审核内容命中当前合规规则，请修改后重新审核')
            if not image_ids or (not listing and not video_id):
                raise HTTPException(409, '已审核内容缺少图片或视频，请重新生成并审核')
            paths = {}
            for identifier in [*image_ids, *([video_id] if video_id else [])]:
                media = session.get(ResourceEntry, identifier)
                manifest = data['assets'][identifier]
                asset = session.get(AssetRef, manifest['asset_id'])
                if (not media or not media.active or media.owner_id != identity.user_id
                        or not asset or asset.entry_id != identifier or asset.checksum != manifest['checksum']):
                    raise HTTPException(409, '审核素材已删除或发生变化，请重新审核')
                source = store.path_for(asset.storage_key).resolve()
                root = (settings.data_dir / 'objects').resolve()
                if not source.is_relative_to(root) or not source.is_file():
                    raise HTTPException(409, '审核素材文件不可用')
                target = directory / f'{identifier}{source.suffix.lower()}'
                shutil.copyfile(source, target)
                with target.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['checksum']:
                        raise HTTPException(409, '审核素材校验失败，请重新审核')
                paths[identifier] = str(target.resolve())
            return {
                'title': title, 'description': description, 'category': config.get('category') or data['category'],
                'model': data.get('model'), 'image_model': None if listing else data.get('image_model'),
                'image_paths': [paths[identifier] for identifier in image_ids],
                'video_path': paths.get(video_id), '_approved_source_id': entry.id,
                'source_summary': {'product_name': data['product_name'], 'image_count': len(image_ids)},
                'classifications': [],
                **({key: config.get(key) for key in ('price', 'condition', 'shipping_fee', 'location')} if listing else {}),
            }
    except Exception:
        shutil.rmtree(directory)
        raise
