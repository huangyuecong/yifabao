"""WeKnora API adapter with local authorization and document-scoped retrieval.

Contract: https://weknora.weixin.qq.com/docs/04-api/02-api-knowledge
Only explicitly configured servers are contacted. This adapter never creates a
workspace, knowledge base, model, or administrative credential.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import select, true
from sqlalchemy.orm import Session

from .models import ExternalRef, LibraryItem, ResourceEntry, ResourceVersion
from .providers import KnowledgeChunk, KnowledgeQuery, ProviderStatus

WEKNORA_SYSTEM = "weknora"
DEFAULT_ALLOWED_DATA_LEVELS = frozenset({"public", "internal"})
_MAX_SEARCH_DOCUMENTS = 200
_MAX_LOOKUP_PAGES = 100


class WeKnoraError(RuntimeError):
    """Safe error for logs/API responses; never contains remote bodies or secrets."""

    def __init__(self, code: str, *, status_code: int | None = None):
        self.code = code
        self.status_code = status_code
        messages = {
            "timeout": "知识平台请求超时，请稍后重试",
            "connection_failed": "暂时无法连接知识平台",
            "selected_resource_denied": "所选资料未获准检索或发送至知识平台",
            "selected_resource_not_indexed": "所选资料尚未完成知识平台索引，请先完成同步",
            "search_scope_violation": "知识平台未遵循授权文档范围，已拒绝本次检索结果",
            "authorization_changed": "资料权限或版本已变化，请重新检索",
            "duplicate_unowned": "知识平台已有同名或相同内容文档，且不属于本次同步",
            "not_found": "知识平台文档不存在",
            "unsupported_knowledge_base_type": "目标知识库必须是文档类型",
            "embedding_not_configured": "目标知识库尚未配置 Embedding 模型",
            "knowledge_base_mismatch": "知识平台返回的知识库与配置不一致",
            "file_integrity_failed": "知识平台原件校验未通过，本地文件未被替换",
            "file_source_mismatch": "知识平台原件与本地资料版本不一致",
            "multimodal_not_configured": "知识库尚未配置图片识别模型，原件未上传",
            "asr_not_configured": "知识库尚未配置语音识别模型，原件未上传",
        }
        self.safe_message = messages.get(code, "知识平台调用失败或返回的数据不符合接口约定")
        super().__init__(self.safe_message + (f" (HTTP {status_code})" if status_code else ""))


def normalize_weknora_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        invalid = (
            not value
            or bool(re.search(r"[\x00-\x20\x7f\\?#]", value))
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path.rstrip("/") not in {"", "/api/v1"}
            or parsed.port == 0
        )
        normalized = str(httpx.URL(value)).rstrip("/")
    except (ValueError, httpx.InvalidURL):
        invalid, normalized = True, ""
    if invalid:
        raise ValueError("WeKnora 地址必须为 HTTP(S) 服务地址或 /api/v1 地址，不能包含凭据或查询串")
    return normalized.removesuffix("/api/v1")


def _identifier(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 255
        or re.search(r"[\x00-\x20\x7f/\\?#]", value)
        or value in {".", ".."}
    ):
        raise WeKnoraError("invalid_identifier")
    return value


def weknora_connection_id(
    base_url: str, knowledge_base_id: str, tenant_id: str | None = None
) -> str:
    """Credential-independent namespace; key rotation keeps document mappings."""
    identity = json.dumps(
        [normalize_weknora_base_url(base_url), _identifier(knowledge_base_id), tenant_id or ""],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class WeKnoraDocument:
    id: str
    knowledge_base_id: str
    title: str
    parse_status: str
    enable_status: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.parse_status == "completed" and self.enable_status in {"", "enabled"}

    @property
    def status(self) -> str:
        return self.parse_status


@dataclass(frozen=True, slots=True)
class WeKnoraDocumentPage:
    documents: tuple[WeKnoraDocument, ...]
    total: int
    page: int
    page_size: int


class WeKnoraClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        knowledge_base_id: str,
        *,
        tenant_id: str | None = None,
        timeout_seconds: float = 30,
        http_transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = normalize_weknora_base_url(base_url)
        self.knowledge_base_id = _identifier(knowledge_base_id)
        if not api_key or not api_key.isascii() or re.search(r"[\x00-\x20\x7f]", api_key):
            raise ValueError("WeKnora API Key 不能为空或包含空白/控制字符")
        if tenant_id is not None:
            tenant_id = str(tenant_id)
            if not re.fullmatch(r"[1-9][0-9]*", tenant_id):
                raise ValueError("WeKnora 平台空间 ID 必须是正整数")
        if not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 300:
            raise ValueError("WeKnora 请求超时必须在 1 到 300 秒之间")
        self.tenant_id = tenant_id
        self.connection_id = weknora_connection_id(self.base_url, knowledge_base_id, tenant_id)
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._http_transport = http_transport

    @property
    def timeout_seconds(self) -> float:
        """Configured request timeout for lease sizing; credentials stay private."""
        return self._timeout_seconds

    def _request(self, method: str, path: str, *, allow_duplicate=False, **kwargs) -> dict:
        headers = {"X-API-Key": self._api_key, "Accept": "application/json"}
        if self.tenant_id:
            headers["X-Tenant-ID"] = self.tenant_id
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                transport=self._http_transport,
            ) as client:
                response = client.request(
                    method, f"{self.base_url}/api/v1{path}", headers=headers, **kwargs
                )
        except httpx.TimeoutException:
            raise WeKnoraError("timeout") from None
        except httpx.HTTPError:
            raise WeKnoraError("connection_failed") from None
        duplicate = response.status_code == 409 and allow_duplicate
        if not 200 <= response.status_code < 300 and not duplicate:
            code = "not_found" if response.status_code == 404 else "request_failed"
            if (
                response.status_code == 400
                and method == "POST"
                and path == self._knowledge_path("file")
            ):
                # These exact errors are emitted before the official file
                # handler creates any knowledge/storage record. Other 400s
                # must retain the uncertain-upload fence in archive callers.
                try:
                    error_body = response.json()
                    detail = error_body.get("error") if isinstance(error_body, dict) else None
                    message = detail.get("message") if isinstance(detail, dict) else None
                except (ValueError, UnicodeError):
                    message = None
                if message == "上传图片文件需要设置VLM模型":
                    code = "multimodal_not_configured"
                elif message == "上传音频文件需要设置ASR语音识别模型":
                    code = "asr_not_configured"
            raise WeKnoraError(code, status_code=response.status_code) from None
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            raise WeKnoraError("invalid_response") from None
        if not isinstance(payload, dict) or (not duplicate and payload.get("success") is not True):
            raise WeKnoraError("invalid_response")
        return payload

    def _document(self, payload: Any) -> WeKnoraDocument:
        if not isinstance(payload, dict):
            raise WeKnoraError("invalid_document")
        document_id = _identifier(payload.get("id", ""))
        if payload.get("knowledge_base_id") != self.knowledge_base_id:
            raise WeKnoraError("knowledge_base_mismatch")
        status = payload.get("parse_status")
        if not isinstance(status, str) or not status:
            raise WeKnoraError("invalid_document_status")
        metadata = payload.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                raise WeKnoraError("invalid_document_metadata") from None
        if not isinstance(metadata, dict):
            raise WeKnoraError("invalid_document_metadata")
        title = payload.get("title") or payload.get("file_name") or ""
        enable_status = payload.get("enable_status")
        if enable_status is None:
            enable_status = ""
        if not isinstance(title, str) or not isinstance(enable_status, str):
            raise WeKnoraError("invalid_document")
        return WeKnoraDocument(
            document_id, self.knowledge_base_id, title, status, enable_status, metadata
        )

    def _knowledge_path(self, suffix: str) -> str:
        return f"/knowledge-bases/{quote(self.knowledge_base_id, safe='')}/knowledge/{suffix}"

    def check_knowledge_base(self) -> dict[str, Any]:
        """Read-only deployment check; exposes no storage/model credentials.

        Reading a KB validates the configured key and read scope, but cannot
        prove ingest permission or runtime embedding availability without writes.
        """
        payload = self._request("GET", f"/knowledge-bases/{quote(self.knowledge_base_id, safe='')}")
        kb = payload.get("data")
        if not isinstance(kb, dict) or kb.get("id") != self.knowledge_base_id:
            raise WeKnoraError("knowledge_base_mismatch")
        kind = kb.get("type")
        if kind != "document":
            raise WeKnoraError("unsupported_knowledge_base_type")
        embedding_id = kb.get("embedding_model_id")
        if not isinstance(embedding_id, str) or not embedding_id.strip():
            raise WeKnoraError("embedding_not_configured")
        return {
            "knowledge_base_id": self.knowledge_base_id,
            "connection_id": self.connection_id,
            "knowledge_base_type": kind,
            "read_access": "verified",
            "embedding_model_configured": True,
            "embedding_runtime": "not_verified",
            "ingest_permission": "not_verified_read_only",
            "knowledge_ids_filter": "requires_deployment_contract_test",
            "server_version": "not_reported_by_this_endpoint",
        }

    def ingest_text(
        self, *, title: str, text: str, source_marker: str | None = None
    ) -> WeKnoraDocument:
        """Use Markdown upload so a durable source marker survives ambiguous responses."""
        if not text.strip():
            raise ValueError("WeKnora 文档正文不能为空")
        metadata = {"local_source_marker": source_marker} if source_marker else {}
        digest = hashlib.sha256((source_marker or title).encode("utf-8")).hexdigest()[:24]
        # WeKnora deduplicates file uploads by their bytes as well as filename.
        # Distinct local versions must remain distinct authorization identities.
        content = f"<!-- content-agent-source:{digest} -->\n\n{text}" if source_marker else text
        return self._upload(
            filename=f"content-agent-{digest}.md",
            title=title,
            content=content.encode("utf-8"),
            media_type="text/markdown",
            metadata=metadata,
        )

    def ingest_manual(self, *, title: str, text: str) -> WeKnoraDocument:
        """Direct manual API for callers not requiring file-ingest recovery markers."""
        if not text.strip():
            raise ValueError("WeKnora 文档正文不能为空")
        payload = self._request(
            "POST",
            self._knowledge_path("manual"),
            json={"title": title, "content": text, "status": "publish", "channel": "api"},
        )
        return self._document(payload.get("data"))

    def ingest_file(
        self,
        path: Path,
        *,
        title: str = "",
        metadata: dict[str, Any] | None = None,
        process_config: dict[str, Any] | None = None,
    ) -> WeKnoraDocument:
        source = Path(path)
        with source.open("rb") as content:
            return self._upload(
                filename=source.name,
                title=title or source.name,
                content=content,
                media_type=mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                metadata=metadata or {},
                process_config=process_config,
            )

    def _upload(
        self, *, filename, title, content, media_type, metadata, process_config=None
    ) -> WeKnoraDocument:
        if not isinstance(metadata, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata.items()
        ):
            raise ValueError("WeKnora 文件 metadata 仅支持字符串键值")
        metadata = {**metadata, "local_title": title}
        marker = metadata.get("local_source_marker")
        # fileName is also the parser's filename, not a separate display title.
        # Preserve the actual suffix and a stable unique portion for versioning.
        suffix = Path(filename).suffix.lower()
        stem = re.sub(r'[\x00-\x20\x7f/\\<>:"|?*]+', "_", title).strip(". _")
        digest = hashlib.sha256((marker or filename).encode("utf-8")).hexdigest()[:24]
        display_filename = f"{stem[:70] or 'content-agent'}-{digest}{suffix}"
        form = {
            "fileName": display_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
            "channel": "api",
        }
        if process_config is not None:
            # Official KnowledgeProcessOverrides is an optional multipart JSON
            # field. Absence preserves existing KB defaults and old uploads.
            if not isinstance(process_config, dict):
                raise ValueError("WeKnora 处理配置必须为对象")
            try:
                form["process_config"] = json.dumps(
                    process_config, ensure_ascii=False, allow_nan=False
                )
            except (TypeError, ValueError):
                raise ValueError("WeKnora 处理配置必须为有效 JSON") from None
        payload = self._request(
            "POST",
            self._knowledge_path("file"),
            files={"file": (filename, content, media_type)},
            data=form,
            allow_duplicate=bool(marker),
        )
        document = self._document(payload.get("data"))
        # No adoption of a same-name file belonging to a different local version.
        if marker and "local_source_marker" not in document.metadata:
            document = self.get_document(document.id)
        if marker and document.metadata.get("local_source_marker") != marker:
            raise WeKnoraError("duplicate_unowned")
        return document

    def get_document(self, document_id: str) -> WeKnoraDocument:
        payload = self._request("GET", f"/knowledge/{quote(_identifier(document_id), safe='')}")
        document = self._document(payload.get("data"))
        if document.id != document_id:
            raise WeKnoraError("document_id_mismatch")
        return document

    def download_document(
        self,
        document_id: str,
        destination: Path,
        *,
        expected_sha256: str,
        expected_size: int,
        source_marker: str,
    ) -> None:
        """Restore an owned original, atomically, after byte-for-byte verification.

        The destination and expected digest come from our persisted archive
        record, never remote filenames or download URLs. The platform proxies
        its storage; redirects to COS or any other host are not followed.
        """
        if (
            not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None
            or type(expected_size) is not int
            or expected_size < 0
            or not isinstance(source_marker, str)
            or not source_marker
        ):
            raise ValueError("原件下载需要完整的版本标识、文件大小和 SHA256")
        deadline = time.monotonic() + self._timeout_seconds
        document = self.get_document(document_id)
        if document.metadata.get("local_source_marker") != source_marker:
            raise WeKnoraError("file_source_mismatch")

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise WeKnoraError("timeout")
            return value

        target = Path(destination)
        temporary: Path | None = None
        headers = {
            "X-API-Key": self._api_key,
            "Accept": "application/octet-stream",
            "Accept-Encoding": "identity",
        }
        if self.tenant_id:
            headers["X-Tenant-ID"] = self.tenant_id
        try:
            with httpx.Client(
                timeout=remaining(),
                follow_redirects=False,
                trust_env=False,
                transport=self._http_transport,
            ) as client, client.stream(
                "GET",
                f"{self.base_url}/api/v1/knowledge/"
                f"{quote(_identifier(document_id), safe='')}/download",
                headers=headers,
            ) as response:
                remaining()
                if response.status_code != 200:
                    code = "not_found" if response.status_code == 404 else "request_failed"
                    raise WeKnoraError(code, status_code=response.status_code)
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise WeKnoraError("file_integrity_failed")
                length = response.headers.get("Content-Length")
                if length is not None and (
                    not length.isascii() or not length.isdecimal() or int(length) != expected_size
                ):
                    raise WeKnoraError("file_integrity_failed")
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=target.parent, prefix=f".{target.name}.", suffix=".part",
                    delete=False,
                ) as output:
                    temporary = Path(output.name)
                    digest = hashlib.sha256()
                    size = 0
                    # Do not request a large coalesced chunk: a slow stream must
                    # return to the deadline check after each received chunk.
                    for chunk in response.iter_bytes():
                        remaining()
                        size += len(chunk)
                        if size > expected_size:
                            raise WeKnoraError("file_integrity_failed")
                        digest.update(chunk)
                        output.write(chunk)
                    remaining()
                    if size != expected_size or not hmac.compare_digest(
                        digest.hexdigest(), expected_sha256.lower()
                    ):
                        raise WeKnoraError("file_integrity_failed")
                    output.flush()
                    os.fsync(output.fileno())
                remaining()
                os.replace(temporary, target)
                temporary = None
        except httpx.TimeoutException:
            raise WeKnoraError("timeout") from None
        except httpx.HTTPError:
            raise WeKnoraError("connection_failed") from None
        except OSError:
            raise WeKnoraError("file_storage_failed") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def delete_document(self, document_id: str) -> dict:
        # The caller owns the persisted mapping; this method does not delete a KB.
        payload = self._request("DELETE", f"/knowledge/{quote(_identifier(document_id), safe='')}")
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise WeKnoraError("invalid_response")
        return data

    def list_documents(self, *, page: int = 1, page_size: int = 100) -> WeKnoraDocumentPage:
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("WeKnora 分页参数无效")
        payload = self._request(
            "GET",
            self._knowledge_path("").rstrip("/"),
            params={"page": page, "page_size": page_size},
        )
        records = payload.get("data")
        total = payload.get("total")
        if not isinstance(records, list) or type(total) is not int or total < 0:
            raise WeKnoraError("invalid_document_page")
        return WeKnoraDocumentPage(
            tuple(self._document(item) for item in records), total, page, page_size
        )

    def find_document_by_source(
        self, source_marker: str, *, stop_event: Event | None = None
    ) -> WeKnoraDocument | None:
        if not source_marker:
            raise ValueError("同步来源标识不能为空")
        matches: dict[str, WeKnoraDocument] = {}
        for page_number in range(1, _MAX_LOOKUP_PAGES + 1):
            if stop_event is not None and stop_event.is_set():
                raise WeKnoraError("cancelled")
            page = self.list_documents(page=page_number)
            for document in page.documents:
                if document.metadata.get("local_source_marker") == source_marker:
                    matches[document.id] = document
            if len(matches) > 1:
                raise WeKnoraError("ambiguous_source_marker")
            if page_number * page.page_size >= page.total:
                return next(iter(matches.values()), None)
            if not page.documents:
                raise WeKnoraError("incomplete_document_page")
        raise WeKnoraError("document_lookup_limit")

    def search(
        self, *, query_text: str, knowledge_ids: Sequence[str], limit: int
    ) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(_identifier(value) for value in knowledge_ids))
        if not ids:
            return []
        if len(ids) > _MAX_SEARCH_DOCUMENTS or not 1 <= limit <= 100:
            raise WeKnoraError("search_scope_limit")
        payload = self._request(
            "POST",
            f"/knowledge-bases/{quote(self.knowledge_base_id, safe='')}/hybrid-search",
            json={
                "query_text": query_text,
                "knowledge_ids": ids,
                "match_count": limit,
                "skip_context_enrichment": True,
            },
        )
        records = payload.get("data")
        # The deployed Go handler serializes an empty result slice as null.
        # Require the field to be present so a malformed envelope still fails.
        if "data" in payload and records is None:
            return []
        if not isinstance(records, list):
            raise WeKnoraError("invalid_search_response")
        allowed_ids = set(ids)
        for item in records:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("knowledge_id"), str)
                or item["knowledge_id"] not in allowed_ids
            ):
                raise WeKnoraError("search_scope_violation")
            if item.get("knowledge_base_id", self.knowledge_base_id) != self.knowledge_base_id:
                raise WeKnoraError("search_scope_violation")
            _identifier(item.get("id", ""))
            if not isinstance(item.get("content"), str) or not item["content"].strip():
                raise WeKnoraError("invalid_search_response")
        return records


def _metadata(value: str) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (ValueError, TypeError):
        raise WeKnoraError("invalid_local_mapping") from None
    if not isinstance(parsed, dict):
        raise WeKnoraError("invalid_local_mapping")
    return parsed


class WeKnoraKnowledgeProvider:
    """Map remote chunks to current, authorized local catalog entries only."""

    def __init__(
        self,
        session: Session,
        client: WeKnoraClient,
        namespace: str = "content-service",
        *,
        allowed_data_levels: frozenset[str] = DEFAULT_ALLOWED_DATA_LEVELS,
    ):
        if not allowed_data_levels or not allowed_data_levels <= {
            "public",
            "internal",
            "restricted",
        }:
            raise ValueError("WeKnora 允许的数据等级配置无效")
        self.session = session
        self.client = client
        self.allowed_data_levels = allowed_data_levels
        self.status = ProviderStatus(
            provider_id=f"{namespace}-weknora",
            name="weknora",
            capability="knowledge-search",
            mode="api",
            external=True,
            enabled=True,
        )

    def _visible(
        self, entry: ResourceEntry, query: KnowledgeQuery, allow_cross_owner: bool
    ) -> bool:
        if not entry.active or entry.data_level not in (
            query.allowed_data_levels & self.allowed_data_levels
        ):
            return False
        if entry.tenant_id and entry.tenant_id != getattr(query, "requester_tenant", None):
            return False
        if entry.department_id and entry.department_id != query.requester_department:
            return False
        try:
            roles = json.loads(entry.allowed_roles_json or "[]")
        except ValueError:
            return False
        if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
            return False
        if roles and not set(roles) & query.requester_roles:
            return False
        return (
            entry.owner_id == query.requester_id
            or entry.data_level == "public"
            or allow_cross_owner
            or bool(entry.department_id and entry.department_id == query.requester_department)
        )

    def search(self, query: KnowledgeQuery) -> Sequence[KnowledgeChunk]:
        return self._search(query, resource_ids=None, allow_cross_owner=False)

    def search_selected(
        self,
        query: KnowledgeQuery,
        resource_ids: Sequence[str],
        *,
        allow_cross_owner: bool = False,
    ) -> Sequence[KnowledgeChunk]:
        if not resource_ids:
            return ()
        return self._search(query, resource_ids=resource_ids, allow_cross_owner=allow_cross_owner)

    def _search(self, query, *, resource_ids, allow_cross_owner) -> tuple[KnowledgeChunk, ...]:
        selected: set[str] | None = None
        if resource_ids is not None:
            selected = set()
            for resource_id in resource_ids:
                entry = self.session.get(ResourceEntry, resource_id, populate_existing=True)
                if entry is None:
                    legacy = self.session.get(LibraryItem, resource_id, populate_existing=True)
                    entry = (
                        self.session.get(
                            ResourceEntry, legacy.resource_entry_id, populate_existing=True
                        )
                        if legacy is not None and legacy.resource_entry_id
                        else None
                    )
                if entry is None or not self._visible(entry, query, allow_cross_owner):
                    raise WeKnoraError("selected_resource_denied")
                selected.add(entry.id)

        statement = (
            select(ExternalRef, ResourceEntry, ResourceVersion)
            .join(ResourceEntry, ExternalRef.entry_id == ResourceEntry.id)
            .join(ResourceVersion, ResourceEntry.current_version_id == ResourceVersion.id)
            .where(
                ExternalRef.system == WEKNORA_SYSTEM,
                ResourceEntry.active == true(),
                ResourceVersion.entry_id == ResourceEntry.id,
            )
            .order_by(ExternalRef.external_id)
            .execution_options(populate_existing=True)
        )
        if selected is not None:
            statement = statement.where(ResourceEntry.id.in_(selected))
        if query.categories:
            statement = statement.where(ResourceEntry.category.in_(query.categories))
        allowed: dict[str, tuple[ResourceEntry, ResourceVersion, ExternalRef]] = {}
        for reference, entry, version in self.session.execute(statement).all():
            if not self._visible(entry, query, allow_cross_owner):
                continue
            mapping = _metadata(reference.metadata_json)
            if (
                mapping.get("connection_id") != self.client.connection_id
                or mapping.get("knowledge_base_id") != self.client.knowledge_base_id
                or mapping.get("version_id") != version.id
                or mapping.get("sync_status") != "ready"
            ):
                continue
            allowed[reference.external_id] = (entry, version, reference)
        if selected is not None and selected != {entry.id for entry, _, _ in allowed.values()}:
            raise WeKnoraError("selected_resource_not_indexed")
        if not allowed:
            return ()

        results = []
        document_ids = list(allowed)
        # Preserve the full authorized scope without ever issuing an unfiltered query.
        for start in range(0, len(document_ids), _MAX_SEARCH_DOCUMENTS):
            results.extend(
                self.client.search(
                    query_text=query.text,
                    knowledge_ids=document_ids[start : start + _MAX_SEARCH_DOCUMENTS],
                    limit=min(max(query.limit, 1), 100),
                )
            )
        results.sort(key=lambda item: -self._score(item.get("score")))
        chunks: list[KnowledgeChunk] = []
        seen = set()
        for result in results:
            document_id = result["knowledge_id"]
            if document_id not in allowed:
                raise WeKnoraError("search_scope_violation")
            entry, version, reference = allowed[document_id]
            # Re-read mutable authorization/version state after the network call.
            self.session.refresh(entry)
            self.session.refresh(reference)
            current_mapping = _metadata(reference.metadata_json)
            if (
                not self._visible(entry, query, allow_cross_owner)
                or entry.current_version_id != version.id
                or current_mapping.get("sync_status") != "ready"
                or current_mapping.get("version_id") != version.id
                or current_mapping.get("connection_id") != self.client.connection_id
                or current_mapping.get("knowledge_base_id") != self.client.knowledge_base_id
            ):
                raise WeKnoraError("authorization_changed")
            chunk_id = f"weknora:{self.client.connection_id}:{result['id']}"
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            chunks.append(
                KnowledgeChunk(
                    id=chunk_id,
                    title=entry.title,
                    text=result["content"],
                    category=entry.category or entry.resource_kind,
                    data_level=entry.data_level,
                    version=version.version,
                    source_uri=version.source_url,
                    metadata={
                        "entry_id": entry.id,
                        "version_id": version.id,
                        "external_system": WEKNORA_SYSTEM,
                        "external_document_id": document_id,
                        "external_chunk_id": result["id"],
                        "knowledge_base_id": self.client.knowledge_base_id,
                        "selected": selected is not None,
                        "score": self._score(result.get("score")),
                    },
                )
            )
        return tuple(chunks[: max(query.limit, 0)])

    @staticmethod
    def _score(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return float(value) if math.isfinite(value) else 0.0
