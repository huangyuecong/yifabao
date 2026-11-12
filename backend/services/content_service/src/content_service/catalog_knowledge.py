"""Retrieval for batch copy generation (RAG): enterprise knowledge about one product.

The product record usually holds only a name, a price and a few specs. The
knowledge library holds the official material (slogan, story, craft, design).
Before writing copy we retrieve the passages about this product, pass them to
the model as quotable references, and keep them on the item so the reviewer
can see what the copy was based on.
"""

from __future__ import annotations

import logging
import os

from .knowledge import normalize_text
from .providers import KnowledgeChunk, KnowledgeQuery

logger = logging.getLogger(__name__)

MAX_REFERENCES = 3
EXCERPT_CHARS = 900


def enabled() -> bool:
    return os.getenv("CONTENT_CATALOG_RAG", "true").strip().lower() not in {"0", "false", "no", "off"}


def _same_product(title: str, name: str) -> bool:
    title, name = normalize_text(title), normalize_text(name)
    return bool(title and name) and (title in name or name in title)


def relevant(chunks: list[KnowledgeChunk], product_name: str) -> list[KnowledgeChunk]:
    """Keep passages about this product, plus general brand material; never another product's facts."""
    kept = [chunk for chunk in chunks
            if (_same_product(chunk.title, product_name) if chunk.category in {"product", "product_image"}
                else True)]
    return kept[:MAX_REFERENCES]


def references_for(session, owner_id: str, name: str, category: str, specs: list[dict]) -> list[dict]:
    """Retrieve quotable references for one product; an unavailable library means no references."""
    if not enabled():
        return []
    from .provider_dependencies import get_knowledge_provider

    query = " ".join([name, category, *(str(spec.get("value", "")) for spec in specs)])
    try:
        provider = get_knowledge_provider(session)
        chunks = list(provider.search(KnowledgeQuery(
            text=query, requester_id=owner_id, task_id="catalog-generation", categories=(),
            allowed_data_levels=frozenset({"public", "internal"}), limit=8,
        )))
    except Exception:  # the library is an enhancement; generation continues without it
        logger.warning("Knowledge retrieval failed for catalog generation", exc_info=True)
        return []
    return [{"id": f"R{index}", "title": chunk.title, "category": chunk.category,
             "text": chunk.text[:EXCERPT_CHARS], "source": chunk.source_uri or ""}
            for index, chunk in enumerate(relevant(chunks, name), 1)]
