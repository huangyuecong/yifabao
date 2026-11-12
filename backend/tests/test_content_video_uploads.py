import hashlib
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from content_service.db import Base
from content_service.models import VideoUploadChunk
from content_service.video_uploads import (
    VideoUploadConflictError,
    VideoUploadIncompleteError,
    VideoUploadValidationError,
    cancel_upload_session,
    complete_upload_session,
    create_upload_session,
    get_upload_session,
    upload_session_json,
    write_upload_chunk,
)

ROOT = Path(__file__).resolve().parents[1]
SUBTITLE = (
    b"1\n00:00:00,000 --> 00:00:01,000\nResume upload safely.\n\n"
    b"2\n00:00:01,000 --> 00:00:02,000\nDone.\n"
)


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def upload_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'uploads.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory, tmp_path / "upload-store"
    finally:
        engine.dispose()


def create_subtitle_upload(session, *, checksum=None, chunk_size=17):
    return create_upload_session(
        session,
        project_id=str(uuid4()),
        owner_id="owner-a",
        role="subtitle",
        filename="captions.srt",
        media_type="text/plain",
        total_size=len(SUBTITLE),
        chunk_size=chunk_size,
        checksum=checksum or sha256(SUBTITLE),
    )


def chunks_for(upload, value=SUBTITLE):
    return [
        value[offset : offset + upload.chunk_size]
        for offset in range(0, len(value), upload.chunk_size)
    ]


def put_chunk(session, upload_id, index, value, upload_root):
    return write_upload_chunk(
        session,
        upload_id,
        index=index,
        stream=BytesIO(value),
        checksum_sha256=sha256(value),
        upload_root=upload_root,
    )


def test_resumable_upload_reports_missing_chunks_and_completes_after_restart(
    upload_database,
):
    session_factory, upload_root = upload_database
    with session_factory() as session:
        upload = create_subtitle_upload(session)
        upload_id = upload.id
        chunks = chunks_for(upload)
        put_chunk(session, upload_id, 0, chunks[0], upload_root)
        put_chunk(session, upload_id, 2, chunks[2], upload_root)
        session.commit()

    with session_factory() as session:
        upload = get_upload_session(session, upload_id)
        state = upload_session_json(session, upload, upload_root=upload_root)
        assert state["received_chunks"] == [0, 2]
        assert state["missing_chunks"] == [1, 3, 4, 5]
        with pytest.raises(VideoUploadIncompleteError) as incomplete:
            complete_upload_session(session, upload_id, upload_root=upload_root)
        assert incomplete.value.missing_chunks == [1, 3, 4, 5]

        for index in state["missing_chunks"]:
            put_chunk(session, upload_id, index, chunks[index], upload_root)
        result = complete_upload_session(session, upload_id, upload_root=upload_root)
        session.commit()

        assert result.path.read_bytes() == SUBTITLE
        assert result.object_key.startswith(f"completed/{upload_id}/")
        assert result.media_kind == "subtitle"
        assert result.media_type == "application/x-subrip"
        assert result.total_size == len(SUBTITLE)
        assert result.checksum_sha256 == sha256(SUBTITLE)
        assert not list((upload_root / "chunks" / upload_id).glob("*.part"))

    with session_factory() as session:
        repeated = complete_upload_session(session, upload_id, upload_root=upload_root)
        state = upload_session_json(
            session,
            get_upload_session(session, upload_id),
            upload_root=upload_root,
        )
        assert repeated.path.read_bytes() == SUBTITLE
        assert state["status"] == "completed"
        assert state["missing_chunks"] == []
        assert state["received_size"] == len(SUBTITLE)


def test_uploading_same_chunk_is_idempotent_but_different_content_conflicts(
    upload_database,
):
    session_factory, upload_root = upload_database
    with session_factory() as session:
        upload = create_subtitle_upload(session)
        first_chunk = chunks_for(upload)[0]
        first = put_chunk(session, upload.id, 0, first_chunk, upload_root)
        duplicate = put_chunk(session, upload.id, 0, first_chunk, upload_root)

        assert first.duplicate is False
        assert duplicate.duplicate is True
        assert duplicate.checksum_sha256 == first.checksum_sha256

        different = b"x" * len(first_chunk)
        with pytest.raises(VideoUploadConflictError, match="不同内容"):
            put_chunk(session, upload.id, 0, different, upload_root)
        receipts = list(
            session.scalars(
                select(VideoUploadChunk).where(VideoUploadChunk.upload_session_id == upload.id)
            )
        )
        assert len(receipts) == 1


def test_chunk_hash_size_and_index_are_validated_without_poisoning_resume(
    upload_database,
):
    session_factory, upload_root = upload_database
    with session_factory() as session:
        upload = create_subtitle_upload(session)
        first_chunk = chunks_for(upload)[0]

        with pytest.raises(VideoUploadValidationError, match="索引越界"):
            put_chunk(
                session,
                upload.id,
                upload.total_chunks,
                first_chunk,
                upload_root,
            )
        with pytest.raises(VideoUploadValidationError, match="大小应为"):
            write_upload_chunk(
                session,
                upload.id,
                index=0,
                stream=BytesIO(first_chunk[:-1]),
                checksum_sha256=sha256(first_chunk[:-1]),
                upload_root=upload_root,
            )
        with pytest.raises(VideoUploadValidationError, match="SHA-256 校验失败"):
            write_upload_chunk(
                session,
                upload.id,
                index=0,
                stream=BytesIO(first_chunk),
                checksum_sha256="0" * 64,
                upload_root=upload_root,
            )
        assert (
            session.scalar(
                select(VideoUploadChunk).where(VideoUploadChunk.upload_session_id == upload.id)
            )
            is None
        )

        recovered = put_chunk(session, upload.id, 0, first_chunk, upload_root)
        assert recovered.duplicate is False


def test_final_checksum_failure_marks_upload_failed_and_cancel_cleans_chunks(
    upload_database,
):
    session_factory, upload_root = upload_database
    wrong_total_checksum = sha256(b"not the requested file")
    with session_factory() as session:
        upload = create_subtitle_upload(session, checksum=wrong_total_checksum)
        upload_id = upload.id
        for index, value in enumerate(chunks_for(upload)):
            put_chunk(session, upload_id, index, value, upload_root)
        with pytest.raises(VideoUploadValidationError, match="合并文件"):
            complete_upload_session(session, upload_id, upload_root=upload_root)
        assert upload.status == "failed"
        assert upload.error == "合并文件的 SHA-256 校验失败"
        session.commit()

    with session_factory() as session:
        upload = get_upload_session(session, upload_id)
        assert upload.status == "failed"
        cancelled = cancel_upload_session(session, upload_id, upload_root=upload_root)
        assert cancelled.status == "cancelled"
        session.commit()
        assert not list((upload_root / "chunks" / upload_id).glob("*.part"))
        with pytest.raises(VideoUploadConflictError):
            put_chunk(session, upload_id, 0, chunks_for(upload)[0], upload_root)


def test_resumable_upload_http_flow_registers_one_project_asset(
    content_client,
    user_headers,
    other_headers,
):
    task = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "video",
            "name": f"断点上传-{uuid4().hex[:8]}",
            "topic": "断点上传接口测试",
        },
    )
    assert task.status_code == 200, task.text
    project = content_client.post(
        f"/api/v1/tasks/{task.json()['id']}/video-project",
        headers=user_headers,
    )
    assert project.status_code == 200, project.text
    chunk_size = 17
    created = content_client.post(
        "/api/v1/video-uploads",
        headers=user_headers,
        json={
            "project_id": project.json()["id"],
            "role": "subtitle",
            "filename": "captions.srt",
            "media_type": "text/plain",
            "total_size": len(SUBTITLE),
            "chunk_size": chunk_size,
            "checksum": sha256(SUBTITLE),
        },
    )
    assert created.status_code == 200, created.text
    upload = created.json()
    assert upload["missing_chunks"] == list(range(upload["total_chunks"]))
    assert (
        content_client.get(
            f"/api/v1/video-uploads/{upload['id']}", headers=other_headers
        ).status_code
        == 403
    )

    chunks = [
        SUBTITLE[offset : offset + chunk_size] for offset in range(0, len(SUBTITLE), chunk_size)
    ]
    for index in (0, 2):
        response = content_client.put(
            f"/api/v1/video-uploads/{upload['id']}/chunks/{index}",
            headers={**user_headers, "X-Chunk-Sha256": sha256(chunks[index])},
            files={"file": (f"{index}.part", chunks[index], "application/octet-stream")},
        )
        assert response.status_code == 200, response.text
    resumed = content_client.get(
        f"/api/v1/video-uploads/{upload['id']}", headers=user_headers
    ).json()
    assert resumed["received_chunks"] == [0, 2]
    assert (
        content_client.post(
            f"/api/v1/video-uploads/{upload['id']}/complete",
            headers=user_headers,
        ).status_code
        == 409
    )

    for index in resumed["missing_chunks"]:
        response = content_client.put(
            f"/api/v1/video-uploads/{upload['id']}/chunks/{index}",
            headers={**user_headers, "X-Chunk-Sha256": sha256(chunks[index])},
            files={"file": (f"{index}.part", chunks[index], "application/octet-stream")},
        )
        assert response.status_code == 200, response.text
    completed = content_client.post(
        f"/api/v1/video-uploads/{upload['id']}/complete",
        headers=user_headers,
    )
    assert completed.status_code == 200, completed.text
    payload = completed.json()
    assert payload["upload"]["status"] == "completed"
    assert payload["upload"]["project_asset_id"] == payload["asset"]["id"]
    assert payload["asset"]["metadata"]["resumable"] is True
    download = content_client.get(payload["asset"]["original_url"], headers=user_headers)
    assert download.content == SUBTITLE

    repeated = content_client.post(
        f"/api/v1/video-uploads/{upload['id']}/complete",
        headers=user_headers,
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["asset"]["id"] == payload["asset"]["id"]


def test_content_0006_resumable_upload_migration_upgrades_and_downgrades(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'content-video-uploads.db'}"
    monkeypatch.setenv("CONTENT_DATABASE_URL", database_url)
    config = Config(str(ROOT / "services" / "content_service" / "alembic.ini"))
    command.upgrade(config, "content_0005")
    engine = create_engine(database_url)
    assert "video_upload_sessions" not in inspect(engine).get_table_names()

    command.upgrade(config, "content_0006")
    inspector = inspect(engine)
    assert {
        "video_upload_sessions",
        "video_upload_chunks",
    } <= set(inspector.get_table_names())
    assert {
        "project_id",
        "owner_id",
        "total_chunks",
        "checksum_sha256",
        "status",
        "object_key",
        "project_asset_id",
    } <= {column["name"] for column in inspector.get_columns("video_upload_sessions")}
    assert {
        "upload_session_id",
        "chunk_index",
        "size_bytes",
        "checksum_sha256",
        "storage_key",
    } <= {column["name"] for column in inspector.get_columns("video_upload_chunks")}

    command.downgrade(config, "content_0005")
    assert "video_upload_sessions" not in inspect(engine).get_table_names()
    assert "video_upload_chunks" not in inspect(engine).get_table_names()
    command.downgrade(config, "base")
    engine.dispose()
