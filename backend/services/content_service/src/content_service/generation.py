from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select, true
from sqlalchemy.orm import Session

from .models import (
    ContentTask,
    LibraryItem,
    ResourceChunk,
    ResourceEntry,
    ResourceVersion,
    SensitiveRule,
)
from .providers import (
    ContentGenerationInput,
    KnowledgeChunk,
    KnowledgeProvider,
    KnowledgeQuery,
    TextGenerationResult,
    TextProvider,
    selected_text_candidate,
)
from .services import load
from .weknora import WeKnoraError

ALL_DATA_LEVELS = frozenset({"public", "internal", "restricted"})
DATA_LEVEL_HIERARCHY = {
    "public": frozenset({"public"}),
    "internal": frozenset({"public", "internal"}),
    "restricted": ALL_DATA_LEVELS,
}
MAX_KNOWLEDGE_CONTEXT_CHARS = 8_000
MAX_KNOWLEDGE_CHUNK_CHARS = 2_000
MAX_KNOWLEDGE_SUMMARY_CHARS = 2_000
KNOWLEDGE_SUMMARY_VERSION = "priority-summary-v1"


class EgressDeniedError(PermissionError):
    pass


class KnowledgeProviderContractError(RuntimeError):
    pass


class KnowledgeSelectionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    allowed_data_levels: frozenset[str]
    allowed_source_data_levels: frozenset[str]
    allow_task_context: bool
    allow_compliance_terms: bool


def expand_allowed_data_levels(levels: Iterable[object]) -> frozenset[str]:
    """Expand a granted ceiling so lower-risk data is permitted as well."""
    expanded: set[str] = set()
    for level in levels:
        if isinstance(level, str):
            expanded.update(DATA_LEVEL_HIERARCHY.get(level, ()))
    return frozenset(expanded)


def generation_egress_policy(
    session: Session,
    task: ContentTask,
    text_provider: TextProvider,
) -> EgressPolicy:
    if not text_provider.status.external:
        return EgressPolicy(ALL_DATA_LEVELS, ALL_DATA_LEVELS, True, True)

    # External providers are enabled by system configuration rather than by a
    # per-task grant.  Keep the data boundary explicit: normal business context
    # may leave the system, while restricted knowledge/source content may not.
    allowed_levels = frozenset({"public", "internal"})
    return EgressPolicy(
        allowed_data_levels=allowed_levels,
        allowed_source_data_levels=allowed_levels,
        allow_task_context=True,
        allow_compliance_terms=True,
    )


def _query_text(task: ContentTask, instruction: str) -> str:
    return " ".join(
        value
        for value in (
            task.name,
            task.topic,
            task.product,
            " ".join(load(task.keywords_json)),
            task.audience,
            task.content_type,
            task.style,
            task.requirements,
            instruction,
        )
        if value
    )


def _valid_knowledge_chunk(
    item: Any,
    allowed_levels: frozenset[str],
) -> bool:
    return (
        isinstance(item, KnowledgeChunk)
        and isinstance(item.id, str)
        and bool(item.id.strip())
        and isinstance(item.title, str)
        and bool(item.title.strip())
        and isinstance(item.text, str)
        and bool(item.text.strip())
        and isinstance(item.category, str)
        and bool(item.category.strip())
        and isinstance(item.data_level, str)
        and item.data_level in allowed_levels
        and isinstance(item.version, int)
        and not isinstance(item.version, bool)
        and item.version >= 1
        and (item.source_uri is None or isinstance(item.source_uri, str))
    )


def _knowledge_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, list):
            parts.extend(str(item).strip() for item in value if str(item).strip())
        elif isinstance(value, dict):
            parts.extend(f"{key}：{item}" for key, item in value.items() if str(item).strip())
    return "\n".join(dict.fromkeys(parts))


def _knowledge_summary(
    *,
    title: str,
    resource_kind: str,
    category: str,
    facts: Any = None,
    notes: Any = None,
    tags: Any = None,
    relevant_text: Any = None,
) -> tuple[str, int, bool]:
    """Build a bounded, deterministic digest before any external model call."""
    sections = (
        (
            "资料信息",
            _knowledge_text(
                f"名称：{title}",
                f"类型：{resource_kind}",
                f"分类：{category}",
            ),
            320,
        ),
        ("关键事实", _knowledge_text(facts), 800),
        ("规范备注", _knowledge_text(notes), 360),
        ("标签", _knowledge_text(tags), 240),
        ("相关内容", _knowledge_text(relevant_text), 1_200),
    )
    source_chars = sum(len(value) for _, value, _ in sections)
    remaining = MAX_KNOWLEDGE_SUMMARY_CHARS
    parts: list[str] = []
    truncated = False
    for label, value, section_budget in sections:
        normalized = " ".join(value.split())
        if not normalized:
            continue
        prefix = f"{label}："
        separator_chars = 1 if parts else 0
        available = min(section_budget, remaining - len(prefix) - separator_chars)
        if available <= 0:
            truncated = True
            break
        if len(normalized) > available:
            normalized = f"{normalized[: max(available - 1, 0)]}…"
            truncated = True
        part = f"{prefix}{normalized}"
        parts.append(part)
        remaining -= len(part) + separator_chars
    summary = "\n".join(parts)
    truncated = truncated or len(summary) < source_chars
    return summary, source_chars, truncated


def _selected_resource_chunk(
    session: Session,
    reference_id: str,
    *,
    requester_id: str,
    allowed_levels: frozenset[str],
    query_text: str,
    allow_cross_owner: bool = False,
    requester_department: str = "",
    requester_roles: frozenset[str] = frozenset(),
) -> KnowledgeChunk:
    entry = session.get(ResourceEntry, reference_id, populate_existing=True)
    legacy_item = None
    if entry is None:
        legacy_item = session.get(LibraryItem, reference_id, populate_existing=True)
        if legacy_item is not None and legacy_item.resource_entry_id:
            entry = session.get(
                ResourceEntry, legacy_item.resource_entry_id, populate_existing=True
            )
    resource = entry or legacy_item
    if resource is None or not resource.active:
        raise KnowledgeSelectionError("所选知识库资料不存在或已停用")
    if (
        resource.owner_id != requester_id
        and resource.data_level != "public"
        and not allow_cross_owner
    ):
        raise KnowledgeSelectionError("无权引用所选知识库资料")
    if entry is not None:
        allowed_roles = set(load(entry.allowed_roles_json or "[]"))
        if entry.department_id and entry.department_id != requester_department:
            raise KnowledgeSelectionError("无权引用所选知识库资料")
        if allowed_roles and not (allowed_roles & set(requester_roles)):
            raise KnowledgeSelectionError("无权引用所选知识库资料")
    if resource.data_level not in allowed_levels:
        raise EgressDeniedError(
            f"所选资料“{entry.title if entry else legacy_item.name}”的数据等级"
            f" {resource.data_level} 未获准发送给当前内容模型"
        )

    if entry is None:
        summary, source_chars, summary_truncated = _knowledge_summary(
            title=legacy_item.name,
            resource_kind="document",
            category=legacy_item.category,
            relevant_text=legacy_item.text_content,
        )
        return KnowledgeChunk(
            id=legacy_item.id,
            title=legacy_item.name,
            text=summary,
            category=legacy_item.category,
            data_level=legacy_item.data_level,
            version=legacy_item.version,
            metadata={
                "legacy_library_item_id": legacy_item.id,
                "selected": True,
                "resource_kind": "document",
                "local_content_available": bool(legacy_item.text_content.strip()),
                "summary_version": KNOWLEDGE_SUMMARY_VERSION,
                "summary_source_chars": source_chars,
                "summary_truncated": summary_truncated,
            },
        )

    version = (
        session.get(ResourceVersion, entry.current_version_id, populate_existing=True)
        if entry.current_version_id
        else None
    )
    # Text is a CLOB on Dameng. Comparing it with an empty string in SQL can
    # fail before any provider call; SQLite tests do not expose that difference.
    # Apply all version/authorization predicates in SQL, then filter empty
    # content after the dialect has decoded the LOB into a Python string.
    chunks = (
        [
            chunk
            for chunk in session.scalars(
                select(ResourceChunk)
                .where(ResourceChunk.version_id == version.id)
                .order_by(ResourceChunk.sequence)
            ).all()
            if isinstance(chunk.text, str) and chunk.text.strip()
        ]
        if version is not None
        else []
    )
    selected_chunk = None
    if chunks:
        query_terms = {
            item.lower()
            for item in query_text.replace("，", " ").replace(",", " ").split()
            if item.strip()
        }
        selected_chunk = max(
            chunks,
            key=lambda item: (
                sum(term in item.text.lower() for term in query_terms),
                -item.sequence,
            ),
        )
    entry_tags = load(entry.tags_json)
    facts = load(version.facts_json) if version is not None else []
    version_metadata = load(version.metadata_json) if version is not None else {}
    text, source_chars, summary_truncated = _knowledge_summary(
        title=entry.title,
        resource_kind=entry.resource_kind,
        category=entry.category,
        facts=facts,
        notes=version.notes if version is not None else "",
        tags=entry_tags,
        relevant_text=(
            selected_chunk.text
            if selected_chunk is not None
            else version.text_content
            if version is not None
            else ""
        ),
    )
    return KnowledgeChunk(
        id=selected_chunk.id if selected_chunk is not None else f"resource:{entry.id}",
        title=entry.title,
        text=text,
        category=entry.category or entry.resource_kind,
        data_level=entry.data_level,
        version=version.version if version is not None else 1,
        source_uri=version.source_url if version is not None else None,
        metadata={
            "entry_id": entry.id,
            "version_id": version.id if version is not None else None,
            "chunk_id": selected_chunk.id if selected_chunk is not None else None,
            "selected": True,
            "resource_kind": entry.resource_kind,
            "local_content_available": version is not None,
            "tags": entry_tags,
            "facts": facts,
            "notes": version.notes if version is not None else "",
            **(version_metadata if isinstance(version_metadata, dict) else {}),
            "summary_version": KNOWLEDGE_SUMMARY_VERSION,
            "summary_source_chars": source_chars,
            "summary_truncated": summary_truncated,
        },
    )


def selected_knowledge_chunks(
    session: Session,
    resource_ids: list[str],
    *,
    requester_id: str,
    allowed_levels: frozenset[str],
    query_text: str,
    allow_cross_owner: bool = False,
    requester_department: str = "",
    requester_roles: frozenset[str] = frozenset(),
) -> tuple[KnowledgeChunk, ...]:
    normalized_ids = list(dict.fromkeys(str(item).strip() for item in resource_ids))
    if any(not item for item in normalized_ids):
        raise KnowledgeSelectionError("知识库资料 ID 不能为空")
    if len(normalized_ids) > 20:
        raise KnowledgeSelectionError("一次最多选择 20 项知识库资料")
    return tuple(
        _selected_resource_chunk(
            session,
            reference_id,
            requester_id=requester_id,
            allowed_levels=allowed_levels,
            query_text=query_text,
            allow_cross_owner=allow_cross_owner,
            requester_department=requester_department,
            requester_roles=requester_roles,
        )
        for reference_id in normalized_ids
    )


def _bounded_knowledge_context(
    chunks: tuple[KnowledgeChunk, ...],
) -> tuple[KnowledgeChunk, ...]:
    """Keep every selected source represented within a predictable LLM budget."""
    if not chunks:
        return ()
    per_chunk_budget = min(
        MAX_KNOWLEDGE_CHUNK_CHARS,
        max(1, MAX_KNOWLEDGE_CONTEXT_CHARS // len(chunks)),
    )
    bounded: list[KnowledgeChunk] = []
    remaining = MAX_KNOWLEDGE_CONTEXT_CHARS
    for item in chunks:
        budget = min(per_chunk_budget, remaining)
        if budget <= 0:
            break
        text = item.text[:budget]
        truncated = len(text) < len(item.text)
        bounded.append(
            KnowledgeChunk(
                id=item.id,
                title=item.title,
                text=text,
                category=item.category,
                data_level=item.data_level,
                version=item.version,
                source_uri=item.source_uri,
                metadata={
                    **item.metadata,
                    "context_truncated": truncated,
                },
            )
        )
        remaining -= len(text)
    return tuple(bounded)


def _selected_knowledge_with_remote_results(
    local: tuple[KnowledgeChunk, ...],
    remote: tuple[KnowledgeChunk, ...],
    allowed_levels: frozenset[str],
) -> tuple[KnowledgeChunk, ...]:
    """Keep one current source per selection, including sources without search hits."""
    selected = {
        item.metadata.get("entry_id", item.id): item
        for item in local
    }
    by_entry: dict[str, KnowledgeChunk] = {}
    seen_ids: set[str] = set()
    for item in remote:
        if not _valid_knowledge_chunk(item, allowed_levels) or item.id in seen_ids:
            raise KnowledgeProviderContractError("所选资料的知识检索返回了异常结构的数据")
        seen_ids.add(item.id)
        entry_id = item.metadata.get("entry_id")
        current = selected.get(entry_id)
        if current is None or item.metadata.get("version_id") != current.metadata.get("version_id"):
            raise KnowledgeProviderContractError("知识检索返回了所选范围或当前版本以外的资料")
        by_entry.setdefault(entry_id, item)
    result = []
    for entry_id, item in selected.items():
        if entry_id in by_entry:
            result.append(by_entry[entry_id])
        elif item.metadata.get("local_content_available"):
            result.append(item)
        else:
            raise KnowledgeProviderContractError("所选资料的当前版本内容不可用，请重新上传资料")
    return tuple(result)


def _local_knowledge_fallback_allowed(error: WeKnoraError) -> bool:
    # Authorization and scope violations must still stop generation. Indexing
    # readiness and temporary service failures do not invalidate current local
    # versions, so those versions can be used without contacting another index.
    return error.code in {"selected_resource_not_indexed", "timeout", "connection_failed"} or (
        error.code == "request_failed"
        and error.status_code is not None
        and (error.status_code == 429 or error.status_code >= 500)
    )


def prepare_generation_input(
    *,
    session: Session,
    task: ContentTask,
    requester_id: str,
    instruction: str,
    knowledge_provider: KnowledgeProvider,
    text_provider: TextProvider,
    knowledge_top_k: int,
    knowledge_resource_ids: list[str] | None = None,
    allow_cross_owner_knowledge: bool = False,
    source_content: dict[str, Any] | None = None,
    requester_department: str = "",
    requester_roles: frozenset[str] = frozenset(),
) -> ContentGenerationInput:
    policy = generation_egress_policy(session, task, text_provider)
    allowed_levels = policy.allowed_data_levels
    if source_content:
        source_level = draft_content_data_level(
            source_reference_metadata(source_content),
            source_content,
        )
        if source_level not in policy.allowed_source_data_levels:
            raise EgressDeniedError(f"来源草稿的数据等级 {source_level} 未获准外发")
    query_text = _query_text(task, instruction)
    configured_resource_ids = knowledge_resource_ids
    explicit_selection = knowledge_resource_ids is not None
    if configured_resource_ids is None:
        stored_ids = load(task.knowledge_resource_ids_json)
        configured_resource_ids = stored_ids if isinstance(stored_ids, list) else []
    if source_content and (not explicit_selection or configured_resource_ids):
        knowledge_levels = getattr(knowledge_provider, "allowed_data_levels", None)
        if knowledge_levels is not None and source_level not in knowledge_levels:
            raise EgressDeniedError(f"来源草稿的数据等级 {source_level} 未获准发送至知识平台")
    query = KnowledgeQuery(
        text=query_text,
        requester_id=requester_id,
        task_id=task.id,
        categories=(),
        allowed_data_levels=allowed_levels,
        limit=max(knowledge_top_k, len(configured_resource_ids)),
        requester_department=requester_department,
        requester_roles=requester_roles,
    )
    if explicit_selection or configured_resource_ids:
        returned_knowledge = selected_knowledge_chunks(
            session,
            configured_resource_ids,
            requester_id=requester_id,
            allowed_levels=allowed_levels,
            query_text=query_text,
            allow_cross_owner=allow_cross_owner_knowledge,
            requester_department=requester_department,
            requester_roles=requester_roles,
        )
        search_selected = getattr(knowledge_provider, "search_selected", None)
        if configured_resource_ids and callable(search_selected):
            # Local selection validation remains authoritative; the remote index
            # may only search the same selected and authorized resources.
            try:
                remote_knowledge = tuple(search_selected(
                    query, configured_resource_ids, allow_cross_owner=allow_cross_owner_knowledge,
                ))
            except WeKnoraError as exc:
                if not _local_knowledge_fallback_allowed(exc):
                    raise KnowledgeProviderContractError(exc.safe_message) from exc
                remote_knowledge = ()
            except Exception as exc:
                raise KnowledgeProviderContractError(
                    "所选资料的知识检索暂不可用，请检查资料的知识索引状态后重试"
                ) from exc
            # Re-read the selected current versions and permissions after the
            # network call; never fall back to the pre-request snapshot.
            local_knowledge = selected_knowledge_chunks(
                session,
                configured_resource_ids,
                requester_id=requester_id,
                allowed_levels=allowed_levels,
                query_text=query_text,
                allow_cross_owner=allow_cross_owner_knowledge,
                requester_department=requester_department,
                requester_roles=requester_roles,
            )
            returned_knowledge = _selected_knowledge_with_remote_results(
                local_knowledge, remote_knowledge, allowed_levels
            )
    else:
        try:
            returned_knowledge = tuple(knowledge_provider.search(query))
        except WeKnoraError as exc:
            raise KnowledgeProviderContractError(exc.safe_message) from exc
        except Exception as exc:
            raise KnowledgeProviderContractError(
                f"知识 Provider {knowledge_provider.status.provider_id} 调用失败："
                f"{type(exc).__name__}"
            ) from exc
    invalid_chunks = [
        item for item in returned_knowledge if not _valid_knowledge_chunk(item, allowed_levels)
    ]
    valid_ids = [
        item.id
        for item in returned_knowledge
        if isinstance(item, KnowledgeChunk) and isinstance(item.id, str)
    ]
    if invalid_chunks or len(valid_ids) != len(set(valid_ids)):
        raise KnowledgeProviderContractError(
            "知识 Provider 返回了未授权等级、错误分类或异常结构的数据"
        )
    knowledge = _bounded_knowledge_context(
        tuple(returned_knowledge[: max(knowledge_top_k, len(configured_resource_ids))])
    )
    forbidden_terms = tuple(
        session.scalars(
            select(SensitiveRule.phrase).where(
                SensitiveRule.active == true(),
                or_(
                    SensitiveRule.rule_type == "forbidden",
                    SensitiveRule.level == "forbidden",
                ),
            )
        ).all()
    )
    if text_provider.status.external and forbidden_terms and not policy.allow_compliance_terms:
        raise EgressDeniedError("当前外发授权未允许发送合规禁用词")
    return ContentGenerationInput(
        task_name=task.name,
        topic=task.topic,
        product=task.product,
        keywords=tuple(load(task.keywords_json)),
        audience=task.audience,
        content_type=task.content_type,
        style=task.style,
        length=task.length,
        requirements=task.requirements,
        instruction=instruction,
        knowledge=knowledge,
        forbidden_terms=forbidden_terms,
        source_content=generation_source_content(source_content) if source_content else None,
    )


def provider_metadata(result: TextGenerationResult) -> dict[str, Any]:
    return {
        "provider_id": result.provider_id,
        "model": result.model,
        "finish_reason": result.finish_reason,
        "usage": result.usage,
        "prompt_version": result.prompt_version,
    }


def source_reference_metadata(content: dict[str, Any]) -> list[dict[str, Any]]:
    references = content.get("references", [])
    if not isinstance(references, list):
        return []
    allowed_fields = {
        "id",
        "title",
        "category",
        "data_level",
        "version",
        "source_uri",
        "resource_entry_id",
        "resource_version_id",
        "resource_chunk_id",
        "legacy_library_item_id",
    }
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for reference in references:
        if not isinstance(reference, dict):
            continue
        reference_id = reference.get("id")
        data_level = reference.get("data_level")
        if (
            not isinstance(reference_id, str)
            or reference_id in seen
            or not isinstance(data_level, str)
            or data_level not in ALL_DATA_LEVELS
        ):
            continue
        values.append({key: value for key, value in reference.items() if key in allowed_fields})
        seen.add(reference_id)
    return values


def draft_content_data_level(
    references: Any,
    source_content: dict[str, Any] | None = None,
) -> str:
    ranks = {"public": 0, "internal": 1, "restricted": 2}
    levels = ["internal"]
    if not isinstance(references, list):
        references = []
    levels.extend(
        item["data_level"]
        for item in references
        if isinstance(item, dict)
        and isinstance(item.get("data_level"), str)
        and item["data_level"] in ALL_DATA_LEVELS
    )
    if source_content:
        source_level = source_content.get("content_data_level")
        if isinstance(source_level, str) and source_level in ALL_DATA_LEVELS:
            levels.append(source_level)
    return max(levels, key=ranks.__getitem__)


def generation_source_content(content: dict[str, Any]) -> dict[str, Any]:
    allowed_fields = {"title", "body", "source_ids"}
    source_ids = {item["id"] for item in source_reference_metadata(content)}

    def sanitized(candidate: dict[str, Any]) -> dict[str, Any]:
        value = {key: item for key, item in candidate.items() if key in allowed_fields}
        candidate_ids = value.get("source_ids", [])
        if not isinstance(candidate_ids, list):
            candidate_ids = []
        value["source_ids"] = [
            item for item in candidate_ids if isinstance(item, str) and item in source_ids
        ]
        return value

    candidates = content.get("candidates")
    if isinstance(candidates, list):
        sanitized_candidates = [
            sanitized(candidate) for candidate in candidates if isinstance(candidate, dict)
        ]
        result: dict[str, Any] = {
            "candidates": sanitized_candidates,
            "source_reference_ids": sorted(source_ids),
        }
        selected_index = content.get("selected_candidate_index")
        if (
            isinstance(selected_index, int)
            and not isinstance(selected_index, bool)
            and 0 <= selected_index < len(candidates)
            and isinstance(candidates[selected_index], dict)
        ):
            result["selected_candidate_index"] = sum(
                isinstance(candidate, dict) for candidate in candidates[:selected_index]
            )
            result["selected_candidate"] = sanitized(candidates[selected_index])
        elif isinstance(content.get("selected_candidate"), dict):
            result["selected_candidate"] = sanitized(selected_text_candidate(content))
        return result
    result = sanitized(content)
    result["source_reference_ids"] = sorted(source_ids)
    return result


def _source_ids(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        source_ids = value.get("source_ids")
        if isinstance(source_ids, list):
            yield from (item for item in source_ids if isinstance(item, str))
        for nested in value.values():
            yield from _source_ids(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _source_ids(nested)


def used_reference_ids(
    content: Any,
    knowledge: tuple[KnowledgeChunk, ...],
    *,
    fallback_to_retrieved: bool = False,
    source_content: dict[str, Any] | None = None,
) -> list[str]:
    available = {item.id for item in knowledge}
    if source_content:
        available.update(item["id"] for item in source_reference_metadata(source_content))
    used = list(dict.fromkeys(item for item in _source_ids(content) if item in available))
    if not used and fallback_to_retrieved:
        return [item.id for item in knowledge]
    return used


def reference_metadata(
    knowledge: tuple[KnowledgeChunk, ...],
    reference_ids: list[str],
    source_content: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    requested = set(reference_ids)
    values = [
        {
            "id": item.id,
            "title": item.title,
            "category": item.category,
            "data_level": item.data_level,
            "version": item.version,
            "source_uri": item.source_uri,
            "resource_entry_id": item.metadata.get("entry_id"),
            "resource_version_id": item.metadata.get("version_id"),
            "resource_chunk_id": item.metadata.get("chunk_id"),
            "legacy_library_item_id": item.metadata.get("legacy_library_item_id"),
        }
        for item in knowledge
        if item.id in requested
    ]
    seen = {item["id"] for item in values}
    if source_content:
        values.extend(
            item
            for item in source_reference_metadata(source_content)
            if item["id"] in requested and item["id"] not in seen
        )
    return values


_COMPLIANCE_TEXT_KEYS = frozenset(
    {
        "title",
        "source_title",
        "body",
        "text",
        "topic",
        "subtitle",
        "narration",
        "visual",
    }
)
_COMPLIANCE_SKIP_KEYS = frozenset(
    {
        "references",
        "reference_item_ids",
        "source_ids",
        "generation",
        "compliance",
        "provider",
        "instruction",
        "summary",
        "source_title_artifact_id",
        "source_title_version",
        "resource_entry_id",
        "resource_version_id",
        "resource_chunk_id",
        "legacy_library_item_id",
        "source_uri",
        "data_level",
        "content_data_level",
        "category",
        "id",
        "rule_id",
    }
)
_COMPLIANCE_FIELD_LABELS = {
    "title": "标题",
    "source_title": "已确认标题",
    "summary": "摘要",
    "body": "文案正文",
    "text": "文本内容",
    "topic": "主题",
    "subtitle": "字幕内容",
    "narration": "旁白内容",
    "visual": "画面描述",
}
_COMPLIANCE_CONTAINER_LABELS = {
    "candidates": "候选文案",
    "selected_candidate": "当前选用文案",
    "shots": "分镜",
    "segments": "字幕",
}
_COMPLIANCE_PATH_TOKEN = re.compile(r"([A-Za-z_]\w*)|\[(\d+)\]")
_COMPLIANCE_SENTENCE_BREAK = re.compile(r"[。！？!?；;\n]")


def compliance_field_label(path: str) -> str:
    labels: list[str] = []
    pending_container: str | None = None
    for match in _COMPLIANCE_PATH_TOKEN.finditer(path):
        name, raw_index = match.group(1), match.group(2)
        if name == "content":
            continue
        if name in _COMPLIANCE_CONTAINER_LABELS:
            pending_container = _COMPLIANCE_CONTAINER_LABELS[name]
            if name == "selected_candidate":
                labels.append(pending_container)
                pending_container = None
            continue
        if raw_index is not None:
            labels.append(f"{pending_container or '候选文案'} {int(raw_index) + 1}")
            pending_container = None
            continue
        if name:
            labels.append(_COMPLIANCE_FIELD_LABELS.get(name, "内容字段"))
    return " · ".join(labels) or "文案正文"


def _compliance_leaf_key(path: str) -> str:
    tokens = [match.group(1) for match in _COMPLIANCE_PATH_TOKEN.finditer(path) if match.group(1)]
    return tokens[-1] if tokens else ""


def _collect_compliance_fields(value: Any, path: str = "content") -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    if isinstance(value, str):
        if _compliance_leaf_key(path) in _COMPLIANCE_TEXT_KEYS and value:
            fields.append((path, value))
        return fields
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in _COMPLIANCE_SKIP_KEYS:
                continue
            fields.extend(_collect_compliance_fields(nested, f"{path}.{key}"))
        return fields
    if isinstance(value, list):
        for index, nested in enumerate(value):
            fields.extend(_collect_compliance_fields(nested, f"{path}[{index}]"))
    return fields


def _compliance_fragment_span(text: str, start: int) -> tuple[int, int]:
    breaks = [0, *[match.end() for match in _COMPLIANCE_SENTENCE_BREAK.finditer(text)]]
    if breaks[-1] != len(text):
        breaks.append(len(text))
    if len(breaks) <= 2:
        return 0, len(text)
    sentence_index = 0
    for index, (left, right) in enumerate(zip(breaks, breaks[1:], strict=False)):
        if left <= start < right or (start == len(text) and index == len(breaks) - 2):
            sentence_index = index
            break
    fragment_start = breaks[sentence_index - 1] if sentence_index > 0 else breaks[sentence_index]
    return fragment_start, breaks[sentence_index + 1]


def compliance_result(session: Session, content: Any) -> dict[str, Any]:
    rules = session.scalars(select(SensitiveRule).where(SensitiveRule.active == true())).all()
    text_fields = _collect_compliance_fields(content)
    hits = []
    seen: set[tuple[str, str, int]] = set()
    for rule in rules:
        phrase = (rule.phrase or "").strip()
        if not phrase:
            continue
        for field, text in text_fields:
            start = 0
            while (position := text.find(phrase, start)) >= 0:
                identity = (phrase, text, position)
                start = position + max(1, len(phrase))
                if identity in seen:
                    continue
                seen.add(identity)
                fragment_start, fragment_end = _compliance_fragment_span(text, position)
                fragment = text[fragment_start:fragment_end]
                relative_start = position - fragment_start
                relative_end = relative_start + len(phrase)
                hits.append(
                    {
                        "rule_id": rule.id,
                        "phrase": phrase,
                        "rule_type": rule.rule_type,
                        "level": rule.level,
                        "replacement": rule.replacement,
                        "field": field,
                        "field_label": compliance_field_label(field),
                        "start": position,
                        "end": position + len(phrase),
                        "prefix": fragment[:relative_start],
                        "match": phrase,
                        "suffix": fragment[relative_end:],
                        "context": fragment,
                    }
                )
    return {
        "passed": not any(hit["rule_type"] == "forbidden" for hit in hits),
        "hits": hits,
    }
