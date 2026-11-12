import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_content_library_upload_flow import PNG
from test_content_video_editor_api import _wait_for_job

from content_service import main
from content_service.db import SessionLocal
from content_service.library_deletion import run_library_cleanup
from content_service.media_adapters import MockAiVideoGenerationPort, MockImageGenerationPort
from content_service.models import (
    AssetRef,
    ContentTask,
    ExternalRef,
    Job,
    LibraryItem,
    OriginalArchive,
    ResourceChunk,
    ResourceEntry,
    ResourceVersion,
    WeKnoraSyncJob,
)
from content_service.provider_dependencies import (
    get_ai_video_generation_port,
    get_image_generation_port,
)


def upload(client, headers, kind="product", category="product"):
    r = client.post(
        "/api/v1/library/items/upload",
        headers=headers,
        params={"resource_kind": kind, "category": category, "name": "典藏2008产品测试"},
        data={"text_content": "典藏2008，53%vol，500ml。瓶身正面为圆形金色标识。"},
        files={"file": ("product.png", PNG, "image/png")},
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("mode", ["copy", "image", "video", "full"])
@pytest.mark.parametrize("legacy", [False, True])
def test_product_attachment_works_across_creation_modes(content_client, user_headers, mode, legacy):
    item = upload(content_client, user_headers, kind="image" if legacy else "product")
    image_provider = MockImageGenerationPort()
    image_requests = []
    original_generate = image_provider.generate
    image_provider.generate = lambda req: (image_requests.append(req), original_generate(req))[1]
    video_provider = MockAiVideoGenerationPort()
    main.app.dependency_overrides[get_image_generation_port] = lambda: image_provider
    main.app.dependency_overrides[get_ai_video_generation_port] = lambda: video_provider
    try:
        r = content_client.post(
            "/api/v1/tasks",
            headers=user_headers,
            json={
                "mode": mode,
                "name": "产品流程验证",
                "topic": "团圆品牌宣传",
                "audience": "成年消费者",
                "product_resource_id": item["id"] if legacy else item["resource_entry_id"],
            },
        )
        assert r.status_code == 200, r.text
        task = r.json()
        assert task["product_resource_id"] == item["resource_entry_id"]
        assert task["knowledge_resource_ids"] == [item["resource_entry_id"]]
        prefix = f"/api/v1/tasks/{task['id']}"
        assert content_client.get(prefix + "/workflow", headers=user_headers).status_code == 200
        if mode in {"copy", "full"}:
            r = content_client.post(
                prefix + "/generate-copy", headers=user_headers, json={"count": 1}
            )
            assert r.status_code == 200, r.text
        if mode in {"image", "full"}:
            r = content_client.post(
                prefix + "/stages/image/generate",
                headers=user_headers,
                json={"count": 2, "parameters": {"prompt": "团圆海报"}},
            )
            assert r.status_code == 200, r.text
            assert len(r.json()["artifacts"]) == 2
            assert image_requests[-1].reference_images == (PNG,)
        if mode in {"video", "full"}:
            project = content_client.post(prefix + "/video-project", headers=user_headers)
            assert project.status_code == 200, project.text
            project = project.json()
            r = content_client.post(
                f"/api/v1/video-projects/{project['id']}/generate-images",
                headers=user_headers,
                json={
                    "expected_revision": project["current_revision"],
                    "prompt": "窖藏产品画面",
                    "count": 2,
                },
            )
            assert r.status_code == 200, r.text
            assert image_requests[-1].reference_images == (PNG,)
            project = content_client.get(
                f"/api/v1/video-projects/{project['id']}", headers=user_headers
            ).json()
            r = content_client.post(
                f"/api/v1/video-projects/{project['id']}/generate-video",
                headers=user_headers,
                json={
                    "expected_revision": project["current_revision"],
                    "prompt": "窖藏产品团圆短片",
                    "idempotency_key": uuid4().hex,
                },
            )
            assert r.status_code == 200, r.text
            job = _wait_for_job(content_client, user_headers, r.json()["id"])
            assert job["status"] == "succeeded", job
    finally:
        main.app.dependency_overrides.pop(get_image_generation_port, None)
        main.app.dependency_overrides.pop(get_ai_video_generation_port, None)


def test_product_alias_access_control_and_document(content_client, user_headers, other_headers):
    item = content_client.post(
        "/api/v1/library/items",
        headers=user_headers,
        json={
            "name": "产品说明",
            "category": "产品资料",
            "resource_kind": "document",
            "text_content": "规格500ml",
        },
    ).json()
    body = {"mode": "copy", "name": "使用产品文档", "product_resource_id": item["id"]}
    assert content_client.post("/api/v1/tasks", headers=other_headers, json=body).status_code == 403
    r = content_client.post("/api/v1/tasks", headers=user_headers, json=body)
    assert r.status_code == 200, r.text
    r = content_client.post(
        f"/api/v1/tasks/{r.json()['id']}/generate-copy", headers=user_headers, json={"count": 1}
    )
    assert r.status_code == 200, r.text


def test_hard_delete_all_versions_and_task_selection(content_client, user_headers, other_headers):
    item = upload(content_client, user_headers)
    r = content_client.post(
        f"/api/v1/library/items/{item['id']}/versions",
        headers=user_headers,
        json={
            "expected_version": 1,
            "resource_kind": "product",
            "category": "product",
            "name": item["name"],
            "text_content": "更新产品资料",
            "metadata": item["metadata"],
        },
    )
    assert r.status_code == 200, r.text
    newer = r.json()
    entry_id = item["resource_entry_id"]
    task = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "copy",
            "name": "保留已有内容",
            "product_resource_id": entry_id,
        },
    ).json()
    with SessionLocal() as session:
        versions = list(
            session.scalars(select(ResourceVersion.id).where(ResourceVersion.entry_id == entry_id))
        )
        session.add(
            WeKnoraSyncJob(
                entry_id=entry_id,
                version_id=versions[0],
                connection_id="qa",
                knowledge_base_id="kb",
            )
        )
        session.add(ExternalRef(entry_id=entry_id, system="test", external_id=uuid4().hex))
        session.commit()
    assert (
        content_client.delete(
            f"/api/v1/library/items/{newer['id']}", headers=other_headers
        ).status_code
        == 403
    )
    r = content_client.delete(f"/api/v1/library/items/{newer['id']}", headers=user_headers)
    assert r.status_code == 200, r.text
    assert r.json()["deletion_mode"] == "hard"
    with SessionLocal() as session:
        assert session.get(ResourceEntry, entry_id) is None
        for model in (ResourceVersion, AssetRef, ExternalRef, WeKnoraSyncJob, OriginalArchive):
            assert session.scalar(select(model.id).where(model.entry_id == entry_id)) is None
        assert not session.scalars(
            select(ResourceChunk.id).where(ResourceChunk.version_id.in_(versions))
        ).all()
        assert session.get(LibraryItem, item["id"]) is None
        assert session.get(LibraryItem, newer["id"]) is None
        stored = session.get(ContentTask, task["id"])
        assert stored.product == item["name"] and stored.product_resource_id is None
        assert json.loads(stored.knowledge_resource_ids_json) == []
    assert content_client.get(item["download_url"], headers=user_headers).status_code == 404
    run_library_cleanup(SessionLocal, main.get_asset_store_port(), lambda: None)
    with SessionLocal() as session:
        assert session.get(Job, r.json()["cleanup_job_id"]).status == "succeeded"
    assert not main.get_asset_store_port().path_for(item["storage_key"]).exists()
    assert (
        content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-copy", headers=user_headers, json={"count": 1}
        ).status_code
        == 200
    )


def test_hard_delete_rejects_inflight_sync_without_partial_changes(content_client, user_headers):
    item = upload(content_client, user_headers)
    with SessionLocal() as session:
        session.add(
            WeKnoraSyncJob(
                entry_id=item["resource_entry_id"],
                version_id=item["resource_version_id"],
                connection_id="qa",
                knowledge_base_id="kb",
                status="processing",
                lease_token="in-flight",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
        session.commit()
    r = content_client.delete(f"/api/v1/library/items/{item['id']}", headers=user_headers)
    assert r.status_code == 409, r.text
    assert content_client.get(item["download_url"], headers=user_headers).status_code == 200


@pytest.mark.parametrize("shared", [False, True])
def test_remote_cleanup_verifies_ownership_retries_and_preserves_shared_originals(
    content_client, user_headers, shared
):
    item = upload(content_client, user_headers)
    other = upload(content_client, user_headers)
    remote_id = uuid4().hex
    with SessionLocal() as session:
        for resource in [item, other] if shared else [item]:
            session.add(
                OriginalArchive(
                    storage_key=resource["storage_key"],
                    entry_id=resource["resource_entry_id"],
                    connection_id="qa",
                    knowledge_base_id="kb",
                    checksum_sha256="a" * 64,
                    size_bytes=len(PNG),
                    source_marker="own-" + resource["id"],
                    remote_source_marker="original-marker",
                    remote_id=remote_id,
                    status="ready",
                )
            )
        session.commit()
    r = content_client.delete(f"/api/v1/library/items/{item['id']}", headers=user_headers)
    assert r.status_code == 200, r.text

    class Client:
        connection_id = "qa"
        marker = "wrong-marker"
        deleted = False
        calls = 0

        def get_document(self, ident):
            assert ident == remote_id
            if self.deleted:
                exc = ValueError("gone")
                exc.status_code = 404
                raise exc
            return SimpleNamespace(
                id=ident, knowledge_base_id="kb", metadata={"local_source_marker": self.marker}
            )

        def delete_document(self, ident):
            assert ident == remote_id
            self.calls += 1
            self.deleted = True

    client = Client()
    cleanup_id = r.json()["cleanup_job_id"]

    def clean():
        with SessionLocal() as session:
            session.get(Job, cleanup_id).available_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
        run_library_cleanup(SessionLocal, main.get_asset_store_port(), lambda: client)

    clean()
    assert client.calls == 0
    with SessionLocal() as session:
        assert session.get(Job, cleanup_id).status == ("succeeded" if shared else "cleanup_pending")
    client.marker = "original-marker"
    clean()
    clean()
    assert client.calls == (0 if shared else 1)
    with SessionLocal() as session:
        assert session.get(Job, cleanup_id).status == "succeeded"
    assert content_client.get(other["download_url"], headers=user_headers).status_code == 200

def test_hard_delete_removes_extracted_catalog_copy(content_client, user_headers):
    from content_service.provider_dependencies import get_knowledge_catalog_port
    r = content_client.post('/api/v1/library/items/upload', headers=user_headers,
                            params={'resource_kind': 'product', 'category': 'product', 'name': 'unique-delete-'+uuid4().hex},
                            files={'file': ('manual.txt', ('唯一删除内容'+uuid4().hex).encode(), 'text/plain')})
    assert r.status_code == 200, r.text
    item = r.json()
    catalog = get_knowledge_catalog_port()
    before = catalog.index_path.read_text(encoding='utf-8')
    assert item['text_content'] in before
    result = content_client.delete(f"/api/v1/library/items/{item['id']}", headers=user_headers)
    assert result.status_code == 200, result.text
    run_library_cleanup(SessionLocal, main.get_asset_store_port(), lambda: None)
    assert item['text_content'] not in catalog.index_path.read_text(encoding='utf-8')


def test_hard_delete_keeps_project_references_intact(content_client, user_headers):
    from content_service.models import ProjectAsset
    item = upload(content_client, user_headers)
    task = content_client.post('/api/v1/tasks', headers=user_headers, json={'mode': 'video', 'name': '正在使用素材'}).json()
    project = content_client.post(f"/api/v1/tasks/{task['id']}/video-project", headers=user_headers).json()
    with SessionLocal() as session:
        session.add(ProjectAsset(project_id=project['id'], source_type='library_item', source_id=item['id'],
                                 library_item_id=item['id'], resource_entry_id=item['resource_entry_id'], created_by=user_headers['X-User-Id']))
        session.commit()
    result = content_client.delete(f"/api/v1/library/items/{item['id']}", headers=user_headers)
    assert result.status_code == 409, result.text
    assert content_client.get(item['download_url'], headers=user_headers).status_code == 200

def test_hard_delete_obeys_foreign_keys(tmp_path):
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session

    from content_service.db import Base
    from content_service.library_deletion import delete_library_records
    engine = create_engine('sqlite:///' + str(tmp_path / 'foreign-keys.db'))
    @event.listens_for(engine, 'connect')
    def enforce_keys(connection, record):
        connection.execute('PRAGMA foreign_keys=ON')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        entry = ResourceEntry(owner_id='owner', resource_kind='product', title='资料', category='product')
        session.add(entry)
        session.flush()
        version = ResourceVersion(entry_id=entry.id, version=1, text_content='事实', created_by='owner')
        session.add(version)
        session.flush()
        entry.current_version_id = version.id
        item = LibraryItem(owner_id='owner', category='product', name='资料', resource_entry_id=entry.id)
        session.add_all([
            item, ResourceChunk(version_id=version.id, sequence=0, text='事实'),
            AssetRef(entry_id=entry.id, version_id=version.id, storage_key='library/test/manual.txt'),
            ExternalRef(entry_id=entry.id, system='test', external_id='test'),
            WeKnoraSyncJob(entry_id=entry.id, version_id=version.id, connection_id='qa', knowledge_base_id='kb'),
            OriginalArchive(entry_id=entry.id, storage_key='library/test/manual.txt', connection_id='qa',
                            knowledge_base_id='kb', checksum_sha256='a'*64, size_bytes=3, source_marker='original'),
            ContentTask(owner_id='owner', name='已有任务', product_resource_id=entry.id),
        ])
        session.commit()
        deleted_id = entry.id
        delete_library_records(session, item, deleted_by='owner')
        session.commit()
        assert session.get(ResourceEntry, deleted_id) is None
        assert session.scalar(select(ContentTask)).product_resource_id is None
    engine.dispose()
