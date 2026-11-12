import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import Text, create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.sql import operators, visitors
from sqlalchemy.sql.elements import BinaryExpression
from test_content_local_permission_scopes import business as business
from test_content_local_permission_scopes import headers
from test_content_local_permission_scopes import manager as manager
from test_content_local_permission_scopes import signed as signed
from test_content_weknora import client, query, response, result, seed

from content_service import db, provider_dependencies, weknora_routes
from content_service.config import load_settings
from content_service.generation import (
    KnowledgeProviderContractError,
    KnowledgeSelectionError,
    prepare_generation_input,
    selected_knowledge_chunks,
)
from content_service.knowledge import LibraryKnowledgeProvider
from content_service.models import (
    Base,
    ContentTask,
    LibraryItem,
    ResourceChunk,
    ResourceEntry,
    WeKnoraSyncJob,
)
from content_service.provider_health import current_health, record_failure, record_success
from content_service.providers import MockTextProvider
from content_service.weknora import WeKnoraError, WeKnoraKnowledgeProvider


def test_local_and_selected_knowledge_filter_empty_clobs_without_sql_comparison():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    def reject_clob_comparison(_connection, clause, *_args):
        for node in visitors.iterate(clause):
            if (
                isinstance(node, BinaryExpression)
                and node.operator in {operators.eq, operators.ne}
                and isinstance(getattr(node.left, "type", None), Text)
                and isinstance(getattr(node.right, "value", None), str)
            ):
                pytest.fail("Dameng CLOB content must not be compared to SQL string literals")

    try:
        with Session(engine) as session:
            entry = seed(session, client(lambda _request: response([])))
            for index, text in enumerate(("", "  \n", "verified product fact")):
                session.add(ResourceChunk(
                    version_id=entry.current_version_id, sequence=index, text=text,
                ))
            legacy = LibraryItem(owner_id="alice", category="product", name="legacy fact", text_content="legacy verified fact")
            session.add_all([
                legacy,
                LibraryItem(owner_id="alice", category="product", name="empty fact", text_content="  "),
                LibraryItem(owner_id="bob", category="product", name="private fact", text_content="private verified fact"),
            ])
            session.commit()
            event.listen(engine, "before_execute", reject_clob_comparison)
            found = LibraryKnowledgeProvider(session).search(query())
            assert {chunk.text for chunk in found} == {"verified product fact", "legacy verified fact"}
            selected = selected_knowledge_chunks(
                session, [entry.id], requester_id="alice",
                allowed_levels=frozenset({"internal"}), query_text="fact",
            )
            assert len(selected) == 1
            assert "verified product fact" in selected[0].text
    finally:
        engine.dispose()


def test_weknora_requires_explicit_configuration_and_redacts_key(monkeypatch):
    monkeypatch.setenv("CONTENT_KNOWLEDGE_PROVIDER", "weknora")
    for name in ("BASE_URL", "API_KEY", "API_KEY_FILE", "KNOWLEDGE_BASE_ID"):
        monkeypatch.delenv(f"CONTENT_WEKNORA_{name}", raising=False)
    with pytest.raises(ValueError, match="API Key"):
        load_settings()
    monkeypatch.setenv("CONTENT_WEKNORA_BASE_URL", "http://knowledge.test:28765")
    monkeypatch.setenv("CONTENT_WEKNORA_API_KEY", "test-private-key-hidden")
    monkeypatch.setenv("CONTENT_WEKNORA_KNOWLEDGE_BASE_ID", "test-kb")
    settings = load_settings()
    assert settings.weknora_sync_enabled is False
    assert settings.weknora_allowed_data_levels == {"public", "internal"}
    assert "test-private-key-hidden" not in repr(settings)
    monkeypatch.setenv("CONTENT_WEKNORA_ALLOWED_DATA_LEVELS", "restricted,invalid")
    with pytest.raises(ValueError, match="DATA_LEVELS"):
        load_settings()


def test_generation_uses_remote_selected_scope_and_empty_selection_sends_nothing():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return response([result(content="远端向量检索命中的产品事实")])

    api = client(handler)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        entry = seed(session, api)
        task = ContentTask(owner_id="alice", mode="copy", name="产品宣传", topic="fact")
        session.add(task)
        session.commit()
        provider = WeKnoraKnowledgeProvider(session, api)
        record_success(provider.status)
        record_failure(provider.status, WeKnoraError("selected_resource_not_indexed"))
        assert current_health(provider.status).status == "available"
        arguments = {
            "session": session, "task": task, "requester_id": "alice", "instruction": "产品文案",
            "knowledge_provider": provider, "text_provider": MockTextProvider(), "knowledge_top_k": 4,
        }
        generated = prepare_generation_input(**arguments, knowledge_resource_ids=[entry.id])
        assert generated.knowledge[0].text == "远端向量检索命中的产品事实"
        assert calls[0]["knowledge_ids"] == ["doc-1"]
        assert prepare_generation_input(**arguments, knowledge_resource_ids=[]).knowledge == ()
        assert len(calls) == 1
        # A replacement not indexed yet must not silently reuse the old index.
        entry.current_version_id = None
        session.commit()
        with pytest.raises(KnowledgeProviderContractError):
            prepare_generation_input(**arguments, knowledge_resource_ids=[entry.id])
        assert len(calls) == 1
    engine.dispose()


@pytest.mark.parametrize("failure", ["not_indexed", "no_hits", "timeout", "unavailable"])
def test_selected_current_local_content_survives_index_readiness_and_remote_outages(failure):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if failure == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        if failure == "unavailable":
            return httpx.Response(503)
        return response([])

    api = client(handler)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        entry = seed(session, api, mapping={"sync_status": "pending"} if failure == "not_indexed" else None)
        task = ContentTask(owner_id="alice", mode="copy", name="所选产品宣传", topic="fact")
        session.add(task)
        session.commit()
        generated = prepare_generation_input(
            session=session, task=task, requester_id="alice", instruction="产品文案",
            knowledge_provider=WeKnoraKnowledgeProvider(session, api),
            text_provider=MockTextProvider(), knowledge_top_k=4,
            knowledge_resource_ids=[entry.id],
        )
        assert len(generated.knowledge) == 1
        assert generated.knowledge[0].metadata["entry_id"] == entry.id
        assert generated.knowledge[0].metadata["version_id"] == entry.current_version_id
        assert "source fact" in generated.knowledge[0].text
        assert len(calls) == (0 if failure == "not_indexed" else 1)
    engine.dispose()


def test_selected_remote_hits_keep_each_selected_source_when_another_source_has_no_hit():
    api = client(lambda request: response([result(content="remote source fact")]))
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        first = seed(session, api)
        second = seed(session, api, document_id="doc-2")
        task = ContentTask(owner_id="alice", mode="copy", name="多产品资料宣传")
        session.add(task)
        session.commit()
        generated = prepare_generation_input(
            session=session, task=task, requester_id="alice", instruction="产品文案",
            knowledge_provider=WeKnoraKnowledgeProvider(session, api),
            text_provider=MockTextProvider(), knowledge_top_k=1,
            knowledge_resource_ids=[first.id, second.id],
        )
        assert [item.metadata["entry_id"] for item in generated.knowledge] == [first.id, second.id]
        assert generated.knowledge[0].text == "remote source fact"
        assert "source fact" in generated.knowledge[1].text
    engine.dispose()


@pytest.mark.parametrize("code", ["search_scope_violation", "selected_resource_denied", "authorization_changed"])
def test_selected_remote_security_failures_do_not_fall_back(code):
    class RejectedKnowledgeProvider:
        def search_selected(self, *_args, **_kwargs):
            raise WeKnoraError(code)

    api = client(lambda request: response([]))
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        entry = seed(session, api)
        task = ContentTask(owner_id="alice", mode="copy", name="产品宣传")
        session.add(task)
        session.commit()
        with pytest.raises(KnowledgeProviderContractError):
            prepare_generation_input(
                session=session, task=task, requester_id="alice", instruction="产品文案",
                knowledge_provider=RejectedKnowledgeProvider(), text_provider=MockTextProvider(),
                knowledge_top_k=4, knowledge_resource_ids=[entry.id],
            )
    engine.dispose()


def test_selected_fallback_revalidates_permissions_after_remote_timeout():
    def handler(request):
        entry.active = False
        session.commit()
        raise httpx.ReadTimeout("test timeout", request=request)

    api = client(handler)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        entry = seed(session, api)
        task = ContentTask(owner_id="alice", mode="copy", name="产品宣传")
        session.add(task)
        session.commit()
        with pytest.raises(KnowledgeSelectionError):
            prepare_generation_input(
                session=session, task=task, requester_id="alice", instruction="产品文案",
                knowledge_provider=WeKnoraKnowledgeProvider(session, api),
                text_provider=MockTextProvider(), knowledge_top_k=4,
                knowledge_resource_ids=[entry.id],
            )
    engine.dispose()


def test_status_and_retry_are_local_authorized_and_do_not_expose_key(business, manager, monkeypatch):
    _, base = manager
    settings = replace(base, knowledge_provider="weknora", weknora_sync_enabled=True,
                       weknora_base_url="http://knowledge.test", weknora_api_key="test-secret",
                       weknora_knowledge_base_id="kb-1")
    monkeypatch.setattr(provider_dependencies, "get_runtime_settings", lambda: settings)
    monkeypatch.setattr(weknora_routes, "get_runtime_settings", lambda: settings)
    monkeypatch.setattr(weknora_routes, "get_weknora_client", lambda: SimpleNamespace(connection_id="test-connection"))
    item = business.post("/api/v1/library/items", headers=headers("owner"), json={
        "category": "company", "name": "同步资料", "text_content": "正文",
    })
    assert item.status_code == 200, item.text
    assert item.json()["knowledge_sync"]["status"] == "not_synced"
    route = f"/api/v1/library/items/{item.json()['id']}/knowledge-sync"
    with db.SessionLocal() as session:
        entry = session.get(ResourceEntry, item.json()["resource_entry_id"])
        session.add(WeKnoraSyncJob(entry_id=entry.id, version_id=entry.current_version_id,
                                 connection_id="test-connection", knowledge_base_id="kb-1",
                                 status="failed", last_error="知识平台请求超时，请重试"))
        session.commit()
    state = business.get(route, headers=headers("owner"))
    assert state.status_code == 200 and state.json()["can_retry"] is True
    assert "test-secret" not in state.text
    assert business.get(route, headers=headers("other")).status_code == 403
    assert business.post(route + "/retry", headers=headers("other")).status_code == 403
    retried = business.post(route + "/retry", headers=headers("owner"))
    assert retried.status_code == 200 and retried.json()["status"] == "queued"
