from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.dml import Update

SRC = Path(__file__).resolve().parents[1] / "services" / "content_service" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from content_service.db import Base
from content_service.media_adapters import LocalAssetStore
from content_service.models import (
    AssetRef,
    ExternalRef,
    LibraryItem,
    ResourceEntry,
    ResourceVersion,
    WeKnoraSyncJob,
)
from content_service.weknora import WeKnoraDocument, WeKnoraError
from content_service.weknora_sync import (
    WeKnoraSyncRunner,
    get_sync_status,
    retry_sync_job,
    source_marker,
)


class FakeClient:
    connection_id = "test-connection"
    knowledge_base_id = "kb-test"
    timeout_seconds = 1

    def __init__(self):
        self.documents = {}
        self.posts = []
        self.deletes = []
        self.response_lost = False
        self.upload_failed = False
        self.parse_status = "processing"

    def ingest_text(self, *, title, text, source_marker):
        self.posts.append((title, text, source_marker))
        if self.upload_failed:
            raise WeKnoraError("timeout")
        doc = WeKnoraDocument(
            id=f"remote-{len(self.posts)}", knowledge_base_id=self.knowledge_base_id,
            title=title, parse_status=self.parse_status,
            metadata={"local_source_marker": source_marker},
        )
        self.documents[doc.id] = doc
        if self.response_lost:
            raise WeKnoraError("timeout")
        return doc

    def find_document_by_source(self, marker, *, stop_event=None):
        return next((d for d in self.documents.values()
                     if d.metadata.get("local_source_marker") == marker), None)

    def get_document(self, remote_id):
        if remote_id not in self.documents:
            raise WeKnoraError("not_found", status_code=404)
        return self.documents[remote_id]

    def delete_document(self, remote_id):
        self.deletes.append(remote_id)
        return {"task_id": "deletion-1"}


@pytest.fixture
def setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sync.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    client = FakeClient()
    runner = WeKnoraSyncRunner(factory, client, interval_seconds=1)
    yield factory, client, runner
    engine.dispose()


def seed(factory, *, formal=True, level="internal", text="经审核的产品资料", title="雅酿资料"):
    with factory() as session:
        entry = ResourceEntry(owner_id="owner", resource_kind="document", title=title,
                              category="product", data_level=level)
        session.add(entry)
        session.flush()
        version = ResourceVersion(entry_id=entry.id, version=1, text_content=text,
                                  created_by="owner")
        session.add(version)
        session.flush()
        entry.current_version_id = version.id
        if formal:
            session.add(LibraryItem(owner_id="owner", category="product", name=title,
                                    resource_entry_id=entry.id, text_content=text))
        session.commit()
        return entry.id, version.id


def due(factory):
    with factory() as session:
        for job in session.scalars(select(WeKnoraSyncJob)).all():
            job.available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()


def job_row(factory, version_id):
    with factory() as session:
        return session.scalar(select(WeKnoraSyncJob).where(WeKnoraSyncJob.version_id == version_id))


def test_only_formal_allowed_nonempty_library_is_enqueued(setup):
    factory, client, runner = setup
    allowed, _ = seed(factory)
    seed(factory, formal=False, title="任务参考图片")
    seed(factory, level="restricted")
    seed(factory, text="")
    result = runner.run_once()
    assert result["enqueued"] == 1
    assert len(client.posts) == 1
    with factory() as session:
        assert session.scalar(select(WeKnoraSyncJob)).entry_id == allowed


def test_parse_status_is_not_ready_until_platform_completes(setup):
    factory, client, runner = setup
    entry_id, version_id = seed(factory)
    runner.run_once()
    assert job_row(factory, version_id).status == "processing"
    doc = next(iter(client.documents.values()))
    client.documents[doc.id] = WeKnoraDocument(doc.id, doc.knowledge_base_id, doc.title,
                                             "completed", metadata=doc.metadata)
    due(factory)
    runner.run_once()
    assert len(client.posts) == 1
    assert job_row(factory, version_id).status == "ready"
    with factory() as session:
        ref = session.scalar(select(ExternalRef))
        assert json.loads(ref.metadata_json)["sync_status"] == "ready"
        assert get_sync_status(session, entry_id, client.connection_id)["status"] == "ready"


def test_response_lost_recovers_existing_document_without_duplicate_upload(setup):
    factory, client, runner = setup
    _, version_id = seed(factory)
    client.response_lost = True
    assert runner.run_once()["failed"] == 1
    assert job_row(factory, version_id).upload_attempted
    due(factory)
    runner.run_once()
    assert len(client.posts) == 1
    assert job_row(factory, version_id).status == "processing"


def test_unknown_upload_stops_instead_of_silent_duplicate_and_retry_only_looks_up(setup):
    factory, client, runner = setup
    entry_id, version_id = seed(factory)
    client.upload_failed = True
    runner.run_once()
    due(factory)
    runner.run_once()
    job = job_row(factory, version_id)
    assert job.status == "ambiguous"
    with factory() as session:
        retry_sync_job(session, entry_id, client.connection_id)
        session.commit()
    runner.run_once()
    assert len(client.posts) == 1
    assert job_row(factory, version_id).status == "ambiguous"
    runner.reset_unknown_upload(job.id)
    client.upload_failed = False
    runner.run_once()
    assert len(client.posts) == 2
    assert job_row(factory, version_id).status == "processing"


def test_explicit_reset_refuses_if_remote_document_has_appeared(setup):
    factory, client, runner = setup
    _, version_id = seed(factory)
    client.upload_failed = True
    runner.run_once()
    due(factory)
    runner.run_once()
    job = job_row(factory, version_id)
    client.documents["late"] = WeKnoraDocument(
        "late", client.knowledge_base_id, "late", "completed",
        metadata={"local_source_marker": source_marker(job.entry_id, job.version_id)},
    )
    with pytest.raises(ValueError, match="已有对应资料"):
        runner.reset_unknown_upload(job.id)
    assert job_row(factory, version_id).upload_attempted


def test_version_replacement_hides_old_mapping_and_cleans_up_asynchronously(setup):
    factory, client, runner = setup
    client.parse_status = "completed"
    entry_id, old_version_id = seed(factory)
    runner.run_once()
    old_remote = job_row(factory, old_version_id).remote_id
    with factory() as session:
        entry = session.get(ResourceEntry, entry_id)
        version = ResourceVersion(entry_id=entry_id, version=2, text_content="新版资料",
                                  created_by="owner")
        session.add(version)
        session.flush()
        entry.current_version_id = version.id
        session.commit()
        new_version_id = version.id
    runner.run_once()
    assert job_row(factory, old_version_id).status == "deleting"
    assert job_row(factory, new_version_id).status == "ready"
    assert client.deletes == [old_remote]
    with factory() as session:
        ref = session.scalar(select(ExternalRef).where(ExternalRef.external_id == old_remote))
        assert json.loads(ref.metadata_json)["sync_status"] == "obsolete"
    del client.documents[old_remote]
    due(factory)
    runner.run_once()
    assert job_row(factory, old_version_id).status == "deleted"


def test_stopped_before_upload_never_leaves_local_project(setup):
    factory, client, runner = setup
    entry_id, version_id = seed(factory)
    runner.reconcile()
    with factory() as session:
        session.get(ResourceEntry, entry_id).active = False
        session.commit()
    runner.run_once()
    assert client.posts == []
    assert job_row(factory, version_id).status == "deleted"


def test_stopped_during_upload_is_not_published_ready(setup):
    factory, client, runner = setup
    entry_id, version_id = seed(factory)
    client.parse_status = "completed"
    original = client.ingest_text

    def upload_and_stop(**kwargs):
        document = original(**kwargs)
        with factory() as session:
            session.get(ResourceEntry, entry_id).active = False
            session.commit()
        return document

    client.ingest_text = upload_and_stop
    runner.run_once(limit=1)
    assert job_row(factory, version_id).status == "deleting"
    with factory() as session:
        assert json.loads(session.scalar(select(ExternalRef)).metadata_json)["sync_status"] == "obsolete"


def test_unowned_remote_document_is_never_mapped_or_deleted(setup):
    factory, client, runner = setup
    entry_id, version_id = seed(factory)
    client.ingest_text = lambda **kw: WeKnoraDocument(
        "other", client.knowledge_base_id, "其他工作区业务", "completed",
        metadata={"local_source_marker": "another-app"},
    )
    assert runner.run_once()["failed"] == 1
    with factory() as session:
        assert session.scalar(select(ExternalRef)) is None
        session.get(ResourceEntry, entry_id).active = False
        session.commit()
    due(factory)
    runner.run_once()
    assert client.deletes == []
    assert job_row(factory, version_id).status == "ambiguous"


def test_second_worker_cannot_claim_live_lease(setup):
    factory, client, runner = setup
    seed(factory)
    runner.reconcile()
    first = runner._claim()
    assert first is not None
    other = WeKnoraSyncRunner(factory, client)
    assert other._claim() is None
    runner._process(*first)
    assert len(client.posts) == 1


def test_error_text_never_persists_remote_secrets(setup):
    factory, client, runner = setup
    _, version_id = seed(factory)

    def crash(**kwargs):
        raise RuntimeError("Bearer PRIVATE_KEY unexpected remote body")

    client.ingest_text = crash
    runner.run_once()
    assert "PRIVATE_KEY" not in job_row(factory, version_id).last_error


def test_restricted_documents_require_explicit_outbound_allowlist(setup):
    factory, client, _ = setup
    _, version_id = seed(factory, level="restricted")
    runner = WeKnoraSyncRunner(factory, client, allowed_data_levels=("restricted",))
    runner.run_once()
    assert job_row(factory, version_id).status == "processing"


def attach_file(factory, store, entry_id, version_id, *, filename="source.txt", content=b"original body"):
    key = f"library/{entry_id}/{filename}"
    store.put_bytes(key, content, "text/plain")
    with factory() as session:
        item = session.scalar(select(LibraryItem).where(LibraryItem.resource_entry_id == entry_id))
        item.storage_key = key
        session.add(AssetRef(entry_id=entry_id, version_id=version_id, storage_key=key,
                             media_type="text/plain"))
        session.commit()
    return key


def test_document_contains_original_attachment_and_manual_description(setup, tmp_path):
    factory, client, _ = setup
    store = LocalAssetStore(tmp_path / "assets")
    entry_id, version_id = seed(factory, text="人工填写说明")
    attach_file(factory, store, entry_id, version_id, content="附件原文产品规格".encode())
    runner = WeKnoraSyncRunner(factory, client, asset_store=store)
    runner.run_once()
    assert "人工填写说明" in client.posts[0][1]
    assert "附件原文产品规格" in client.posts[0][1]
    assert "# 雅酿资料" in client.posts[0][1]


def test_text_only_revision_keeps_original_attachment_in_index(setup, tmp_path):
    factory, client, _ = setup
    store = LocalAssetStore(tmp_path / "assets")
    entry_id, version_id = seed(factory, text="旧描述")
    key = attach_file(factory, store, entry_id, version_id, content="沿用的原附件正文".encode())
    with factory() as session:
        old_item = session.scalar(select(LibraryItem).where(LibraryItem.resource_entry_id == entry_id))
        old_item.active = False
        version = ResourceVersion(entry_id=entry_id, version=2, text_content="新描述", created_by="owner")
        session.add(version)
        session.flush()
        session.get(ResourceEntry, entry_id).current_version_id = version.id
        session.add(LibraryItem(owner_id="owner", category="product", name="新描述", version=2,
                                resource_entry_id=entry_id, storage_key=key))
        session.commit()
    runner = WeKnoraSyncRunner(factory, client, asset_store=store)
    runner.run_once()
    assert "新描述" in client.posts[0][1]
    assert "沿用的原附件正文" in client.posts[0][1]
    assert "旧描述" not in client.posts[0][1]


def test_failed_original_extraction_does_not_claim_complete_index(setup, tmp_path):
    factory, client, _ = setup
    store = LocalAssetStore(tmp_path / "assets")
    entry_id, version_id = seed(factory, text="只有手填描述")
    attach_file(factory, store, entry_id, version_id, filename="invalid.pdf", content=b"not a pdf")
    runner = WeKnoraSyncRunner(factory, client, asset_store=store)
    runner.run_once()
    job = job_row(factory, version_id)
    assert job.status == "failed"
    assert "原文解析失败" in job.last_error
    assert not job.upload_attempted
    assert client.posts == []
    runner.run_once()
    assert job_row(factory, version_id).attempt == 1


def test_media_attachment_is_not_uploaded_or_parsed(setup, tmp_path):
    factory, client, _ = setup
    store = LocalAssetStore(tmp_path / "assets")
    entry_id, version_id = seed(factory, text="视频说明文字")
    attach_file(factory, store, entry_id, version_id, filename="video.mp4", content=b"media data")
    with factory() as session:
        session.get(ResourceEntry, entry_id).resource_kind = "video"
        session.commit()
    runner = WeKnoraSyncRunner(factory, client, asset_store=store)
    runner.run_once()
    assert "视频说明文字" in client.posts[0][1]
    assert "media data" not in client.posts[0][1]


def test_sync_status_excludes_unindexed_media_and_task_references(setup):
    factory, client, _ = setup
    empty_id, _ = seed(factory, text="")
    reference_id, _ = seed(factory, formal=False, text="参考图")
    with factory() as session:
        assert get_sync_status(session, empty_id, client.connection_id)["status"] == "excluded"
        assert get_sync_status(session, reference_id, client.connection_id)["status"] == "excluded"


def test_document_without_manual_text_can_sync_original(setup, tmp_path):
    factory, client, _ = setup
    store = LocalAssetStore(tmp_path / "assets")
    entry_id, version_id = seed(factory, text="")
    attach_file(factory, store, entry_id, version_id, content="只有附件正文".encode())
    with factory() as session:
        assert get_sync_status(session, entry_id, client.connection_id)["status"] == "not_synced"
    runner = WeKnoraSyncRunner(factory, client, asset_store=store)
    runner.run_once()
    assert "只有附件正文" in client.posts[0][1]


def test_worker_stops_between_documents(setup):
    factory, client, runner = setup
    seed(factory)
    seed(factory)
    stop = Event()
    original = client.ingest_text

    def upload_and_stop(**kwargs):
        document = original(**kwargs)
        stop.set()
        return document

    client.ingest_text = upload_and_stop
    result = runner.run_once(stop_event=stop)
    assert result["processed"] == 1
    assert len(client.posts) == 1
    assert runner.run_once(stop_event=stop)["processed"] == 0


def test_rejected_api_key_upload_can_retry_after_key_is_fixed(setup):
    factory, client, runner = setup
    _, version_id = seed(factory)
    original = client.ingest_text

    def unauthorized(**kwargs):
        raise WeKnoraError("http_error", status_code=401)

    client.ingest_text = unauthorized
    runner.run_once()
    assert not job_row(factory, version_id).upload_attempted
    client.ingest_text = original
    due(factory)
    runner.run_once()
    assert job_row(factory, version_id).status == "processing"
    assert len(client.posts) == 1


def test_cancelled_recovery_preserves_upload_fence_and_retry_budget(setup):
    factory, client, runner = setup
    _, version_id = seed(factory)
    client.response_lost = True
    runner.run_once()
    before = job_row(factory, version_id).attempt

    def cancelled(*args, **kwargs):
        raise WeKnoraError("cancelled")

    client.find_document_by_source = cancelled
    due(factory)
    runner.run_once(limit=1)
    job = job_row(factory, version_id)
    assert job.attempt == before
    assert job.upload_attempted
    assert job.lease_token is None
    assert len(client.posts) == 1


def interleaved_runner(factory, client, predicate, competing_action):
    """Pause a real database write after its stale read; another session advances."""
    fired = []

    class InterleavedSession(Session):
        def execute(self, statement, *args, **kwargs):
            if (not fired and isinstance(statement, Update)
                    and statement.table.name == WeKnoraSyncJob.__tablename__
                    and predicate(statement.compile().params)):
                fired.append(True)
                competing_action()
            return super().execute(statement, *args, **kwargs)

    race_factory = sessionmaker(factory.kw["bind"], class_=InterleavedSession, expire_on_commit=False)
    return WeKnoraSyncRunner(race_factory, client, interval_seconds=1), fired


def test_claim_respects_backoff_written_after_candidate_read(setup):
    factory, client, runner = setup
    _, version_id = seed(factory)
    runner.reconcile()
    job = job_row(factory, version_id)

    def competing_backoff():
        with factory() as session:
            session.execute(update(WeKnoraSyncJob).where(WeKnoraSyncJob.id == job.id).values(
                available_at=datetime.now(UTC) + timedelta(hours=1),
            ))
            session.commit()

    delayed, fired = interleaved_runner(
        factory, client, lambda p: bool(p.get("lease_token")), competing_backoff,
    )
    assert delayed._claim() is None
    assert fired
    assert job_row(factory, version_id).lease_token is None


def test_stale_restore_scan_cannot_clear_another_workers_successful_upload_fence(setup):
    factory, client, runner = setup
    client.parse_status = "completed"
    _, version_id = seed(factory)
    runner.reconcile()
    with factory() as session:
        job = session.scalar(select(WeKnoraSyncJob))
        job.status = "deleted"
        job.operation = "cleanup"
        job.upload_attempted = True
        job.remote_id = "previous-deleted-document"
        session.commit()

    def competing_restore():
        runner.run_once()

    delayed, fired = interleaved_runner(
        factory, client,
        lambda p: p.get("status") == "queued" and p.get("upload_attempted") is False,
        competing_restore,
    )
    delayed.reconcile()
    assert fired
    job = job_row(factory, version_id)
    assert job.status == "ready"
    assert job.operation == "index"
    assert job.upload_attempted
    assert job.remote_id == "remote-1"
    runner.run_once()
    assert len(client.posts) == 1


def test_stale_obsolete_scan_does_not_reset_inflight_cleanup_progress(setup):
    factory, client, runner = setup
    client.parse_status = "completed"
    entry_id, version_id = seed(factory)
    runner.run_once()
    with factory() as session:
        session.get(ResourceEntry, entry_id).active = False
        session.commit()

    delayed, fired = interleaved_runner(
        factory, client, lambda p: p.get("operation") == "cleanup",
        runner.run_once,
    )
    delayed.reconcile()
    assert fired
    job = job_row(factory, version_id)
    assert job.status == "deleting"
    assert job.operation == "cleanup"
    assert job.delete_requested
    assert job.attempt == 1
    assert len(client.deletes) == 1


def test_reset_after_remote_lookup_cannot_override_new_worker_lease(setup):
    factory, client, runner = setup
    entry_id, version_id = seed(factory)
    client.upload_failed = True
    runner.run_once()
    due(factory)
    runner.run_once()
    job = job_row(factory, version_id)
    claims = []

    def competing_retry():
        with factory() as session:
            retry_sync_job(session, entry_id, client.connection_id)
            session.commit()
        claims.append(runner._claim())

    delayed, fired = interleaved_runner(
        factory, client,
        lambda p: p.get("status") == "queued" and p.get("upload_attempted") is False,
        competing_retry,
    )
    with pytest.raises(ValueError, match="同步状态已变化"):
        delayed.reset_unknown_upload(job.id)
    assert fired
    assert claims[0] is not None
    current = job_row(factory, version_id)
    assert current.upload_attempted
    assert current.lease_token == claims[0][1]
    assert len(client.posts) == 1


def test_failed_index_is_cleaned_after_stop_but_failed_cleanup_has_bounded_retries(setup):
    factory, client, runner = setup
    client.parse_status = "failed"
    entry_id, version_id = seed(factory)
    runner.run_once()
    assert job_row(factory, version_id).status == "failed"
    assert job_row(factory, version_id).operation == "index"
    with factory() as session:
        session.get(ResourceEntry, entry_id).active = False
        session.commit()
    calls = []

    def rejected_delete(remote_id):
        calls.append(remote_id)
        raise WeKnoraError("connection_failed")

    client.delete_document = rejected_delete
    for _ in range(5):
        due(factory)
        runner.run_once()
    assert len(calls) == 5
    job = job_row(factory, version_id)
    assert job.status == "failed"
    assert job.operation == "cleanup"
    for _ in range(3):
        due(factory)
        runner.run_once()
    assert len(calls) == 5
    assert job_row(factory, version_id).attempt == 5


def test_failed_index_after_version_replacement_still_gets_remote_cleanup(setup):
    factory, client, runner = setup
    client.parse_status = "failed"
    entry_id, version_id = seed(factory)
    runner.run_once()
    with factory() as session:
        version = ResourceVersion(entry_id=entry_id, version=2, text_content="新资料", created_by="owner")
        session.add(version)
        session.flush()
        session.get(ResourceEntry, entry_id).current_version_id = version.id
        session.commit()
    runner.run_once()
    assert "remote-1" in client.deletes
    assert job_row(factory, version_id).operation == "cleanup"
