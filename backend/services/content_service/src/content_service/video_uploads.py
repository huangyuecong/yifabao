from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .media_ingest import (
    MediaIngestPolicy,
    MediaKind,
    MediaValidationError,
    read_upload_head,
    safe_filename,
    validate_upload_header,
)
from .models import VideoUploadChunk, VideoUploadSession

MAX_CHUNKS_PER_UPLOAD = 10_000
MAX_CHUNK_SIZE = 64 * 1024 * 1024
STREAM_BUFFER_SIZE = 1024 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_EXTENSION_KINDS: dict[str, MediaKind] = {
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    ".mp4": "video",
    ".mov": "video",
    ".m4v": "video",
    ".webm": "video",
    ".mkv": "video",
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
    ".aac": "audio",
    ".ogg": "audio",
    ".flac": "audio",
    ".srt": "subtitle",
    ".vtt": "subtitle",
    ".txt": "subtitle",
}
_ROLES_BY_KIND: dict[MediaKind, frozenset[str]] = {
    "image": frozenset({"video", "overlay", "logo", "intro", "outro"}),
    "video": frozenset({"video", "intro", "outro"}),
    "audio": frozenset({"voice", "music", "sfx"}),
    "subtitle": frozenset({"subtitle"}),
}


class VideoUploadError(RuntimeError):
    pass


class VideoUploadNotFoundError(VideoUploadError):
    pass


class VideoUploadConflictError(VideoUploadError):
    pass


class VideoUploadValidationError(VideoUploadError):
    pass


class VideoUploadIncompleteError(VideoUploadConflictError):
    def __init__(self, missing_chunks: list[int]):
        self.missing_chunks = missing_chunks
        super().__init__(f"上传尚缺少分块：{missing_chunks}")


@dataclass(frozen=True, slots=True)
class ChunkWriteResult:
    upload_id: str
    index: int
    size_bytes: int
    checksum_sha256: str
    duplicate: bool


@dataclass(frozen=True, slots=True)
class UploadCompletion:
    upload_id: str
    path: Path
    object_key: str
    media_kind: MediaKind
    media_type: str
    total_size: int
    checksum_sha256: str


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_checksum(value: str, *, field_name: str) -> str:
    normalized = value.strip().lower()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise VideoUploadValidationError(f"{field_name} 必须是 64 位 SHA-256")
    return normalized


def _upload_root(upload_root: Path) -> Path:
    root = upload_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _storage_path(upload_root: Path, storage_key: str) -> Path:
    root = _upload_root(upload_root)
    if not storage_key or Path(storage_key).is_absolute():
        raise VideoUploadValidationError("上传临时存储 key 必须是相对路径")
    target = (root / storage_key).resolve()
    if target != root and root not in target.parents:
        raise VideoUploadValidationError("上传临时存储 key 越界")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(STREAM_BUFFER_SIZE):
            digest.update(block)
    return digest.hexdigest()


def get_upload_session(session: Session, upload_id: str) -> VideoUploadSession:
    upload = session.get(VideoUploadSession, upload_id)
    if upload is None:
        raise VideoUploadNotFoundError("分片上传会话不存在")
    return upload


def create_upload_session(
    session: Session,
    *,
    project_id: str,
    owner_id: str,
    role: str,
    filename: str,
    media_type: str,
    total_size: int,
    chunk_size: int,
    checksum: str,
    policy: MediaIngestPolicy | None = None,
) -> VideoUploadSession:
    current_policy = policy or MediaIngestPolicy()
    normalized_filename = safe_filename(filename)
    media_kind = _EXTENSION_KINDS.get(Path(normalized_filename).suffix.lower())
    if media_kind is None:
        raise VideoUploadValidationError("不支持的素材扩展名")
    if role not in _ROLES_BY_KIND[media_kind]:
        raise VideoUploadValidationError(f"{media_kind} 素材不能用作 {role}")
    if not owner_id.strip():
        raise VideoUploadValidationError("上传会话属主不能为空")
    if not media_type.strip() or len(media_type) > 128:
        raise VideoUploadValidationError("媒体类型不能为空且不能超过 128 个字符")
    if total_size < 1 or total_size > current_policy.max_bytes[media_kind]:
        raise VideoUploadValidationError("素材文件超过当前类型的大小限制")
    if chunk_size < 1 or chunk_size > MAX_CHUNK_SIZE:
        raise VideoUploadValidationError("分块大小必须在 1 字节到 64 MiB 之间")
    total_chunks = math.ceil(total_size / chunk_size)
    if total_chunks > MAX_CHUNKS_PER_UPLOAD:
        raise VideoUploadValidationError(f"单次上传最多允许 {MAX_CHUNKS_PER_UPLOAD} 个分块")
    upload = VideoUploadSession(
        project_id=project_id,
        owner_id=owner_id,
        role=role,
        filename=normalized_filename,
        media_type=media_type.split(";", 1)[0].strip().lower(),
        media_kind=media_kind,
        total_size=total_size,
        chunk_size=chunk_size,
        total_chunks=total_chunks,
        checksum_sha256=_normalize_checksum(checksum, field_name="checksum"),
        status="uploading",
    )
    session.add(upload)
    session.flush()
    return upload


def _chunk_rows(session: Session, upload_id: str) -> list[VideoUploadChunk]:
    return list(
        session.scalars(
            select(VideoUploadChunk)
            .where(VideoUploadChunk.upload_session_id == upload_id)
            .order_by(VideoUploadChunk.chunk_index)
        ).all()
    )


def _expected_chunk_size(upload: VideoUploadSession, index: int) -> int:
    if not 0 <= index < upload.total_chunks:
        raise VideoUploadValidationError(f"分块索引越界，有效范围为 0 到 {upload.total_chunks - 1}")
    if index < upload.total_chunks - 1:
        return upload.chunk_size
    return upload.total_size - upload.chunk_size * (upload.total_chunks - 1)


def upload_session_json(
    session: Session,
    upload: VideoUploadSession,
    *,
    upload_root: Path,
) -> dict:
    rows = _chunk_rows(session, upload.id)
    if upload.status == "completed":
        valid_rows = rows
    else:
        valid_rows = [
            row
            for row in rows
            if (path := _storage_path(upload_root, row.storage_key)).is_file()
            and path.stat().st_size == row.size_bytes
        ]
    received_chunks = sorted({row.chunk_index for row in valid_rows})
    received_set = set(received_chunks)
    missing_chunks = [index for index in range(upload.total_chunks) if index not in received_set]
    return {
        "id": upload.id,
        "project_id": upload.project_id,
        "owner_id": upload.owner_id,
        "role": upload.role,
        "filename": upload.filename,
        "media_type": upload.media_type,
        "media_kind": upload.media_kind,
        "total_size": upload.total_size,
        "chunk_size": upload.chunk_size,
        "total_chunks": upload.total_chunks,
        "checksum_sha256": upload.checksum_sha256,
        "status": upload.status,
        "project_asset_id": upload.project_asset_id,
        "received_chunks": received_chunks,
        "missing_chunks": [] if upload.status == "completed" else missing_chunks,
        "received_size": sum(row.size_bytes for row in valid_rows),
        "error": upload.error,
        "created_at": upload.created_at,
        "updated_at": upload.updated_at,
        "completed_at": upload.completed_at,
    }


def write_upload_chunk(
    session: Session,
    upload_id: str,
    *,
    index: int,
    stream: BinaryIO,
    checksum_sha256: str,
    upload_root: Path,
) -> ChunkWriteResult:
    upload = get_upload_session(session, upload_id)
    if upload.status != "uploading":
        raise VideoUploadConflictError(f"状态为 {upload.status} 的上传会话不能再写入分块")
    expected_size = _expected_chunk_size(upload, index)
    checksum = _normalize_checksum(
        checksum_sha256,
        field_name="X-Chunk-Sha256",
    )
    existing = session.scalar(
        select(VideoUploadChunk).where(
            VideoUploadChunk.upload_session_id == upload.id,
            VideoUploadChunk.chunk_index == index,
        )
    )
    if existing is not None and existing.checksum_sha256 != checksum:
        raise VideoUploadConflictError("该分块索引已经写入了不同内容")
    final_key = f"chunks/{upload.id}/{index:08d}.part"
    final_path = _storage_path(upload_root, final_key)
    if (
        existing is not None
        and final_path.is_file()
        and final_path.stat().st_size == expected_size
        and _file_sha256(final_path) == checksum
    ):
        return ChunkWriteResult(
            upload_id=upload.id,
            index=index,
            size_bytes=existing.size_bytes,
            checksum_sha256=existing.checksum_sha256,
            duplicate=True,
        )

    temporary_key = f"chunks/{upload.id}/.{index:08d}-{uuid4().hex}.tmp"
    temporary_path = _storage_path(upload_root, temporary_key)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with temporary_path.open("wb") as output:
            while block := stream.read(STREAM_BUFFER_SIZE):
                size_bytes += len(block)
                if size_bytes > expected_size:
                    raise VideoUploadValidationError(
                        f"分块 {index} 超过预期大小 {expected_size} 字节"
                    )
                digest.update(block)
                output.write(block)
        if size_bytes != expected_size:
            raise VideoUploadValidationError(
                f"分块 {index} 大小应为 {expected_size} 字节，实际为 {size_bytes} 字节"
            )
        actual_checksum = digest.hexdigest()
        if actual_checksum != checksum:
            raise VideoUploadValidationError(f"分块 {index} 的 SHA-256 校验失败")
        temporary_path.replace(final_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    if existing is None:
        existing = VideoUploadChunk(
            upload_session_id=upload.id,
            chunk_index=index,
            size_bytes=size_bytes,
            checksum_sha256=checksum,
            storage_key=final_key,
        )
        session.add(existing)
    else:
        existing.size_bytes = size_bytes
        existing.checksum_sha256 = checksum
        existing.storage_key = final_key
    upload.error = None
    session.flush()
    return ChunkWriteResult(
        upload_id=upload.id,
        index=index,
        size_bytes=size_bytes,
        checksum_sha256=checksum,
        duplicate=False,
    )


def _completed_result(
    upload: VideoUploadSession,
    *,
    upload_root: Path,
) -> UploadCompletion:
    if not upload.object_key:
        upload.status = "failed"
        upload.error = "已完成会话缺少合并文件引用"
        raise VideoUploadValidationError(upload.error)
    path = _storage_path(upload_root, upload.object_key)
    if not path.is_file():
        upload.status = "failed"
        upload.error = "已完成会话的合并文件不存在"
        raise VideoUploadValidationError(upload.error)
    return UploadCompletion(
        upload_id=upload.id,
        path=path,
        object_key=upload.object_key,
        media_kind=upload.media_kind,
        media_type=upload.media_type,
        total_size=upload.total_size,
        checksum_sha256=upload.checksum_sha256,
    )


def complete_upload_session(
    session: Session,
    upload_id: str,
    *,
    upload_root: Path,
    policy: MediaIngestPolicy | None = None,
) -> UploadCompletion:
    upload = get_upload_session(session, upload_id)
    if upload.status == "completed":
        return _completed_result(upload, upload_root=upload_root)
    if upload.status != "uploading":
        raise VideoUploadConflictError(f"状态为 {upload.status} 的上传会话不能完成")
    rows = _chunk_rows(session, upload.id)
    rows_by_index = {row.chunk_index: row for row in rows}
    missing = [
        index
        for index in range(upload.total_chunks)
        if index not in rows_by_index
        or not _storage_path(upload_root, rows_by_index[index].storage_key).is_file()
    ]
    if missing:
        raise VideoUploadIncompleteError(missing)

    assembling_key = f"assembling/{upload.id}-{uuid4().hex}.tmp"
    assembling_path = _storage_path(upload_root, assembling_key)
    total_digest = hashlib.sha256()
    merged_size = 0
    try:
        with assembling_path.open("wb") as output:
            for index in range(upload.total_chunks):
                row = rows_by_index[index]
                chunk_path = _storage_path(upload_root, row.storage_key)
                expected_size = _expected_chunk_size(upload, index)
                chunk_digest = hashlib.sha256()
                chunk_size = 0
                with chunk_path.open("rb") as source:
                    while block := source.read(STREAM_BUFFER_SIZE):
                        chunk_size += len(block)
                        chunk_digest.update(block)
                        total_digest.update(block)
                        output.write(block)
                if (
                    chunk_size != expected_size
                    or chunk_size != row.size_bytes
                    or chunk_digest.hexdigest() != row.checksum_sha256
                ):
                    chunk_path.unlink(missing_ok=True)
                    session.delete(row)
                    session.flush()
                    raise VideoUploadValidationError(f"分块 {index} 的文件已损坏，请重新上传")
                merged_size += chunk_size
        actual_checksum = total_digest.hexdigest()
        if merged_size != upload.total_size:
            upload.status = "failed"
            upload.error = f"合并后大小应为 {upload.total_size} 字节，实际为 {merged_size} 字节"
            session.flush()
            raise VideoUploadValidationError(upload.error)
        if actual_checksum != upload.checksum_sha256:
            upload.status = "failed"
            upload.error = "合并文件的 SHA-256 校验失败"
            session.flush()
            raise VideoUploadValidationError(upload.error)
        try:
            with assembling_path.open("rb") as assembled_source:
                assembled_head = read_upload_head(assembled_source)
            media_kind, canonical_media_type = validate_upload_header(
                filename=upload.filename,
                declared_media_type=upload.media_type,
                head=assembled_head,
                size_bytes=merged_size,
                policy=policy or MediaIngestPolicy(),
            )
        except MediaValidationError as exc:
            upload.status = "failed"
            upload.error = str(exc)[:1000]
            session.flush()
            raise VideoUploadValidationError(str(exc)) from exc
        if media_kind != upload.media_kind:
            upload.status = "failed"
            upload.error = "合并文件类型与上传会话不一致"
            session.flush()
            raise VideoUploadValidationError(upload.error)
        completed_key = f"completed/{upload.id}/{upload.filename}"
        completed_path = _storage_path(upload_root, completed_key)
        assembling_path.replace(completed_path)
    finally:
        assembling_path.unlink(missing_ok=True)

    for row in rows:
        _storage_path(upload_root, row.storage_key).unlink(missing_ok=True)
    upload.status = "completed"
    upload.object_key = completed_key
    upload.media_type = canonical_media_type
    upload.error = None
    upload.completed_at = _now()
    session.flush()
    return _completed_result(upload, upload_root=upload_root)


def cancel_upload_session(
    session: Session,
    upload_id: str,
    *,
    upload_root: Path,
) -> VideoUploadSession:
    upload = get_upload_session(session, upload_id)
    if upload.status == "completed":
        raise VideoUploadConflictError("已完成的上传会话不能取消")
    if upload.status == "cancelled":
        return upload
    for row in _chunk_rows(session, upload.id):
        _storage_path(upload_root, row.storage_key).unlink(missing_ok=True)
    upload.status = "cancelled"
    upload.error = None
    session.flush()
    return upload
