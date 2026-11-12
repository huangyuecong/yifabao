import json
from pathlib import Path

import pytest

from content_service.knowledge_ingestion import (
    DocumentExtractionError,
    LocalKnowledgeCatalog,
    chunk_document_text,
    extract_document_text,
    ingest_document_file,
    load_official_jsonl_bundle,
)
from content_service.providers import KnowledgeIngestRequest


def test_extract_document_text_rejects_invalid_files(tmp_path: Path):
    missing = tmp_path / "missing.md"
    with pytest.raises(DocumentExtractionError, match="不存在"):
        extract_document_text(missing)

    huge = tmp_path / "huge.md"
    huge.write_text("x", encoding="utf-8")
    with pytest.raises(DocumentExtractionError, match="超过允许大小"):
        extract_document_text(huge, max_bytes=0)

    binary = tmp_path / "clip.bin"
    binary.write_bytes(b"not-a-document")
    with pytest.raises(DocumentExtractionError, match="仅支持"):
        extract_document_text(binary)

    empty = tmp_path / "empty.md"
    empty.write_text("   \n\n", encoding="utf-8")
    with pytest.raises(DocumentExtractionError, match="未提取到"):
        extract_document_text(empty)

    latin = tmp_path / "latin.txt"
    latin.write_bytes("品牌".encode("gbk"))
    with pytest.raises(DocumentExtractionError, match="UTF-8"):
        extract_document_text(latin)


def test_chunk_document_text_splits_long_paragraphs():
    with pytest.raises(ValueError, match="不能小于"):
        chunk_document_text("正文", max_chars=10)
    with pytest.raises(ValueError, match="重叠"):
        chunk_document_text("正文", max_chars=120, overlap_chars=120)
    assert chunk_document_text("   ") == ()
    text = "第一句。第二句！第三句？第四句；第五句\n第六句 " + ("展开。" * 80)
    chunks = chunk_document_text(text, max_chars=120, overlap_chars=20)
    assert chunks
    assert all(len(item) <= 140 for item in chunks)


def test_local_catalog_ingests_and_lists_categories(tmp_path: Path):
    source = tmp_path / "brand.md"
    source.write_text("臻品文化。\n\n第二段资料。", encoding="utf-8")
    catalog = LocalKnowledgeCatalog(tmp_path / "catalog", "content-test")
    assert catalog.categories() == ()
    result = catalog.ingest(
        KnowledgeIngestRequest(
            source_path=source,
            owner_id="user-a",
            title="  ",
            category="brand",
        )
    )
    assert result.title == "brand"
    assert result.chunks
    categories = catalog.categories()
    assert categories[0].id == "brand"
    assert categories[0].document_count == 1
    repeated = ingest_document_file(
        KnowledgeIngestRequest(source_path=source, owner_id="user-a", category="brand")
    )
    assert repeated.document_id == result.document_id


def test_official_jsonl_bundle_validates_manifest(tmp_path: Path):
    root = tmp_path / "bundle"
    root.mkdir()
    with pytest.raises(ValueError, match="manifest.json"):
        load_official_jsonl_bundle(root)

    (root / "manifest.json").write_text(
        json.dumps({"categories": [{"id": "culture", "file": "culture.jsonl"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="缺少分类文件"):
        load_official_jsonl_bundle(root)

    (root / "culture.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"id": "dup", "title": "一", "summary": "摘要一"}),
                "{",
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="不是有效 JSON"):
        load_official_jsonl_bundle(root)

    (root / "culture.jsonl").write_text(
        json.dumps({"id": "dup", "title": "一", "summary": "摘要一"})
        + "\n"
        + json.dumps({"id": "dup", "title": "二", "summary": "摘要二"})
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="重复"):
        load_official_jsonl_bundle(root)

    (root / "culture.jsonl").write_text(
        json.dumps({"id": "empty", "title": "空", "summary": "   "}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="没有可检索内容"):
        load_official_jsonl_bundle(root)
