from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import MetaData, Table, create_engine, select
from sqlalchemy.orm import sessionmaker

from content_service.db import Base
from content_service.media_adapters import LocalAssetStore
from content_service.models import (
    AssetRef,
    ExternalRef,
    LibraryItem,
    OriginalArchive,
    ResourceEntry,
)
from content_service.original_archives import (
    ArchiveIntegrityError,
    ArchiveUnavailableError,
    CacheInUseError,
    CacheMaintenanceLock,
    OriginalArchiveRunner,
    RemoteArchiveAssetStore,
    get_archive_status,
    purge_local_verified_cache,
    retry_archive,
)
from content_service.weknora import WeKnoraDocument, WeKnoraError


class FakeClient:
    connection_id = "originals-test"
    knowledge_base_id = "kb-test"
    timeout_seconds = 1

    def __init__(self):
        self.documents = {}
        self.bytes = {}
        self.posts = []
        self.downloads = []
        self.failure = None
        self.failure_status = None
        self.lose_response = False
        self.bad_download = False
        self.parse_status = "completed"

    def ingest_file(self, path, *, title, metadata):
        self.posts.append((path.name, dict(metadata)))
        if self.failure:
            raise WeKnoraError(self.failure, status_code=self.failure_status)
        doc = WeKnoraDocument(f"original-{len(self.posts)}", self.knowledge_base_id,
                             title, self.parse_status, "enabled", dict(metadata))
        self.documents[doc.id] = doc
        self.bytes[doc.id] = Path(path).read_bytes()
        if self.lose_response:
            raise WeKnoraError("timeout")
        return doc

    def get_document(self, doc_id):
        if doc_id not in self.documents:
            raise WeKnoraError("not_found", status_code=404)
        return self.documents[doc_id]

    def find_document_by_source(self, marker, *, stop_event=None):
        return next((doc for doc in self.documents.values()
                     if doc.metadata.get("local_source_marker") == marker), None)

    def download_document(self, doc_id, destination, *, expected_sha256, expected_size, source_marker):
        self.downloads.append(doc_id)
        doc = self.get_document(doc_id)
        if doc.metadata.get("local_source_marker") != source_marker:
            raise WeKnoraError("file_source_mismatch")
        data = b"corrupted" if self.bad_download else self.bytes[doc_id]
        # The runner must also detect adapter failures, not just rely on it.
        Path(destination).write_bytes(data)

    def delete_document(self, *_):
        pytest.fail("Original archive must never delete a remote original")


@pytest.fixture
def setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'originals.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    store, client = LocalAssetStore(tmp_path / "assets"), FakeClient()
    runner = OriginalArchiveRunner(factory, client, asset_store=store, interval_seconds=1)
    yield factory, store, client, runner
    engine.dispose()


def seed(setup, key="library/source.pdf", *, formal=True, active=True,
         source_type=None, level="internal", content=b"original PDF bytes with illustrations"):
    factory, store, _, _ = setup
    result = store.put_bytes(key, content, "application/pdf")
    with factory() as session:
        entry = ResourceEntry(owner_id="owner", title="产品原件", resource_kind="document",
                              data_level=level, active=active,
                              metadata_json=json.dumps({"source_type": source_type}))
        session.add(entry)
        session.flush()
        session.add(AssetRef(entry_id=entry.id, storage_key=key, media_type="application/pdf",
                             size_bytes=result.size, checksum=result.checksum_sha256))
        if formal:
            session.add(LibraryItem(owner_id="owner", category="product", name="产品原件",
                                    resource_entry_id=entry.id, storage_key=key, active=active))
        session.commit()
        return entry.id


def due(factory):
    with factory() as session:
        for row in session.scalars(select(OriginalArchive)):
            row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()


def row_for(factory, key="library/source.pdf"):
    with factory() as session:
        return session.scalar(select(OriginalArchive).where(OriginalArchive.storage_key == key))


def test_exact_original_bytes_are_archived_with_separate_namespace_and_no_search_ref(setup):
    factory, store, client, runner = setup
    seed(setup)
    assert runner.run_once() == {"enqueued": 1, "processed": 1, "failed": 0}
    row = row_for(factory)
    assert row.status == "ready"
    assert row.source_marker.startswith("content-agent-original:")
    assert row.verified_at
    assert client.bytes[row.remote_id] == store.read_bytes(row.storage_key)
    with factory() as session:
        assert session.scalar(select(ExternalRef)) is None
        assert get_archive_status(session, row.storage_key, client.connection_id)["status"] == "ready"


def test_historic_inactive_originals_retained_but_references_and_restricted_excluded(setup):
    factory, _, client, runner = setup
    seed(setup, "library/inactive.pdf", active=False)
    seed(setup, "task/not-formal.pdf", formal=False)
    seed(setup, "task/reference.pdf", source_type="task-image-reference")
    seed(setup, "library/restricted.pdf", level="restricted")
    runner.run_once()
    assert len(client.posts) == 1
    assert row_for(factory, "library/inactive.pdf").status == "ready"
    for key in ("task/not-formal.pdf", "task/reference.pdf", "library/restricted.pdf"):
        assert row_for(factory, key) is None


def test_video_and_oversized_original_never_uploaded_or_purged(setup):
    factory, store, client, runner = setup
    seed(setup, "library/video.mp4")
    seed(setup, "library/large.pdf", content=b"a" * 101)
    runner.max_file_bytes = 100
    runner.run_once()
    assert client.posts == []
    assert row_for(factory, "library/video.mp4").status == "unsupported"
    assert row_for(factory, "library/large.pdf").status == "unsupported"
    assert purge_local_verified_cache(factory, client, store, apply=True)["removed"] == 0
    assert store.exists("library/video.mp4") and store.exists("library/large.pdf")


@pytest.mark.parametrize("parse_status", ["failed", "processing", "completed"])
def test_custody_depends_on_original_integrity_not_embedding_or_ocr(setup, parse_status):
    factory, _, client, runner = setup
    seed(setup)
    client.parse_status = parse_status
    runner.run_once()
    assert row_for(factory).status == "ready"


def test_lost_upload_response_recovers_marker_without_reposting(setup):
    factory, _, client, runner = setup
    seed(setup)
    client.lose_response = True
    assert runner.run_once()["failed"] == 1
    assert row_for(factory).upload_attempted
    due(factory)
    runner.run_once()
    assert len(client.posts) == 1
    assert row_for(factory).status == "ready"


def test_unknown_upload_is_not_reposted_even_after_manual_retry(setup):
    factory, store, client, runner = setup
    seed(setup)
    client.failure = "timeout"
    runner.run_once()
    due(factory)
    runner.run_once()
    assert row_for(factory).status == "ambiguous"
    with factory() as session:
        retry_archive(session, "library/source.pdf", client.connection_id)
        session.commit()
    runner.run_once()
    assert row_for(factory).status == "ambiguous"
    assert len(client.posts) == 1
    assert store.exists("library/source.pdf")


def test_unowned_dedup_is_never_adopted_or_evicted(setup):
    factory, store, client, runner = setup
    seed(setup)
    client.failure = "duplicate_unowned"
    runner.run_once()
    row = row_for(factory)
    assert row.status == "failed" and row.remote_id is None
    assert purge_local_verified_cache(factory, client, store, apply=True)["removed"] == 0
    assert store.exists(row.storage_key)


def test_download_corruption_never_establishes_custody(setup):
    factory, store, client, runner = setup
    seed(setup)
    client.bad_download = True
    runner.run_once()
    assert row_for(factory).status == "failed"
    assert store.exists("library/source.pdf")
    assert purge_local_verified_cache(factory, client, store, apply=True)["removed"] == 0


def test_source_change_between_scan_and_upload_is_retained_and_rejected(setup):
    factory, store, client, runner = setup
    seed(setup)
    runner.reconcile()
    store.put_bytes("library/source.pdf", b"changed file", "application/pdf")
    runner.run_once()
    assert row_for(factory).status == "failed"
    assert not client.posts
    assert store.read_bytes("library/source.pdf") == b"changed file"


def test_retiring_or_replacing_library_never_deletes_original(setup):
    factory, _, client, runner = setup
    entry_id = seed(setup)
    runner.run_once()
    first = row_for(factory)
    with factory() as session:
        entry = session.get(ResourceEntry, entry_id)
        entry.active = False
        item = session.scalar(select(LibraryItem))
        item.active = False
        session.commit()
    seed(setup, "library/replacement.pdf", content=b"new original")
    runner.run_once()
    assert row_for(factory).remote_id == first.remote_id
    assert len(client.documents) == 2


def test_compare_and_swap_claim_excludes_other_worker_and_fences_stale_result(setup):
    factory, store, client, runner = setup
    seed(setup)
    runner.reconcile()
    first_id, token = runner._claim()
    second = OriginalArchiveRunner(factory, client, asset_store=store)
    assert second._claim() is None
    with factory() as session:
        row = session.get(OriginalArchive, first_id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert second._claim()[0] == first_id
    runner._finish(first_id, token, status="ready", remote_id="stale-writer")
    assert row_for(factory).status == "queued"
    assert row_for(factory).remote_id is None


def test_dryrun_keeps_cache_and_apply_verifies_each_remote_before_removal(setup):
    factory, store, client, runner = setup
    seed(setup)
    runner.run_once()
    original = store.read_bytes("library/source.pdf")
    before = len(client.downloads)
    result = purge_local_verified_cache(factory, client, store)
    assert result["eligible"] == 1 and result["removed"] == 0
    assert len(client.downloads) == before and store.exists("library/source.pdf")
    result = purge_local_verified_cache(factory, client, store, apply=True)
    assert result["removed"] == 1 and result["removed_bytes"] == len(original)
    assert len(client.downloads) == before + 1 and not store.exists("library/source.pdf")
    assert row_for(factory).status == "ready" and len(client.documents) == 1


@pytest.mark.parametrize("failure", ["missing", "corrupt", "unowned"])
def test_purge_preserves_only_local_copy_if_remote_proof_fails(setup, failure):
    factory, store, client, runner = setup
    seed(setup)
    runner.run_once()
    row = row_for(factory)
    if failure == "missing":
        del client.documents[row.remote_id]
    elif failure == "corrupt":
        client.bad_download = True
    else:
        client.documents[row.remote_id] = replace(client.documents[row.remote_id], metadata={})
    result = purge_local_verified_cache(factory, client, store, apply=True)
    assert result["removed"] == 0 and result["failed"] == 1
    assert store.exists(row.storage_key)


def test_live_store_shared_lock_blocks_maintenance_for_full_returned_path_lifetime(setup):
    factory, store, client, runner = setup
    seed(setup)
    runner.run_once()
    runtime = RemoteArchiveAssetStore(store, factory, client)
    try:
        path = runtime.path_for("library/source.pdf")
        with pytest.raises(CacheInUseError):
            purge_local_verified_cache(factory, client, store, apply=True)
        # Multiple application workers can share the lock.
        with CacheMaintenanceLock(store.root):
            assert path.is_file()
        assert purge_local_verified_cache(factory, client, store)["eligible"] == 1
    finally:
        runtime.close()
    assert purge_local_verified_cache(factory, client, store, apply=True)["removed"] == 1


def test_reads_rehydrate_exact_original_after_eviction_and_writes_do_not_fetch(setup):
    factory, store, client, runner = setup
    seed(setup)
    runner.run_once()
    original = store.read_bytes("library/source.pdf")
    purge_local_verified_cache(factory, client, store, apply=True)
    runtime = RemoteArchiveAssetStore(store, factory, client)
    try:
        before = len(client.downloads)
        assert runtime.exists("library/source.pdf")
        assert len(client.downloads) == before  # exists does not fetch
        assert runtime.read_bytes("library/source.pdf") == original
        assert len(client.downloads) == before + 1
        runtime.put_bytes("new/local.bin", b"abc", "application/octet-stream")
        runtime.put_stream("new/stream.bin", io.BytesIO(b"xyz"), "application/octet-stream")
        assert len(client.downloads) == before + 1
        with pytest.raises(ArchiveIntegrityError):
            runtime.put_bytes("library/source.pdf", b"overwrite", "application/pdf")
        assert runtime.read_bytes("library/source.pdf") == original
    finally:
        runtime.close()


def test_remote_outage_is_not_silently_reported_as_missing_file(setup):
    factory, store, client, runner = setup
    seed(setup)
    runner.run_once()
    purge_local_verified_cache(factory, client, store, apply=True)
    client.documents.clear()
    runtime = RemoteArchiveAssetStore(store, factory, client)
    try:
        with pytest.raises(ArchiveUnavailableError):
            runtime.path_for("library/source.pdf")
        assert not store.exists("library/source.pdf")
    finally:
        runtime.close()


def test_changed_connection_never_silently_strands_remote_original(setup):
    factory, store, client, runner = setup
    seed(setup)
    runner.run_once()
    purge_local_verified_cache(factory, client, store, apply=True)
    client.connection_id = "another-platform"
    runtime = RemoteArchiveAssetStore(store, factory, client)
    try:
        with pytest.raises(ArchiveUnavailableError, match="另一知识库"):
            runtime.path_for("library/source.pdf")
    finally:
        runtime.close()


def test_corrupt_mapping_does_not_delete_changed_local_file(setup):
    factory, store, client, runner = setup
    seed(setup)
    runner.run_once()
    store.put_bytes("library/source.pdf", b"external writer changed this", "application/pdf")
    result = purge_local_verified_cache(factory, client, store, apply=True)
    assert result["skipped"] == 1 and result["removed"] == 0
    assert store.read_bytes("library/source.pdf") == b"external writer changed this"


def test_untrusted_stored_key_cannot_escape_storage_root(setup):
    factory, store, client, _ = setup
    runtime = RemoteArchiveAssetStore(store, factory, client)
    try:
        with pytest.raises(ValueError):
            runtime.path_for("../outside.pdf")
    finally:
        runtime.close()


def test_repeated_range_reads_do_not_rehash_unchanged_file_but_changed_content_is_rejected(
    setup, monkeypatch,
):
    from content_service import original_archives as module

    factory, store, client, runner = setup
    seed(setup)
    runner.run_once()
    runtime = RemoteArchiveAssetStore(store, factory, client)
    real_fingerprint = module._fingerprint
    calls = []

    def counted(path):
        calls.append(path)
        return real_fingerprint(path)

    monkeypatch.setattr(module, "_fingerprint", counted)
    try:
        for _ in range(10):
            runtime.path_for("library/source.pdf")
        assert len(calls) == 1
        store.path_for("library/source.pdf").write_bytes(b"modified outside the app")
        with pytest.raises(ArchiveIntegrityError):
            runtime.path_for("library/source.pdf")
        assert len(calls) == 2
    finally:
        runtime.close()


@pytest.mark.parametrize("initial", ["missing", "corrupt"])
def test_restored_original_can_retry_only_against_original_upload_receipt(setup, initial):
    factory, store, client, runner = setup
    seed(setup)
    original = store.read_bytes("library/source.pdf")
    path = store.path_for("library/source.pdf")
    if initial == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt file")
    runner.run_once()
    assert row_for(factory).status == "failed"
    assert not row_for(factory).upload_attempted
    path.write_bytes(original)
    with factory() as session:
        retry_archive(session, "library/source.pdf", client.connection_id)
        session.commit()
    runner.run_once()
    assert row_for(factory).status == "ready"
    assert client.bytes[row_for(factory).remote_id] == original


def test_retry_cannot_adopt_file_that_still_disagrees_with_original_receipt(setup):
    factory, store, client, runner = setup
    seed(setup)
    store.path_for("library/source.pdf").write_bytes(b"corrupt file")
    runner.run_once()
    with factory() as session:
        retry_archive(session, "library/source.pdf", client.connection_id)
        session.commit()
    runner.run_once()
    assert row_for(factory).status == "failed"
    assert client.posts == []


def test_migration_retains_previous_schema_and_refuses_stranding_nonempty_archive(
    tmp_path, monkeypatch,
):
    from alembic import command
    from sqlalchemy import inspect

    from content_service.database_tools import migration_config

    url = f"sqlite:///{tmp_path / 'upgrade-originals.db'}"
    monkeypatch.setenv("CONTENT_DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("CONTENT_DATABASE_URL", url)
    config = migration_config(url)
    command.upgrade(config, "content_0016")
    engine = create_engine(url)
    previous_tables = set(inspect(engine).get_table_names())
    command.upgrade(config, "content_0017")
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == previous_tables | {"original_archives"}
    assert {"storage_key", "checksum_sha256", "remote_id", "verified_at"} <= {
        col["name"] for col in inspector.get_columns("original_archives")
    }
    factory = sessionmaker(engine)
    with factory() as session:
        entry = ResourceEntry(owner_id="owner", title="retained", resource_kind="document")
        session.add(entry)
        session.flush()
        entry_id = entry.id
        session.commit()
    legacy_archives = Table("original_archives", MetaData(), autoload_with=engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(legacy_archives.insert().values(
            id="retained-id", storage_key="retained.pdf", entry_id=entry_id,
            connection_id="test", knowledge_base_id="kb", checksum_sha256="a" * 64,
            size_bytes=1, source_marker="original:test", status="ready",
            available_at=now, created_at=now, updated_at=now,
        ))
    with pytest.raises(RuntimeError, match="原件托管记录非空"):
        command.downgrade(config, "content_0016")
    with engine.connect() as connection:
        assert connection.scalar(select(legacy_archives.c.storage_key)) == "retained.pdf"
    engine.dispose()


@pytest.mark.parametrize("code", ["multimodal_not_configured", "asr_not_configured"])
def test_known_precreation_model_rejection_can_retry_upload_after_configuration(setup, code):
    factory, _, client, runner = setup
    seed(setup)
    client.failure, client.failure_status = code, 400
    runner.run_once()
    row = row_for(factory)
    assert row.status == "failed" and row.upload_attempted is False
    assert code not in row.last_error
    client.failure = None
    runner.retry_job(row.id)
    runner.run_once()
    assert row_for(factory).status == "ready" and len(client.posts) == 2


def test_other_http400_keeps_upload_fence(setup):
    factory, _, client, runner = setup
    seed(setup)
    client.failure, client.failure_status = "request_failed", 400
    runner.run_once()
    assert row_for(factory).upload_attempted
    due(factory)
    runner.run_once()
    assert row_for(factory).status == "ambiguous" and len(client.posts) == 1


def test_confirmed_unknown_upload_reset_preserves_fingerprint_then_can_upload(setup):
    factory, _, client, runner = setup
    seed(setup)
    client.failure = "timeout"
    runner.run_once()
    due(factory)
    runner.run_once()
    row = row_for(factory)
    fingerprint = (row.checksum_sha256, row.size_bytes, row.source_marker)
    runner.reset_unknown_upload(row.id)
    reset = row_for(factory)
    assert not reset.upload_attempted and reset.status == "queued"
    assert (reset.checksum_sha256, reset.size_bytes, reset.source_marker) == fingerprint
    client.failure = None
    runner.run_once()
    assert row_for(factory).status == "ready"


def test_unknown_upload_reset_refuses_existing_remote_original(setup):
    factory, _, client, runner = setup
    seed(setup)
    client.lose_response = True
    runner.run_once()
    with factory() as session:
        row = session.scalar(select(OriginalArchive))
        row.status = "ambiguous"
        session.commit()
        archive_id = row.id
    with pytest.raises(ValueError, match="平台已有"):
        runner.reset_unknown_upload(archive_id)
    assert row_for(factory).upload_attempted and len(client.posts) == 1


def test_reset_compare_and_swap_rejects_state_changed_during_remote_lookup(setup, monkeypatch):
    factory, _, client, runner = setup
    seed(setup)
    client.failure = "timeout"
    runner.run_once()
    due(factory)
    runner.run_once()
    row = row_for(factory)

    def raced_lookup(marker):
        with factory() as session:
            current = session.get(OriginalArchive, row.id)
            current.status = "queued"
            current.lease_token = "another-worker"
            current.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            session.commit()
        return None

    monkeypatch.setattr(client, "find_document_by_source", raced_lookup)
    with pytest.raises(ValueError, match="状态已变化"):
        runner.reset_unknown_upload(row.id)
    assert row_for(factory).upload_attempted
    assert row_for(factory).lease_token == "another-worker"


def test_status_output_omits_storage_paths_remote_ids_and_documents(setup):
    _, _, _, runner = setup
    seed(setup)
    runner.run_once()
    assert set(runner.statuses()[0]) == {"archive_id", "status", "error", "can_retry"}


def test_stream_write_to_new_key_does_not_stage_large_video_twice(setup, monkeypatch):
    from content_service import original_archives as module

    factory, store, client, _ = setup
    runtime = RemoteArchiveAssetStore(store, factory, client)

    def never_stage(*args, **kwargs):
        pytest.fail("A new unarchived key should stream directly to its final local store")

    monkeypatch.setattr(module.tempfile, "TemporaryFile", never_stage)
    try:
        runtime.put_stream("uploads/new-video.mp4", io.BytesIO(b"video bytes"), "video/mp4")
        assert store.read_bytes("uploads/new-video.mp4") == b"video bytes"
    finally:
        runtime.close()


def test_stream_cannot_overwrite_original_from_previous_connection(setup):
    factory, store, client, runner = setup
    seed(setup)
    runner.run_once()
    client.connection_id = "another-endpoint"
    runtime = RemoteArchiveAssetStore(store, factory, client)
    try:
        with pytest.raises(ArchiveIntegrityError):
            runtime.put_stream("library/source.pdf", io.BytesIO(b"different"), "application/pdf")
    finally:
        runtime.close()


@pytest.mark.parametrize("suffix", [".webp", ".bmp", ".aac", ".mp4", ".mov", ".svg", ".zip"])
def test_platform_unsupported_extensions_never_attempt_post_or_lose_local_bytes(setup, suffix):
    factory, store, client, runner = setup
    key = "library/unsupported" + suffix
    seed(setup, key)
    runner.run_once()
    row = row_for(factory, key)
    assert row.status == "unsupported"
    assert not row.upload_attempted
    assert client.posts == []
    assert purge_local_verified_cache(factory, client, store, apply=True)["removed"] == 0
    assert store.exists(key)


@pytest.mark.parametrize("suffix", [".doc", ".xls", ".ppt", ".markdown", ".epub",
                                   ".html", ".htm", ".mhtml", ".xmind", ".json"])
def test_platform_supported_originals_can_archive_without_expanding_local_text_parser(setup, suffix):
    factory, _, client, runner = setup
    key = "library/supported" + suffix
    seed(setup, key)
    runner.run_once()
    assert row_for(factory, key).status == "ready"
    assert len(client.posts) == 1


def _ready_original_and_copy(setup):
    """Two independent upload receipts containing exactly the same bytes."""
    factory, _, _, runner = setup
    seed(setup)
    runner.run_once()
    canonical = row_for(factory)
    seed(setup, "library/copy.pdf")
    runner.reconcile()
    return canonical, row_for(factory, "library/copy.pdf")


def _legacy_duplicate_failure(factory, archive_id):
    with factory() as session:
        row = session.get(OriginalArchive, archive_id)
        row.status = "failed"
        row.upload_attempted = True
        row.attempt = 1
        row.last_error = WeKnoraError("duplicate_unowned").safe_message + "；本地文件保留"
        row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()


@pytest.mark.parametrize("previously_failed", [False, True])
def test_duplicate_original_shares_verified_remote_without_another_upload(setup, previously_failed):
    factory, store, client, runner = setup
    canonical, copied = _ready_original_and_copy(setup)
    if previously_failed:
        _legacy_duplicate_failure(factory, copied.id)
    before_downloads = len(client.downloads)
    runner.run_once()
    actual = row_for(factory, copied.storage_key)
    assert actual.status == "ready"
    assert actual.remote_id == canonical.remote_id
    assert actual.remote_source_marker == canonical.source_marker
    assert actual.source_marker == copied.source_marker != canonical.source_marker
    assert actual.verified_at is not None
    assert actual.last_error is None
    assert len(client.posts) == 1
    assert len(client.downloads) > before_downloads
    assert store.read_bytes(copied.storage_key) == store.read_bytes(canonical.storage_key)


def test_three_same_originals_need_one_upload_and_shared_copies_rehydrate_after_purge(setup):
    factory, store, client, runner = setup
    canonical, copied = _ready_original_and_copy(setup)
    seed(setup, "library/third.pdf")
    runner.run_once()
    third = row_for(factory, "library/third.pdf")
    assert row_for(factory, copied.storage_key).remote_id == third.remote_id == canonical.remote_id
    assert len(client.posts) == len(client.documents) == 1
    original = store.read_bytes(canonical.storage_key)
    result = purge_local_verified_cache(factory, client, store, apply=True)
    assert result["removed"] == 3 and result["failed"] == 0
    assert not store.exists(canonical.storage_key)
    runtime = RemoteArchiveAssetStore(store, factory, client)
    try:
        for key in (copied.storage_key, third.storage_key, canonical.storage_key):
            assert runtime.read_bytes(key) == original
    finally:
        runtime.close()
    assert len(client.posts) == 1


def test_legacy_null_remote_marker_remains_readable_and_usable_as_shared_source(setup):
    factory, store, client, runner = setup
    canonical, copied = _ready_original_and_copy(setup)
    with factory() as session:
        session.get(OriginalArchive, canonical.id).remote_source_marker = None
        session.commit()
    runner.run_once()
    assert row_for(factory, copied.storage_key).status == "ready"
    assert purge_local_verified_cache(factory, client, store, apply=True)["removed"] == 2
    runtime = RemoteArchiveAssetStore(store, factory, client)
    try:
        assert runtime.read_bytes(copied.storage_key) == client.bytes[canonical.remote_id]
        assert runtime.read_bytes(canonical.storage_key) == client.bytes[canonical.remote_id]
    finally:
        runtime.close()


def test_canonical_original_can_be_reused_after_its_local_cache_was_evicted(setup):
    factory, store, client, runner = setup
    canonical, copied = _ready_original_and_copy(setup)
    assert purge_local_verified_cache(factory, client, store, apply=True)["removed"] == 1
    assert not store.exists(canonical.storage_key) and store.exists(copied.storage_key)
    runner.run_once()
    actual = row_for(factory, copied.storage_key)
    assert actual.status == "ready" and actual.remote_id == canonical.remote_id
    assert len(client.posts) == 1


@pytest.mark.parametrize("damage", ["missing", "changed", "missing_receipt", "no_checksum", "no_size"])
def test_sharing_requires_current_local_bytes_and_complete_upload_receipt(setup, damage):
    factory, store, client, runner = setup
    _, copied = _ready_original_and_copy(setup)
    if damage == "missing":
        store.path_for(copied.storage_key).unlink()
    elif damage == "changed":
        store.path_for(copied.storage_key).write_bytes(b"changed bytes")
    else:
        with factory() as session:
            asset = session.scalar(select(AssetRef).where(AssetRef.storage_key == copied.storage_key))
            if damage == "missing_receipt":
                session.delete(asset)
            elif damage == "no_checksum":
                asset.checksum = None
            else:
                asset.size_bytes = None
            session.commit()
    _legacy_duplicate_failure(factory, copied.id)
    runner.run_once()
    actual = row_for(factory, copied.storage_key)
    assert actual.status != "ready" and actual.remote_id is None
    assert len(client.posts) == 1
    if damage != "missing":
        assert store.exists(copied.storage_key)


@pytest.mark.parametrize("damage", [
    "wrong_marker", "wrong_kb", "same_size_bad_hash", "wrong_size",
    "wrong_kind", "wrong_metadata_hash", "wrong_metadata_size",
])
def test_reuse_does_not_trust_old_ready_flag_when_remote_proof_changed(setup, damage):
    factory, store, client, runner = setup
    canonical, copied = _ready_original_and_copy(setup)
    _legacy_duplicate_failure(factory, copied.id)
    document = client.documents[canonical.remote_id]
    if damage == "wrong_marker":
        client.documents[canonical.remote_id] = replace(
            document, metadata={**document.metadata, "local_source_marker": "another-project"},
        )
    elif damage == "wrong_kb":
        client.documents[canonical.remote_id] = replace(document, knowledge_base_id="other-kb")
    elif damage == "same_size_bad_hash":
        client.bytes[canonical.remote_id] = b"x" * canonical.size_bytes
    elif damage in {"wrong_kind", "wrong_metadata_hash", "wrong_metadata_size"}:
        key, value = {
            "wrong_kind": ("local_archive_kind", "text-index"),
            "wrong_metadata_hash": ("local_original_sha256", "f" * 64),
            "wrong_metadata_size": ("local_original_size", "99999"),
        }[damage]
        client.documents[canonical.remote_id] = replace(
            document, metadata={**document.metadata, key: value},
        )
    else:
        client.bytes[canonical.remote_id] = b"too short"
    runner.run_once()
    actual = row_for(factory, copied.storage_key)
    assert actual.status != "ready" and actual.remote_id is None
    assert len(client.posts) == 1
    assert store.exists(copied.storage_key)


@pytest.mark.parametrize("damage", ["missing_receipt", "wrong_receipt", "forged_marker", "unverified"])
def test_reuse_requires_provenance_of_the_existing_local_mapping(setup, damage):
    factory, _, client, runner = setup
    canonical, copied = _ready_original_and_copy(setup)
    _legacy_duplicate_failure(factory, copied.id)
    with factory() as session:
        source = session.get(OriginalArchive, canonical.id)
        asset = session.scalar(select(AssetRef).where(AssetRef.storage_key == canonical.storage_key))
        if damage == "missing_receipt":
            session.delete(asset)
        elif damage == "wrong_receipt":
            asset.checksum = "0" * 64
        elif damage == "forged_marker":
            source.source_marker = "content-agent-original:forged"
        else:
            source.verified_at = None
        session.commit()
    runner.run_once()
    actual = row_for(factory, copied.storage_key)
    assert actual.status != "ready" and actual.remote_id is None
    assert len(client.posts) == 1


@pytest.mark.parametrize("field,value", [
    ("connection_id", "other-connection"),
    ("knowledge_base_id", "other-kb"),
    ("checksum_sha256", "0" * 64),
    ("size_bytes", 123456),
])
def test_shared_archive_candidates_are_scoped_to_connection_kb_hash_and_size(setup, field, value):
    factory, _, client, runner = setup
    canonical, copied = _ready_original_and_copy(setup)
    with factory() as session:
        setattr(session.get(OriginalArchive, canonical.id), field, value)
        session.commit()
    runner.run_once()
    actual = row_for(factory, copied.storage_key)
    assert actual.status == "ready"
    assert actual.remote_id != canonical.remote_id
    assert len(client.posts) == 2


def test_unrelated_failed_upload_does_not_become_an_automatic_retry(setup):
    factory, _, client, runner = setup
    _, copied = _ready_original_and_copy(setup)
    with factory() as session:
        row = session.get(OriginalArchive, copied.id)
        row.status = "failed"
        row.upload_attempted = True
        row.attempt = 5
        row.last_error = "原件托管暂未完成，请检查平台连接及本地文件；本地文件保留"
        session.commit()
    runner.run_once()
    assert row_for(factory, copied.storage_key).status == "failed"
    assert len(client.posts) == 1


def test_attempted_unknown_upload_can_reuse_verified_local_original_without_reposting(setup):
    factory, _, client, runner = setup
    canonical, copied = _ready_original_and_copy(setup)
    with factory() as session:
        row = session.get(OriginalArchive, copied.id)
        row.upload_attempted = True
        row.status = "queued"
        session.commit()
    runner.run_once()
    assert row_for(factory, copied.storage_key).status == "ready"
    assert row_for(factory, copied.storage_key).remote_id == canonical.remote_id
    assert len(client.posts) == 1


def test_prior_attempt_does_not_bypass_formal_data_eligibility_when_sharing(setup):
    factory, _, client, runner = setup
    _, copied = _ready_original_and_copy(setup)
    _legacy_duplicate_failure(factory, copied.id)
    with factory() as session:
        session.get(ResourceEntry, copied.entry_id).data_level = "restricted"
        session.commit()
    runner.run_once()
    actual = row_for(factory, copied.storage_key)
    assert actual.status != "ready" and actual.remote_id is None
    assert len(client.posts) == 1


def test_expired_shared_verification_cannot_overwrite_new_worker_lease(setup, monkeypatch):
    factory, _, client, runner = setup
    _, copied = _ready_original_and_copy(setup)
    original_download = client.download_document

    def superseded_download(*args, **kwargs):
        original_download(*args, **kwargs)
        with factory() as session:
            row = session.get(OriginalArchive, copied.id)
            row.lease_token = "new-worker"
            row.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
            session.commit()

    monkeypatch.setattr(client, "download_document", superseded_download)
    runner.run_once()
    actual = row_for(factory, copied.storage_key)
    assert actual.status == "queued" and actual.remote_id is None
    assert actual.lease_token == "new-worker"
    assert len(client.posts) == 1


def test_donor_mapping_change_during_download_prevents_committing_shared_custody(setup, monkeypatch):
    factory, _, client, runner = setup
    canonical, copied = _ready_original_and_copy(setup)
    _legacy_duplicate_failure(factory, copied.id)
    original_download = client.download_document

    def changed_source_download(*args, **kwargs):
        original_download(*args, **kwargs)
        with factory() as session:
            session.get(OriginalArchive, canonical.id).remote_id = "changed-remotely"
            session.commit()

    monkeypatch.setattr(client, "download_document", changed_source_download)
    runner.run_once()
    actual = row_for(factory, copied.storage_key)
    assert actual.status != "ready" and actual.remote_id is None
    assert len(client.posts) == 1


@pytest.mark.parametrize("change", ["restricted", "reference_only", "remove_library_association"])
def test_formal_eligibility_change_during_download_prevents_sharing(setup, monkeypatch, change):
    factory, _, client, runner = setup
    _, copied = _ready_original_and_copy(setup)
    _legacy_duplicate_failure(factory, copied.id)
    original_download = client.download_document

    def changed_eligibility_download(*args, **kwargs):
        original_download(*args, **kwargs)
        with factory() as session:
            entry = session.get(ResourceEntry, copied.entry_id)
            if change == "restricted":
                entry.data_level = "restricted"
            elif change == "reference_only":
                entry.metadata_json = json.dumps({"source_type": "task-image-reference"})
            else:
                item = session.scalar(select(LibraryItem).where(
                    LibraryItem.resource_entry_id == copied.entry_id,
                ))
                session.delete(item)
            session.commit()

    monkeypatch.setattr(client, "download_document", changed_eligibility_download)
    runner.run_once()
    actual = row_for(factory, copied.storage_key)
    assert actual.status != "ready" and actual.remote_id is None
    assert len(client.posts) == 1


def test_shared_marker_migration_preserves_legacy_original_and_nullable_fallback(tmp_path, monkeypatch):
    from alembic import command
    from sqlalchemy import inspect

    from content_service.database_tools import migration_config

    url = f"sqlite:///{tmp_path / 'upgrade-shared-originals.db'}"
    monkeypatch.setenv("CONTENT_DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("CONTENT_DATABASE_URL", url)
    config = migration_config(url)
    command.upgrade(config, "content_0017")
    engine = create_engine(url)
    legacy = Table("original_archives", MetaData(), autoload_with=engine)
    factory = sessionmaker(engine)
    with factory() as session:
        entry = ResourceEntry(owner_id="owner", title="old original", resource_kind="document")
        session.add(entry)
        session.flush()
        entry_id = entry.id
        session.commit()
    now = datetime.now(UTC)
    values = {
        "id": "legacy-original", "storage_key": "library/legacy.pdf", "entry_id": entry_id,
        "connection_id": "connection", "knowledge_base_id": "kb", "checksum_sha256": "a" * 64,
        "size_bytes": 123, "source_marker": "original-marker", "remote_id": "remote-original",
        "status": "ready", "upload_attempted": True, "available_at": now, "verified_at": now,
        "created_at": now, "updated_at": now,
    }
    with engine.begin() as connection:
        connection.execute(legacy.insert().values(**values))
    command.upgrade(config, "content_0018")
    columns = {column["name"]: column for column in inspect(engine).get_columns("original_archives")}
    assert columns["remote_source_marker"]["nullable"] is True
    with factory() as session:
        original = session.get(OriginalArchive, "legacy-original")
        assert original.remote_source_marker is None
        for key in ("storage_key", "source_marker", "remote_id", "checksum_sha256", "size_bytes", "status"):
            assert getattr(original, key) == values[key]
        original.remote_source_marker = "shared-remote-owner"
        session.commit()
    with pytest.raises(RuntimeError, match="共享原件托管记录"):
        command.downgrade(config, "content_0017")
    with factory() as session:
        original = session.get(OriginalArchive, "legacy-original")
        assert original.remote_source_marker == "shared-remote-owner"
        original.remote_source_marker = None
        session.commit()
    command.downgrade(config, "content_0017")
    assert "remote_source_marker" not in {
        column["name"] for column in inspect(engine).get_columns("original_archives")
    }
    with engine.connect() as connection:
        assert connection.scalar(select(legacy.c.remote_id)) == "remote-original"
    engine.dispose()


def test_shared_marker_migration_compiles_with_actual_dameng_dialect(monkeypatch):
    from alembic import command

    from content_service.database_tools import migration_config

    pytest.importorskip("dmSQLAlchemy")
    monkeypatch.setenv("CONTENT_DATABASE_BACKEND", "dm")
    config = migration_config("dm+dmPython://compile_only:unused@invalid:5236/APP")
    output = io.StringIO()
    config.output_buffer = output
    command.upgrade(config, "content_0017:content_0018", sql=True)
    sql = output.getvalue()
    assert "ALTER TABLE original_archives ADD remote_source_marker" in sql
    assert "VARCHAR2(255 CHAR)" in sql
