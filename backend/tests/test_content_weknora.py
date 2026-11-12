import json
import re
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from threading import Event

import httpx
import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "services" / "content_service" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from content_service.models import (
    Base,
    ExternalRef,
    LibraryItem,
    ResourceEntry,
    ResourceVersion,
    WeKnoraSyncJob,
)
from content_service.providers import KnowledgeQuery
from content_service.weknora import (
    WeKnoraClient,
    WeKnoraError,
    WeKnoraKnowledgeProvider,
    normalize_weknora_base_url,
    weknora_connection_id,
)
from content_service.weknora_diagnostics import main as diagnostics_main
from content_service.weknora_sync import WeKnoraSyncRunner


def client(handler, **kwargs):
    return WeKnoraClient(
        "https://knowledge.example.test/api/v1",
        "test-key-not-a-real-secret",
        "kb-1",
        http_transport=httpx.MockTransport(handler),
        **kwargs,
    )


def document(document_id="doc-1", **kwargs):
    return {
        "id": document_id,
        "knowledge_base_id": "kb-1",
        "title": "Manual",
        "parse_status": "completed",
        "enable_status": "enabled",
        **kwargs,
    }


def result(document_id="doc-1", **kwargs):
    return {
        "id": f"chunk-{document_id}",
        "knowledge_id": document_id,
        "knowledge_base_id": "kb-1",
        "content": "A verified fact",
        "score": 0.8,
        **kwargs,
    }


def response(data, **kwargs):
    return httpx.Response(200, json={"success": True, "data": data, **kwargs})


def query(**kwargs):
    values = {
        "text": "fact",
        "requester_id": "alice",
        "task_id": "task-1",
        "categories": (),
        "allowed_data_levels": frozenset({"public", "internal", "restricted"}),
        "limit": 4,
        "requester_department": "sales",
        "requester_roles": frozenset({"editor"}),
    }
    values.update(kwargs)
    return KnowledgeQuery(**values)


def seed(session, api, document_id="doc-1", *, mapping=None, **kwargs):
    entry = ResourceEntry(
        id=f"entry-{document_id}",
        owner_id="alice",
        resource_kind="document",
        title=f"Local {document_id}",
        category="product",
        data_level="internal",
        **kwargs,
    )
    session.add(entry)
    session.flush()
    version = ResourceVersion(
        id=f"version-{document_id}",
        entry_id=entry.id,
        version=1,
        text_content="source fact",
        created_by="alice",
        source_url="https://example.test/source",
    )
    session.add(version)
    session.flush()
    entry.current_version_id = version.id
    session.add(
        ExternalRef(
            entry_id=entry.id,
            system="weknora",
            external_id=document_id,
            metadata_json=json.dumps(
                {
                    "connection_id": api.connection_id,
                    "knowledge_base_id": api.knowledge_base_id,
                    "version_id": version.id,
                    "sync_status": "ready",
                    **(mapping or {}),
                }
            ),
        )
    )
    session.commit()
    return entry


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.test",
        "https://user:password@example.test",
        "https://example.test?key=private",
        "https://example.test/#part",
        "https://example.test/unexpected",
        "https://example.test\\other",
    ],
)
def test_rejects_invalid_endpoint_without_exposing_credentials(url):
    with pytest.raises(ValueError) as error:
        normalize_weknora_base_url(url)
    assert "password" not in str(error.value)


def test_connection_namespace_is_normalized_and_key_rotation_independent():
    assert weknora_connection_id("https://example.test/api/v1/", "kb-1") == (
        weknora_connection_id("https://example.test", "kb-1")
    )


def test_client_exposes_timeout_for_sync_lease_sizing():
    api = client(lambda request: response({}), timeout_seconds=300)
    runner = WeKnoraSyncRunner(lambda: None, api)
    assert api.timeout_seconds == 300
    assert runner.lease_seconds >= api.timeout_seconds * 100


def test_read_only_preflight_checks_the_actual_kb_and_reports_only_safe_fields(capsys):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/v1/knowledge-bases/kb-1"
        assert request.headers["X-API-Key"] == "test-key-not-a-real-secret"
        return response(
            {
                "id": "kb-1",
                "type": "document",
                "embedding_model_id": "embed-1",
                "storage_provider_config": {"secret": "must-not-print"},
            }
        )

    assert diagnostics_main([], client_factory=lambda: client(handler)) == 0
    printed = capsys.readouterr().out
    report = json.loads(printed)
    assert report["read_access"] == "verified"
    assert report["embedding_model_configured"] is True
    assert report["embedding_runtime"] == "not_verified"
    assert "must-not-print" not in printed
    assert "test-key" not in printed
    assert len(calls) == 1


@pytest.mark.parametrize(
    "kb,code",
    [
        ({"id": "wrong", "type": "document", "embedding_model_id": "e"}, "knowledge_base_mismatch"),
        (
            {"id": "kb-1", "type": "faq", "embedding_model_id": "e"},
            "unsupported_knowledge_base_type",
        ),
        ({"id": "kb-1", "type": "document"}, "embedding_not_configured"),
    ],
)
def test_read_only_preflight_rejects_wrong_kb_or_missing_embedding(kb, code, capsys):
    assert diagnostics_main([], client_factory=lambda: client(lambda request: response(kb))) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == code


def test_read_only_preflight_redacts_authentication_failure_and_configuration(capsys):
    api = client(lambda request: httpx.Response(403, text="private upstream config"))
    assert diagnostics_main([], client_factory=lambda: api) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["http_status"] == 403
    assert "private" not in str(report)

    def invalid_config():
        raise ValueError("do not display secret-key or filepath")

    assert diagnostics_main([], client_factory=invalid_config) == 2
    assert "secret-key" not in capsys.readouterr().out
    assert weknora_connection_id("https://example.test", "kb-1", "1") != (
        weknora_connection_id("https://example.test", "kb-1", "2")
    )


def test_text_upload_marker_and_duplicate_are_verified_and_get_status(tmp_path):
    calls = []
    marker = "content-agent:entry-1:version-1"

    def handler(request):
        calls.append(request)
        assert request.headers["X-API-Key"] == "test-key-not-a-real-secret"
        assert request.headers["X-Tenant-ID"] == "10"
        assert "Authorization" not in request.headers
        if request.method == "POST":
            assert request.url.path == "/api/v1/knowledge-bases/kb-1/knowledge/file"
            body = request.read().decode()
            assert 'name="file"; filename="content-agent-' in body
            assert 'name="fileName"' in body
            display_filename = re.search(r'name="fileName"\r\n\r\n([^\r]+)', body)[1]
            assert display_filename.endswith(".md")
            assert display_filename.startswith("版本一-")
            assert '"local_source_marker": "content-agent:entry-1:version-1"' in body
            assert "Content-Type: text/markdown" in body
            assert "source content" in body
            return httpx.Response(409, json={"data": document()})
        return response(document(metadata={"local_source_marker": marker}))

    uploaded = client(handler, tenant_id="10").ingest_text(
        title="版本一", text="source content", source_marker=marker
    )
    assert uploaded.ready and uploaded.status == "completed"
    assert len(calls) == 2


def test_file_upload_stream_and_manual_api(tmp_path):
    file = tmp_path / "guide.pdf"
    file.write_bytes(b"%PDF-1.7 test")
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("manual"):
            assert json.loads(request.content) == {
                "title": "manual",
                "content": "text",
                "status": "publish",
                "channel": "api",
            }
        else:
            assert b"%PDF-1.7 test" in request.read()
            assert b"application/pdf" in request.read()
            filename = re.search(rb'name="fileName"\r\n\r\n([^\r]+)', request.read())[1]
            assert filename.endswith(b".pdf")
        return response(document(parse_status="processing"))

    api = client(handler)
    assert not api.ingest_file(file).ready
    assert not api.ingest_manual(title="manual", text="text").ready
    assert len(calls) == 2


def test_same_text_in_different_local_versions_has_distinct_upload_bytes():
    posted = []

    def handler(request):
        body = request.read().decode()
        marker = "version-two" if "version-two" in body else "version-one"
        posted.append(body)
        return response(document(metadata={"local_source_marker": marker}))

    api = client(handler)
    for marker in ("version-one", "version-two", "version-one"):
        api.ingest_text(title="same/title:名称", text="same content", source_marker=marker)
    files = [
        re.search(r"Content-Type: text/markdown\r\n\r\n(.*?)\r\n--", body, re.S)[1]
        for body in posted
    ]
    assert files[0] != files[1]
    assert files[0] == files[2]
    for body in posted:
        filename = re.search(r'name="fileName"\r\n\r\n([^\r]+)', body)[1]
        assert filename.endswith(".md") and "/" not in filename and ":" not in filename


def test_duplicate_belonging_to_another_version_is_not_adopted():
    api = client(
        lambda request: httpx.Response(
            409, json={"data": document(metadata={"local_source_marker": "another-version"})}
        )
    )
    with pytest.raises(WeKnoraError) as error:
        api.ingest_text(title="manual", text="same-content", source_marker="our-version")
    assert error.value.code == "duplicate_unowned"


def test_get_status_delete_and_source_lookup_are_kb_scoped():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "DELETE":
            assert request.url.path == "/api/v1/knowledge/doc-1"
            return response({"task_id": "delete-1"})
        if request.url.path.endswith("/knowledge"):
            return response(
                [
                    document("other", metadata={"local_source_marker": "other"}),
                    document(metadata={"local_source_marker": "ours"}),
                ],
                total=2,
                page=1,
                page_size=100,
            )
        return response(document(parse_status="finalizing"))

    api = client(handler)
    assert not api.get_document("doc-1").ready
    assert api.find_document_by_source("ours").id == "doc-1"
    assert api.find_document_by_source("absent") is None
    assert api.delete_document("doc-1") == {"task_id": "delete-1"}
    assert len(calls) == 4


def test_duplicate_source_markers_fail_closed():
    api = client(
        lambda request: response(
            [
                document("one", metadata={"local_source_marker": "same"}),
                document("two", metadata={"local_source_marker": "same"}),
            ],
            total=2,
        )
    )
    with pytest.raises(WeKnoraError) as error:
        api.find_document_by_source("same")
    assert error.value.code == "ambiguous_source_marker"


def test_source_lookup_stops_between_pages_without_more_network_requests():
    stop = Event()
    calls = []

    def handler(request):
        calls.append(request)
        stop.set()
        return response([document()], total=101)

    with pytest.raises(WeKnoraError) as error:
        client(handler).find_document_by_source("absent", stop_event=stop)
    assert error.value.code == "cancelled"
    assert len(calls) == 1


@pytest.mark.parametrize("status", ["failed", "pending", "processing", "finalizing", "unknown"])
def test_only_completed_documents_are_ready(status):
    api = client(lambda request: response(document(parse_status=status)))
    assert not api.get_document("doc-1").ready


@pytest.mark.parametrize("status", ["disabled", "unknown"])
def test_disabled_or_unknown_enable_state_is_not_ready(status):
    api = client(lambda request: response(document(enable_status=status)))
    assert not api.get_document("doc-1").ready


def test_redirect_and_error_bodies_never_escape():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            302,
            headers={"Location": "https://attacker.test"},
            text="secret-token-and-sensitive-document",
        )

    with pytest.raises(WeKnoraError) as error:
        client(handler).get_document("doc-1")
    assert len(calls) == 1
    assert error.value.status_code == 302
    assert "secret-token" not in str(error.value)
    assert "attacker" not in str(error.value)


def test_timeout_error_is_redacted():
    def handler(request):
        raise httpx.ReadTimeout("secret-key document body", request=request)

    with pytest.raises(WeKnoraError) as error:
        client(handler).get_document("doc-1")
    assert error.value.code == "timeout"
    assert "secret" not in str(error.value)


def test_search_always_sends_knowledge_ids_and_disables_enrichment():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/api/v1/knowledge-bases/kb-1/hybrid-search"
        assert json.loads(request.content) == {
            "query_text": "product",
            "knowledge_ids": ["doc-1"],
            "match_count": 3,
            "skip_context_enrichment": True,
        }
        return response([result()])

    api = client(handler)
    assert api.search(query_text="product", knowledge_ids=[], limit=3) == []
    assert api.search(query_text="product", knowledge_ids=["doc-1"], limit=3)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "bad_result",
    [
        result("not-permitted"),
        result(knowledge_base_id="wrong-kb"),
        {"id": "c", "content": "missing-document-id"},
        result(content=""),
    ],
)
def test_unfiltered_or_malformed_search_response_is_rejected(bad_result):
    api = client(lambda request: response([result(), bad_result]))
    with pytest.raises(WeKnoraError):
        api.search(query_text="product", knowledge_ids=["doc-1"], limit=3)


def test_provider_limits_remote_scope_to_authorized_ready_current_version():
    sent = []

    def handler(request):
        sent.extend(json.loads(request.content)["knowledge_ids"])
        return response([result()])

    api = client(handler)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        entry = seed(session, api)
        seed(session, api, "old", mapping={"version_id": "not-current"})
        seed(session, api, "not-ready", mapping={"sync_status": "processing"})
        seed(session, api, "wrong-server", mapping={"connection_id": "other"})
        seed(session, api, "wrong-kb", mapping={"knowledge_base_id": "other"})
        seed(session, api, "inactive", active=False)
        seed(session, api, "wrong-dept", department_id="other")
        seed(session, api, "wrong-role", allowed_roles_json='["admin"]')
        seed(session, api, "tenant", tenant_id="tenant-a")
        foreign = seed(session, api, "foreign")
        foreign.owner_id = "bob"
        restricted = seed(session, api, "restricted")
        restricted.data_level = "restricted"
        session.commit()
        chunks = WeKnoraKnowledgeProvider(session, api).search(query())
        assert sent == ["doc-1"]
        assert len(chunks) == 1
        assert chunks[0].title == "Local doc-1"
        assert chunks[0].metadata["entry_id"] == entry.id
        assert chunks[0].data_level == "internal"
        assert chunks[0].version == 1


def test_provider_respects_public_department_role_tenant_and_egress_intersection():
    sent = []

    def handler(request):
        ids = json.loads(request.content)["knowledge_ids"]
        sent.extend(ids)
        return response([result(document_id) for document_id in ids])

    api = client(handler)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        public = seed(session, api, "public")
        public.owner_id, public.data_level = "bob", "public"
        department = seed(
            session, api, "department", department_id="sales", allowed_roles_json='["editor"]'
        )
        department.owner_id = "bob"
        seed(session, api, "tenant", tenant_id="tenant-a")
        session.commit()
        provider = WeKnoraKnowledgeProvider(session, api)
        provider.search(query(allowed_data_levels=frozenset({"public"})))
        assert sent == ["public"]
        sent.clear()
        provider.search(query(requester_tenant="tenant-a"))
        assert set(sent) == {"public", "department", "tenant"}


def test_selection_fails_before_network_when_denied_or_not_ready():
    calls = []
    api = client(lambda request: calls.append(request) or response([]))
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        denied = seed(session, api)
        denied.department_id = "other"
        pending = seed(session, api, "pending", mapping={"sync_status": "processing"})
        session.commit()
        provider = WeKnoraKnowledgeProvider(session, api)
        assert provider.search_selected(query(), []) == ()
        with pytest.raises(WeKnoraError) as denied_error:
            provider.search_selected(query(), [denied.id])
        assert denied_error.value.code == "selected_resource_denied"
        with pytest.raises(WeKnoraError) as pending_error:
            provider.search_selected(query(), [pending.id])
        assert pending_error.value.code == "selected_resource_not_indexed"
        assert calls == []


def test_explicit_selection_cannot_search_other_authorized_documents():
    sent = []

    def handler(request):
        sent.extend(json.loads(request.content)["knowledge_ids"])
        return response([result("chosen")])

    api = client(handler)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        chosen = seed(session, api, "chosen")
        chosen.owner_id = "bob"
        seed(session, api, "another")
        session.commit()
        provider = WeKnoraKnowledgeProvider(session, api)
        with pytest.raises(WeKnoraError):
            provider.search_selected(query(), [chosen.id])
        chunks = provider.search_selected(query(), [chosen.id], allow_cross_owner=True)
        assert sent == ["chosen"]
        assert chunks[0].metadata["selected"] is True


def test_deployed_null_search_result_means_no_matches():
    api = client(lambda request: response(None))
    assert api.search(query_text="no matching document", knowledge_ids=["doc-1"], limit=1) == []


def test_missing_search_data_is_still_a_contract_error():
    api = client(lambda request: httpx.Response(200, json={"success": True}))
    with pytest.raises(WeKnoraError) as error:
        api.search(query_text="no matching document", knowledge_ids=["doc-1"], limit=1)
    assert error.value.code == "invalid_search_response"


def test_empty_authorized_catalog_never_queries_entire_workspace():
    api = client(lambda request: pytest.fail("Empty local scope must not call WeKnora"))
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        assert WeKnoraKnowledgeProvider(session, api).search(query()) == ()


@pytest.mark.parametrize("change", ["active", "version", "roles", "sync_status"])
def test_permission_changes_during_remote_request_are_rechecked(tmp_path, change):
    engine = create_engine(f"sqlite:///{(tmp_path / 'scope.db').as_posix()}")
    Base.metadata.create_all(engine)

    def handler(request):
        with engine.begin() as connection:
            if change == "sync_status":
                connection.execute(
                    update(ExternalRef).values(
                        metadata_json=json.dumps(
                            {
                                "sync_status": "deleting",
                            }
                        )
                    )
                )
                return response([result()])
            values = {
                "active": {"active": False},
                "version": {"current_version_id": "new-version"},
                "roles": {"allowed_roles_json": '["admin"]'},
            }[change]
            connection.execute(
                update(ResourceEntry).where(ResourceEntry.id == "entry-doc-1").values(**values)
            )
        return response([result()])

    api = client(handler)
    with Session(engine) as session:
        seed(session, api)
        with pytest.raises(WeKnoraError) as error:
            WeKnoraKnowledgeProvider(session, api).search(query())
        assert error.value.code == "authorization_changed"


def test_stale_session_cannot_send_revoked_document_ids(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'stale-scope.db').as_posix()}")
    Base.metadata.create_all(engine)
    api = client(lambda request: pytest.fail("Revoked scope must not be sent"))
    with Session(engine, expire_on_commit=False) as session:
        entry = seed(session, api)
        assert entry.active
        with engine.begin() as connection:
            connection.execute(update(ResourceEntry).values(active=False))
        assert WeKnoraKnowledgeProvider(session, api).search(query()) == ()
        with pytest.raises(WeKnoraError) as error:
            WeKnoraKnowledgeProvider(session, api).search_selected(query(), [entry.id])
        assert error.value.code == "selected_resource_denied"


def test_large_authorized_catalog_uses_bounded_scopes_never_unfiltered():
    batches = []

    def handler(request):
        ids = json.loads(request.content)["knowledge_ids"]
        batches.append(ids)
        return response([result(ids[0], score=len(batches))])

    api = client(handler)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for i in range(201):
            seed(session, api, f"doc-{i:03}")
        chunks = WeKnoraKnowledgeProvider(session, api).search(replace(query(), limit=1))
        assert [len(batch) for batch in batches] == [200, 1]
        assert len(chunks) == 1
        assert chunks[0].metadata["external_document_id"] == batches[1][0]


def test_real_http_client_sync_and_authorized_retrieval_work_together(tmp_path):
    """Exercise multipart/response contracts across sync+client+provider boundaries."""
    calls = []
    uploaded_metadata = {}

    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.headers["X-API-Key"] == "test-key-not-a-real-secret"
        if request.url.path.endswith("/knowledge/file"):
            assert request.method == "POST"
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {request.headers['Content-Type']}\r\n\r\n".encode() + request.read()
            )
            fields = {
                part.get_param("name", header="content-disposition"): part
                for part in message.iter_parts()
            }
            filename = fields["fileName"].get_payload(decode=True).decode()
            assert filename.endswith(".md")
            assert fields["file"].get_filename().endswith(".md")
            text = fields["file"].get_payload(decode=True).decode()
            assert "经审核的资料正文" in text
            assert "content-agent-source:" in text
            metadata = json.loads(fields["metadata"].get_payload(decode=True))
            assert all(isinstance(value, str) for value in metadata.values())
            assert metadata["local_source_marker"].startswith("content-agent:")
            uploaded_metadata.update(metadata)
            return response(
                document(parse_status="processing", enable_status="disabled", metadata=metadata)
            )
        if request.url.path == "/api/v1/knowledge/doc-1":
            assert request.method == "GET"
            return response(document(metadata=uploaded_metadata))
        if request.url.path.endswith("/hybrid-search"):
            assert request.method == "POST"
            payload = json.loads(request.content)
            assert payload["knowledge_ids"] == ["doc-1"]
            assert payload["skip_context_enrichment"] is True
            return response([result(content="经审核的资料正文")])
        pytest.fail(f"Unexpected API path: {request.url.path}")

    api = client(handler)
    engine = create_engine(f"sqlite:///{(tmp_path / 'http-sync.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        entry = ResourceEntry(
            owner_id="alice",
            resource_kind="document",
            title="产品资料",
            category="product",
            data_level="internal",
        )
        session.add(entry)
        session.flush()
        version = ResourceVersion(
            entry_id=entry.id, version=1, text_content="经审核的资料正文", created_by="alice"
        )
        session.add(version)
        session.flush()
        entry.current_version_id = version.id
        session.add(
            LibraryItem(
                owner_id="alice",
                category="product",
                name=entry.title,
                resource_entry_id=entry.id,
                text_content=version.text_content,
            )
        )
        session.commit()
        entry_id = entry.id

    runner = WeKnoraSyncRunner(factory, api, interval_seconds=1)
    assert runner.run_once(limit=1)["processed"] == 1
    with factory() as session:
        job = session.query(WeKnoraSyncJob).one()
        assert job.status == "processing"
        assert WeKnoraKnowledgeProvider(session, api).search(query()) == ()
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert runner.run_once(limit=1)["processed"] == 1
    with factory() as session:
        assert session.query(WeKnoraSyncJob).one().status == "ready"
        provider = WeKnoraKnowledgeProvider(session, api)
        chunks = provider.search_selected(query(), [entry_id])
        assert len(chunks) == 1
        assert chunks[0].title == "产品资料"
        assert chunks[0].metadata["entry_id"] == entry_id
        assert provider.search(query(requester_id="unauthorized", requester_department="")) == ()
    assert calls == [
        ("POST", "/api/v1/knowledge-bases/kb-1/knowledge/file"),
        ("GET", "/api/v1/knowledge/doc-1"),
        ("POST", "/api/v1/knowledge-bases/kb-1/hybrid-search"),
    ]
    engine.dispose()
