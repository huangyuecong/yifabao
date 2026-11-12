from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import or_, select, true
from sqlalchemy.orm import Session

from .models import LibraryItem, ResourceChunk, ResourceEntry, ResourceVersion
from .providers import KnowledgeChunk, KnowledgeQuery, ProviderStatus


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def text_tokens(value: str) -> set[str]:
    lowered = re.sub(r"\s+", " ", value).lower()
    normalized = normalize_text(value)
    tokens = set(re.findall(r"[a-z0-9]+(?:\.[0-9]+)?(?:%vol)?", lowered))
    for sequence in re.findall(r"[\u4e00-\u9fff]+", normalized):
        if len(sequence) <= 12:
            tokens.add(sequence)
        tokens.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return tokens


def rank_chunks(
    query_text: str,
    chunks: Sequence[KnowledgeChunk],
    limit: int,
) -> list[KnowledgeChunk]:
    query_tokens = text_tokens(query_text)
    normalized_query = normalize_text(query_text)
    ranked: list[tuple[int, str, KnowledgeChunk]] = []
    for chunk in chunks:
        score = len(query_tokens & text_tokens(f"{chunk.title} {chunk.text} {chunk.category}"))
        if normalize_text(chunk.title) in normalized_query:
            score += 12
        if normalize_text(chunk.category) in normalized_query:
            score += 4
        ranked.append((score, chunk.id, chunk))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in ranked[:limit] if row[0] > 0]


class LibraryKnowledgeProvider:
    def __init__(self, session: Session, namespace: str = "content-service"):
        self.session = session
        self.status = ProviderStatus(
            provider_id=f"{namespace}-library-db",
            name="content-library-db",
            capability="knowledge-search",
            mode="database",
            external=False,
            enabled=True,
        )

    def search(self, query: KnowledgeQuery) -> Sequence[KnowledgeChunk]:
        statement = (
            select(ResourceChunk, ResourceVersion, ResourceEntry)
            .join(ResourceVersion, ResourceChunk.version_id == ResourceVersion.id)
            .join(ResourceEntry, ResourceVersion.entry_id == ResourceEntry.id)
            .where(
                ResourceEntry.active == true(),
                ResourceEntry.current_version_id == ResourceVersion.id,
                ResourceEntry.data_level.in_(query.allowed_data_levels),
                or_(
                    ResourceEntry.owner_id == query.requester_id,
                    ResourceEntry.data_level == "public",
                    # Department-scoped restricted resources may be owned by
                    # another user in the same department.  The role check
                    # below remains the final authorization gate.
                    ResourceEntry.department_id == query.requester_department,
                ),
            )
        )
        if query.categories:
            statement = statement.where(ResourceEntry.category.in_(query.categories))
        rows = self.session.execute(statement).all()
        chunks = []
        for chunk, version, entry in rows:
            # Dameng stores Text as CLOB, which must not be compared to '' in
            # SQL. Empty/whitespace content is filtered after LOB conversion.
            if not isinstance(chunk.text, str) or not chunk.text.strip():
                continue
            # Department/role scoping is optional.  A null department keeps the
            # local single-tenant shared-library behavior; restricted entries
            # with a department require an explicit matching identity.
            allowed_roles = set(json.loads(entry.allowed_roles_json or "[]"))
            if entry.department_id and entry.department_id != query.requester_department:
                continue
            if allowed_roles and not (allowed_roles & set(query.requester_roles)):
                continue
            entry_metadata = json.loads(entry.metadata_json or "{}")
            chunks.append(
                KnowledgeChunk(
                    id=chunk.id,
                    title=entry.title,
                    text=chunk.text,
                    category=entry.category,
                    data_level=entry.data_level,
                    version=version.version,
                    source_uri=version.source_url,
                    metadata={
                        "entry_id": entry.id,
                        "version_id": version.id,
                        "chunk_id": chunk.id,
                        "legacy_library_item_id": entry_metadata.get("legacy_library_item_id"),
                        "sequence": chunk.sequence,
                        "tags": json.loads(entry.tags_json or "[]"),
                        "facts": json.loads(version.facts_json or "[]"),
                        "notes": version.notes,
                        **json.loads(chunk.metadata_json or "{}"),
                    },
                )
            )

        legacy_statement = select(LibraryItem).where(
            LibraryItem.active == true(),
            LibraryItem.resource_entry_id.is_(None),
            LibraryItem.data_level.in_(query.allowed_data_levels),
            or_(
                LibraryItem.owner_id == query.requester_id,
                LibraryItem.data_level == "public",
            ),
        )
        if query.categories:
            legacy_statement = legacy_statement.where(LibraryItem.category.in_(query.categories))
        legacy_rows = self.session.scalars(legacy_statement).all()
        chunks.extend(
            KnowledgeChunk(
                id=row.id,
                title=row.name,
                text=row.text_content,
                category=row.category,
                data_level=row.data_level,
                version=row.version,
                metadata={"owner_id": row.owner_id, "storage_key": row.storage_key},
            )
            for row in legacy_rows
            if isinstance(row.text_content, str) and row.text_content.strip()
        )
        return rank_chunks(query.text, chunks, query.limit)


class JsonlKnowledgeProvider:
    def __init__(self, directory: Path, namespace: str = "content-service"):
        self.directory = directory.expanduser().resolve()
        manifest_path = self.directory / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError("JSONL 知识库目录缺少 manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.documents: list[KnowledgeChunk] = []
        for category in manifest.get("categories", []):
            source_path = (self.directory / category["file"]).resolve()
            try:
                source_path.relative_to(self.directory)
            except ValueError as exc:
                raise ValueError("JSONL 分类文件不能位于知识库目录之外") from exc
            if not source_path.is_file():
                raise ValueError(f"JSONL 知识库缺少分类文件：{category['file']}")
            for line in source_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                document = json.loads(line)
                facts_value = document.get("facts", [])
                if isinstance(facts_value, str):
                    facts_value = [facts_value]
                facts = "；".join(str(value) for value in facts_value)
                notes_value = document.get("notes", document.get("remarks", []))
                if isinstance(notes_value, str):
                    notes_value = [notes_value]
                notes = "；".join(str(value) for value in notes_value)
                text = "\n".join(
                    value for value in (document.get("summary", ""), facts, notes) if value
                )
                self.documents.append(
                    KnowledgeChunk(
                        id=document["id"],
                        title=document["title"],
                        text=text,
                        category=document.get("category", category["id"]),
                        data_level=document.get(
                            "data_level",
                            manifest.get("data_level", "public"),
                        ),
                        version=int(document.get("version", manifest.get("version", 1))),
                        source_uri=document.get("source_url"),
                        metadata={
                            "facts": facts_value,
                            "notes": notes_value,
                            "crawl_time": document.get(
                                "crawl_time",
                                document.get("crawled_at"),
                            ),
                            "source_file": category["file"],
                        },
                    )
                )
        self.status = ProviderStatus(
            provider_id=f"{namespace}-jsonl",
            name="configured-jsonl-knowledge",
            capability="knowledge-search",
            mode="fixture",
            external=False,
            enabled=True,
        )

    def search(self, query: KnowledgeQuery) -> Sequence[KnowledgeChunk]:
        chunks = [
            chunk
            for chunk in self.documents
            if chunk.data_level in query.allowed_data_levels
            and (not query.categories or chunk.category in query.categories)
        ]
        return rank_chunks(query.text, chunks, query.limit)
