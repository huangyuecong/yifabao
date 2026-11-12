from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from .providers import (
    KnowledgeCategory,
    KnowledgeChunk,
    KnowledgeIngestRequest,
    KnowledgeIngestResult,
    ProviderStatus,
)

SUPPORTED_DOCUMENT_SUFFIXES = frozenset({".pdf", ".docx", ".txt", ".md", ".markdown"})


class DocumentExtractionError(ValueError):
    pass


def _normalize_extracted_text(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    compact: list[str] = []
    blank = False
    for line in lines:
        if line:
            compact.append(line)
            blank = False
        elif compact and not blank:
            compact.append("")
            blank = True
    return "\n".join(compact).strip()


def _extract_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise DocumentExtractionError("缺少 python-docx，无法解析 DOCX") from exc
    try:
        document = Document(path)
    except Exception as exc:
        raise DocumentExtractionError(f"DOCX 解析失败：{type(exc).__name__}") from exc
    values = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                values.append("\t".join(cells))
    return _normalize_extracted_text("\n".join(values))


def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentExtractionError("缺少 pypdf，无法解析 PDF") from exc
    try:
        reader = PdfReader(path)
        values = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise DocumentExtractionError(f"PDF 解析失败：{type(exc).__name__}") from exc
    return _normalize_extracted_text("\n\n".join(values))


def extract_document_text(path: Path, *, max_bytes: int = 50 * 1024 * 1024) -> str:
    source = path.expanduser().resolve()
    if not source.is_file():
        raise DocumentExtractionError("导入文件不存在")
    if source.stat().st_size > max_bytes:
        raise DocumentExtractionError("导入文件超过允许大小")
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_DOCUMENT_SUFFIXES:
        raise DocumentExtractionError("仅支持 PDF、DOCX、TXT、Markdown 文档")
    if suffix == ".pdf":
        text = _extract_pdf(source)
    elif suffix == ".docx":
        text = _extract_docx(source)
    else:
        try:
            text = _normalize_extracted_text(source.read_text(encoding="utf-8-sig"))
        except UnicodeDecodeError as exc:
            raise DocumentExtractionError("文本文件必须使用 UTF-8 编码") from exc
    if not text:
        raise DocumentExtractionError("文档未提取到可检索文本")
    return text


def chunk_document_text(
    text: str,
    *,
    max_chars: int = 1200,
    overlap_chars: int = 120,
) -> tuple[str, ...]:
    if max_chars < 100:
        raise ValueError("知识切片最大字符数不能小于 100")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("知识切片重叠字符数必须小于最大字符数")
    normalized = _normalize_extracted_text(text)
    if not normalized:
        return ()
    paragraphs = [item.strip() for item in re.split(r"\n{2,}", normalized) if item.strip()]
    chunks: list[str] = []
    current = ""

    def emit() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
            current = current[-overlap_chars:] if overlap_chars else ""

    for paragraph in paragraphs:
        remaining = paragraph
        while remaining:
            available = max_chars - len(current) - (2 if current else 0)
            if available <= 0:
                emit()
                continue
            if len(remaining) <= available:
                current = f"{current}\n\n{remaining}".strip() if current else remaining
                remaining = ""
                continue
            boundary = max(
                remaining.rfind(mark, 0, available)
                for mark in ("。", "！", "？", ";", "；", "\n", " ")
            )
            take = boundary + 1 if boundary >= max(40, available // 3) else available
            part, remaining = remaining[:take].strip(), remaining[take:].strip()
            current = f"{current}\n\n{part}".strip() if current else part
            emit()
    if current.strip() and (not chunks or current.strip() != chunks[-1]):
        chunks.append(current.strip())
    return tuple(chunks)


def _stable_document_id(request: KnowledgeIngestRequest, text: str) -> str:
    fingerprint = "\0".join(
        [
            request.owner_id,
            request.category,
            request.source_uri or str(request.source_path.resolve()),
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ]
    )
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:36]


def ingest_document_file(
    request: KnowledgeIngestRequest,
    *,
    max_chunk_chars: int = 1200,
    overlap_chars: int = 120,
) -> KnowledgeIngestResult:
    text = extract_document_text(request.source_path)
    document_id = _stable_document_id(request, text)
    title = request.title.strip() or request.source_path.stem
    raw_chunks = chunk_document_text(
        text,
        max_chars=max_chunk_chars,
        overlap_chars=overlap_chars,
    )
    metadata = {
        **request.metadata,
        "source_filename": request.source_path.name,
        "source_suffix": request.source_path.suffix.lower(),
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "chunk_count": len(raw_chunks),
    }
    chunks = tuple(
        KnowledgeChunk(
            id=f"{document_id}:{index}",
            title=title,
            text=value,
            category=request.category,
            data_level=request.data_level,
            version=1,
            source_uri=request.source_uri,
            metadata={"sequence": index, **metadata},
        )
        for index, value in enumerate(raw_chunks, start=1)
    )
    return KnowledgeIngestResult(
        document_id=document_id,
        title=title,
        category=request.category,
        text=text,
        chunks=chunks,
        metadata=metadata,
    )


def _validated_bundle_path(directory: Path, relative: str) -> Path:
    target = (directory / relative).resolve()
    if target != directory and directory not in target.parents:
        raise ValueError("JSONL 分类文件不能位于知识库目录之外")
    if not target.is_file():
        raise ValueError(f"JSONL 知识库缺少分类文件：{relative}")
    return target


def _official_document_text(document: dict[str, Any]) -> str:
    facts = document.get("facts", [])
    notes = document.get("notes", document.get("remarks", []))
    if isinstance(facts, str):
        facts = [facts]
    if isinstance(notes, str):
        notes = [notes]
    values = [str(document.get("summary", "")).strip()]
    values.extend(str(value).strip() for value in facts if str(value).strip())
    values.extend(str(value).strip() for value in notes if str(value).strip())
    return _normalize_extracted_text("\n".join(value for value in values if value))


def load_official_jsonl_bundle(
    directory: Path,
    *,
    default_data_level: str = "public",
    max_chunk_chars: int = 1200,
    overlap_chars: int = 120,
) -> tuple[KnowledgeIngestResult, ...]:
    root = directory.expanduser().resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("JSONL 知识库目录缺少 manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    categories = manifest.get("categories", [])
    if not isinstance(categories, list):
        raise TypeError("JSONL manifest.categories 必须是列表")
    results: list[KnowledgeIngestResult] = []
    seen_ids: set[str] = set()
    for category in categories:
        if not isinstance(category, dict):
            raise TypeError("JSONL 分类配置结构无效")
        category_id = str(category.get("id", "")).strip()
        relative = str(category.get("file", "")).strip()
        if not category_id or not relative:
            raise ValueError("JSONL 分类必须包含 id 和 file")
        source_path = _validated_bundle_path(root, relative)
        for line_number, line in enumerate(
            source_path.read_text(encoding="utf-8-sig").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                document = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{relative} 第 {line_number} 行不是有效 JSON") from exc
            if not isinstance(document, dict):
                raise TypeError(f"{relative} 第 {line_number} 行必须是 JSON 对象")
            document_id = str(document.get("id", "")).strip()
            title = str(document.get("title", "")).strip()
            if not document_id or not title or document_id in seen_ids:
                raise ValueError(f"{relative} 第 {line_number} 行 ID/标题无效或重复")
            seen_ids.add(document_id)
            text = _official_document_text(document)
            if not text:
                raise ValueError(f"{relative} 第 {line_number} 行没有可检索内容")
            document_category = str(document.get("category") or category_id)
            data_level = str(
                document.get("data_level") or manifest.get("data_level") or default_data_level
            )
            version = int(document.get("version", manifest.get("version", 1)))
            source_uri = document.get("source_url") or document.get("source_uri")
            metadata = {
                key: value
                for key, value in document.items()
                if key
                not in {
                    "id",
                    "title",
                    "summary",
                    "facts",
                    "notes",
                    "remarks",
                    "category",
                    "data_level",
                    "version",
                    "source_url",
                    "source_uri",
                }
            }
            metadata.update(
                {
                    "source_file": relative,
                    "source_line": line_number,
                    "source_url": source_uri,
                    "crawl_time": document.get("crawl_time", document.get("crawled_at")),
                    "facts": document.get("facts", []),
                    "notes": document.get("notes", document.get("remarks", [])),
                }
            )
            raw_chunks = chunk_document_text(
                text,
                max_chars=max_chunk_chars,
                overlap_chars=overlap_chars,
            )
            chunks = tuple(
                KnowledgeChunk(
                    id=f"{document_id}:{index}",
                    title=title,
                    text=value,
                    category=document_category,
                    data_level=data_level,
                    version=version,
                    source_uri=str(source_uri) if source_uri else None,
                    metadata={"sequence": index, **metadata},
                )
                for index, value in enumerate(raw_chunks, start=1)
            )
            results.append(
                KnowledgeIngestResult(
                    document_id=document_id,
                    title=title,
                    category=document_category,
                    text=text,
                    chunks=chunks,
                    metadata=metadata,
                )
            )
    return tuple(results)


def discover_official_jsonl_categories(directory: Path) -> tuple[KnowledgeCategory, ...]:
    counts = Counter(item.category for item in load_official_jsonl_bundle(directory))
    manifest = json.loads((directory.resolve() / "manifest.json").read_text(encoding="utf-8"))
    names = {
        str(item.get("id")): str(item.get("name") or item.get("title") or item.get("id"))
        for item in manifest.get("categories", [])
        if isinstance(item, dict) and item.get("id")
    }
    return tuple(
        KnowledgeCategory(category_id, names.get(category_id, category_id), count)
        for category_id, count in sorted(counts.items())
    )


class LocalKnowledgeCatalog:
    """Small local catalog adapter; DB-backed catalogs can implement the same port."""

    def __init__(self, directory: Path, namespace: str = "content-service"):
        self.directory = directory.expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.index_path = self.directory / "catalog.jsonl"
        self._lock = threading.Lock()
        self.status = ProviderStatus(
            provider_id=f"{namespace}-local-catalog",
            name="local-knowledge-catalog",
            capability="knowledge-catalog",
            mode="filesystem",
            external=False,
            enabled=True,
        )

    def ingest(self, request: KnowledgeIngestRequest) -> KnowledgeIngestResult:
        result = ingest_document_file(request)
        record = {
            "id": result.document_id,
            "title": result.title,
            "category": result.category,
            "text": result.text,
            "metadata": result.metadata,
            "chunks": [
                {
                    "id": item.id,
                    "text": item.text,
                    "data_level": item.data_level,
                    "version": item.version,
                    "source_uri": item.source_uri,
                    "metadata": item.metadata,
                }
                for item in result.chunks
            ],
        }
        with self._lock, self.index_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
        return result

    def remove_documents(self, document_ids: set[str]) -> None:
        """Remove extracted copies of precisely identified, permanently deleted uploads."""
        with self._lock:
            if not self.index_path.is_file():
                return
            lines = self.index_path.read_text(encoding="utf-8").splitlines()
            retained = [line for line in lines if line.strip() and json.loads(line).get("id") not in document_ids]
            if len(retained) == len(lines):
                return
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, delete=False) as output:
                    temporary = Path(output.name)
                    output.write("\n".join(retained) + ("\n" if retained else ""))
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.index_path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def categories(self) -> tuple[KnowledgeCategory, ...]:
        if not self.index_path.is_file():
            return ()
        counts: Counter[str] = Counter()
        for line in self.index_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                counts[str(item.get("category") or "document")] += 1
        return tuple(
            KnowledgeCategory(category, category, count)
            for category, count in sorted(counts.items())
        )
