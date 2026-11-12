"""Durable, conservative synchronization of the formal library to WeKnora.

The local resource and its current version remain the authorization authority.
An upload is recorded *before* network I/O. If its outcome is unknown we only
look up its immutable marker; we never silently upload another copy.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .knowledge_ingestion import extract_document_text
from .models import (
    AssetRef,
    ExternalRef,
    LibraryItem,
    ResourceEntry,
    ResourceVersion,
    WeKnoraSyncJob,
)

logger = logging.getLogger(__name__)
MAX_FAILURES = 5
RUNNABLE = ("queued", "processing", "deleting")
DOCUMENT_KINDS = frozenset({"document", "product", "spec"})


class SyncPreparationError(ValueError):
    code = "document_extraction_failed"


def source_marker(entry_id: str, version_id: str) -> str:
    return f"content-agent:{entry_id}:{version_id}"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _unleased(now: datetime):
    return or_(WeKnoraSyncJob.lease_expires_at.is_(None), WeKnoraSyncJob.lease_expires_at <= now)


def _metadata(ref: ExternalRef) -> dict:
    try:
        value = json.loads(ref.metadata_json or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _formal_entry(session: Session, entry: ResourceEntry) -> bool:
    # A task reference/engineering asset may have a ResourceEntry but never a
    # formal LibraryItem. Do not broaden this to scan every stored asset.
    try:
        metadata = json.loads(entry.metadata_json or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(metadata, dict) or metadata.get("source_type") == "task-image-reference":
        return False
    return session.scalar(
        select(LibraryItem.id).where(
            LibraryItem.resource_entry_id == entry.id,
            LibraryItem.active == true(),
        ).limit(1)
    ) is not None


def _library_asset_key(session: Session, entry: ResourceEntry, version: ResourceVersion) -> str | None:
    item = session.scalar(select(LibraryItem).where(
        LibraryItem.resource_entry_id == entry.id, LibraryItem.active == true(),
        LibraryItem.version == version.version,
    ))
    if item is not None and item.storage_key:
        # Text-only revisions deliberately keep the previous immutable file.
        return item.storage_key
    asset = session.scalar(select(AssetRef).where(
        AssetRef.entry_id == entry.id, AssetRef.version_id == version.id,
    ).order_by(AssetRef.created_at.desc()))
    return asset.storage_key if asset else None


def _wanted(
    session: Session, entry: ResourceEntry | None, version: ResourceVersion | None,
    allowed_data_levels: frozenset[str],
) -> bool:
    return bool(
        entry and version and entry.active
        and entry.current_version_id == version.id
        and version.entry_id == entry.id
        and entry.data_level in allowed_data_levels
        and _formal_entry(session, entry)
        and (version.text_content.strip() or (
            entry.resource_kind in DOCUMENT_KINDS and _library_asset_key(session, entry, version)
        ))
    )


def _refs(session: Session, job: WeKnoraSyncJob) -> list[ExternalRef]:
    return [
        ref for ref in session.scalars(select(ExternalRef).where(
            ExternalRef.entry_id == job.entry_id, ExternalRef.system == "weknora"
        )).all()
        if _metadata(ref).get("connection_id") == job.connection_id
        and _metadata(ref).get("version_id") == job.version_id
    ]


def _ref_status(session: Session, job: WeKnoraSyncJob, status: str) -> None:
    for ref in _refs(session, job):
        ref.metadata_json = _json({**_metadata(ref), "sync_status": status})


def _public_job(job: WeKnoraSyncJob) -> dict:
    return {
        "job_id": job.id, "entry_id": job.entry_id, "version_id": job.version_id,
        "status": job.status, "operation": job.operation,
        "attempt": job.attempt, "error": job.last_error,
        "updated_at": job.updated_at.isoformat(),
        "can_retry": job.status in {"failed", "ambiguous"},
    }


def get_sync_status(session: Session, entry_id: str, connection_id: str) -> dict:
    entry = session.get(ResourceEntry, entry_id)
    if entry is None:
        raise ValueError("资料不存在")
    job = session.scalar(select(WeKnoraSyncJob).where(
        WeKnoraSyncJob.entry_id == entry_id,
        WeKnoraSyncJob.version_id == entry.current_version_id,
        WeKnoraSyncJob.connection_id == connection_id,
    ))
    if job is None:
        version = session.get(ResourceVersion, entry.current_version_id) if entry.current_version_id else None
        participates = _wanted(session, entry, version, frozenset({"public", "internal", "restricted"}))
        return {"entry_id": entry_id, "version_id": entry.current_version_id,
                "status": "not_synced" if participates else "excluded",
                "can_retry": False, "error": None}
    return _public_job(job)


def retry_sync_job(session: Session, entry_id: str, connection_id: str) -> dict:
    """Retry a failed current version without bypassing uncertain-upload checks."""
    entry = session.get(ResourceEntry, entry_id)
    if entry is None:
        raise ValueError("资料不存在")
    job = session.scalar(select(WeKnoraSyncJob).where(
        WeKnoraSyncJob.entry_id == entry_id,
        WeKnoraSyncJob.version_id == entry.current_version_id,
        WeKnoraSyncJob.connection_id == connection_id,
    ))
    return _requeue_job(session, job)


def _requeue_job(session: Session, job: WeKnoraSyncJob | None) -> dict:
    if job is None or job.status not in {"failed", "ambiguous"}:
        raise ValueError("当前资料没有可重试的同步任务")
    now = datetime.now(UTC)
    changed = session.execute(update(WeKnoraSyncJob).where(
        WeKnoraSyncJob.id == job.id,
        WeKnoraSyncJob.status.in_(("failed", "ambiguous")),
        or_(WeKnoraSyncJob.lease_expires_at.is_(None), WeKnoraSyncJob.lease_expires_at <= now),
    ).values(status="queued", attempt=0, last_error=None, available_at=now,
             lease_token=None, lease_expires_at=None))
    if changed.rowcount != 1:
        raise ValueError("同步任务正在运行，请稍后再试")
    session.flush()
    session.refresh(job)
    return _public_job(job)


class WeKnoraSyncRunner:
    def __init__(
        self, session_factory: Callable[[], Session], client: Any, *,
        allowed_data_levels: Iterable[str] = ("public", "internal"),
        interval_seconds: float = 30,
        asset_store: Any = None,
    ):
        self.session_factory = session_factory
        self.client = client
        self.connection_id = client.connection_id
        self.knowledge_base_id = client.knowledge_base_id
        self.allowed_data_levels = frozenset(allowed_data_levels)
        self.interval_seconds = max(1, interval_seconds)
        self.asset_store = asset_store
        self.stop_event: Event | None = None
        # The lease covers a whole bounded paginated recovery lookup. A stale
        # worker is additionally fenced before saving each result.
        self.lease_seconds = max(300, int(getattr(client, "timeout_seconds", 30) * 105))

    def _document_text(self, session: Session, entry: ResourceEntry, version: ResourceVersion) -> str:
        sections = [f"# {entry.title}", version.text_content.strip()]
        if entry.resource_kind in DOCUMENT_KINDS:
            key = _library_asset_key(session, entry, version)
            if key:
                if self.asset_store is None:
                    raise SyncPreparationError("未配置文档存储，不能完整同步原附件")
                try:
                    original = extract_document_text(self.asset_store.path_for(key))
                except Exception as exc:
                    raise SyncPreparationError("原附件未提取到可检索正文") from exc
                if original.strip() != version.text_content.strip():
                    sections.append(f"## 文档原文\n\n{original}")
        try:
            facts = json.loads(version.facts_json or "[]")
        except (ValueError, TypeError):
            facts = []
        if facts:
            sections.append("## 关键事实\n\n" + _json(facts))
        if version.notes.strip():
            sections.append("## 备注\n\n" + version.notes.strip())
        return "\n\n".join(section for section in sections if section)

    def reconcile(self) -> int:
        now = datetime.now(UTC)
        created = 0
        with self.session_factory() as session:
            rows = session.execute(select(ResourceEntry, ResourceVersion).join(
                ResourceVersion, ResourceEntry.current_version_id == ResourceVersion.id
            ).where(ResourceEntry.active == true(),
                    ResourceEntry.data_level.in_(self.allowed_data_levels))).all()
            for entry, version in rows:
                if not _wanted(session, entry, version, self.allowed_data_levels):
                    continue
                job = session.scalar(select(WeKnoraSyncJob).where(
                    WeKnoraSyncJob.connection_id == self.connection_id,
                    WeKnoraSyncJob.version_id == version.id,
                ))
                if job is None:
                    try:
                        with session.begin_nested():
                            session.add(WeKnoraSyncJob(
                                entry_id=entry.id, version_id=version.id,
                                connection_id=self.connection_id,
                                knowledge_base_id=self.knowledge_base_id,
                            ))
                            session.flush()
                        created += 1
                    except IntegrityError:
                        # Another server process enqueued the same immutable version.
                        pass
                elif job.status == "deleted":
                    # Remote deletion was confirmed; restoring this exact local
                    # version is the one case in which a fresh upload is safe.
                    # A stale scan must not clear a newer worker's upload fence.
                    session.execute(update(WeKnoraSyncJob).where(
                        WeKnoraSyncJob.id == job.id,
                        WeKnoraSyncJob.status == "deleted", _unleased(now),
                    ).values(
                        status="queued", operation="index", remote_id=None,
                        upload_attempted=False, delete_requested=False,
                        attempt=0, last_error=None, available_at=now,
                        lease_token=None, lease_expires_at=None,
                    ).execution_options(synchronize_session=False))
            for job in session.scalars(select(WeKnoraSyncJob).where(
                WeKnoraSyncJob.connection_id == self.connection_id,
                WeKnoraSyncJob.status != "deleted",
            )).all():
                entry = session.get(ResourceEntry, job.entry_id)
                version = session.get(ResourceVersion, job.version_id)
                if _wanted(session, entry, version, self.allowed_data_levels):
                    continue
                # Index failures still need one cleanup operation after a
                # version is replaced/stopped. Failed cleanup is left for an
                # explicit retry, rather than reset on every scan forever.
                if job.operation != "cleanup":
                    changed = session.execute(update(WeKnoraSyncJob).where(
                        WeKnoraSyncJob.id == job.id,
                        WeKnoraSyncJob.status == job.status,
                        WeKnoraSyncJob.operation == "index", _unleased(now),
                    ).values(
                        operation="cleanup", status="deleting", attempt=0,
                        available_at=now, last_error=None,
                    ).execution_options(synchronize_session=False))
                    if changed.rowcount == 1:
                        _ref_status(session, job, "obsolete")
            session.commit()
        return created

    def _claim(self) -> tuple[str, str] | None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            ids = session.scalars(select(WeKnoraSyncJob.id).where(
                WeKnoraSyncJob.connection_id == self.connection_id,
                WeKnoraSyncJob.status.in_(RUNNABLE),
                WeKnoraSyncJob.available_at <= now,
                or_(WeKnoraSyncJob.lease_expires_at.is_(None),
                    WeKnoraSyncJob.lease_expires_at <= now),
            ).order_by(WeKnoraSyncJob.available_at, WeKnoraSyncJob.id).limit(10)).all()
            for job_id in ids:
                token = uuid4().hex
                changed = session.execute(update(WeKnoraSyncJob).where(
                    WeKnoraSyncJob.id == job_id,
                    WeKnoraSyncJob.status.in_(RUNNABLE),
                    WeKnoraSyncJob.available_at <= now,
                    or_(WeKnoraSyncJob.lease_expires_at.is_(None),
                        WeKnoraSyncJob.lease_expires_at <= now),
                ).values(lease_token=token,
                         lease_expires_at=now + timedelta(seconds=self.lease_seconds)))
                session.commit()
                if changed.rowcount == 1:
                    return job_id, token
        return None

    def _load_claim(self, session: Session, job_id: str, token: str) -> WeKnoraSyncJob | None:
        return session.scalar(select(WeKnoraSyncJob).where(
            WeKnoraSyncJob.id == job_id, WeKnoraSyncJob.lease_token == token,
            WeKnoraSyncJob.lease_expires_at > datetime.now(UTC),
        ))

    def _save_document(self, session: Session, job: WeKnoraSyncJob, document: Any) -> None:
        marker = source_marker(job.entry_id, job.version_id)
        if (document.knowledge_base_id != self.knowledge_base_id
                or document.metadata.get("local_source_marker") != marker):
            raise ValueError("远端文档归属无法确认，已停止同步以避免覆盖其他资料")
        job.remote_id = document.id
        refs = _refs(session, job)
        ref = next((ref for ref in refs if ref.external_id == document.id), None)
        existing = session.scalar(select(ExternalRef).where(
            ExternalRef.system == "weknora", ExternalRef.external_id == document.id
        ))
        if existing is not None and existing is not ref:
            raise ValueError("远端文档已绑定其他资料或连接，不能重复绑定")
        if ref is None:
            ref = ExternalRef(entry_id=job.entry_id, system="weknora", external_id=document.id)
            session.add(ref)
        if document.ready:
            job.status = "ready"
            job.last_error = None
            job.attempt = 0
        elif document.parse_status in {"failed", "error"}:
            job.status = "failed"
            job.last_error = "知识库平台解析失败，请在平台修复后重试"
        else:
            job.status = "processing"
            job.last_error = None
            job.available_at = datetime.now(UTC) + timedelta(seconds=self.interval_seconds)
        ref.metadata_json = _json({
            "version_id": job.version_id, "knowledge_base_id": self.knowledge_base_id,
            "connection_id": self.connection_id, "sync_status": job.status,
            "source_marker": marker,
        })

    def _finish_cleanup(self, job_id: str, token: str, remote_id: str | None) -> None:
        with self.session_factory() as session:
            job = self._load_claim(session, job_id, token)
            if job is None:
                return
            job.remote_id = remote_id
            job.status = "deleted"
            job.last_error = None
            job.lease_token = None
            job.lease_expires_at = None
            _ref_status(session, job, "deleted")
            session.commit()

    def _process(self, job_id: str, token: str) -> None:
        with self.session_factory() as session:
            job = self._load_claim(session, job_id, token)
            if job is None:
                return
            entry = session.get(ResourceEntry, job.entry_id)
            version = session.get(ResourceVersion, job.version_id)
            # Once remote deletion was accepted, wait until disappearance even
            # if the local version was restored in the meantime. Reconciliation
            # will then create a fresh remote copy safely.
            wanted = (
                _wanted(session, entry, version, self.allowed_data_levels)
                and job.operation != "cleanup"
                and not job.delete_requested
            )
            remote_id = job.remote_id
            attempted = job.upload_attempted
            marker = source_marker(job.entry_id, job.version_id)
            delete_requested = job.delete_requested
            attempt = job.attempt
            title = entry.title if entry else "已停用资料"
            text = ""
            if not wanted:
                job.status = "deleting"
                job.operation = "cleanup"
                _ref_status(session, job, "obsolete")
            elif not remote_id and not attempted:
                text = self._document_text(session, entry, version)
                # Persist before HTTP; even a killed process never retries a
                # POST blindly after an uncertain server-side success.
                job.upload_attempted = True
            session.commit()

        document = None
        if remote_id:
            try:
                document = self.client.get_document(remote_id)
            except Exception as exc:
                if getattr(exc, "status_code", None) != 404:
                    raise
                if not wanted:
                    self._finish_cleanup(job_id, token, remote_id)
                    return
                # A known document disappeared; do not recreate until an
                # operator explicitly confirms the remote state.
                raise ValueError("远端文档不存在，请核查平台后重置同步任务") from exc
        elif attempted:
            document = self.client.find_document_by_source(marker, stop_event=self.stop_event)
            if document is None:
                with self.session_factory() as session:
                    job = self._load_claim(session, job_id, token)
                    if job is None:
                        return
                    job.status = "ambiguous"
                    job.last_error = "上次上传结果未知；尚未找到归属匹配的文档，请核对平台后重试或重置"
                    job.lease_token = None
                    job.lease_expires_at = None
                    _ref_status(session, job, "obsolete" if not wanted else "ambiguous")
                    session.commit()
                return
        elif wanted:
            try:
                document = self.client.ingest_text(title=title, text=text, source_marker=marker)
            except Exception as exc:
                if getattr(exc, "status_code", None) in {401, 403}:
                    # A rejected upload did not create a document. This applies
                    # only to this POST, never to an uncertain upload's later
                    # lookup, so rotating an invalid key can resume normally.
                    with self.session_factory() as session:
                        job = self._load_claim(session, job_id, token)
                        if job is not None:
                            job.upload_attempted = False
                            session.commit()
                raise

        if not wanted:
            if document is None:
                self._finish_cleanup(job_id, token, None)
                return
            if (document.knowledge_base_id != self.knowledge_base_id
                    or document.metadata.get("local_source_marker") != marker):
                raise ValueError("远端文档归属无法确认，拒绝删除其他资料")
            if not delete_requested or attempt == 0:
                self.client.delete_document(document.id)
            with self.session_factory() as session:
                job = self._load_claim(session, job_id, token)
                if job is None:
                    return
                job.remote_id = document.id
                job.delete_requested = True
                job.status = "deleting"
                if delete_requested and attempt > 0:
                    job.attempt += 1
                    if job.attempt >= 60:
                        job.status = "failed"
                        job.last_error = "知识库平台长期未完成删除，请检查平台删除任务后重试"
                else:
                    job.attempt = 1
                job.available_at = datetime.now(UTC) + timedelta(seconds=self.interval_seconds)
                job.lease_token = None
                job.lease_expires_at = None
                _ref_status(session, job, "obsolete")
                session.commit()
            return

        with self.session_factory() as session:
            job = self._load_claim(session, job_id, token)
            if job is None:
                return
            self._save_document(session, job, document)
            # The resource can change while HTTP is in flight. Never publish
            # an obsolete version as searchable, even for one polling interval.
            entry = session.get(ResourceEntry, job.entry_id)
            version = session.get(ResourceVersion, job.version_id)
            if not _wanted(session, entry, version, self.allowed_data_levels):
                job.status = "deleting"
                job.operation = "cleanup"
                job.attempt = 0
                _ref_status(session, job, "obsolete")
            job.lease_token = None
            job.lease_expires_at = None
            session.commit()

    def _failure(self, job_id: str, token: str, exc: Exception) -> None:
        with self.session_factory() as session:
            job = self._load_claim(session, job_id, token)
            if job is None:
                return
            if getattr(exc, "code", None) == "cancelled":
                # Shutdown during recovery is not a platform failure. Preserve
                # the upload fence and resume the same lookup after restart.
                job.lease_token = None
                job.lease_expires_at = None
                job.available_at = datetime.now(UTC)
                session.commit()
                return
            job.attempt += 1
            # Never persist remote response bodies, keys or connection strings.
            safe_code = getattr(exc, "code", None)
            code = safe_code if isinstance(safe_code, str) and safe_code.isidentifier() else "request_failed"
            job.last_error = f"知识库同步失败（{code}），请检查连接、授权和平台解析状态"
            if isinstance(exc, SyncPreparationError):
                job.last_error = "原文解析失败，未同步到知识库；请检查附件格式，扫描 PDF 需先完成 OCR"
                job.status = "failed"
            elif job.attempt >= MAX_FAILURES:
                job.status = "failed"
            job.available_at = datetime.now(UTC) + timedelta(seconds=min(3600, 15 * 2 ** job.attempt))
            job.lease_token = None
            job.lease_expires_at = None
            if job.status == "failed":
                _ref_status(session, job, "failed")
            session.commit()

    def run_once(self, limit: int = 20, *, stop_event: Event | None = None) -> dict:
        self.stop_event = stop_event
        if stop_event is not None and stop_event.is_set():
            return {"enqueued": 0, "processed": 0, "failed": 0}
        created = self.reconcile()
        processed = 0
        failed = 0
        for _ in range(max(0, limit)):
            if stop_event is not None and stop_event.is_set():
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
        while not stop_event.is_set():
            try:
                self.run_once(stop_event=stop_event)
            except Exception:
                # Credentials and document contents must not enter log output.
                logger.warning("WeKnora 同步扫描未完成；稍后重试")
            stop_event.wait(self.interval_seconds)

    def reset_unknown_upload(self, job_id: str) -> None:
        """Explicit operator recovery only, after proving no marked remote copy exists."""
        with self.session_factory() as session:
            job = session.get(WeKnoraSyncJob, job_id)
            if job is None or job.connection_id != self.connection_id:
                raise ValueError("同步任务不存在或不属于当前知识库连接")
            if job.status not in {"failed", "ambiguous"}:
                raise ValueError("只能重置失败或结果未知的同步任务")
            marker = source_marker(job.entry_id, job.version_id)
        if self.client.find_document_by_source(marker) is not None:
            raise ValueError("平台已有对应资料，请使用重试恢复映射，不要重新上传")
        with self.session_factory() as session:
            now = datetime.now(UTC)
            changed = session.execute(update(WeKnoraSyncJob).where(
                WeKnoraSyncJob.id == job_id,
                WeKnoraSyncJob.connection_id == self.connection_id,
                WeKnoraSyncJob.status.in_(("failed", "ambiguous")), _unleased(now),
            ).values(
                upload_attempted=False, remote_id=None, delete_requested=False,
                status="queued", operation="index", attempt=0, last_error=None,
                available_at=now, lease_token=None, lease_expires_at=None,
            ).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise ValueError("同步状态已变化，请刷新后重试")
            session.commit()

    def retry_job(self, job_id: str) -> dict:
        """Operator retry also covers cleanup of inactive and historic versions."""
        with self.session_factory() as session:
            job = session.get(WeKnoraSyncJob, job_id)
            if job is None or job.connection_id != self.connection_id:
                raise ValueError("同步任务不存在或不属于当前知识库连接")
            result = _requeue_job(session, job)
            session.commit()
            return result

    def statuses(self) -> list[dict]:
        with self.session_factory() as session:
            return [_public_job(job) for job in session.scalars(
                select(WeKnoraSyncJob).where(
                    WeKnoraSyncJob.connection_id == self.connection_id,
                    WeKnoraSyncJob.status.in_(("failed", "ambiguous", "deleting")),
                ).order_by(WeKnoraSyncJob.updated_at.desc()).limit(100)
            ).all()]


def main() -> None:
    parser = argparse.ArgumentParser(description="同步正式资料到 WeKnora，保留本地权限与版本控制")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--once", action="store_true")
    action.add_argument("--watch", action="store_true")
    action.add_argument("--reset-unknown-upload", metavar="JOB_ID")
    action.add_argument("--retry", metavar="JOB_ID")
    action.add_argument("--status", action="store_true")
    parser.add_argument("--confirm-remote-absent", action="store_true")
    args = parser.parse_args()
    from .config import load_settings
    from .db import SessionLocal, init_database
    from .provider_dependencies import get_asset_store_port
    from .weknora import WeKnoraClient

    settings = load_settings()
    if not settings.weknora_api_key or not settings.weknora_knowledge_base_id:
        parser.error("请先配置专用知识库 API Key 和知识库 ID")
    client = WeKnoraClient(
        settings.weknora_base_url, settings.weknora_api_key, settings.weknora_knowledge_base_id,
        tenant_id=settings.weknora_tenant_id, timeout_seconds=settings.weknora_timeout_seconds,
    )
    init_database()
    runner = WeKnoraSyncRunner(
        SessionLocal, client, allowed_data_levels=settings.weknora_allowed_data_levels,
        interval_seconds=settings.weknora_sync_interval_seconds,
        asset_store=get_asset_store_port(),
    )
    if args.reset_unknown_upload:
        if not args.confirm_remote_absent:
            parser.error("重置前必须人工确认平台没有对应文档，并加 --confirm-remote-absent")
        runner.reset_unknown_upload(args.reset_unknown_upload)
        print("同步任务已重置，下次扫描将重新上传")
    elif args.retry:
        print(_json(runner.retry_job(args.retry)))
    elif args.status:
        print(_json(runner.statuses()))
    elif args.watch:
        runner.run_forever(Event())
    else:
        print(_json(runner.run_once()))


if __name__ == "__main__":
    main()
