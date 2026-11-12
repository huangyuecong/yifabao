"""Retrieval-augmented batch copy generation: which passages are used and where they are kept."""

import pytest
from test_catalog import BASE, HEADERS, catalog, create_product  # noqa: F401

from content_service import automation_content, catalog_knowledge, provider_dependencies
from content_service.providers import KnowledgeChunk


def chunk(title, category="product", text="资料正文"):
    return KnowledgeChunk(id=title, title=title, text=text, category=category, data_level="public",
                          source_uri=f"https://example.com/{title}")


def test_only_this_products_passages_and_brand_material_are_kept():
    chunks = [chunk("雅集酒（遇雅集）"), chunk("臻品雅酿"), chunk("雅酿品牌介绍", "brand"), chunk("雅集酒")]
    kept = catalog_knowledge.relevant(chunks, "雅集酒（遇雅集）")
    assert [item.title for item in kept] == ["雅集酒（遇雅集）", "雅酿品牌介绍", "雅集酒"]


def test_references_come_from_the_configured_library(monkeypatch):
    seen = []

    class Provider:
        def search(self, query):
            seen.append(query)
            return [chunk("雅集酒（遇雅集）", text="宣传语：友趣生活 雅集一下"), chunk("臻品雅酿")]

    monkeypatch.setattr(provider_dependencies, "get_knowledge_provider", lambda session: Provider())
    refs = catalog_knowledge.references_for(None, "alice", "雅集酒（遇雅集）", "白酒", [{"value": "53%vol"}])
    assert refs == [{"id": "R1", "title": "雅集酒（遇雅集）", "category": "product",
                     "text": "宣传语：友趣生活 雅集一下", "source": "https://example.com/雅集酒（遇雅集）"}]
    assert seen[0].text == "雅集酒（遇雅集） 白酒 53%vol" and seen[0].requester_id == "alice"

    monkeypatch.setenv("CONTENT_CATALOG_RAG", "false")
    assert catalog_knowledge.references_for(None, "alice", "雅集酒", "", []) == []
    monkeypatch.setenv("CONTENT_CATALOG_RAG", "true")

    def broken(session):
        raise RuntimeError("library offline")

    monkeypatch.setattr(provider_dependencies, "get_knowledge_provider", broken)
    assert catalog_knowledge.references_for(None, "alice", "雅集酒", "", []) == []


def test_generation_passes_references_and_shows_them_to_the_reviewer(catalog, monkeypatch):  # noqa: F811
    client, _, _ = catalog
    product = create_product(client, "雅集酒（遇雅集）")
    received = []
    monkeypatch.setattr(catalog_knowledge, "references_for", lambda *args: [
        {"id": "R1", "title": "雅集酒（遇雅集）", "category": "product", "text": "宣传语：友趣生活 雅集一下",
         "source": "https://example.com/zhijiao"}])

    def draft(config, product_doc, images, rules, settings):
        received.append(product_doc)
        return ({"title": "雅集一下", "description": "友趣生活", "category": "白酒", "listing_title": "雅集酒",
                 "listing_description": "53%vol", "classifications": []}, "test-model")

    monkeypatch.setattr(automation_content, "generate_draft", draft)
    response = client.post(f"{BASE}/batches", headers=HEADERS, json={"product_ids": [product["id"]]})
    assert response.status_code == 201, response.text
    item = client.get(f"{BASE}/items", params={"status": "review"}).json()["items"][0]
    assert received[0]["references"][0]["text"] == "宣传语：友趣生活 雅集一下"
    assert item["references"] == [{"title": "雅集酒（遇雅集）", "excerpt": "宣传语：友趣生活 雅集一下",
                                   "source": "https://example.com/zhijiao"}]


@pytest.mark.parametrize("parts", [["images"]])
def test_no_retrieval_when_copy_is_not_regenerated(catalog, monkeypatch, parts):  # noqa: F811
    client, _, _ = catalog
    product = create_product(client)
    called = []
    monkeypatch.setattr(catalog_knowledge, "references_for", lambda *args: called.append(args) or [])
    client.post(f"{BASE}/batches", headers=HEADERS, json={"product_ids": [product["id"]], "with_copy": False,
                                                           "with_images": True})
    assert called == []
