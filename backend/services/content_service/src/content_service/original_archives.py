"""Verified original-file custody; original documents are never deleted here.

Search indexes have a separate marker and lifecycle. An archive is ready only
after its bytes have been downloaded and hashed. The platform may index it too;
the application's retriever only queries the separately authorized text IDs.
Cache eviction is an explicit, offline operation protected against live readers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, RLock
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from .models import AssetRef, LibraryItem, OriginalArchive, ResourceEntry
from .weknora import WeKnoraError

logger = logging.getLogger(__name__)
SUPPORTED_SUFFIXES = frozenset({
    # WeKnora knowledge_util.go: supportedImportFileExtensions. Parser/model
    # configuration can still reject a supported type; never broaden this to
    # the formats accepted by the application's local media uploader.
    ".pdf", ".txt", ".docx", ".doc", ".epub",
    ".html", ".htm", ".mhtml", ".md", ".markdown", ".xmind",
    ".png", ".jpg", ".jpeg", ".gif",
    ".csv", ".xlsx", ".xls", ".pptx", ".ppt", ".json",
    ".mp3", ".wav", ".m4a", ".flac", ".ogg",
})
RUNNABLE = ("queued", "processing")
DUPLICATE_ARCHIVE_ERROR = WeKnoraError("duplicate_unowned").safe_message + "；本地文件保留"


class ArchiveUnavailableError(RuntimeError):
    """A mapped original cannot be recovered; do not misreport it as deleted."""

    code = "original_archive_unavailable"


class ArchiveIntegrityError(ArchiveUnavailableError):
    code = "original_archive_integrity"


class CacheInUseError(RuntimeError):
    pass


def _fingerprint(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def archive_marker(connection_id: str, storage_key: str, checksum: str) -> str:
    value = json.dumps([connection_id, storage_key, checksum], separators=(",", ":"))
    return "content-agent-original:" + hashlib.sha256(value.encode()).hexdigest()


def _formal(session: Session, entry: ResourceEntry | None, levels: frozenset[str]) -> bool:
    if entry is None or entry.data_level not in levels:
        return False
    try:
        metadata = json.loads(entry.metadata_json or "{}")
    except (ValueError, TypeError):
        return False
    if not isinstance(metadata, dict) or metadata.get("source_type") == "task-image-reference":
        return False
    # Inactive and historical LibraryItems intentionally retain their originals.
    return session.scalar(select(LibraryItem.id).where(
        LibraryItem.resource_entry_id == entry.id,
    ).limit(1)) is not None


def lookup_archive(session: Session, key: str, connection_id: str) -> OriginalArchive | None:
    return session.scalar(select(OriginalArchive).where(
        OriginalArchive.storage_key == key, OriginalArchive.connection_id == connection_id,
    ))


def _public(row: OriginalArchive) -> dict:
    return {"archive_id": row.id, "storage_key": row.storage_key, "status": row.status,
            "size_bytes": row.size_bytes, "error": row.last_error,
            "can_retry": row.status in {"failed", "ambiguous"},
            "verified_at": row.verified_at.isoformat() if row.verified_at else None}


def get_archive_status(session: Session, key: str, connection_id: str) -> dict:
    row = lookup_archive(session, key, connection_id)
    return _public(row) if row else {
        "storage_key": key, "status": "not_archived", "can_retry": False, "error": None,
    }


def retry_archive(session: Session, key: str, connection_id: str) -> dict:
    row = lookup_archive(session, key, connection_id)
    if row is None or row.status not in {"failed", "ambiguous"}:
        raise ValueError("当前原件没有可重试的托管任务")
    now = datetime.now(UTC)
    result = session.execute(update(OriginalArchive).where(
        OriginalArchive.id == row.id, OriginalArchive.status.in_(("failed", "ambiguous")),
        or_(OriginalArchive.lease_expires_at.is_(None), OriginalArchive.lease_expires_at <= now),
    ).values(status="queued", attempt=0, available_at=now, last_error=None,
             lease_token=None, lease_expires_at=None))
    if result.rowcount != 1:
        raise ValueError("原件托管任务正在运行")
    session.flush()
    session.refresh(row)
    return _public(row)


class CacheMaintenanceLock:
    """Cross-process shared runtime / exclusive offline-maintenance lock.

    All runtime stores keep their shared handle for their complete lifetime,
    covering FileResponse and ffmpeg consumers that outlive path_for().
    """

    def __init__(self, root: Path, *, exclusive: bool = False):
        self.root = Path(root)
        self.exclusive = exclusive
        self.handle = None
        self._windows_overlapped = None

    def acquire(self):
        if self.handle is not None:
            return self
        self.root.mkdir(parents=True, exist_ok=True)
        handle = (self.root / ".original-cache.lock").open("a+b")
        try:
            if os.name == "nt":
                import ctypes
                import msvcrt
                from ctypes import wintypes

                class Overlapped(ctypes.Structure):
                    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                                ("hEvent", wintypes.HANDLE)]

                self._windows_overlapped = Overlapped()
                api = ctypes.WinDLL("kernel32", use_last_error=True).LockFileEx
                api.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
                api.restype = wintypes.BOOL
                flags = 1 | (2 if self.exclusive else 0)  # fail immediately; exclusive
                if not api(msvcrt.get_osfhandle(handle.fileno()), flags, 0, 1, 0,
                           ctypes.byref(self._windows_overlapped)):
                    raise OSError(ctypes.get_last_error(), "cache lock unavailable")
            else:
                import fcntl
                flags = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
                fcntl.flock(handle.fileno(), flags | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise CacheInUseError("文件存储正在使用；清理缓存前必须停止所有应用及工作进程") from exc
        self.handle = handle
        return self

    def close(self):
        if self.handle is not None:
            # Closing the sole handle releases LockFileEx/flock, including on
            # abnormal process termination. Never unlink the lock inode.
            self.handle.close()
            self.handle = None
            self._windows_overlapped = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()


def remote_source_marker(row: OriginalArchive) -> str:
    return row.remote_source_marker or row.source_marker


def _check_document(client: Any, row: OriginalArchive, document: Any) -> None:
    if (document.knowledge_base_id != row.knowledge_base_id
            or row.connection_id != client.connection_id
            or document.metadata.get("local_source_marker") != remote_source_marker(row)
            or (row.remote_id and row.remote_id != document.id)):
        raise ArchiveIntegrityError("远端原件归属校验失败，保留本地文件")


def _download(client: Any, row: OriginalArchive, destination: Path) -> None:
    if not row.remote_id or not row.checksum_sha256 or row.size_bytes <= 0:
        raise ArchiveUnavailableError("原件托管信息不完整")
    client.download_document(row.remote_id, destination,
                             expected_sha256=row.checksum_sha256,
                             expected_size=row.size_bytes, source_marker=remote_source_marker(row))
    # Defense in depth: callers must never trust an adapter which failed to
    # enforce its promised digest and size checks.
    if _fingerprint(destination) != (row.checksum_sha256, row.size_bytes):
        destination.unlink(missing_ok=True)
        raise ArchiveIntegrityError("平台原件校验不一致")


class RemoteArchiveAssetStore:
    """Local writes and transparent, checksum-verified original rehydration."""

    def __init__(self, local_store: Any, session_factory: Callable[[], Session], client: Any):
        self.local_store = local_store
        self.root = local_store.root
        self.session_factory = session_factory
        self.client = client
        self.provider_media_url_signer = local_store.provider_media_url_signer
        self._runtime_lock = CacheMaintenanceLock(self.root)
        self._read_lock = RLock()
        self._verified_local: dict[str, tuple] = {}
        self.acquire_runtime_lock()

    def acquire_runtime_lock(self):
        self._runtime_lock.acquire()

    def close(self):
        self._runtime_lock.close()

    def __del__(self):
        if hasattr(self, "_runtime_lock"):
            self.close()

    def _mapping(self, key: str) -> OriginalArchive | None:
        with self.session_factory() as session:
            row = lookup_archive(session, key, self.client.connection_id)
            if row is not None:
                session.expunge(row)
            return row

    @staticmethod
    def _stat_signature(path: Path) -> tuple[int, ...]:
        stat = path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def _local_verified(self, path: Path, row: OriginalArchive) -> bool:
        """Rehash only when this process sees changed bytes/identity/metadata.

        Purge never consults this cache: offline deletion always hashes the real
        local and downloaded bytes again. This optimization is for repeated
        authorized range/preview requests only.
        """
        with self._read_lock:
            before = self._stat_signature(path)
            cache_key = (before, row.id, row.checksum_sha256, row.size_bytes)
            if self._verified_local.get(row.storage_key) == cache_key:
                return True
            valid = _fingerprint(path) == (row.checksum_sha256, row.size_bytes)
            if valid and self._stat_signature(path) == before:
                self._verified_local[row.storage_key] = cache_key
                return True
            self._verified_local.pop(row.storage_key, None)
            return False

    def path_for(self, key: str) -> Path:
        self.acquire_runtime_lock()
        path = self.local_store.path_for(key)
        row = self._mapping(key)
        if path.is_file():
            if row and row.status == "ready" and not self._local_verified(path, row):
                raise ArchiveIntegrityError("本地原件与托管记录不一致，已保留现场文件")
            return path
        if row is None:
            with self.session_factory() as session:
                other = session.scalar(select(OriginalArchive.id).where(
                    OriginalArchive.storage_key == key,
                ).limit(1))
            if other:
                raise ArchiveUnavailableError("原件属于另一知识库连接，请恢复原连接配置")
            return path
        if row.status != "ready":
            raise ArchiveUnavailableError("原件尚未完成托管且本地文件缺失")
        with self._read_lock:
            if path.is_file() and self._local_verified(path, row):
                return path
            # The adapter verifies metadata ownership, downloads to a temporary
            # sibling and atomically replaces only after integrity checks pass.
            try:
                _download(self.client, row, path)
                self._verified_local.pop(key, None)
            except Exception as exc:
                raise ArchiveUnavailableError("平台原件暂时无法读取，请稍后重试") from exc
        return path

    def _check_write(self, key: str, digest: str, size: int) -> None:
        with self.session_factory() as session:
            fingerprints = session.execute(select(
                OriginalArchive.checksum_sha256, OriginalArchive.size_bytes,
            ).where(OriginalArchive.storage_key == key)).all()
        if any(tuple(fingerprint) != (digest, size) for fingerprint in fingerprints):
            raise ArchiveIntegrityError("托管原件不可覆盖，请使用新文件存储标识")

    def put_bytes(self, key: str, content: bytes, media_type: str):
        self.acquire_runtime_lock()
        self._check_write(key, hashlib.sha256(content).hexdigest(), len(content))
        return self.local_store.put_bytes(key, content, media_type)

    def put_stream(self, key: str, stream, media_type: str):
        self.acquire_runtime_lock()
        with self.session_factory() as session:
            mapped = session.scalar(select(OriginalArchive.id).where(
                OriginalArchive.storage_key == key,
            ).limit(1))
        if mapped is None:
            # New upload keys are UUIDs and have no formal AssetRef until after
            # this write completes. Avoid staging a second copy of large video
            # uploads or assembled chunks which cannot have an archive yet.
            return self.local_store.put_stream(key, stream, media_type)
        # Stage writes locally; never re-download an old original on a write.
        with tempfile.TemporaryFile() as staged:
            digest, size = hashlib.sha256(), 0
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                staged.write(chunk)
            self._check_write(key, digest.hexdigest(), size)
            staged.seek(0)
            return self.local_store.put_stream(key, staged, media_type)

    def read_bytes(self, key: str) -> bytes:
        return self.path_for(key).read_bytes()

    def exists(self, key: str) -> bool:
        self.acquire_runtime_lock()
        if self.local_store.path_for(key).is_file():
            return True
        row = self._mapping(key)
        return bool(row and row.status == "ready" and row.remote_id)

    def provider_url(self, key: str) -> str | None:
        if not self.exists(key):
            return None
        return self.provider_media_url_signer.create_url(Path(key).as_posix())


class OriginalArchiveRunner:
    def __init__(self, session_factory: Callable[[], Session], client: Any, *,
                 asset_store: Any, allowed_data_levels: Iterable[str] = ("public", "internal"),
                 interval_seconds: float = 30, max_file_bytes: int = 50 * 1024 * 1024):
        self.session_factory, self.client = session_factory, client
        self.connection_id, self.knowledge_base_id = client.connection_id, client.knowledge_base_id
        self.asset_store = getattr(asset_store, "local_store", asset_store)
        self.allowed_data_levels = frozenset(allowed_data_levels)
        self.interval_seconds = max(1, interval_seconds)
        self.max_file_bytes = max_file_bytes
        self.lease_seconds = max(300, int(getattr(client, "timeout_seconds", 30) * 110))
        self.stop_event: Event | None = None

    def _matching_ready(self, row: OriginalArchive):
        return select(OriginalArchive).where(
            OriginalArchive.id != row.id,
            OriginalArchive.connection_id == self.connection_id,
            OriginalArchive.knowledge_base_id == self.knowledge_base_id,
            OriginalArchive.status == "ready", OriginalArchive.remote_id.is_not(None),
            OriginalArchive.verified_at.is_not(None),
            OriginalArchive.checksum_sha256 == row.checksum_sha256,
            OriginalArchive.size_bytes == row.size_bytes, OriginalArchive.size_bytes > 0,
        ).order_by(OriginalArchive.id)

    @staticmethod
    def _archive_snapshot(row: OriginalArchive) -> dict:
        return {name: getattr(row, name) for name in (
            "id", "storage_key", "entry_id", "connection_id", "knowledge_base_id",
            "checksum_sha256", "size_bytes", "source_marker", "remote_id",
            "remote_source_marker", "status", "verified_at", "updated_at",
        )}

    @staticmethod
    def _receipt_snapshot(session: Session, row: OriginalArchive) -> dict:
        asset = session.scalar(select(AssetRef).where(
            AssetRef.storage_key == row.storage_key, AssetRef.entry_id == row.entry_id,
        ))
        if (asset is None or not asset.checksum or asset.size_bytes is None
                or (asset.checksum, asset.size_bytes) != (row.checksum_sha256, row.size_bytes)
                or row.source_marker != archive_marker(
                    row.connection_id, row.storage_key, row.checksum_sha256,
                )):
            raise ArchiveIntegrityError("共享原件未匹配完整原上传记录，未建立关联")
        return {name: getattr(asset, name) for name in (
            "id", "storage_key", "entry_id", "checksum", "size_bytes",
        )}

    @staticmethod
    def _snapshot_condition(model: Any, snapshot: dict):
        return and_(*(getattr(model, name) == value for name, value in snapshot.items()))

    @classmethod
    def _snapshot_exists(cls, model: Any, snapshot: dict):
        other = aliased(model)
        return select(other.id).where(cls._snapshot_condition(other, snapshot)).correlate(None).exists()

    def _try_reuse(self, row_id: str, token: str) -> bool:
        """Reuse only a locally owned, previously verified, identical original.

        No remote object is claimed, changed or uploaded here. All ownership
        evidence is checked again by the final SQL lease compare-and-swap.
        """
        with self.session_factory() as session:
            row = self._load(session, row_id, token)
            if row is None:
                return True  # An expired worker must not continue to upload.
            entry = session.get(ResourceEntry, row.entry_id)
            if (row.remote_id or row.connection_id != self.connection_id
                    or row.knowledge_base_id != self.knowledge_base_id
                    or not _formal(session, entry, self.allowed_data_levels)):
                return False
            candidate = session.scalar(self._matching_ready(row).limit(1))
            if candidate is None:
                return False
            target_snapshot = self._archive_snapshot(row)
            entry_snapshot = {name: getattr(entry, name) for name in ("id", "data_level", "updated_at")}
            receipts = [self._receipt_snapshot(session, row),
                        self._receipt_snapshot(session, candidate)]
            path = self.asset_store.path_for(row.storage_key)
            if (path.suffix.lower() not in SUPPORTED_SUFFIXES
                    or not 0 < row.size_bytes <= self.max_file_bytes
                    or not path.is_file()
                    or _fingerprint(path) != (row.checksum_sha256, row.size_bytes)):
                raise ArchiveIntegrityError("本地原件与共享托管记录不一致，未建立关联")
            candidate_snapshot = self._archive_snapshot(candidate)
            marker = remote_source_marker(candidate)
            # A shared candidate still points at the original local owner's
            # ready record; never turn an arbitrary remote marker into trust.
            owner = session.scalar(select(OriginalArchive).where(
                OriginalArchive.connection_id == self.connection_id,
                OriginalArchive.knowledge_base_id == self.knowledge_base_id,
                OriginalArchive.status == "ready", OriginalArchive.verified_at.is_not(None),
                OriginalArchive.source_marker == marker,
                OriginalArchive.remote_id == candidate.remote_id,
                OriginalArchive.checksum_sha256 == row.checksum_sha256,
                OriginalArchive.size_bytes == row.size_bytes,
                or_(OriginalArchive.remote_source_marker.is_(None),
                    OriginalArchive.remote_source_marker == OriginalArchive.source_marker),
            ).limit(1))
            if owner is None:
                raise ArchiveIntegrityError("共享原件缺少本项目的原始归属记录，未建立关联")
            owner_snapshot = self._archive_snapshot(owner)
            receipts.append(self._receipt_snapshot(session, owner))
            session.expunge(candidate)
        document = self.client.get_document(candidate.remote_id)
        _check_document(self.client, candidate, document)
        if (document.metadata.get("local_archive_kind") != "original"
                or document.metadata.get("local_original_sha256") != candidate.checksum_sha256
                or document.metadata.get("local_original_size") != str(candidate.size_bytes)):
            raise ArchiveIntegrityError("平台共享原件的类型或文件摘要不匹配，未建立关联")
        with tempfile.TemporaryDirectory(prefix="content-original-reuse-") as folder:
            _download(self.client, candidate, Path(folder) / "original")
        if not path.is_file() or _fingerprint(path) != (candidate.checksum_sha256, candidate.size_bytes):
            raise ArchiveIntegrityError("验证期间本地原件发生变化，未建立关联")
        now = datetime.now(UTC)
        with self.session_factory() as session:
            conditions = [
                self._snapshot_condition(OriginalArchive, target_snapshot),
                OriginalArchive.lease_token == token, OriginalArchive.lease_expires_at > now,
                self._snapshot_exists(OriginalArchive, candidate_snapshot),
                self._snapshot_exists(OriginalArchive, owner_snapshot),
                self._snapshot_exists(ResourceEntry, entry_snapshot),
                select(LibraryItem.id).where(
                    LibraryItem.resource_entry_id == row.entry_id,
                ).correlate(None).exists(),
                *(self._snapshot_exists(AssetRef, receipt) for receipt in receipts),
            ]
            result = session.execute(update(OriginalArchive).where(*conditions).values(
                status="ready", remote_id=candidate.remote_id, remote_source_marker=marker,
                last_error=None, verified_at=now, attempt=0,
                available_at=now + timedelta(seconds=self.interval_seconds),
                lease_token=None, lease_expires_at=None,
            ).execution_options(synchronize_session=False))
            session.commit()
            if result.rowcount != 1:
                # Candidate, receipt or lease changed while downloading. Never
                # fall through to a POST using this stale view of ownership.
                self._finish(row_id, token, status="failed",
                             last_error="验证期间托管记录发生变化，未建立共享关联；本地文件保留")
        return True

    def _requeue_owned_duplicates(self, session: Session) -> int:
        now = datetime.now(UTC)
        rows = session.scalars(select(OriginalArchive).where(
            OriginalArchive.connection_id == self.connection_id,
            OriginalArchive.knowledge_base_id == self.knowledge_base_id,
            OriginalArchive.status == "failed", OriginalArchive.remote_id.is_(None),
            or_(OriginalArchive.lease_expires_at.is_(None), OriginalArchive.lease_expires_at <= now),
        )).all()
        requeued = 0
        for row in rows:
            # Compare Text in Python for Dameng CLOB compatibility. Only the
            # exact historical duplicate error is auto-recovered, once. Any
            # new verification failure gets a different error and stays failed.
            if row.last_error != DUPLICATE_ARCHIVE_ERROR:
                continue
            candidate = session.scalar(self._matching_ready(row).limit(1))
            if candidate is None:
                continue
            result = session.execute(update(OriginalArchive).where(
                OriginalArchive.id == row.id, OriginalArchive.status == "failed",
                OriginalArchive.remote_id.is_(None), OriginalArchive.updated_at == row.updated_at,
                or_(OriginalArchive.lease_expires_at.is_(None), OriginalArchive.lease_expires_at <= now),
            ).values(status="queued", available_at=now, last_error=None,
                     lease_token=None, lease_expires_at=None))
            requeued += result.rowcount
        return requeued

    def reconcile(self) -> int:
        created = 0
        with self.session_factory() as session:
            rows = session.execute(select(AssetRef, ResourceEntry).join(
                ResourceEntry, AssetRef.entry_id == ResourceEntry.id,
            )).all()
            for asset, entry in rows:
                if not _formal(session, entry, self.allowed_data_levels):
                    continue
                if lookup_archive(session, asset.storage_key, self.connection_id):
                    continue
                path = self.asset_store.path_for(asset.storage_key)
                status, error = "queued", None
                if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    status, error = "unsupported", "知识平台不支持此原件格式，保留在文件存储"
                elif not path.is_file():
                    status, error = "failed", "本地原件缺失，未托管"
                elif not 0 < path.stat().st_size <= self.max_file_bytes:
                    status, error = "unsupported", "原件为空或超过平台上传大小限制，保留在文件存储"
                checksum, size = ("", 0)
                if status == "queued":
                    checksum, size = _fingerprint(path)
                    if ((asset.checksum and asset.checksum != checksum)
                            or (asset.size_bytes is not None and asset.size_bytes != size)):
                        status, error = "failed", "本地原件与资料记录校验不一致，未上传"
                try:
                    with session.begin_nested():
                        session.add(OriginalArchive(
                            storage_key=asset.storage_key, entry_id=entry.id,
                            connection_id=self.connection_id, knowledge_base_id=self.knowledge_base_id,
                            checksum_sha256=checksum, size_bytes=size,
                            source_marker=archive_marker(self.connection_id, asset.storage_key, checksum),
                            status=status, last_error=error,
                        ))
                        session.flush()
                    created += 1
                except IntegrityError:
                    pass
            created += self._requeue_owned_duplicates(session)
            session.commit()
        return created

    def _claim(self) -> tuple[str, str] | None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            ids = session.scalars(select(OriginalArchive.id).where(
                OriginalArchive.connection_id == self.connection_id,
                OriginalArchive.status.in_(RUNNABLE), OriginalArchive.available_at <= now,
                or_(OriginalArchive.lease_expires_at.is_(None), OriginalArchive.lease_expires_at <= now),
            ).order_by(OriginalArchive.available_at, OriginalArchive.id).limit(10)).all()
            for row_id in ids:
                token = uuid4().hex
                result = session.execute(update(OriginalArchive).where(
                    OriginalArchive.id == row_id, OriginalArchive.status.in_(RUNNABLE),
                    OriginalArchive.available_at <= now,
                    or_(OriginalArchive.lease_expires_at.is_(None), OriginalArchive.lease_expires_at <= now),
                ).values(lease_token=token, lease_expires_at=now + timedelta(seconds=self.lease_seconds)))
                session.commit()
                if result.rowcount == 1:
                    return row_id, token
        return None

    def _load(self, session: Session, row_id: str, token: str) -> OriginalArchive | None:
        return session.scalar(select(OriginalArchive).where(
            OriginalArchive.id == row_id, OriginalArchive.lease_token == token,
            OriginalArchive.lease_expires_at > datetime.now(UTC),
        ))

    def _process(self, row_id: str, token: str) -> None:
        if self._try_reuse(row_id, token):
            return
        with self.session_factory() as session:
            row = self._load(session, row_id, token)
            if row is None:
                return
            entry = session.get(ResourceEntry, row.entry_id)
            title = entry.title if entry is not None else Path(row.storage_key).name
            if not row.upload_attempted and not _formal(session, entry, self.allowed_data_levels):
                row.status, row.last_error = "excluded", "资料不在允许托管范围，未上传"
                row.lease_token = row.lease_expires_at = None
                session.commit()
                return
            attempted = row.upload_attempted
            if not attempted and not row.remote_id:
                path = self.asset_store.path_for(row.storage_key)
                actual = _fingerprint(path)
                asset = session.scalar(select(AssetRef).where(
                    AssetRef.storage_key == row.storage_key, AssetRef.entry_id == row.entry_id,
                ))
                if (asset is None
                        or (asset.checksum and actual[0] != asset.checksum)
                        or (asset.size_bytes is not None and actual[1] != asset.size_bytes)):
                    raise ArchiveIntegrityError("本地原件未匹配原上传记录，请恢复原文件或创建新版本")
                if actual != (row.checksum_sha256, row.size_bytes):
                    # A first scan may have found a missing/corrupt local file.
                    # Recovery before any HTTP attempt is allowed only against
                    # the immutable upload receipt, never merely a new file at
                    # the same path. After an attempted POST this branch cannot
                    # run and the persisted archive fingerprint never changes.
                    if (asset is None or not asset.checksum or asset.size_bytes is None
                            or actual != (asset.checksum, asset.size_bytes)
                            or not 0 < actual[1] <= self.max_file_bytes
                            or path.suffix.lower() not in SUPPORTED_SUFFIXES):
                        raise ArchiveIntegrityError("原件无法匹配原上传记录，请恢复原文件或创建新版本")
                    row.checksum_sha256, row.size_bytes = actual
                    row.source_marker = archive_marker(self.connection_id, row.storage_key, actual[0])
                row.upload_attempted = True
            session.commit()
            session.refresh(row)
            session.expunge(row)
        if row.remote_id:
            document = self.client.get_document(row.remote_id)
        elif attempted:
            document = self.client.find_document_by_source(row.source_marker, stop_event=self.stop_event)
            if document is None:
                self._finish(row_id, token, status="ambiguous",
                             last_error="上传结果未知；未找到归属匹配的原件，仅可核查后重试恢复映射")
                return
        else:
            path = self.asset_store.path_for(row.storage_key)
            try:
                document = self.client.ingest_file(path, title=title, metadata={
                    "local_source_marker": row.source_marker, "local_archive_kind": "original",
                    "local_original_sha256": row.checksum_sha256,
                    "local_original_size": str(row.size_bytes),
                })
            except Exception as exc:
                if getattr(exc, "status_code", None) in {401, 403}:
                    with self.session_factory() as session:
                        current = self._load(session, row_id, token)
                        if current:
                            current.upload_attempted = False
                            session.commit()
                raise
        _check_document(self.client, row, document)
        row.remote_id = document.id
        # Downloadability and exact byte preservation establish original-file
        # custody independently of OCR/ASR/embedding availability. The public
        # platform contract has no supported per-document disable operation.
        # This document never becomes an ExternalRef used by local retrieval.
        with tempfile.TemporaryDirectory(prefix="content-original-verify-") as folder:
            _download(self.client, row, Path(folder) / "original")
        self._finish(row_id, token, status="ready", remote_id=document.id, last_error=None,
                     verified_at=datetime.now(UTC), attempt=0)

    def _finish(self, row_id: str, token: str, **values) -> None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            session.execute(update(OriginalArchive).where(
                OriginalArchive.id == row_id, OriginalArchive.lease_token == token,
                OriginalArchive.lease_expires_at > now,
            ).values(**values, available_at=now + timedelta(seconds=self.interval_seconds),
                     lease_token=None, lease_expires_at=None))
            session.commit()

    def _failure(self, row_id: str, token: str, exc: Exception) -> None:
        with self.session_factory() as session:
            row = self._load(session, row_id, token)
            if row is None:
                return
            code = getattr(exc, "code", "request_failed")
            if code == "cancelled":
                row.lease_token = row.lease_expires_at = None
                row.available_at = datetime.now(UTC)
                session.commit()
                return
            row.attempt += 1
            if isinstance(exc, WeKnoraError):
                row.last_error = exc.safe_message + "；本地文件保留"
            elif isinstance(exc, ArchiveIntegrityError):
                row.last_error = str(exc) + "；本地文件保留"
            else:
                row.last_error = "原件托管暂未完成，请检查平台连接及本地文件；本地文件保留"
            if isinstance(exc, WeKnoraError) and code in {"multimodal_not_configured", "asr_not_configured"}:
                # The adapter classifies only exact, documented HTTP 400
                # pre-creation validation messages. Other 400s and all unknown
                # results keep the persisted POST fence.
                row.upload_attempted = False
                row.status = "failed"
            if isinstance(exc, ArchiveIntegrityError) or code == "duplicate_unowned" or row.attempt >= 5:
                row.status = "failed"
            row.available_at = datetime.now(UTC) + timedelta(seconds=min(3600, 15 * 2 ** row.attempt))
            row.lease_token = row.lease_expires_at = None
            session.commit()

    def retry_job(self, archive_id: str) -> dict:
        with self.session_factory() as session:
            row = session.get(OriginalArchive, archive_id)
            if row is None or row.connection_id != self.connection_id:
                raise ValueError("原件托管记录不存在或不属于当前知识库连接")
            result = retry_archive(session, row.storage_key, self.connection_id)
            session.commit()
            return result

    def statuses(self) -> list[dict]:
        with self.session_factory() as session:
            return [{key: value for key, value in _public(row).items()
                     if key in {"archive_id", "status", "error", "can_retry"}}
                    for row in session.scalars(select(OriginalArchive).where(
                        OriginalArchive.connection_id == self.connection_id,
                    ).order_by(OriginalArchive.updated_at.desc()).limit(100))]

    def reset_unknown_upload(self, archive_id: str) -> None:
        """Operator-only recovery after explicit confirmation and remote lookup.

        The CLI requires --confirm-remote-absent. The immutable key/digest/size
        never change; even a concurrent reset/retry cannot pass the stale CAS.
        """
        with self.session_factory() as session:
            row = session.get(OriginalArchive, archive_id)
            if (row is None or row.connection_id != self.connection_id
                    or row.status != "ambiguous" or row.remote_id or not row.upload_attempted):
                raise ValueError("只能重置当前连接中上传结果未知、尚无远端映射的原件")
            marker, prior_updated_at = row.source_marker, row.updated_at
        if self.client.find_document_by_source(marker) is not None:
            raise ValueError("平台已有对应原件，请使用重试恢复映射，不能重新上传")
        now = datetime.now(UTC)
        with self.session_factory() as session:
            result = session.execute(update(OriginalArchive).where(
                OriginalArchive.id == archive_id, OriginalArchive.connection_id == self.connection_id,
                OriginalArchive.status == "ambiguous", OriginalArchive.remote_id.is_(None),
                OriginalArchive.upload_attempted == true(), OriginalArchive.source_marker == marker,
                OriginalArchive.updated_at == prior_updated_at,
                or_(OriginalArchive.lease_expires_at.is_(None), OriginalArchive.lease_expires_at <= now),
            ).values(status="queued", upload_attempted=False, attempt=0, last_error=None,
                     available_at=now, lease_token=None, lease_expires_at=None, updated_at=now)
                .execution_options(synchronize_session=False))
            if result.rowcount != 1:
                raise ValueError("托管记录状态已变化或正在处理，请刷新后核查")
            session.commit()

    def run_once(self, limit: int = 20, *, stop_event: Event | None = None) -> dict:
        self.stop_event = stop_event
        if stop_event and stop_event.is_set():
            return {"enqueued": 0, "processed": 0, "failed": 0}
        # The runtime lock also protects standalone worker processes.
        with CacheMaintenanceLock(self.asset_store.root):
            created, processed, failed = self.reconcile(), 0, 0
            for _ in range(max(0, limit)):
                if stop_event and stop_event.is_set():
                    break
                claim = self._claim()
                if claim is None:
                    break
                try:
                    self._process(*claim)
                except Exception as exc:
                    self._failure(*claim, exc)
                    failed += 1
                processed += 1
            return {"enqueued": created, "processed": processed, "failed": failed}

    def run_forever(self, stop_event: Event) -> None:
        with CacheMaintenanceLock(self.asset_store.root):
            while not stop_event.is_set():
                try:
                    self.run_once(stop_event=stop_event)
                except Exception:
                    logger.warning("原件托管扫描未完成，将稍后重试；本地文件保留")
                stop_event.wait(self.interval_seconds)


def purge_local_verified_cache(session_factory: Callable[[], Session], client: Any,
                               local_store: Any, *, apply: bool = False) -> dict:
    """Offline-only cache removal; reread every remote original before unlinking.

    Dry runs report eligible bytes without network access or removal. Actual
    removal takes the same exclusive lock required to exclude all live stores.
    Unsupported files, generated results and unfinished archives are untouched.
    """
    local_store = getattr(local_store, "local_store", local_store)
    result = {"apply": apply, "eligible": 0, "eligible_bytes": 0,
              "removed": 0, "removed_bytes": 0, "skipped": 0, "failed": 0}
    with CacheMaintenanceLock(local_store.root, exclusive=apply), session_factory() as session:
        rows = session.scalars(select(OriginalArchive).where(
            OriginalArchive.connection_id == client.connection_id,
            OriginalArchive.status == "ready",
        )).all()
        for row in rows:
            path = local_store.path_for(row.storage_key)
            if (not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES
                    or _fingerprint(path) != (row.checksum_sha256, row.size_bytes)):
                result["skipped"] += 1
                continue
            result["eligible"] += 1
            result["eligible_bytes"] += row.size_bytes
            if not apply:
                continue
            try:
                document = client.get_document(row.remote_id)
                _check_document(client, row, document)
                with tempfile.TemporaryDirectory(prefix="content-cache-verify-") as folder:
                    _download(client, row, Path(folder) / "original")
                # Fingerprint immediately before removal; never erase a
                # changed file even if an external noncooperating writer ran.
                if _fingerprint(path) != (row.checksum_sha256, row.size_bytes):
                    result["skipped"] += 1
                    continue
                path.unlink()
                result["removed"] += 1
                result["removed_bytes"] += row.size_bytes
            except Exception:
                result["failed"] += 1
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="原件托管运维；不删除知识平台上的原件")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true")
    action.add_argument("--retry", metavar="ARCHIVE_ID")
    action.add_argument("--reset-unknown-upload", metavar="ARCHIVE_ID")
    parser.add_argument("--confirm-remote-absent", action="store_true")
    args = parser.parse_args()
    if args.reset_unknown_upload and not args.confirm_remote_absent:
        parser.error("重置前须人工核查平台无对应原件，并加 --confirm-remote-absent")
    try:
        from .config import load_settings
        from .db import SessionLocal
        from .provider_dependencies import get_asset_store_port
        from .weknora import WeKnoraClient

        settings = load_settings()
        if not all((settings.weknora_base_url, settings.weknora_api_key, settings.weknora_knowledge_base_id)):
            raise ValueError("请先配置知识平台连接、密钥和知识库 ID")
        client = WeKnoraClient(settings.weknora_base_url, settings.weknora_api_key,
                               settings.weknora_knowledge_base_id,
                               tenant_id=settings.weknora_tenant_id,
                               timeout_seconds=settings.weknora_timeout_seconds)
        runner = OriginalArchiveRunner(SessionLocal, client, asset_store=get_asset_store_port(),
                                        allowed_data_levels=settings.weknora_allowed_data_levels,
                                        max_file_bytes=settings.weknora_originals_max_file_bytes)
        if args.reset_unknown_upload:
            runner.reset_unknown_upload(args.reset_unknown_upload)
            print("已重置；原件指纹保留，下次启用的托管扫描会重新上传")
        elif args.retry:
            result = runner.retry_job(args.retry)
            print(json.dumps({key: result[key] for key in ("archive_id", "status", "can_retry")},
                             ensure_ascii=False))
        else:
            print(json.dumps(runner.statuses(), ensure_ascii=False))
    except WeKnoraError as exc:
        raise SystemExit(exc.safe_message) from None
    except ValueError as exc:
        # Only our explicit operational errors are meaningful here. Config
        # parser messages can contain supplied values, so avoid echoing them.
        known = ("原件", "平台已有", "托管记录", "请先配置")
        message = str(exc)
        raise SystemExit(message if message.startswith(known) else "配置或参数校验失败") from None
    except Exception:
        raise SystemExit("原件托管命令执行失败，请检查服务配置、数据库迁移及知识平台连接") from None


if __name__ == "__main__":
    main()
